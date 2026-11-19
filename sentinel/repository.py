"""資料寫入與查詢封裝。"""
from datetime import date, datetime

import pandas as pd
from sqlalchemy import case, exists, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .indicators import compute_monthly_kd
from .models import (DailyPrice, Fundamentals, MonthlyIndicator, StockNews, StockReport,
                     Watchlist)


def _nan_to_none(v):
    return None if pd.isna(v) else v


def add_watch(session: Session, symbol: str, name=None, market=None,
              target_low=None, target_high=None, notes=None) -> None:
    market = market or ("TW" if symbol.upper().endswith((".TW", ".TWO")) else "US")
    stmt = insert(Watchlist).values(
        symbol=symbol.upper(), name=name, market=market,
        target_low=target_low, target_high=target_high, notes=notes, is_active=True,
        category="core",
    )
    # 重複新增時只更新「使用者有明確給的欄位」，避免覆蓋掉先前設定的警示價
    update = {k: v for k, v in dict(name=name, target_low=target_low,
                                    target_high=target_high, notes=notes).items()
              if v is not None}
    update["is_active"] = True
    # 使用者手動新增＝明確表示要關注，所以連 S&P 500 池裡的標的也升級為 core
    # （反過來，批次匯入絕不會把 core 降級，見 import_universe）。
    update["category"] = "core"
    # 研究報告佇列：只有「原本不是 core（或已停用）」、這次才變成 core 的標的才重新排入；
    # 已是 core 的重複新增不能把 needs_report 洗回 1，否則已完成的報告會被無故排回待分析。
    # SQL 的 SET 子句右側讀到的是更新前的列，所以這裡的判斷用的是「舊的 category / is_active」。
    update["needs_report"] = case(
        ((Watchlist.category != "core") | (~Watchlist.is_active), 1),
        else_=Watchlist.needs_report)
    session.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_=update))


def active_symbols(session: Session, category: str | None = "core") -> list[str]:
    """啟用中的代號。預設只回 core：匯入 S&P 500 後，
    無參數的 `python sync.py` 才不會變成逐檔同步 500 檔（含新聞）而被限流。傳 None 取全部。"""
    q = select(Watchlist.symbol).where(Watchlist.is_active)
    if category is not None:
        q = q.where(Watchlist.category == category)
    return list(session.scalars(q.order_by(Watchlist.symbol)))


def import_universe(session: Session, items: list[tuple[str, str]],
                    category: str = "sp500") -> tuple[int, int]:
    """批次匯入標的池，回傳 (新增檔數, 既有略過檔數)。

    衝突時刻意「不碰 category、警示價、is_active」：若 NVDA 已在核心自選，
    匯入 S&P 500 不能把它降級成 sp500，否則它會從預設同步與警示檢查中消失。
    只在既有名稱為空時補上名稱。
    """
    if not items:
        return 0, 0
    existing = set(session.scalars(select(Watchlist.symbol)))
    rows = [dict(symbol=sym, name=name, market="TW" if sym.endswith((".TW", ".TWO")) else "US",
                 is_active=True, category=category) for sym, name in items]
    stmt = insert(Watchlist)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={"name": func.coalesce(Watchlist.name, stmt.excluded.name)})
    session.execute(stmt, rows)
    added = sum(1 for sym, _ in items if sym not in existing)
    return added, len(items) - added


def latest_trade_date(session: Session, symbol: str) -> date | None:
    return session.scalar(
        select(func.max(DailyPrice.trade_date)).where(DailyPrice.symbol == symbol))


def upsert_daily(session: Session, symbol: str, df: pd.DataFrame) -> int:
    """寫入日線。

    為什麼用 Upsert：增量同步時會刻意重疊抓最近幾天（修正除權息後的 adj_close、
    以及盤中抓到的未定案收盤價）。若用一般 INSERT 會撞主鍵報錯，
    ON CONFLICT DO UPDATE 讓「同一天重抓」變成覆蓋更新，永遠不會重複寫入。
    """
    rows = [dict(
        symbol=symbol, trade_date=idx.date(),
        open=_nan_to_none(r.open), high=_nan_to_none(r.high),
        low=_nan_to_none(r.low), close=_nan_to_none(r.close),
        adj_close=_nan_to_none(r.adj_close),
        volume=None if pd.isna(r.volume) else int(r.volume),
    ) for idx, r in df.iterrows()]
    if not rows:
        return 0
    stmt = insert(DailyPrice)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "trade_date"],
        set_={c: stmt.excluded[c] for c in
              ("open", "high", "low", "close", "adj_close", "volume")},
    )
    session.execute(stmt, rows)
    return len(rows)


