"""stock-sentinel 網頁入口（路由器）。

啟動：streamlit run app.py
本檔只負責頁面標題設定與側邊欄導覽註冊；各頁內容在 pages/，共用程式在 app_common.py，
資料抓取、KD 計算、寫庫全部沿用 sentinel/ 既有模組。
"""
import streamlit as st

from sentinel.db import init_db

# set_page_config 只能在入口檔呼叫一次（而且必須是第一個 st 指令），各頁面不得再呼叫
st.set_page_config(page_title="Stock Sentinel", page_icon="📈", layout="wide")
init_db()

# 頁面路徑相對於本檔；pages/screener.py 內的 st.switch_page 也用同一組路徑
nav = st.navigation([
    st.Page("pages/dashboard.py", title="個股戰情室", icon="📊", default=True),
    st.Page("pages/screener.py", title="價值與月 KD 選股雷達", icon="🎯"),
    st.Page("pages/sync_manager.py", title="資料庫與同步中心", icon="⚙️"),
])
nav.run()
