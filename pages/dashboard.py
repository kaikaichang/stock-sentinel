"""📊 個股戰情室：選一檔股票，看卡片、月 K 與月 KD 圖、基本面與新聞。

可由其他頁面透過 st.session_state["selected_symbol"] 帶入代號（例如選股雷達點選一列）。
"""
import pandas as pd
import streamlit as st

import app_common as ac
from sentinel import repository as repo, syncer
from sentinel.db import SessionLocal

# 其他頁面（選股雷達、同步中心）改寫 selected_symbol 後切過來時，標的池可能不對：
# 與本頁上次寫入的值不同，就代表是「外部帶入」，此時把標的池切到該代號所屬的池。
# 不能每次都強制跟隨，否則使用者自己切換標的池時會被立刻彈回去。
incoming = st.session_state.get("selected_symbol")
if incoming and incoming != st.session_state.get("_dash_synced"):
    target_pool = ac.pool_for(incoming)
    if target_pool:
        st.session_state["pool"] = target_pool

with st.sidebar:
    st.header("📊 個股戰情室")
    save_pool = ac.sticky("pool", "dash_pool_w", ac.DEFAULT_POOL)
    pool_label = st.radio("標的池", list(ac.POOLS), key="dash_pool_w", horizontal=True,
                          on_change=save_pool)
    category = ac.POOLS[pool_label]
    is_scan = category == "sp500"

    watch = ac.load_watchlist(category)
    gc_set, alert_set = ac.load_markers(category)
    options = list(watch)

    def label(sym: str) -> str:
        marks = ("🟢" if sym in gc_set else "") + ("🔔" if sym in alert_set else "")
        return f"{marks + ' ' if marks else ''}{sym} {watch.get(sym, '')}".strip()

    symbol = None
    if options:
        # 以 selected_symbol 為準灌回 widget；它不在這個池的名單內時退回第一檔
        current = st.session_state.get("selected_symbol")
        st.session_state["dash_symbol_w"] = current if current in options else options[0]
        symbol = st.selectbox(
            "選擇股票", options, key="dash_symbol_w", format_func=label,
            on_change=lambda: st.session_state.__setitem__(
                "selected_symbol", st.session_state["dash_symbol_w"]))
        st.caption("🟢 月KD<20 黃金交叉　🔔 觸及警示價")
        st.session_state["selected_symbol"] = symbol
        st.session_state["_dash_synced"] = symbol
    elif is_scan:
        st.info("掃描池是空的，請先執行 `python sync.py --import-sp500`。")
    else:
        st.info("資料庫還沒有標的，請到「資料庫與同步中心」新增。")

    st.divider()
    st.caption("資料更新")
    update_clicked = st.button(
        "🔄 更新當前標的", width="stretch", disabled=not symbol,
        help="重新抓取目前顯示的這一檔" + ("（掃描池只抓日線與月 KD）" if is_scan else ""))

ac.show_flash()
job_box = st.container()  # 同步進度（st.status）顯示在這裡

if update_clicked and symbol:
    def _sync_current(log, progress):
        syncer.sync_one(symbol, log, light=is_scan)
        progress(1, 1)
        return ("success", f"已更新 {symbol}")
    ac.run_job(job_box, f"更新 {symbol}", _sync_current)

if not symbol:
    st.title("尚無資料")
    st.stop()

with SessionLocal() as s:
    monthly = repo.monthly_series(s, symbol)
    last_px = repo.latest_close(s, symbol)
    news = repo.recent_news(s, symbol)
    fund = repo.fundamentals_map(s).get(symbol)
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
    st.plotly_chart(ac.build_chart(monthly, symbol), width="stretch")

# 基本面卡片：與技術面分開判斷，沒資料的標的仍能看圖，只是這一列顯示「—」
f1, f2, f3, f4 = st.columns(4)
f1.metric("PE（本益比）", ac.fmt_num(fund.pe if fund else None))
f2.metric("PB（股價淨值比）", ac.fmt_num(fund.pb if fund else None))
f3.metric("ROE", ac.fmt_pct(fund.roe if fund else None))
f4.metric("殖利率", ac.fmt_pct(fund.dividend_yield if fund else None))
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