def upsert_news(session: Session, symbol: str, items: list[dict]) -> int:
    if not items:
        return 0
    rows = [dict(symbol=symbol, **it) for it in items]
    # (symbol, link) 唯一：同一則新聞重抓時直接略過，保留既有的 ai_analysis_json
    stmt = insert(StockNews).on_conflict_do_nothing(index_elements=["symbol", "link"])
    session.execute(stmt, rows)
    return len(rows)


def recompute_monthly(session: Session, symbol: str) -> int:
    """從日線整檔重算月 KD 並 Upsert。

    為什麼整檔重算而非增量：KD 為遞推公式，任何一個月的 K/D 變動都會影響後面全部月份；
    歷史日線被修正（除權息、補資料）時，增量更新會讓後續數值悄悄失準。
    資料量極小（數百個月），整檔重算最簡單也最不會錯。
    """
    q = select(DailyPrice.trade_date, DailyPrice.open, DailyPrice.high,
               DailyPrice.low, DailyPrice.close).where(
        DailyPrice.symbol == symbol).order_by(DailyPrice.trade_date)
    daily = pd.DataFrame(session.execute(q).all(),
                         columns=["trade_date", "open", "high", "low", "close"])
    if daily.empty:
        return 0
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    monthly = compute_monthly_kd(daily.set_index("trade_date"))

    rows = [dict(symbol=symbol, year_month=ym,
                 open=r.open, high=r.high, low=r.low, close=r.close,
                 k_val=r.k_val, d_val=r.d_val,
                 prev_k=_nan_to_none(r.prev_k), prev_d=_nan_to_none(r.prev_d),
                 is_partial=bool(r.is_partial))
            for ym, r in monthly.iterrows()]
    stmt = insert(MonthlyIndicator)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "year_month"],
        set_={c: stmt.excluded[c] for c in
              ("open", "high", "low", "close", "k_val", "d_val",
               "prev_k", "prev_d", "is_partial")},
    )
    session.execute(stmt, rows)
    return len(rows)


def golden_cross_candidates(session: Session, category: str | None = None) -> list[tuple]:
    """每檔最新一期月線：K<20 且 D<20，且發生黃金交叉。category 為 None 表示不分池。"""
    latest = (select(MonthlyIndicator.symbol,
                     func.max(MonthlyIndicator.year_month).label("ym"))
              .group_by(MonthlyIndicator.symbol).subquery())
    q = (select(MonthlyIndicator, Watchlist.name)
         .join(latest, (MonthlyIndicator.symbol == latest.c.symbol)
               & (MonthlyIndicator.year_month == latest.c.ym))
         .join(Watchlist, Watchlist.symbol == MonthlyIndicator.symbol)
         .where(Watchlist.is_active,
                MonthlyIndicator.k_val < 20, MonthlyIndicator.d_val < 20,
                MonthlyIndicator.prev_k <= MonthlyIndicator.prev_d,
                MonthlyIndicator.k_val > MonthlyIndicator.d_val)
         .order_by(MonthlyIndicator.symbol))
    if category is not None:
        q = q.where(Watchlist.category == category)
    return list(session.execute(q).all())


def price_alerts(session: Session) -> list[tuple]:
    """回傳 (Watchlist, 最新交易日, 最新收盤, 'LOW'|'HIGH')。以最新收盤判斷。
    只看有設警示價的標的：S&P 500 池沒有警示價，先過濾掉可避免對 500 檔各查一次最新收盤。"""
    out = []
    has_target = Watchlist.target_low.is_not(None) | Watchlist.target_high.is_not(None)
    for w in session.scalars(select(Watchlist).where(Watchlist.is_active, has_target)):
        last = session.execute(
            select(DailyPrice.trade_date, DailyPrice.close)
            .where(DailyPrice.symbol == w.symbol, DailyPrice.close.is_not(None))
            .order_by(DailyPrice.trade_date.desc()).limit(1)).first()
        if not last:
            continue
        d, c = last
        if w.target_low is not None and c <= w.target_low:
            out.append((w, d, c, "LOW"))
        if w.target_high is not None and c >= w.target_high:
            out.append((w, d, c, "HIGH"))
    return out


# ---- 以下為網頁 (app.py) 使用的唯讀查詢 ----

