"""📊 個股戰情室：選一檔核心觀察股，看卡片、月 K 與月 KD 圖、基本面、Claude 研究報告與新聞。

可由其他頁面透過 st.session_state["selected_symbol"] 帶入代號（例如選股雷達的「查看個股」）。
"""
import pandas as pd
import streamlit as st

import app_common as ac
from sentinel import repository as repo, syncer
from sentinel.db import SessionLocal

core_watch = ac.load_watchlist("core")
gc_set, alert_set = ac.load_markers("core")

# 從雷達帶來的標的可能不是核心（例如 S&P 500 池）。下拉選單平常只列核心，
# 這種標的就「臨時」放在最上方並標示 🔍，讓使用者仍能看圖；一旦改選別檔，它就不再出現。
incoming = st.session_state.get("selected_symbol")
temp_symbol = None
temp_name = ""
if incoming and incoming not in core_watch:
    all_watch = ac.load_watchlist(None)
    if incoming in all_watch:
        temp_symbol, temp_name = incoming, all_watch[incoming]

options = ([temp_symbol] if temp_symbol else []) + list(core_watch)


def label(sym: str) -> str:
    if sym == temp_symbol:
        return f"🔍非核心 {sym} {temp_name}".strip()
    marks = ("🟢" if sym in gc_set else "") + ("🔔" if sym in alert_set else "")
    return f"{marks + ' ' if marks else ''}{sym} {core_watch.get(sym, '')}".strip()


with st.sidebar:
    st.header("📊 個股戰情室")
    symbol = None
    if options:
        # 以 selected_symbol 為準灌回 widget（見 app_common.sticky 的說明）；不在名單內時退回第一檔
        st.session_state["dash_symbol_w"] = incoming if incoming in options else options[0]
        symbol = st.selectbox(
            "核心觀察名單", options, key="dash_symbol_w", format_func=label,
            on_change=lambda: st.session_state.__setitem__(
                "selected_symbol", st.session_state["dash_symbol_w"]))
        st.caption("🟢 月KD<20 黃金交叉　🔔 觸及警示價")
        st.session_state["selected_symbol"] = symbol
    else:
        st.info("核心觀察名單是空的。請到「資料庫與同步中心」新增股票，"
                "或在「選股雷達」把標的加入核心。")

    st.divider()
    st.caption("資料更新")
    # 非核心標的只更新日線與月 KD（不抓新聞與基本面），與批次同步的範圍一致
    is_light = bool(symbol) and symbol not in core_watch
    update_clicked = st.button(
        "🔄 單檔即時更新", width="stretch", disabled=not symbol,
        help="重新抓取目前顯示的這一檔" + ("（非核心標的只抓日線與月 KD）" if is_light else ""))

ac.show_flash()
job_box = st.container()  # 同步進度（st.status）顯示在這裡

if update_clicked and symbol:
    def _sync_current(log, progress):
        syncer.sync_one(symbol, log, light=is_light)
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
    report = repo.latest_report(s, symbol)
    queued = repo.get_needs_report(s, symbol)  # 是否已排入待（重新）分析佇列
    # 在 session 內把需要的欄位取出，避免離開 with 後存取已脫離的物件
    news_rows = [(n.title, n.link, n.publisher, n.published_at, n.summary) for n in news]
    report_data = None if report is None else dict(
        verdict=report.verdict, business_model=report.business_model,
        moat_and_valuation=report.moat_and_valuation, catalysts=report.catalysts,
        risks_and_triggers=report.risks_and_triggers, created_at=report.created_at)

name = core_watch.get(symbol) or (temp_name if symbol == temp_symbol else "")
st.title(f"{symbol}　{name}")
if symbol == temp_symbol:
    st.caption("🔍 這檔不在核心觀察名單。可到「選股雷達」勾選後按「⭐ 加入核心觀察名單」。")

# ───────────────────────── 上半：數據卡片與圖表 ─────────────────────────
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

st.divider()

# ───────────────────────── 下半：研究報告（左）與新聞（右）─────────────────────────
left, right = st.columns(2)

with left:
    head, head_action = st.columns([3, 1], vertical_alignment="center")
    head.subheader("🧠 Claude 投資研究報告")
    # 只有核心觀察股才有「待分析」佇列，非核心的臨時標的不顯示（需先加入核心）。
    # 已排入佇列（含新加入、尚無報告者）就只顯示狀態，避免重複點擊；
    # 否則顯示按鈕，點擊後旗標設為 1，到同步中心的待分析清單就會看到它。
    if symbol in core_watch:
        if queued:
            head_action.caption("⏳ 已排入待重算佇列")
        elif head_action.button("🔄 標記重新分析", key="mark_redo", width="stretch",
                                help="把這檔排入待分析清單，之後會產生新的研究報告"):
            with SessionLocal.begin() as s:
                repo.set_needs_report(s, symbol, True)
            st.rerun()
    if report_data is None:
        st.info("這檔還沒有研究報告。報告建立後會顯示在這裡；"
                "可到「資料庫與同步中心」查看待分析清單。")
    else:
        # 報告內容由 Claude 產生，屬於「資料」：一律用 st.markdown 顯示純文字格式，
        # 不開 unsafe_allow_html，避免內容夾帶 HTML 被執行。
        with st.container(border=True):
            st.markdown(f"#### {report_data['verdict']}")
            st.caption(f"報告時間：{report_data['created_at']}")
        sections = [("🏢 公司與產業在做什麼", report_data["business_model"]),
                    ("🏰 護城河與當前估值", report_data["moat_and_valuation"]),
                    ("🚀 未來 6~12 個月催化劑", report_data["catalysts"]),
                    ("⚠️ 核心風險與觀察指標", report_data["risks_and_triggers"])]
        for title, body in sections:
            st.markdown(f"**{title}**")
            st.markdown(body or "—")

with right:
    st.subheader("📰 近期相關新聞")
    if not news_rows:
        st.caption("資料庫中沒有這檔的新聞。")
    for title, link, publisher, published, summary in news_rows:
        st.markdown(f"**[{title}]({link})**")
        st.caption(f"{publisher or '未知來源'} · {published:%Y-%m-%d %H:%M}" if published
                   else (publisher or "未知來源"))
        if summary:
            st.write(summary)
        st.divider()
