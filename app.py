"""本地股票搜尋與看圖網頁。

啟動：streamlit run app.py
本檔只負責 UI 與組裝；資料抓取、KD 計算、寫庫全部沿用 sentinel/ 既有模組
（同步流程在 sentinel/syncer.py，與 sync.py 命令列共用同一份）。
"""
import hashlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from sentinel import fetcher, repository as repo, syncer
from sentinel.db import SessionLocal, init_db

# 台股慣例：紅漲綠跌
UP_COLOR, DOWN_COLOR = "#e53935", "#2e9e5b"
K_COLOR, D_COLOR = "#f59e0b", "#3b82f6"

st.set_page_config(page_title="Stock Sentinel", page_icon="📈", layout="wide")
init_db()


POOLS = {"核心自選": "core", "S&P 500 掃描池": "sp500"}
SIGNALS = {"低檔黃金交叉": "golden_cross", "低檔勾頭向上": "hook_up", "高檔死亡交叉（>80）": "death_cross"}
CACHE_TTL = 300  # 秒。終端機另外跑 sync.py 時，網頁最多 5 分鐘後也會反映；網頁按鈕完成後會手動清除


# 為什麼查詢要快取：每次點選、勾選都會讓整支腳本重跑，
# 500 檔的名單、標記與篩選若每次都重查資料庫，操作會明顯卡頓。
# 只快取「純資料」（dict / set / DataFrame）；ORM 物件不可序列化，所以基本面 fmap 不快取。
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_markers(category: str) -> tuple[set[str], set[str]]:
    """回傳 (月 KD 低檔黃金交叉的代號集合, 觸及警示價的代號集合)。判斷規則與 screen.py 完全相同。"""
    with SessionLocal() as s:
        gc = {m.symbol for m, _ in repo.golden_cross_candidates(s, category)}
        alerts = {w.symbol for w, *_ in repo.price_alerts(s)}
    return gc, alerts


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_watchlist(category: str) -> dict[str, str]:
    """{代號: 名稱}，只含啟用中且屬於該標的池的標的。"""
    from sentinel.models import Watchlist
    from sqlalchemy import select
    with SessionLocal() as s:
        rows = s.execute(select(Watchlist.symbol, Watchlist.name)
                         .where(Watchlist.is_active, Watchlist.category == category)
                         .order_by(Watchlist.symbol)).all()
    return {sym: (name or "") for sym, name in rows}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def run_screener(signal_type: str, lookback: int, threshold: float,
                 still_bullish: bool, category: str) -> pd.DataFrame:
    with SessionLocal() as s:
        return repo.screen_monthly_kd(s, signal_type, lookback, threshold, still_bullish, category)


def add_and_sync(raw_symbol: str) -> str:
    """抓日線、新聞、補名稱並寫庫，回傳標準化後的代號。失敗時丟出例外且不寫入 watchlist。

    為什麼「先抓、確認有資料，才寫入 watchlist」：
    打錯代號時 yfinance 只會回傳空表，若先 add_watch 就會在清單裡留下一檔永遠沒資料的殭屍標的。
    """
    symbol = raw_symbol.strip().upper()
    if not symbol:
        raise ValueError("請輸入股票代號")
    with SessionLocal() as s:
        last = repo.latest_trade_date(s, symbol)  # 已存在則增量抓，與 sync.py 行為一致
    df = fetcher.fetch_daily(symbol, last)
    if df.empty and last is None:
        raise ValueError(f"查無 {symbol} 的日線資料（台股請加後綴，例如 2330.TW）")
    name = fetcher.fetch_name(symbol) if last is None else None
    news = fetcher.fetch_news(symbol)
    fund = fetcher.fetch_fundamentals(symbol)  # 內部已容錯：抓不到就是全 None，不影響日線寫入
    with SessionLocal.begin() as s:
        # 順序不可顛倒：daily_prices / news 的外鍵指向 watchlist，需先有母表資料（foreign_keys=ON）
        repo.add_watch(s, symbol, name=name)
        repo.upsert_daily(s, symbol, df)
        repo.upsert_news(s, symbol, news)
        repo.recompute_monthly(s, symbol)
        repo.upsert_fundamentals(s, symbol, fund)
    return symbol


