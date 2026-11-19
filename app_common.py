"""三個頁面共用的程式：快取查詢、圖表、格式化、同步進度、跨頁狀態輔助。

為什麼放在根目錄而不是 sentinel/：sentinel/ 刻意不依賴 streamlit（命令列也要用），
這裡是「網頁專用」的共用層。Streamlit 會把入口檔（app.py）所在資料夾放進 sys.path，
所以各頁可以直接 `import app_common`。
"""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import select

from sentinel import repository as repo
from sentinel.db import SessionLocal
from sentinel.models import Watchlist

# 台股慣例：紅漲綠跌
UP_COLOR, DOWN_COLOR = "#e53935", "#2e9e5b"
K_COLOR, D_COLOR = "#f59e0b", "#3b82f6"

# 選股雷達的候選池：None ＝ 不限分類（核心＋掃描池）
SCREEN_POOLS = {"全部標的": None, "Core 名單": "core"}
SIGNALS = {"低檔黃金交叉": "golden_cross", "低檔勾頭向上": "hook_up", "高檔死亡交叉（>80）": "death_cross"}
CACHE_TTL = 300  # 秒。終端機另外跑 sync.py 時，網頁最多 5 分鐘後也會反映；網頁按鈕完成後會手動清除


# ───────────────────────── 快取查詢 ─────────────────────────
# 為什麼查詢要快取：每次點選、勾選都會讓整支腳本重跑，
# 500 檔的名單、標記與篩選若每次都重查資料庫，操作會明顯卡頓。
# 只快取「純資料」（dict / set / DataFrame）；ORM 物件不可序列化，所以基本面 fmap 不快取。
# 放在共用模組：三個頁面共用同一份快取，同步完成後一次 st.cache_data.clear() 全部失效。
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_markers(category: str) -> tuple[set[str], set[str]]:
    """回傳 (月 KD 低檔黃金交叉的代號集合, 觸及警示價的代號集合)。判斷規則與 screen.py 完全相同。"""
    with SessionLocal() as s:
        gc = {m.symbol for m, _ in repo.golden_cross_candidates(s, category)}
        alerts = {w.symbol for w, *_ in repo.price_alerts(s)}
    return gc, alerts


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_watchlist(category: str | None) -> dict[str, str]:
    """{代號: 名稱}，只含啟用中且屬於該標的池的標的；category=None 代表全部。"""
    q = select(Watchlist.symbol, Watchlist.name).where(Watchlist.is_active)
    if category is not None:
        q = q.where(Watchlist.category == category)
    with SessionLocal() as s:
        rows = s.execute(q.order_by(Watchlist.symbol)).all()
    return {sym: (name or "") for sym, name in rows}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def run_screener(signal_type: str, lookback: int, threshold: float,
                 still_bullish: bool, category: str | None) -> pd.DataFrame:
    with SessionLocal() as s:
        return repo.screen_monthly_kd(s, signal_type, lookback, threshold, still_bullish, category)


# ───────────────────────── 跨頁狀態 ─────────────────────────
# 為什麼要「持久值」與「widget key」分開：
# Streamlit 在某個 widget 沒被渲染的那一輪會丟掉它的 state，切到別頁再回來，
# 下拉選單、滑桿都會被重置。做法是把值另外存成普通 session_state（不會被清掉），
# widget 建立前先把持久值灌回去；使用者操作時由 on_change 回寫持久值。
# （widget 建立「之後」不能再改它的 state，所以一定要在建立前呼叫。）
def sticky(store_key: str, widget_key: str, default):
    """在建立 widget 之前呼叫；回傳要給該 widget 的 on_change callback。"""
    if store_key not in st.session_state:
        st.session_state[store_key] = default
    st.session_state[widget_key] = st.session_state[store_key]

    def _save() -> None:
        st.session_state[store_key] = st.session_state[widget_key]
    return _save


# ───────────────────────── 同步進度 ─────────────────────────
def show_flash() -> None:
    """顯示上一輪同步的結果摘要。

    同步完成後會 rerun，而 rerun 會清掉 st.status 區塊，
    所以摘要先存進 session_state["flash"]，重跑後在頁面頂端顯示。
    """
    flash = st.session_state.pop("flash", None)
    if flash:
        {"success": st.success, "info": st.info,
         "warning": st.warning, "error": st.error}[flash[0]](flash[1])


def run_job(box, title: str, job) -> None:
    """在 box（st.container）顯示 st.status 進度並執行 job(log, progress)。

    成功後清除快取（資料變了，名單與篩選結果都要重算）、把摘要存起來並 rerun；
    失敗則停在原地顯示錯誤，不 rerun，讓使用者看得到錯誤訊息。
    注意：同步中若再點別的控制項，Streamlit 會中斷並重跑腳本；
    每一檔都是各自的交易，已寫入的資料不會損毀，重新按一次即可從增量接續。
    """
    with box:
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


# ───────────────────────── 圖表與格式 ─────────────────────────
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
