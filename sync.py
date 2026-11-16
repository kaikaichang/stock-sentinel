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

from sentinel import repository as repo, syncer
from sentinel.db import SessionLocal, init_db

SEED = [("NVDA", "NVIDIA"), ("GOOGL", "Alphabet"), ("GRAB", "Grab Holdings"),
        ("2881.TW", "富邦金"), ("2330.TW", "台積電")]
# 同步邏輯在 sentinel/syncer.py：網頁按鈕與本 CLI 共用同一份，行為才不會分歧


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
            syncer.import_sp500()
        if args.batch_sp500:
            syncer.sync_sp500_batch(period=args.period, max_batches=args.max_batches)
        return

    with SessionLocal.begin() as s:
        if args.seed:
            for sym, name in SEED:
                repo.add_watch(s, sym, name=name)
        if args.add:
            repo.add_watch(s, args.add, args.name, args.market, args.low, args.high, args.notes)
        symbols = [args.symbol.upper()] if args.symbol else repo.active_symbols(s)

    syncer.sync_symbols(symbols)


if __name__ == "__main__":
    main()
