"""核心觀察名單的移出／刪除：repository 與網頁（AppTest）測試。

執行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v

為什麼一開頭就設環境變數：sentinel.db 的 engine 在 import 時就建立，
必須在 import sentinel 之前把資料庫指到暫存檔，測試才不會碰到正式的 data/stocks.db。
"""
import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="sentinel_test_"))
os.environ["STOCK_SENTINEL_DB"] = str(_TMP / "test.db")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)  # Windows 上檔案偶爾仍被占用，清不掉就略過

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import sentinel.db as sdb  # noqa: E402
from sentinel import repository as repo  # noqa: E402
from sentinel.db import SessionLocal, init_db  # noqa: E402
from sentinel.models import (Base, DailyPrice, MonthlyIndicator, StockNews,  # noqa: E402
                             Watchlist)

ROOT = Path(__file__).resolve().parent.parent
THIS_MONTH = date.today().strftime("%Y-%m")


def reset_db() -> None:
    """每個測試都從乾淨的資料庫開始，並清掉網頁的 st.cache_data（否則會讀到上一個測試的名單）。"""
    assert str(sdb.DB_PATH).startswith(str(_TMP)), "測試必須使用暫存資料庫，不可碰正式資料"
    Base.metadata.drop_all(sdb.engine)
    init_db()
    st.cache_data.clear()


def seed(*specs) -> None:
    """specs: (代號, category, in_sp500)；core 與 sp500 預設 is_active=True，needs_report=1。"""
    with SessionLocal.begin() as s:
        for sym, cat, sp in specs:
            s.add(Watchlist(symbol=sym, name=f"{sym}公司", market="US", category=cat,
                            in_sp500=sp, is_active=True, needs_report=1))


def seed_golden_cross(symbol: str) -> None:
    """為該標的塞一根符合「低檔黃金交叉」的最新月線，讓篩選器抓得到。"""
    with SessionLocal.begin() as s:
        s.add(MonthlyIndicator(symbol=symbol, year_month=THIS_MONTH, open=10, high=12, low=9, close=11,
                               k_val=15, d_val=12, prev_k=10, prev_d=12, is_partial=True))


