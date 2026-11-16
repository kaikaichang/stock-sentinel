"""同步流程：抓資料、寫庫、重算月 KD。

為什麼獨立成模組：終端機（sync.py）與網頁按鈕（app.py）要做同一件事，
共用一份邏輯才不會兩邊各改各的而行為分歧。
輸出一律走 log / progress callback：CLI 傳 print，網頁傳 st.status 的寫入函式，
本模組本身不 import streamlit，也不直接 print。
"""
import time
from collections.abc import Callable
from datetime import timedelta

from . import fetcher, repository as repo
from .db import SessionLocal

Log = Callable[[str], None]
Progress = Callable[[int, int], None]  # (已完成, 總數)

BATCH_SIZE = 50    # 每批打包幾檔給 yf.download
BATCH_SLEEP = 2    # 批與批之間休息秒數，避免被 Yahoo 限流
SYMBOL_SLEEP = 1   # 逐檔同步的間隔秒數


def _noop_progress(done: int, total: int) -> None:
    pass


def sync_one(symbol: str, log: Log = print, light: bool = False) -> None:
    """同步單檔：日線、新聞、基本面，並重算月 KD。

    light=True 只抓日線與月 KD（S&P 500 池的標的用）：
    與批次同步的範圍一致，也避免為了一檔掃描池標的去打新聞與 info 兩個額外請求。
    """
    with SessionLocal() as s:
        last = repo.latest_trade_date(s, symbol)
    df = fetcher.fetch_daily(symbol, last)
    news, fund = ([], None) if light else (fetcher.fetch_news(symbol),
                                          # 基本面只是輔助資料：fetch_fundamentals 內部已容錯，失敗時各欄為 None
                                          fetcher.fetch_fundamentals(symbol))
    with SessionLocal.begin() as s:
        n_px = repo.upsert_daily(s, symbol, df)
        n_news = repo.upsert_news(s, symbol, news)
        n_m = repo.recompute_monthly(s, symbol)
        if fund is not None:
            repo.upsert_fundamentals(s, symbol, fund)
    if light:
        log(f"  日線 {n_px} 筆、月線 {n_m} 根")
        return
    ok = "已更新" if any(v is not None for v in fund.values()) else "無資料"
    log(f"  日線 {n_px} 筆、新聞 {n_news} 則、月線 {n_m} 根、基本面 {ok}")


def sync_symbols(symbols: list[str], log: Log = print,
                 progress: Progress = _noop_progress, light: bool = False) -> list[str]:
    """逐檔同步，單檔失敗不中斷其他檔；回傳失敗的代號。"""
    failed: list[str] = []
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(SYMBOL_SLEEP)  # 每檔間隔，避免被 Yahoo 限流
        log(f"[{sym}]")
        try:
            sync_one(sym, log, light=light)
        except Exception as e:
            failed.append(sym)
            log(f"  失敗：{e}")
        progress(i + 1, len(symbols))
    return failed


def sync_core(log: Log = print, progress: Progress = _noop_progress) -> list[str]:
    """同步所有啟用中的核心自選（不含 S&P 500 池）。"""
    with SessionLocal() as s:
        symbols = repo.active_symbols(s, category="core")
    return sync_symbols(symbols, log, progress)


def import_sp500(log: Log = print) -> None:
    items = fetcher.fetch_sp500_list()
    with SessionLocal.begin() as s:
        added, skipped = repo.import_universe(s, items, category="sp500")
    log(f"S&P 500 名單 {len(items)} 檔：新增 {added} 檔，已存在略過 {skipped} 檔（既有核心自選不受影響）")


def sync_sp500_batch(log: Log = print, progress: Progress = _noop_progress,
                     period: str = "2y", max_batches: int | None = None) -> list[str]:
    """分批抓 S&P 500 池的日線並重算月 KD，回傳無資料或失敗的代號。

    刻意只做價量與月 KD：新聞是逐檔請求，500 檔一次打下去最容易被限流，
    而黃金交叉篩選只需要價量。基本面同理，需要時再對個別標的用 --symbol 同步。
    """
    with SessionLocal() as s:
        symbols = repo.active_symbols(s, category="sp500")
    if not symbols:
        log("S&P 500 池是空的，請先執行 --import-sp500")
        return []
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    if max_batches:
        batches = batches[:max_batches]
    failed: list[str] = []
    for i, batch in enumerate(batches, 1):
        if i > 1:
            time.sleep(BATCH_SLEEP)
        log(f"[批次 {i}/{len(batches)}] {len(batch)} 檔 {batch[0]}…{batch[-1]}")
        try:
            with SessionLocal() as s:
                lasts = [repo.latest_trade_date(s, sym) for sym in batch]
            # 整批都有歷史資料才走增量（只抓近期）；只要有一檔是新的就整批抓完整區間，
            # 否則新標的只會拿到最近幾天，月 KD 會算不出來。
            start = (min(lasts) - timedelta(days=fetcher.OVERLAP_DAYS)
                     if all(lasts) else None)
            data, missing = fetcher.fetch_daily_batch(batch, start=start, period=period)
            with SessionLocal.begin() as s:  # 一批一個交易：單批失敗不影響其他批
                n_px = sum(repo.upsert_daily(s, sym, df) for sym, df in data.items())
                n_m = sum(repo.recompute_monthly(s, sym) for sym in data)
            failed += missing
            log(f"  成功 {len(data)} 檔、日線 {n_px} 筆、月線 {n_m} 根；無資料 {len(missing)} 檔")
        except Exception as e:  # 單批失敗不中斷其他批
            failed += batch
            log(f"  此批失敗：{e}")
        progress(i, len(batches))
    if failed:
        log(f"\n無資料或失敗共 {len(failed)} 檔：{', '.join(failed)}")
    return failed


def recompute_all_monthly(log: Log = print, progress: Progress = _noop_progress) -> int:
    """對全庫（核心＋S&P 500）從日線重算月 KD，不連網。回傳處理檔數。

    用途：調整 KD 參數、修正歷史日線後，不必重新下載就能讓月線與日線重新一致。
    每檔各自一個交易：單檔出錯不會讓已完成的檔案回滾，也讓寫入鎖的持有時間很短，
    網頁同時在讀取時不會被擋住。
    """
    with SessionLocal() as s:
        symbols = repo.active_symbols(s, category=None)
    done = 0
    for i, sym in enumerate(symbols, 1):
        try:
            with SessionLocal.begin() as s:
                repo.recompute_monthly(s, sym)
            done += 1
        except Exception as e:
            log(f"  {sym} 重算失敗：{e}")
        progress(i, len(symbols))
    log(f"完成：重算 {done}/{len(symbols)} 檔")
    return done
