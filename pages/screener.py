"""🎯 價值與月 KD 選股雷達：多條件篩選，可把標的加入核心名單，或帶到個股戰情室查看。"""
import hashlib

import pandas as pd
import streamlit as st

import app_common as ac
from sentinel import repository as repo
from sentinel.db import SessionLocal

st.title("🎯 價值與月 KD 選股雷達")
ac.show_flash()


def bind(store_key: str, widget_key: str, default):
    """篩選條件都要跨頁保留：從戰情室回來時，條件不能被重置（見 app_common.sticky）。"""
    return ac.sticky(store_key, widget_key, default)


with st.container(border=True):
    st.subheader("篩選條件")
    pool_label = st.radio("候選池", list(ac.SCREEN_POOLS), key="scr_pool_w", horizontal=True,
                          on_change=bind("scr_pool", "scr_pool_w", "全部標的"))
    category = ac.SCREEN_POOLS[pool_label]  # None ＝ 不限分類

    c1, c2, c3, c4 = st.columns([1.3, 1.3, 1, 1.2])
    signal_label = c1.selectbox("訊號型態", list(ac.SIGNALS), key="scr_signal_w",
                                on_change=bind("scr_signal", "scr_signal_w", "低檔黃金交叉"))
    signal_type = ac.SIGNALS[signal_label]
    lookback = c2.slider("回溯月數", 1, 6, key="scr_lookback_w",
                         on_change=bind("scr_lookback", "scr_lookback_w", 3))
    low_mode = signal_type != "death_cross"
    threshold = c3.selectbox("低檔 KD 門檻", [20, 25, 30, 50], key="scr_threshold_w",
                             on_change=bind("scr_threshold", "scr_threshold_w", 20),
                             disabled=not low_mode,
                             help="K、D（或勾頭的 K）需低於此值；高檔死亡交叉固定為 80" if low_mode
                             else "高檔死亡交叉固定為 K、D > 80")
    bullish_mode = signal_type == "golden_cross"
    still_bullish = c4.checkbox("最新期仍 K > D", key="scr_bullish_w",
                                on_change=bind("scr_bullish", "scr_bullish_w", True),
                                disabled=not bullish_mode,
                                help="只適用於黃金交叉：交叉後最新一期沒有又掉回去" if bullish_mode
                                else "只適用於黃金交叉")

    st.markdown("**價值面條件**（勾選的條件彼此為 AND，並與上面的訊號一起篩選）")
    v1, v2, v3 = st.columns(3)
    f_pe = v1.checkbox("PE < 15", key="scr_pe_w", on_change=bind("scr_pe", "scr_pe_w", False))
    f_roe = v2.checkbox("ROE > 15%", key="scr_roe_w", on_change=bind("scr_roe", "scr_roe_w", False))
    f_dy = v3.checkbox("殖利率 > 4%", key="scr_dy_w", on_change=bind("scr_dy", "scr_dy_w", False))

    st.caption("回溯範圍以「最新一根月線算第 1 個月」計算，1 ＝ 只看最新一期；"
               "訊號月若是尚未走完的當月，K/D 會隨每日資料變動。")

result = ac.run_screener(signal_type, lookback, float(threshold), bool(still_bullish), category)

with SessionLocal() as s:
    fmap = repo.fundamentals_map(s)
passed = repo.filter_by_fundamentals(
    fmap, max_pe=15 if f_pe else None,
    min_roe=0.15 if f_roe else None, min_yield=0.04 if f_dy else None)
if passed is not None:
    # 沒資料（None）的標的在條件啟用時一律排除，避免把「沒資料」當成「便宜」
    result = result[result["symbol"].isin(passed)].reset_index(drop=True)
    if category != "core":
        st.warning("S&P 500 池為批次同步，沒有基本面資料，啟用價值面條件時這些標的會被排除。")

if result.empty:
    st.info("沒有符合條件的標的。可放寬回溯月數、低檔門檻或價值面條件。"
            if ac.load_watchlist(category) else "這個候選池還沒有資料。")
    st.stop()

st.write(f"共 **{len(result)}** 檔符合。勾選列後可「加入核心觀察名單」（可多選），"
         "或選一檔按「查看個股」。")


def _fund_col(attr: str, scale: float = 1.0) -> list:
    out = []
    for sym in result["symbol"]:
        f = fmap.get(sym)
        v = getattr(f, attr) if f else None
        out.append(None if v is None else v * scale)
    return out


core_set = set(ac.load_watchlist("core"))
partial = result["is_partial"].astype(bool) & (result["signal_month"] == result["latest_month"])
table = pd.DataFrame({
    "代號": result["symbol"], "名稱": result["name"].fillna(""),
    "分類": result["symbol"].map(lambda sym: "core" if sym in core_set else "sp500"),
    "訊號月份": result["signal_month"] + partial.map({True: "（未收盤）", False: ""}),
    "訊號時 K": result["sig_k"], "訊號時 D": result["sig_d"],
    "最新 K": result["k_val"], "最新 D": result["d_val"], "最新收盤": result["close"],
    "PE": _fund_col("pe"), "ROE %": _fund_col("roe", 100), "殖利率 %": _fund_col("dividend_yield", 100),
})

# 選取狀態以「列索引」記錄，所以 key 要帶入篩選條件與結果內容：
# 條件一變、列的內容變了，選取就自動清空，不會讓舊的索引指到別檔股票。
# nonce：加入核心後遞增，換一個新的表格 key，讓勾選狀態乾淨地重置。
nonce = st.session_state.get("screen_nonce", 0)
sig = hashlib.md5("|".join(result["symbol"]).encode()).hexdigest()[:8]
table_key = f"screen_{nonce}_{category}_{signal_type}_{lookback}_{threshold}_{still_bullish}_{sig}"
num1 = st.column_config.NumberColumn(format="%.1f")
event = st.dataframe(
    table, hide_index=True, width="stretch", height=min(35 * len(table) + 38, 520),
    on_select="rerun", selection_mode="multi-row", key=table_key,
    column_config={c: num1 for c in ("訊號時 K", "訊號時 D", "最新 K", "最新 D",
                                     "PE", "ROE %", "殖利率 %")}
    | {"最新收盤": st.column_config.NumberColumn(format="%.2f")})

picked = result.iloc[event.selection.rows]["symbol"].tolist() if event.selection.rows else []
st.caption(f"已選取 {len(picked)} 檔" + (f"：{', '.join(picked)}" if picked else ""))

b1, b2 = st.columns(2)
add_clicked = b1.button("⭐ 加入核心觀察名單（設為 core）", width="stretch", disabled=not picked,
                        help="可多選；已是核心的會略過，警示價與備註不會被改動")
view_clicked = b2.button("📊 查看個股", width="stretch", disabled=len(picked) != 1,
                         help="請剛好選取 1 檔" if len(picked) != 1 else "跳到個股戰情室查看這一檔")

if add_clicked:
    with SessionLocal.begin() as s:
        n = repo.promote_to_core(s, picked)
    st.cache_data.clear()  # 名單與標記都快取過，不清掉就看不到變動
    st.session_state["screen_nonce"] = nonce + 1
    st.session_state["flash"] = (
        ("success", f"已將 {n} 檔加入核心觀察名單（選取的其餘標的本來就是核心）。"
                    "新聞與基本面可到戰情室按「單檔即時更新」補抓。")
        if n else ("info", "選取的標的都已經在核心觀察名單中。"))
    st.rerun()

if view_clicked and len(picked) == 1:
    st.session_state["selected_symbol"] = picked[0]
    st.switch_page("pages/dashboard.py")