def info(symbol: str) -> dict:
    with sdb.engine.connect() as c:
        r = c.exec_driver_sql(
            "select category, is_active, needs_report, in_sp500, target_low, notes "
            "from watchlist where symbol=?", (symbol,)).first()
    return None if r is None else dict(zip(
        ("category", "is_active", "needs_report", "in_sp500", "target_low", "notes"), r))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_demote_sp500_member_returns_to_scan_pool(self):
        seed(("AAA", "core", 1))
        with SessionLocal.begin() as s:
            s.query(Watchlist).filter_by(symbol="AAA").update({"target_low": 5.0, "notes": "保留我"})
            repo.add_report(s, "AAA", "結論")
        with SessionLocal.begin() as s:
            self.assertEqual(repo.demote_from_core(s, "aaa"), "sp500")  # 代號大小寫不拘
        i = info("AAA")
        self.assertEqual((i["category"], i["is_active"], i["needs_report"]), ("sp500", 1, 0))
        self.assertEqual((i["target_low"], i["notes"]), (5.0, "保留我"))  # 警示價與備註保留
        with SessionLocal() as s:
            self.assertIsNotNone(repo.latest_report(s, "AAA"))              # 研究報告保留
            self.assertNotIn("AAA", repo.active_symbols(s, "core"))
            self.assertIn("AAA", repo.active_symbols(s, "sp500"))
            stats = repo.db_stats(s)
            self.assertEqual((stats["core"], stats["sp500"]), (0, 1))

    def test_demote_non_member_is_archived_and_hidden_everywhere(self):
        seed(("BBB", "core", 0), ("KEEP", "core", 0))
        for sym in ("BBB", "KEEP"):
            seed_golden_cross(sym)
        with SessionLocal.begin() as s:
            s.add(DailyPrice(symbol="BBB", trade_date=date.today(), close=10.0))
            s.query(Watchlist).filter_by(symbol="BBB").update({"target_high": 5.0})  # 收盤 10 > 5 會觸發警示
            repo.add_report(s, "BBB", "結論")
            s.query(Watchlist).filter_by(symbol="BBB").update({"needs_report": 1})    # 移出前仍排在待分析
        with SessionLocal() as s:  # 移出前：各清單都看得到
            self.assertIn("BBB", [sym for sym, _, _ in repo.pending_reports(s)])
            self.assertIn("BBB", [w.symbol for w, *_ in repo.price_alerts(s)])
            self.assertIn("BBB", repo.screen_monthly_kd(s, "golden_cross", 1, 20, True, None)["symbol"].tolist())
        with SessionLocal.begin() as s:
            self.assertEqual(repo.demote_from_core(s, "BBB"), "archived")
        i = info("BBB")
        self.assertEqual((i["category"], i["is_active"], i["needs_report"]), ("archived", 0, 0))
        with SessionLocal() as s:  # 移出後：所有日常清單都不再出現，但資料與報告保留
            self.assertNotIn("BBB", repo.active_symbols(s, None))
            self.assertNotIn("BBB", [sym for sym, _, _ in repo.pending_reports(s)])
            self.assertNotIn("BBB", [w.symbol for w, *_ in repo.price_alerts(s)])
            self.assertNotIn("BBB", repo.screen_monthly_kd(s, "golden_cross", 1, 20, True, None)["symbol"].tolist())
            self.assertIn("KEEP", repo.screen_monthly_kd(s, "golden_cross", 1, 20, True, None)["symbol"].tolist())
            self.assertEqual(repo.db_stats(s)["core"], 1)
            self.assertIsNotNone(repo.latest_report(s, "BBB"))
            self.assertIsNotNone(repo.latest_close(s, "BBB"))

    def test_demote_returns_none_for_non_core_or_missing(self):
        seed(("CCC", "sp500", 1))
        with SessionLocal.begin() as s:
            self.assertIsNone(repo.demote_from_core(s, "CCC"))
            self.assertIsNone(repo.demote_from_core(s, "NOPE"))
        self.assertEqual(info("CCC")["category"], "sp500")

    def test_readd_after_archive_returns_to_core_and_queues_report(self):
        seed(("DDD", "core", 0))
        with SessionLocal.begin() as s:
            repo.demote_from_core(s, "DDD")
        with SessionLocal.begin() as s:
            repo.add_watch(s, "DDD")  # 同步中心重新輸入代號
        i = info("DDD")
        self.assertEqual((i["category"], i["is_active"], i["needs_report"]), ("core", 1, 1))
        # 再封存一次，改走「加入核心」按鈕的 promote_to_core 路徑
        with SessionLocal.begin() as s:
            repo.demote_from_core(s, "DDD")
            self.assertEqual(repo.promote_to_core(s, ["DDD"]), 1)
        i = info("DDD")
        self.assertEqual((i["category"], i["is_active"], i["needs_report"]), ("core", 1, 1))

    def test_import_universe_marks_existing_core_without_changing_category(self):
        seed(("EEE", "core", 0))
        with SessionLocal.begin() as s:
            repo.import_universe(s, [("EEE", "E 公司"), ("NEW1", "新成分股")], category="sp500")
        self.assertEqual((info("EEE")["category"], info("EEE")["in_sp500"]), ("core", 1))
        self.assertEqual((info("NEW1")["category"], info("NEW1")["in_sp500"]), ("sp500", 1))
        with SessionLocal.begin() as s:
            self.assertEqual(repo.demote_from_core(s, "EEE"), "sp500")  # 補標記後移出會降回掃描池

    def test_delete_stock_removes_all_related_rows_only_for_that_symbol(self):
        seed(("DEL", "core", 0), ("STAY", "core", 0))
        with SessionLocal.begin() as s:
            for sym in ("DEL", "STAY"):
                s.add(DailyPrice(symbol=sym, trade_date=date.today(), close=1.0))
                s.add(MonthlyIndicator(symbol=sym, year_month=THIS_MONTH, k_val=1, d_val=1))
                s.add(StockNews(symbol=sym, title="t", link=f"http://x/{sym}"))
                repo.upsert_fundamentals(s, sym, {"pe": 10.0})
                repo.add_report(s, sym, "結論")
        with SessionLocal.begin() as s:
            counts = repo.delete_stock(s, "del")
        self.assertEqual(counts, {"stock_reports": 1, "stock_news": 1, "fundamentals": 1,
                                  "monthly_indicators": 1, "daily_prices": 1, "watchlist": 1})
        self.assertIsNone(info("DEL"))
        with sdb.engine.connect() as c:
            for table in ("daily_prices", "monthly_indicators", "stock_news", "fundamentals", "stock_reports"):
                n = c.exec_driver_sql(f"select count(*) from {table} where symbol='DEL'").scalar()
                self.assertEqual(n, 0, table)
                n = c.exec_driver_sql(f"select count(*) from {table} where symbol='STAY'").scalar()
                self.assertEqual(n, 1, f"{table} 的其他標的不應被刪")
        with SessionLocal.begin() as s:  # 不存在的代號：全部 0、不報錯
            self.assertEqual(sum(repo.delete_stock(s, "NOPE").values()), 0)


