"""篩選 CLI：月 KD 黃金交叉（K<20、D<20）與警示價檢查。"""
from sentinel import repository as repo
from sentinel.db import SessionLocal, init_db


def main() -> None:
    init_db()
    with SessionLocal() as s:
        cands = repo.golden_cross_candidates(s)
        alerts = repo.price_alerts(s)

    print("=== 月 KD 低檔黃金交叉（K<20、D<20、prev_K<=prev_D 且 K>D）===")
    if not cands:
        print("（無符合標的）")
    for m, name in cands:
        flag = "（當月未收盤）" if m.is_partial else ""
        print(f"{m.symbol:<9}{name or '':<14}{m.year_month}  "
              f"K {m.prev_k:.1f}→{m.k_val:.1f}  D {m.prev_d:.1f}→{m.d_val:.1f}  "
              f"收盤 {m.close:.2f} {flag}")

    print("\n=== 警示價觸發 ===")
    if not alerts:
        print("（無觸發）")
    for w, d, close, kind in alerts:
        label = f"跌破低價 {w.target_low}" if kind == "LOW" else f"突破高價 {w.target_high}"
        print(f"{w.symbol:<9}{w.name or '':<14}{d}  收盤 {close:.2f}  {label}")


if __name__ == "__main__":
    main()