def monthly_series(session: Session, symbol: str) -> pd.DataFrame:
    """該標的的月 K 與月 KD，依月份由舊到新排序（繪圖需要時間遞增）。"""
    q = (select(MonthlyIndicator.year_month, MonthlyIndicator.open,
                MonthlyIndicator.high, MonthlyIndicator.low, MonthlyIndicator.close,
                MonthlyIndicator.k_val, MonthlyIndicator.d_val,
                MonthlyIndicator.prev_k, MonthlyIndicator.prev_d,
                MonthlyIndicator.is_partial)
         .where(MonthlyIndicator.symbol == symbol)
         .order_by(MonthlyIndicator.year_month))
    return pd.DataFrame(session.execute(q).all(), columns=[
        "year_month", "open", "high", "low", "close",
        "k_val", "d_val", "prev_k", "prev_d", "is_partial"])


def latest_close(session: Session, symbol: str) -> tuple[date, float] | None:
    """最新一筆有收盤價的 (交易日, 收盤價)；沒有資料回傳 None。"""
    row = session.execute(
        select(DailyPrice.trade_date, DailyPrice.close)
        .where(DailyPrice.symbol == symbol, DailyPrice.close.is_not(None))
        .order_by(DailyPrice.trade_date.desc()).limit(1)).first()
    return (row[0], row[1]) if row else None


def recent_news(session: Session, symbol: str, limit: int = 15) -> list[StockNews]:
    """最新新聞。published_at 可能為 NULL，改以 fetched_at 當次要排序鍵，
    避免沒有發布時間的新聞被排到最前面（SQLite 的 NULL 在 DESC 時排最後，此處明確處理）。"""
    return list(session.scalars(
        select(StockNews).where(StockNews.symbol == symbol)
        .order_by(StockNews.published_at.is_(None), StockNews.published_at.desc(),
                  StockNews.fetched_at.desc())
        .limit(limit)))


def promote_to_core(session: Session, symbols: list[str]) -> int:
    """把指定標的升級為核心觀察名單，回傳「實際有變動」的檔數（本來就是 core 的不算）。

    只改 category / is_active / needs_report，絕不動警示價與備註：使用者可能已經替這檔設好警示價。
    實際「變成 core」的標的同時 needs_report=1（排入待分析）；本來就是 core 的不動，
    所以重複按「加入核心」不會把已完成的報告又排回佇列（WHERE 已經把它們排除在外）。
    不存在的代號直接略過（UPDATE 比對不到列），不會報錯。
    """
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return 0
    res = session.execute(
        update(Watchlist)
        .where(Watchlist.symbol.in_(syms),
               (Watchlist.category != "core") | (~Watchlist.is_active))
        .values(category="core", is_active=True, needs_report=1))
    return res.rowcount


# ---- 個股研究報告 ----

def add_report(session: Session, symbol: str, verdict: str, business_model: str | None = None,
               moat_and_valuation: str | None = None, catalysts: str | None = None,
               risks_and_triggers: str | None = None) -> int:
    """新增一份研究報告並回傳 id。同一檔可累積多份，不覆蓋舊報告。

    寫入成功後在「同一個交易」內把該標的 needs_report 歸 0：報告與旗標要嘛一起成功、
    要嘛一起回滾，不會出現「報告寫了但還卡在待分析」或「旗標清了卻沒有報告」的不一致。
    """
    symbol = symbol.upper()
    report = StockReport(symbol=symbol, verdict=verdict, business_model=business_model,
                         moat_and_valuation=moat_and_valuation, catalysts=catalysts,
                         risks_and_triggers=risks_and_triggers)
    session.add(report)
    session.flush()  # 取得自增 id
    session.execute(update(Watchlist).where(Watchlist.symbol == symbol).values(needs_report=0))
    return report.id


def set_needs_report(session: Session, symbol: str, needs_report: bool) -> bool:
    """手動設定「需要（重新）分析」旗標；代號不存在回傳 False。"""
    res = session.execute(update(Watchlist).where(Watchlist.symbol == symbol.upper())
                          .values(needs_report=1 if needs_report else 0))
    return res.rowcount > 0


def get_needs_report(session: Session, symbol: str) -> bool:
    """該標的目前是否已排入待（重新）分析；代號不存在回傳 False。"""
    v = session.scalar(select(Watchlist.needs_report).where(Watchlist.symbol == symbol.upper()))
    return bool(v)