class MigrationTests(unittest.TestCase):
    def test_in_sp500_column_is_added_and_backfilled_once(self):
        path = _TMP / "legacy.db"
        path.unlink(missing_ok=True)
        con = sqlite3.connect(path)
        con.executescript("""
            create table watchlist (symbol varchar(20) primary key, name varchar(100), market varchar(2) not null,
              target_low float, target_high float, notes text, is_active boolean,
              category varchar(10) not null default 'core', needs_report integer not null default 1,
              created_at datetime default current_timestamp);
            insert into watchlist(symbol,name,market,is_active,category) values
              ('C1','核心','US',1,'core'),('S1','掃描','US',1,'sp500');
        """)
        con.commit(); con.close()
        original = sdb.engine
        sdb.engine = sdb.get_engine(path)
        try:
            init_db()
            con = sqlite3.connect(path)
            self.assertEqual(dict(con.execute("select symbol, in_sp500 from watchlist")), {"C1": 0, "S1": 1})
            con.execute("update watchlist set in_sp500 = 1 where symbol = 'C1'")  # 模擬之後匯入名單補上的標記
            con.commit(); con.close()
            init_db(); init_db()  # 重複啟動不可把標記洗回去
            con = sqlite3.connect(path)
            self.assertEqual(dict(con.execute("select symbol, in_sp500 from watchlist")), {"C1": 1, "S1": 1})
            con.close()
        finally:
            sdb.engine.dispose()
            sdb.engine = original


def run_app(**state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    return at


def errors(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.exception]


class DashboardDemoteTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        seed(("AAA", "core", 0), ("BBB", "core", 1), ("CCC", "core", 0), ("SP1", "sp500", 1))

    def options(self, at) -> list[str]:
        return list(at.selectbox(key="dash_symbol_w").options)

    def test_dropdown_lists_core_only(self):
        at = run_app()
        self.assertEqual(errors(at), [])
        labels = " ".join(self.options(at))
        for sym in ("AAA", "BBB", "CCC"):
            self.assertIn(sym, labels)
        self.assertNotIn("SP1", labels)

    def test_demote_middle_switches_to_next_symbol(self):
        at = run_app(selected_symbol="BBB")
        at.button(key="demote_core").click().run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["selected_symbol"], "CCC")  # 下一檔
        self.assertNotIn("BBB", " ".join(self.options(at)))
        self.assertEqual(info("BBB")["category"], "sp500")            # BBB 是成分股 → 降回掃描池
        self.assertTrue(any("已移出 BBB" in s.value for s in at.success))

    def test_demote_last_falls_back_to_previous(self):
        at = run_app(selected_symbol="CCC")
        at.button(key="demote_core").click().run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["selected_symbol"], "BBB")
        self.assertEqual(info("CCC")["category"], "archived")          # 非成分股 → 封存

    def test_demote_only_remaining_core_shows_empty_state_without_error(self):
        reset_db()
        seed(("ONLY", "core", 0))
        at = run_app(selected_symbol="ONLY")
        at.button(key="demote_core").click().run()
        self.assertEqual(errors(at), [])
        self.assertNotIn("selected_symbol", at.session_state)
        self.assertTrue(any("核心觀察名單是空的" in i.value for i in at.sidebar.info))

    def test_stale_selected_symbol_of_archived_stock_falls_back_safely(self):
        with SessionLocal.begin() as s:
            repo.demote_from_core(s, "AAA")  # 封存（in_sp500=0）
        st.cache_data.clear()
        at = run_app(selected_symbol="AAA")   # 模擬 session 殘留已封存的代號
        self.assertEqual(errors(at), [])
        self.assertNotIn("AAA", " ".join(self.options(at)))
        self.assertIn("BBB", at.selectbox(key="dash_symbol_w").value)  # 退回名單第一檔

    def test_non_core_temporary_symbol_has_no_demote_button(self):
        at = run_app(selected_symbol="SP1")
        self.assertEqual(errors(at), [])
        self.assertTrue(any("非核心" in o for o in self.options(at)))
        self.assertFalse(any(b.key == "demote_core" for b in at.button))


class ScreenerButtonTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        seed(("COR", "core", 0), ("SPX", "sp500", 1))
        seed_golden_cross("COR")
        seed_golden_cross("SPX")

    def test_buttons_are_disabled_without_selection(self):
        at = run_app()
        at.switch_page("pages/screener.py").run()
        self.assertEqual(errors(at), [])
        states = {b.label: b.disabled for b in at.button}
        self.assertEqual(states["⭐ 加入核心觀察名單"], True)
        self.assertEqual(states["📤 移出核心觀察名單"], True)
        self.assertEqual(states["📊 查看個股"], True)

    def test_table_shows_category_column_for_both_pools(self):
        at = run_app()
        at.switch_page("pages/screener.py").run()
        df = at.dataframe[0].value
        self.assertEqual(dict(zip(df["代號"], df["分類"])), {"COR": "core", "SPX": "sp500"})


if __name__ == "__main__":
    unittest.main()
