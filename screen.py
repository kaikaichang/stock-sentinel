"""篩選 CLI：月 KD 黃金交叉（K<20、D<20）、警示價檢查，以及基本面過濾。

用法：
    python screen.py                                   # 與以往相同
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


def main() -> None:
    p = argparse.ArgumentParser(description="stock-sentinel 篩選工具")
    p.add_argument("--max-pe", type=float, help="PE 小於此值")
    p.add_argument("--max-pb", type=float, help="PB 小於此值")
    p.add_argument("--min-roe", type=float, help="ROE 大於此值（百分比，例如 15）")
    p.add_argument("--min-yield", type=float, help="殖利率大於此值（百分比，例如 4）")
    args = p.parse_args()
    # 使用者輸入的是百分比，資料庫存小數（0.15 = 15%），在這裡統一換算，避免比較差 100 倍
    roe = None if args.min_roe is None else args.min_roe / 100
    dy = None if args.min_yield is None else args.min_yield / 100

    init_db()
    with SessionLocal() as s:
        cands = repo.golden_cross_candidates(s)
        alerts = repo.price_alerts(s)
        fmap = repo.fundamentals_map(s)
        passed = repo.filter_by_fundamentals(
            fmap, max_pe=args.max_pe, max_pb=args.max_pb, min_roe=roe, min_yield=dy)
        active = set(repo.active_symbols(s))

    filtering = passed is not None
    if filtering:
        passed &= active  # 已停用的標的不列入
        cands = [(m, n) for m, n in cands if m.symbol in passed]

    title = "（並符合基本面條件）" if filtering else ""
    print(f"=== 月 KD 低檔黃金交叉（K<20、D<20、prev_K<=prev_D 且 K>D）{title}===")
    if not cands:
        print("（無符合標的）")
    for m, name in cands:
        flag = "（當月未收盤）" if m.is_partial else ""
        extra = f"  {_fund_text(fmap.get(m.symbol))}" if filtering else ""
        print(f"{m.symbol:<9}{name or '':<14}{m.year_month}  "
              f"K {m.prev_k:.1f}→{m.k_val:.1f}  D {m.prev_d:.1f}→{m.d_val:.1f}  "
              f"收盤 {m.close:.2f} {flag}{extra}")

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