def latest_report(session: Session, symbol: str) -> StockReport | None:
    """該標的最新一份報告（id 最大者）；沒有報告回傳 None。"""
    return session.scalar(
        select(StockReport).where(StockReport.symbol == symbol)
        .order_by(StockReport.id.desc()).limit(1))


def pending_reports(session: Session) -> list[tuple[str, str, bool]]:
    """待分析清單：[(代號, 名稱, 是否已有報告)]，依代號排序。

    條件：啟用中的 core，且 (needs_report = 1 或 stock_reports 中沒有任何紀錄)。
    「沒有任何紀錄」這一條是保險：就算旗標被誤清成 0，從沒產生過報告的標的也不會被漏掉。
    第三個欄位讓介面分辨「尚無報告」（False）與「待重新分析」（True）。
    """
    has_report = exists().where(StockReport.symbol == Watchlist.symbol)
    q = (select(Watchlist.symbol, Watchlist.name, has_report)
         .where(Watchlist.is_active, Watchlist.category == "core",
                (Watchlist.needs_report == 1) | ~has_report)
         .order_by(Watchlist.symbol))
    return [(sym, name or "", bool(has)) for sym, name, has in session.execute(q)]


def db_stats(session: Session) -> dict:
    """資料庫概況：各標的池啟用中的標的數與全庫最新收盤日期（同步中心顯示用）。"""
    rows = session.execute(
        select(Watchlist.category, func.count()).where(Watchlist.is_active)
        .group_by(Watchlist.category)).all()
    counts = {cat: n for cat, n in rows}
    return {"core": counts.get("core", 0), "sp500": counts.get("sp500", 0),
            "total": sum(counts.values()),
            "latest_date": session.scalar(select(func.max(DailyPrice.trade_date)))}


# ---- 基本面 ----

FUNDAMENTAL_COLS = ("pe", "forward_pe", "pb", "roe", "dividend_yield",
                    "free_cashflow", "debt_to_equity")


def upsert_fundamentals(session: Session, symbol: str, data: dict) -> None:
    """寫入基本面快照。

    唯一鍵為 symbol：每檔只保留最新一份，重抓時 ON CONFLICT DO UPDATE 直接覆蓋，不會新增重複列。
    抓到 None 也照樣覆蓋成 None（不保留舊值）：舊的 PE 若被留下，
    使用者會把「已過期的便宜價」當成現況，寧可顯示「—」。
    """
    values = {c: data.get(c) for c in FUNDAMENTAL_COLS}
    values["updated_at"] = datetime.now()
    stmt = insert(Fundamentals).values(symbol=symbol, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_=values))


def fundamentals_map(session: Session) -> dict[str, Fundamentals]:
    """一次取回所有基本面，供篩選與網頁使用，避免逐檔查詢。"""
    return {f.symbol: f for f in session.scalars(select(Fundamentals))}


def filter_by_fundamentals(fmap: dict[str, Fundamentals], *, max_pe: float | None = None,
                           max_pb: float | None = None, min_roe: float | None = None,
                           min_yield: float | None = None) -> set[str] | None:
    """回傳通過所有「已啟用條件」的代號集合；沒有任何條件時回傳 None（代表不過濾）。

    roe / min_yield 以小數比較（0.15 = 15%）。條件間為 AND。
    欄位為 None 且該條件啟用 → 視為不通過：沒資料不能當作便宜。
    """
    checks = []
    if max_pe is not None:
        checks.append(lambda f: f.pe is not None and f.pe < max_pe)
    if max_pb is not None:
        checks.append(lambda f: f.pb is not None and f.pb < max_pb)
    if min_roe is not None:
        checks.append(lambda f: f.roe is not None and f.roe > min_roe)
    if min_yield is not None:
        checks.append(lambda f: f.dividend_yield is not None and f.dividend_yield > min_yield)
    if not checks:
        return None
    return {sym for sym, f in fmap.items() if all(c(f) for c in checks)}


# ---- 月 KD 多條件回溯搜尋 ----

SIGNAL_TYPES = ("golden_cross", "hook_up", "death_cross")
DEATH_CROSS_HIGH = 80  # 高檔死亡交叉的門檻固定 80；kd_threshold 只管低檔訊號


