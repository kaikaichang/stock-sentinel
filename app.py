"""本地股票搜尋與看圖網頁。

啟動：streamlit run app.py
本檔只負責 UI 與組裝；資料抓取、KD 計算、寫庫全部沿用 sentinel/ 既有模組。
"""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from sentinel import fetcher, repository as repo
from sentinel.db import SessionLocal, init_db

# 台股慣例：紅漲綠跌
UP_COLOR, DOWN_COLOR = "#e53935", "#2e9e5b"
K_COLOR, D_COLOR = "#f59e0b", "#3b82f6"

st.set_page_config(page_title="Stock Sentinel", page_icon="📈", layout="wide")
init_db()


def load_markers() -> tuple[set[str], set[str]]:
    """回傳 (月 KD 低檔黃金交叉的代號集合, 觸及警示價的代號集合)。判斷規則與 screen.py 完全相同。"""
    with SessionLocal() as s:
        gc = {m.symbol for m, _ in repo.golden_cross_candidates(s)}
        alerts = {w.symbol for w, *_ in repo.price_alerts(s)}
    return gc, alerts


def load_watchlist() -> dict[str, str]:
    """{代號: 名稱}，只含啟用中的標的。"""
    from sentinel.models import Watchlist
    from sqlalchemy import select
    with SessionLocal() as s:
        rows = s.execute(select(Watchlist.symbol, Watchlist.name)
                         .where(Watchlist.is_active).order_by(Watchlist.symbol)).all()
    return {sym: (name or "") for sym, name in rows}


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


# ───────────────────────── 側邊欄 ─────────────────────────
watch = load_watchlist()
gc_set, alert_set = load_markers()
with SessionLocal() as _s:
    fmap = repo.fundamentals_map(_s)

# 新增股票成功後要自動切換選取；selectbox 建立後不能再改它的 state，
# 所以先把目標存在 pending_select，下一輪重跑、selectbox 建立「之前」再套用。
if "pending_select" in st.session_state:
    st.session_state["symbol"] = st.session_state.pop("pending_select")


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def fmt_num(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def label(sym: str) -> str:
    marks = ("🟢" if sym in gc_set else "") + ("🔔" if sym in alert_set else "")
    return f"{marks + ' ' if marks else ''}{sym} {watch.get(sym, '')}".strip()


with st.sidebar:
    st.header("📈 Stock Sentinel")
    st.subheader("基本面過濾")
    f_pe = st.checkbox("只看 PE < 15")
    f_roe = st.checkbox("只看 ROE > 15%")
    f_dy = st.checkbox("只看殖利率 > 4%")
    # 三個條件為 AND；沒資料（None）的標的在條件啟用時一律排除，避免把「沒資料」當成「便宜」
    passed = repo.filter_by_fundamentals(
        fmap, max_pe=15 if f_pe else None,
        min_roe=0.15 if f_roe else None, min_yield=0.04 if f_dy else None)
    options = [s for s in watch if passed is None or s in passed]

    st.subheader("已收錄標的")
    if options:
        # 過濾後原本選的標的可能不在清單內；selectbox 的 state 必須是合法選項，先重置避免例外
        if st.session_state.get("symbol") not in options:
            st.session_state.pop("symbol", None)
        symbol = st.selectbox("選擇股票", options, key="symbol", format_func=label)
        st.caption("🟢 月KD<20 黃金交叉　🔔 觸及警示價")
    else:
        symbol = None
        if watch:
            st.warning("沒有標的符合目前的基本面條件，請取消部分勾選。")
        else:
            st.info("資料庫還沒有標的，請先在下方新增。")

    st.divider()
    st.subheader("新增股票")
    new_sym = st.text_input("股票代號", placeholder="AAPL 或 2330.TW")
    if st.button("抓取並寫入", type="primary", width="stretch"):
        try:
            with st.spinner("抓取日線、新聞並計算月 KD…"):
                added = add_and_sync(new_sym)
            st.session_state["pending_select"] = added
            st.rerun()
        except Exception as e:
            st.error(f"新增失敗：{e}")

# ───────────────────────── 主畫面 ─────────────────────────
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
               f"{symbol}` 同步，或在側邊欄重新抓取。")
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
