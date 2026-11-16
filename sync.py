"""同步 CLI：新增自選股、增量抓日線與新聞、重算月 KD。

用法：
    python sync.py --seed                     # 寫入預設 5 檔測試股並同步
    python sync.py --add AAPL --low 150 --high 250 --name Apple
    python sync.py --symbol NVDA              # 只同步單檔
    python sync.py                            # 同步所有啟用中的自選股
"""
import argparse
import time

from sentinel import fetcher, repository as repo
from sentinel.db import SessionLocal, init_db

SEED = [("NVDA", "NVIDIA"), ("GOOGL", "Alphabet"), ("GRAB", "Grab Holdings"),
        ("2881.TW", "富邦金"), ("2330.TW", "台積電")]


def sync_one(symbol: str) -> None:
    with SessionLocal.begin() as s:
        last = repo.latest_trade_date(s, symbol)
    df = fetcher.fetch_daily(symbol, last)
    news = fetcher.fetch_news(symbol)
    with SessionLocal.begin() as s:
        n_px = repo.upsert_daily(s, symbol, df)
        n_news = repo.upsert_news(s, symbol, news)
        n_m = repo.recompute_monthly(s, symbol)
    print(f"  日線 {n_px} 筆、新聞 {n_news} 則、月線 {n_m} 根")


def main() -> None:
    p = argparse.ArgumentParser(description="stock-sentinel 同步工具")
    p.add_argument("--seed", action="store_true", help="加入預設測試自選股")
    p.add_argument("--add", metavar="SYMBOL", help="新增（或更新）一檔自選股")
    p.add_argument("--name"); p.add_argument("--market", choices=["US", "TW"])
    p.add_argument("--low", type=float, help="低價警示"); p.add_argument("--high", type=float, help="高價警示")
    p.add_argument("--notes")
    p.add_argument("--symbol", help="只同步指定的一檔")
    args = p.parse_args()

    init_db()
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
