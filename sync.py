"""同步 CLI：新增自選股、增量抓日線與新聞、重算月 KD。

用法：
    python sync.py --seed                     # 寫入預設 5 檔測試股並同步
    python sync.py --add AAPL --low 150 --high 250 --name Apple
    python sync.py --symbol NVDA              # 只同步單檔
    python sync.py                            # 同步所有啟用中的「核心自選」股（不含 S&P 500 池）
    python sync.py --import-sp500             # 匯入 S&P 500 成分股名單（category='sp500'）
    python sync.py --batch-sp500              # 打包批次同步 S&P 500 池的日線與月 KD（不抓新聞）
"""
import argparse
import time
from datetime import timedelta

from sentinel import fetcher, repository as repo
from sentinel.db import SessionLocal, init_db

BATCH_SIZE = 50    # 每批打包幾檔給 yf.download
BATCH_SLEEP = 2    # 批與批之間休息秒數，避免被 Yahoo 限流

SEED = [("NVDA", "NVIDIA"), ("GOOGL", "Alphabet"), ("GRAB", "Grab Holdings"),
        ("2881.TW", "富邦金"), ("2330.TW", "台積電")]


def sync_one(symbol: str) -> None:
    with SessionLocal.begin() as s:
        last = repo.latest_trade_date(s, symbol)
    df = fetcher.fetch_daily(symbol, last)
    news = fetcher.fetch_news(symbol)
    # 基本面只是輔助資料：fetch_fundamentals 內部已容錯，失敗時各欄為 None，不會中斷日線同步
    fund = fetcher.fetch_fundamentals(symbol)
    with SessionLocal.begin() as s:
        n_px = repo.upsert_daily(s, symbol, df)
        n_news = repo.upsert_news(s, symbol, news)
        n_m = repo.recompute_monthly(s, symbol)
        repo.upsert_fundamentals(s, symbol, fund)
    ok = "已更新" if any(v is not None for v in fund.values()) else "無資料"
    print(f"  日線 {n_px} 筆、新聞 {n_news} 則、月線 {n_m} 根、基本面 {ok}")


def import_sp500() -> None:
    items = fetcher.fetch_sp500_list()
    with SessionLocal.begin() as s:
        added, skipped = repo.import_universe(s, items, category="sp500")
    print(f"S&P 500 名單 {len(items)} 檔：新增 {added} 檔，已存在略過 {skipped} 檔（既有核心自選不受影響）")


def batch_sp500(period: str = "2y", max_batches: int | None = None) -> None:
    """分批抓 S&P 500 池的日線並重算月 KD。

    刻意只做價量與月 KD：新聞是逐檔請求，500 檔一次打下去最容易被限流，
    而黃金交叉篩選只需要價量。基本面同理，需要時再對個別標的用 --symbol 同步。
    """
    with SessionLocal() as s:
        symbols = repo.active_symbols(s, category="sp500")
    if not symbols:
        print("S&P 500 池是空的，請先執行 --import-sp500")
        return
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    if max_batches:
        batches = batches[:max_batches]
    failed: list[str] = []
    for i, batch in enumerate(batches, 1):
        if i > 1:
            time.sleep(BATCH_SLEEP)
        print(f"[批次 {i}/{len(batches)}] {len(batch)} 檔 {batch[0]}…{batch[-1]}")
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
            print(f"  成功 {len(data)} 檔、日線 {n_px} 筆、月線 {n_m} 根；無資料 {len(missing)} 檔")
        except Exception as e:  # 單批失敗不中斷其他批
            failed += batch
            print(f"  此批失敗：{e}")
    if failed:
        print(f"\n無資料或失敗共 {len(failed)} 檔：{', '.join(failed)}")


def main() -> None:
    p = argparse.ArgumentParser(description="stock-sentinel 同步工具")
    p.add_argument("--seed", action="store_true", help="加入預設測試自選股")
    p.add_argument("--add", metavar="SYMBOL", help="新增（或更新）一檔自選股")
    p.add_argument("--name"); p.add_argument("--market", choices=["US", "TW"])
    p.add_argument("--low", type=float, help="低價警示"); p.add_argument("--high", type=float, help="高價警示")
    p.add_argument("--notes")
    p.add_argument("--symbol", help="只同步指定的一檔")
    p.add_argument("--import-sp500", action="store_true", help="匯入 S&P 500 成分股名單")
    p.add_argument("--batch-sp500", action="store_true", help="批次同步 S&P 500 池日線與月 KD")
    p.add_argument("--period", default="2y", help="--batch-sp500 首次抓取的區間（預設 2y）")
    p.add_argument("--max-batches", type=int, help="--batch-sp500 最多跑幾批（試跑用）")
    args = p.parse_args()

    init_db()
    if args.import_sp500 or args.batch_sp500:
        if args.import_sp500:
            import_sp500()
        if args.batch_sp500:
            batch_sp500(args.period, args.max_batches)
        return

    with SessionLocal.begin() as s:
        if args.seed:
            for sym, name in SEED:
                repo.add_watch(s, sym, name=name)
        if args.add:
            repo.add_watch(s, args.add, args.name, args.market, args.low, args.high, args.notes)
        symbols = [args.symbol.upper()] if args.symbol else repo.active_symbols(s)

    for i, sym in enumerate(symbols):
        if i:
            time.sleep(1)  # 每檔間隔 1 秒，避免被 Yahoo 限流
        print(f"[{sym}]")
        try:
            sync_one(sym)
        except Exception as e:  # 單檔失敗不中斷其他檔
            print(f"  失敗：{e}")


if __name__ == "__main__":
    main()