def build_chart(m: pd.DataFrame, symbol: str) -> go.Figure:
    """上：月 K 線；下：月 KD。兩圖共用 x 軸，縮放與十字線自動連動。"""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.62, 0.38])
    fig.add_trace(go.Candlestick(
        x=m["year_month"], open=m["open"], high=m["high"], low=m["low"], close=m["close"],
        increasing=dict(line=dict(color=UP_COLOR), fillcolor=UP_COLOR),
        decreasing=dict(line=dict(color=DOWN_COLOR), fillcolor=DOWN_COLOR),
        name="月K", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=m["year_month"], y=m["k_val"], name="K",
                             line=dict(color=K_COLOR, width=2)), row=2, col=1)
    fig.add_trace(go.Scatter(x=m["year_month"], y=m["d_val"], name="D",
                             line=dict(color=D_COLOR, width=2)), row=2, col=1)
    # 20 / 80 為低檔與高檔參考線（篩選條件即是 K、D 皆 < 20）
    for y in (20, 80):
        fig.add_hline(y=y, line=dict(color="gray", dash="dash", width=1), row=2, col=1)

    fig.update_xaxes(type="category", rangeslider_visible=False,
                     showspikes=True, spikemode="across", spikethickness=1)
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    fig.update_yaxes(title_text="價格", row=1, col=1)
    fig.update_yaxes(title_text="KD", row=2, col=1)
    fig.update_layout(height=680, hovermode="x unified", margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.04, x=1, xanchor="right"),
                      title=f"{symbol} 月K 與月KD")
    return fig


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def fmt_num(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


# ───────────────────────── 側邊欄 ─────────────────────────
# 新增股票成功後要自動切換選取；widget 建立後不能再改它的 state，
# 所以先把目標存在 pending_select，下一輪重跑、widget 建立「之前」再套用。
# 新增的標的一律是核心自選，因此連標的池也要一併切回 core，否則使用者會看不到剛新增的股票。
if "pending_select" in st.session_state:
    st.session_state["pool"] = "核心自選"
    st.session_state["symbol_core"] = st.session_state.pop("pending_select")


def label(sym: str) -> str:
    marks = ("🟢" if sym in gc_set else "") + ("🔔" if sym in alert_set else "")
    return f"{marks + ' ' if marks else ''}{sym} {watch.get(sym, '')}".strip()


with st.sidebar:
    st.header("📈 Stock Sentinel")
    pool_label = st.radio("標的池", list(POOLS), key="pool", horizontal=True)
    category = POOLS[pool_label]
    is_scan = category == "sp500"

    # 更新按鈕要知道「目前實際顯示哪一檔」，而那要等主畫面的篩選器選取結果才確定。
    # 所以這裡先預留位置，等 symbol 決定後再回頭填入按鈕，版面上仍會出現在這個位置。
    actions = st.container()

    # 標的池一變，名單與標記就跟著變，所以必須在 radio 之後才載入
    watch = load_watchlist(category)
    gc_set, alert_set = load_markers(category)
    with SessionLocal() as _s:
        fmap = repo.fundamentals_map(_s)

    passed = None
    only_gc = False
    if is_scan:
        # 500 檔的下拉選單無法瀏覽，預設只列黃金交叉候選，這才是掃描池的用途
        only_gc = st.checkbox("只列月 KD 黃金交叉", value=True)
        st.caption("掃描池為批次同步，只有價量與月 KD，沒有新聞與基本面。")
    else:
        st.subheader("基本面過濾")
        f_pe = st.checkbox("只看 PE < 15")
        f_roe = st.checkbox("只看 ROE > 15%")
        f_dy = st.checkbox("只看殖利率 > 4%")
        # 三個條件為 AND；沒資料（None）的標的在條件啟用時一律排除，避免把「沒資料」當成「便宜」
        passed = repo.filter_by_fundamentals(
            fmap, max_pe=15 if f_pe else None,
            min_roe=0.15 if f_roe else None, min_yield=0.04 if f_dy else None)
    options = [s for s in watch
               if (passed is None or s in passed) and (not only_gc or s in gc_set)]

    st.subheader("已收錄標的" if not is_scan else f"掃描結果（{len(options)} 檔）")
    sel_key = f"symbol_{category}"  # 兩個池各自記住選取，切換回來不會被重置
    if options:
        # 過濾後原本選的標的可能不在清單內；selectbox 的 state 必須是合法選項，先重置避免例外
        if st.session_state.get(sel_key) not in options:
            st.session_state.pop(sel_key, None)
        sidebar_symbol = st.selectbox("選擇股票", options, key=sel_key, format_func=label)
        st.caption("🟢 月KD<20 黃金交叉　🔔 觸及警示價")
    else:
        sidebar_symbol = None
        if not watch:
            st.info("掃描池是空的，請先執行 `python sync.py --import-sp500`。" if is_scan
                    else "資料庫還沒有標的，請先在下方新增。")
        elif is_scan:
            st.warning("目前沒有月 KD 黃金交叉的標的，可取消勾選瀏覽全部。")
        else:
            st.warning("沒有標的符合目前的基本面條件，請取消部分勾選。")

    st.divider()
    st.subheader("新增股票")
    new_sym = st.text_input("股票代號", placeholder="AAPL 或 2330.TW")
    if st.button("抓取並寫入", type="primary", width="stretch"):
        try:
            with st.spinner("抓取日線、新聞並計算月 KD…"):
                added = add_and_sync(new_sym)
            st.cache_data.clear()  # 名單與標記都快取過，不清掉就看不到新標的
            st.session_state["pending_select"] = added
            st.rerun()
        except Exception as e:
            st.error(f"新增失敗：{e}")

# ───────────────────────── 主畫面：訊息與進度 ─────────────────────────
# 同步按鈕完成後會 rerun，而 rerun 會清掉 st.status 區塊，
# 所以結果摘要先存進 session_state，重跑後在這裡顯示。
flash = st.session_state.pop("flash", None)
if flash:
    {"success": st.success, "warning": st.warning, "error": st.error}[flash[0]](flash[1])
job_box = st.container()  # 同步進度（st.status）顯示在這裡

# ───────────────────────── 主畫面：月 KD 多條件篩選器 ─────────────────────────
screen_pick = None  # 使用者在篩選結果表中點選的代號
with st.container(border=True):
    st.subheader("🎯 月 KD 多條件篩選器")
    st.caption(f"範圍：{pool_label}。回溯範圍以「最新一根月線算第 1 個月」計算，"
               "1 ＝ 只看最新一期；訊號月若是尚未走完的當月，K/D 會隨每日資料變動。")
    c1, c2, c3, c4 = st.columns([1.3, 1.3, 1, 1.2])
    signal_label = c1.selectbox("訊號類型", list(SIGNALS))
    signal_type = SIGNALS[signal_label]
    lookback = c2.slider("回溯月數", 1, 6, 3)
    low_mode = signal_type != "death_cross"
    threshold = c3.selectbox("低檔門檻", [20, 25, 30, 50], disabled=not low_mode,
                             help="K、D（或勾頭的 K）需低於此值；高檔死亡交叉固定為 80" if low_mode
                             else "高檔死亡交叉固定為 K、D > 80")
    bullish_mode = signal_type == "golden_cross"
    still_bullish = c4.checkbox("最新期仍 K > D", value=True, disabled=not bullish_mode,
                                help="只適用於黃金交叉：交叉後最新一期沒有又掉回去" if bullish_mode
                                else "只適用於黃金交叉")

    result = run_screener(signal_type, lookback, float(threshold), bool(still_bullish), category)
    if result.empty:
        st.info("沒有符合條件的標的。可放寬回溯月數或低檔門檻。"
                if len(watch) else "這個標的池還沒有資料。")
    else:
        st.write(f"共 **{len(result)}** 檔符合；點選任一列即可在下方查看該檔圖表。")
        partial = result["is_partial"].astype(bool) & (result["signal_month"] == result["latest_month"])
        table = pd.DataFrame({
            "代號": result["symbol"], "名稱": result["name"].fillna(""),
            "訊號月份": result["signal_month"] + partial.map({True: "（未收盤）", False: ""}),
            "訊號時 K": result["sig_k"], "訊號時 D": result["sig_d"],
            "最新 K": result["k_val"], "最新 D": result["d_val"], "最新收盤": result["close"],
        })
        # 選取狀態以「列索引」記錄，所以 key 要帶入篩選條件與結果內容：
        # 條件一變、列的內容變了，選取就自動清空，不會讓舊的索引指到別檔股票。
        sig = hashlib.md5("|".join(result["symbol"]).encode()).hexdigest()[:8]
        table_key = f"screen_{category}_{signal_type}_{lookback}_{threshold}_{still_bullish}_{sig}"
        event = st.dataframe(
            table, hide_index=True, width="stretch", height=min(35 * len(table) + 38, 420),
            on_select="rerun", selection_mode="single-row", key=table_key,
            column_config={c: st.column_config.NumberColumn(format="%.1f")
                           for c in ("訊號時 K", "訊號時 D", "最新 K", "最新 D")}
            | {"最新收盤": st.column_config.NumberColumn(format="%.2f")})
        if event.selection.rows:
            screen_pick = result.iloc[event.selection.rows[0]]["symbol"]

# 有點選篩選結果就優先顯示該檔，否則用側邊欄的選取。
# 不把篩選結果回寫到側邊欄選單：widget 建立後不能改 state，硬做會引發重跑迴圈。
symbol = screen_pick or sidebar_symbol
if screen_pick:
    st.caption(f"📌 目前顯示篩選結果中的 **{screen_pick}**（取消選取該列即回到側邊欄的選擇）")


# ───────────────────────── 側邊欄：更新與重算按鈕 ─────────────────────────
def run_job(title: str, job) -> None:
    """在主畫面顯示 st.status 進度並執行 job(log, progress)。

    成功後清除快取（資料變了，名單與篩選結果都要重算）、把摘要存起來並 rerun；
    失敗則停在原地顯示錯誤，不 rerun，讓使用者看得到錯誤訊息。
    注意：同步中若再點別的控制項，Streamlit 會中斷並重跑腳本；
    每一檔都是各自的交易，已寫入的資料不會損毀，重新按一次即可從增量接續。
    """
    with job_box:
        with st.status(f"{title}…", expanded=True) as status:
            bar = st.progress(0.0)

            def log(msg: str) -> None:
                st.write(msg)

            def progress(done: int, total: int) -> None:
                bar.progress(min(done / total, 1.0) if total else 1.0, text=f"{done} / {total}")

            try:
                summary = job(log, progress)
            except Exception as e:
                status.update(label=f"{title}失敗", state="error", expanded=True)
                st.error(f"{type(e).__name__}：{e}")
                return
            status.update(label=f"{title}完成", state="complete", expanded=False)
    st.cache_data.clear()
    st.session_state["flash"] = summary
    st.rerun()


def _sync_current(log, progress):
    syncer.sync_one(symbol, log, light=is_scan)
    progress(1, 1)
    return ("success", f"已更新 {symbol}")


def _sync_all(log, progress):
    failed = (syncer.sync_sp500_batch(log, progress) if is_scan
              else syncer.sync_core(log, progress))
    if failed:
        return ("warning", f"同步完成，但有 {len(failed)} 檔無資料或失敗：{', '.join(failed[:15])}"
                           + ("…" if len(failed) > 15 else ""))
    return ("success", "同步完成，全部成功")


def _recompute(log, progress):
    n = syncer.recompute_all_monthly(log, progress)
    return ("success", f"已重算 {n} 檔的月 KD")


with actions:
    st.caption("資料更新")
    if st.button("🔄 更新當前標的", width="stretch", disabled=not symbol,
                 help="重新抓取目前顯示的這一檔" + ("（掃描池只抓日線與月 KD）" if is_scan else "")):
        run_job(f"更新 {symbol}", _sync_current)
    if st.button("🚀 同步 S&P 500 池（批次）" if is_scan else "🚀 同步全部自選股", width="stretch",
                 help="分批抓日線與月 KD，不抓新聞，約需 1 分鐘" if is_scan
                 else "逐檔抓日線、新聞與基本面，每檔間隔 1 秒"):
        run_job("同步 S&P 500 池" if is_scan else "同步全部自選股", _sync_all)
    if st.button("🧮 僅重算全庫月 KD", width="stretch",
                 help="不連網，從資料庫的日線重新計算全部標的的月 K 與 KD"):
        run_job("重算全庫月 KD", _recompute)
    st.divider()

# ───────────────────────── 主畫面：個股詳情 ─────────────────────────
if not symbol:
    st.title("尚無符合的標的" if watch else "尚無資料")
    st.stop()

with SessionLocal() as s:
    monthly = repo.monthly_series(s, symbol)
    last_px = repo.latest_close(s, symbol)
    news = repo.recent_news(s, symbol)
    # 在 session 內把需要的欄位取出，避免離開 with 後存取已脫離的物件
    news_rows = [(n.title, n.link, n.publisher, n.published_at, n.summary) for n in news]

st.title(f"{symbol}　{watch.get(symbol, '')}")

if monthly.empty or last_px is None:
    st.warning("這檔還沒有價格資料，請先用 `python sync.py --symbol "
               f"{symbol}` 同步（S&P 500 池可用 `python sync.py --batch-sp500`）。")
else:
    cur = monthly.iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric(f"最新收盤（{last_px[0]}）", f"{last_px[1]:,.2f}")
    note = "（當月未收盤）" if cur["is_partial"] else ""
    # delta_color="off"：K/D 上升不代表「好」，避免套用預設紅綠造成誤讀
    c2.metric(f"{cur['year_month']} K{note}", f"{cur['k_val']:.1f}",
              None if pd.isna(cur["prev_k"]) else f"{cur['k_val'] - cur['prev_k']:+.1f}",
              delta_color="off")
    c3.metric(f"{cur['year_month']} D{note}", f"{cur['d_val']:.1f}",
              None if pd.isna(cur["prev_d"]) else f"{cur['d_val'] - cur['prev_d']:+.1f}",
              delta_color="off")
    st.plotly_chart(build_chart(monthly, symbol), width="stretch")

# 基本面卡片：與技術面分開判斷，沒資料的標的仍能看圖，只是這一列顯示「—」
fund = fmap.get(symbol)
f1, f2, f3, f4 = st.columns(4)
f1.metric("PE（本益比）", fmt_num(fund.pe if fund else None))
f2.metric("PB（股價淨值比）", fmt_num(fund.pb if fund else None))
f3.metric("ROE", fmt_pct(fund.roe if fund else None))
f4.metric("殖利率", fmt_pct(fund.dividend_yield if fund else None))
if fund is None:
    st.caption(f"尚無基本面資料，可執行 `python sync.py --symbol {symbol}` 補抓。")
else:
    st.caption(f"基本面更新於 {fund.updated_at:%Y-%m-%d %H:%M}")

st.subheader("📰 最新新聞")
if not news_rows:
    st.caption("資料庫中沒有這檔的新聞。")
for title, link, publisher, published, summary in news_rows:
    st.markdown(f"**[{title}]({link})**")
    st.caption(f"{publisher or '未知來源'} · {published:%Y-%m-%d %H:%M}" if published
               else (publisher or "未知來源"))
    if summary:
        st.write(summary)
    st.divider()