def _signal_month_floor(max_stale_months: int, today: date | None = None) -> str:
    """最新月線至少要是這個 'YYYY-MM' 才算「資料還新」。"""
    today = today or date.today()
    idx = today.year * 12 + (today.month - 1) - max_stale_months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def screen_monthly_kd(session: Session, signal_type: str, lookback_months: int = 3,
                      kd_threshold: float = 20, still_bullish: bool = True,
                      category: str | None = None,
                      max_stale_months: int = 1) -> pd.DataFrame:
    """回溯「最新 N 根月線」內曾發生指定訊號的標的，每檔只回最近一次訊號。

    signal_type：
      golden_cross  低檔黃金交叉：前期 K<=D、本期 K>D，且 K、D 都 < kd_threshold
      hook_up       低檔勾頭向上：尚未交叉（K<=D），K < kd_threshold 且 K 比前期高
      death_cross   高檔死亡交叉：前期 K>=D、本期 K<D，且 K、D 都 > 80

    為什麼用 ROW_NUMBER 排名、而不是用日曆月份相減：
    窗口要的是「最新 N 根月線」。停牌、剛上市或缺月的標的若用日曆月份，窗口會錯位；
    排名則永遠以該檔自己的最新一根為第 1 根，N=1 就等同於「只看最新一期」。

    為什麼用兩層視窗函數：
    第一層 rn 幫每檔的月線由新到舊編號（rn=1 為最新一根，也拿來取「最新 K/D/收盤」）；
    第二層 sr 只在「符合訊號」的月線中再編號，取 sr=1 就是最近一次訊號，
    不必在 Python 端逐檔迴圈，500 檔也是一條 SQL。

    注意：訊號月若是尚未走完的當月（is_partial），K/D 會隨每日資料變動，
    月初出現的訊號到月底可能消失，所以結果帶出 is_partial 讓介面標示。
    max_stale_months：最新月線太舊（久未同步或已下市）的標的不列入，避免把舊訊號當現況。
    """
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"signal_type 必須是 {SIGNAL_TYPES} 之一")
    if lookback_months < 1:
        raise ValueError("lookback_months 至少為 1")

    M = MonthlyIndicator
    rn = func.row_number().over(partition_by=M.symbol, order_by=M.year_month.desc()).label("rn")
    base = (select(M.symbol, M.year_month, M.k_val, M.d_val, M.prev_k, M.prev_d,
                   M.close, M.is_partial, Watchlist.name, rn)
            .join(Watchlist, Watchlist.symbol == M.symbol)
            .where(Watchlist.is_active))
    if category is not None:
        base = base.where(Watchlist.category == category)
    ranked = base.subquery("ranked")
    c = ranked.c

    # prev_k / prev_d 為 NULL（第一根月線）時比較結果為 NULL，會自然被排除，不需額外判斷
    if signal_type == "golden_cross":
        cond = ((c.prev_k <= c.prev_d) & (c.k_val > c.d_val)
                & (c.k_val < kd_threshold) & (c.d_val < kd_threshold))
    elif signal_type == "hook_up":
        cond = ((c.k_val <= c.d_val) & (c.k_val < kd_threshold) & (c.k_val > c.prev_k))
    else:  # death_cross
        cond = ((c.prev_k >= c.prev_d) & (c.k_val < c.d_val)
                & (c.k_val > DEATH_CROSS_HIGH) & (c.d_val > DEATH_CROSS_HIGH))

    sr = func.row_number().over(partition_by=c.symbol, order_by=c.rn).label("sr")
    sig = (select(c.symbol, c.name, c.year_month.label("signal_month"),
                  c.k_val.label("sig_k"), c.d_val.label("sig_d"), sr)
           .where(c.rn <= lookback_months, cond)).subquery("sig")
    latest = (select(c.symbol, c.year_month, c.k_val, c.d_val, c.close, c.is_partial)
              .where(c.rn == 1)).subquery("latest")

    q = (select(sig.c.symbol, sig.c.name, sig.c.signal_month, sig.c.sig_k, sig.c.sig_d,
                latest.c.k_val, latest.c.d_val, latest.c.close, latest.c.is_partial,
                latest.c.year_month.label("latest_month"))
         .join(latest, latest.c.symbol == sig.c.symbol)
         .where(sig.c.sr == 1, latest.c.year_month >= _signal_month_floor(max_stale_months)))
    if still_bullish and signal_type == "golden_cross":
        q = q.where(latest.c.k_val > latest.c.d_val)  # 最新一期仍維持 K > D（交叉後沒有又掉回去）
    q = q.order_by(sig.c.signal_month.desc(), sig.c.sig_k.asc())

    return pd.DataFrame(session.execute(q).all(), columns=[
        "symbol", "name", "signal_month", "sig_k", "sig_d",
        "k_val", "d_val", "close", "is_partial", "latest_month"])
