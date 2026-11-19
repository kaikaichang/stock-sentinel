"""⚙️ 資料庫與同步中心：新增股票、批次同步、重算月 KD，並顯示資料庫概況。"""
import streamlit as st

import app_common as ac
from sentinel import repository as repo, syncer
from sentinel.db import SessionLocal

st.title("⚙️ 資料庫與同步中心")
ac.show_flash()
job_box = st.container()  # 同步進度（st.status）顯示在這裡

# 概況不快取：資料量極小，而且同步完成後一定要立刻看到新數字
with SessionLocal() as s:
    stats = repo.db_stats(s)
m1, m2, m3, m4 = st.columns(4)
m1.metric("自選股總數", stats["total"])
m2.metric("核心自選", stats["core"])
m3.metric("S&P 500 池", stats["sp500"])
m4.metric("最新收盤日期", str(stats["latest_date"]) if stats["latest_date"] else "—")
if stats["sp500"] == 0:
    st.caption("S&P 500 池是空的，可先執行 `python sync.py --import-sp500` 匯入成分股名單。")

st.divider()
st.subheader("新增股票")
with st.form("add_form", border=False):
    new_sym = st.text_input("股票代號", placeholder="AAPL 或 2330.TW")
    submitted = st.form_submit_button("抓取並寫入", type="primary")
if submitted:
    try:
        with st.spinner("抓取日線、新聞、基本面並計算月 KD…"):
            added = syncer.add_and_sync(new_sym)
    except Exception as e:
        st.error(f"新增失敗：{e}")
    else:
        st.cache_data.clear()  # 名單與標記都快取過，不清掉就看不到新標的
        # 預先指定戰情室要看的標的：新增的一律是核心自選，所以標的池也一併切回核心
        st.session_state["selected_symbol"] = added
        st.session_state["pool"] = ac.DEFAULT_POOL
        st.session_state["flash"] = ("success", f"已新增 {added}，可到「個股戰情室」查看。")
        st.rerun()

st.divider()
st.subheader("批次同步")


def _sync_core(log, progress):
    failed = syncer.sync_core(log, progress)
    if failed:
        return ("warning", f"同步完成，但有 {len(failed)} 檔無資料或失敗：{', '.join(failed[:15])}"
                           + ("…" if len(failed) > 15 else ""))
    return ("success", "同步完成，全部成功")


def _sync_sp500(log, progress):
    failed = syncer.sync_sp500_batch(log, progress)
    if failed:
        return ("warning", f"同步完成，但有 {len(failed)} 檔無資料或失敗：{', '.join(failed[:15])}"
                           + ("…" if len(failed) > 15 else ""))
    return ("success", "同步完成，全部成功")


def _recompute(log, progress):
    n = syncer.recompute_all_monthly(log, progress)
    return ("success", f"已重算 {n} 檔的月 KD")


b1, b2, b3 = st.columns(3)
if b1.button("🚀 同步全部自選股", width="stretch",
             help="逐檔抓日線、新聞與基本面，每檔間隔 1 秒"):
    ac.run_job(job_box, "同步全部自選股", _sync_core)
if b2.button("🌐 同步 S&P 500 池（批次）", width="stretch",
             help="分批抓日線與月 KD，不抓新聞，約需 1 分鐘"):
    ac.run_job(job_box, "同步 S&P 500 池", _sync_sp500)
if b3.button("🧮 僅重算全庫月 KD", width="stretch",
             help="不連網，從資料庫的日線重新計算全部標的的月 K 與 KD"):
    ac.run_job(job_box, "重算全庫月 KD", _recompute)
