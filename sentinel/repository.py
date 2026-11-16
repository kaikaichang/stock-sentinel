"""資料寫入與查詢封裝。"""
from datetime import date, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .indicators import compute_monthly_kd
from .models import DailyPrice, MonthlyIndicator, StockNews, Watchlist


def _nan_to_none(v):
    return None if pd.isna(v) else v


def add_watch(session: Session, symbol: str, name=None, market=None,
              target_low=None, target_high=None, notes=None) -> None:
    market = market or ("TW" if symbol.upper().endswith((".TW", ".TWO")) else "US")
    stmt = insert(Watchlist).values(
        symbol=symbol.upper(), name=name, market=market,
        target_low=target_low, target_high=target_high, notes=notes, is_active=True,
    )
    # 重複新增時只更新「使用者有明確給的欄位」，避免覆蓋掉先前設定的警示價
    update = {k: v for k, v in dict(name=name, target_low=target_low,
                                    target_high=target_high, notes=notes).items()
              if v is not None}
    update["is_active"] = True
    session.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_=update))


def active_symbols(session: Session) -> list[str]:
    return list(session.scalars(
        select(Watchlist.symbol).where(Watchlist.is_active).order_by(Watchlist.symbol)))


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


def golden_cross_candidates(session: Session) -> list[tuple]:
    """每檔最新一期月線：K<20 且 D<20，且發生黃金交叉。"""
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
    return list(session.execute(q).all())


def price_alerts(session: Session) -> list[tuple]:
    """回傳 (Watchlist, 最新交易日, 最新收盤, 'LOW'|'HIGH')。以最新收盤判斷。"""
    out = []
    for w in session.scalars(select(Watchlist).where(Watchlist.is_active)):
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
