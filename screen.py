"""篩選 CLI：月 KD 黃金交叉（K<20、D<20）、警示價檢查，以及基本面過濾。

用法：
    python screen.py                                   # 與以往相同
    python screen.py --pool sp500                      # 掃 S&P 500 池的月 KD 黃金交叉
    python screen.py --pool sp500 --signal hook --lookback 3 --threshold 30   # 近 3 根月線內曾低檔勾頭
    python screen.py --max-pe 15 --min-roe 15 --min-yield 4
        # 有給基本面條件時：黃金交叉候選會再 AND 過濾，並另列「基本面篩選」區塊
"""
import argparse

from sentinel import repository as repo
from sentinel.db import SessionLocal, init_db


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _fund_text(f) -> str:
    if f is None:
        return "PE — PB — ROE — 殖利率 —"
    return f"PE {_num(f.pe)} PB {_num(f.pb)} ROE {_pct(f.roe)} 殖利率 {_pct(f.dividend_yield)}"


SIGNAL_ARGS = {"golden": "golden_cross", "hook": "hook_up", "death": "death_cross"}


def _signal_title(signal: str, threshold: float, lookback: int) -> str:
    desc = {"golden": f"低檔黃金交叉（K、D < {threshold:g}，前期 K<=D 且本期 K>D）",
            "hook": f"低檔勾頭向上（尚未交叉 K<=D，K < {threshold:g} 且 K 高於前期）",
            "death": "高檔死亡交叉（K、D > 80，前期 K>=D 且本期 K<D）"}[signal]
    scope = "最新一期" if lookback == 1 else f"最近 {lookback} 根月線內"
    return f"月 KD {desc}，{scope}"


def main() -> None:
    p = argparse.ArgumentParser(description="stock-sentinel 篩選工具")
    p.add_argument("--pool", choices=["core", "sp500", "all"], default="core",
                   help="掃描範圍：core 核心自選（預設）、sp500 成分股池、all 全部")
    p.add_argument("--signal", choices=list(SIGNAL_ARGS), default="golden",
                   help="訊號：golden 低檔黃金交叉（預設）、hook 低檔勾頭向上、death 高檔死亡交叉")
    p.add_argument("--lookback", type=int, default=1,
                   help="回溯最近幾根月線內曾出現訊號（預設 1＝只看最新一期，與舊版一致；網頁預設為 3）")
    p.add_argument("--threshold", type=float, default=20, help="低檔門檻上限（預設 20；死亡交叉固定 80）")
    p.add_argument("--any-trend", action="store_true",
                   help="黃金交叉不要求最新一期仍維持 K>D（預設要求）")
    p.add_argument("--max-pe", type=float, help="PE 小於此值")
    p.add_argument("--max-pb", type=float, help="PB 小於此值")
    p.add_argument("--min-roe", type=float, help="ROE 大於此值（百分比，例如 15）")
    p.add_argument("--min-yield", type=float, help="殖利率大於此值（百分比，例如 4）")
    args = p.parse_args()
    # 使用者輸入的是百分比，資料庫存小數（0.15 = 15%），在這裡統一換算，避免比較差 100 倍
    roe = None if args.min_roe is None else args.min_roe / 100
    dy = None if args.min_yield is None else args.min_yield / 100

    # 預設 core：保持與加入 S&P 500 池之前相同的範圍，不會一次噴出數十檔
    category = None if args.pool == "all" else args.pool
    init_db()
    with SessionLocal() as s:
        cands = repo.screen_monthly_kd(
            s, SIGNAL_ARGS[args.signal], lookback_months=args.lookback,
            kd_threshold=args.threshold, still_bullish=not args.any_trend, category=category)
        alerts = repo.price_alerts(s)
        fmap = repo.fundamentals_map(s)
        passed = repo.filter_by_fundamentals(
            fmap, max_pe=args.max_pe, max_pb=args.max_pb, min_roe=roe, min_yield=dy)
        active = set(repo.active_symbols(s, category))

    filtering = passed is not None
    if filtering:
        passed &= active  # 已停用的標的不列入
        cands = cands[cands["symbol"].isin(passed)]

    title = "（並符合基本面條件）" if filtering else ""
    pool_name = {"core": "", "sp500": "【S&P 500 池】", "all": "【全部】"}[args.pool]
    print(f"=== {pool_name}{_signal_title(args.signal, args.threshold, args.lookback)}{title}===")
    if filtering and args.pool != "core":
        print("（提醒：S&P 500 池為批次同步，沒有基本面資料，基本面條件啟用時會被排除）")
    if cands.empty:
        print("（無符合標的）")
    else:
        print(f"（共 {len(cands)} 檔）")
    for m in cands.itertuples():
        # 訊號月若就是最新那根且尚未收盤，K/D 還會變動，特別標示
        flag = "（當月未收盤）" if m.is_partial and m.signal_month == m.latest_month else ""
        extra = f"  {_fund_text(fmap.get(m.symbol))}" if filtering else ""
        print(f"{m.symbol:<9}{(m.name or '')[:18]:<19}訊號 {m.signal_month} K {m.sig_k:.1f} D {m.sig_d:.1f}  "
              f"最新 K {m.k_val:.1f} D {m.d_val:.1f}  收盤 {m.close:.2f} {flag}{extra}")

    if args.pool != "sp500":  # S&P 500 池沒有設警示價，不需列出
        print("\n=== 警示價觸發 ===")
        if not alerts:
            print("（無觸發）")
        for w, d, close, kind in alerts:
            label = f"跌破低價 {w.target_low}" if kind == "LOW" else f"突破高價 {w.target_high}"
            print(f"{w.symbol:<9}{w.name or '':<14}{d}  收盤 {close:.2f}  {label}")

    if filtering:
        # 不限黃金交叉，單純列出基本面達標的自選股
        print("\n=== 基本面篩選（不限技術面）===")
        if not passed:
            print("（無符合標的）")
        for sym in sorted(passed):
            print(f"{sym:<9}{_fund_text(fmap[sym])}")


if __name__ == "__main__":
    main()
