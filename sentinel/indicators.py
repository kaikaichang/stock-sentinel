"""日線 → 月 K → 月 KD。純函式，不碰資料庫，方便單獨驗證。"""
from datetime import date

import pandas as pd

KD_PERIOD = 9      # RSV 回看 9 個月
KD_SMOOTH = 3      # 平滑係數：K = (1-1/3)*K前 + 1/3*RSV


def aggregate_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """把日線動態聚合成月 K。

    為什麼每次都從日線重算，而不是存好月 K 再增量更新？
    因為「當月截至今日」的月 K 每天都在變（high/low/close 持續更新），
    若只存結果，當月那一根會過期；從日線聚合則最後一根永遠代表最新狀態。
    """
    # 'ME' = 月底；月內第一筆開盤、最高、最低、最後一筆收盤
    monthly = daily.resample("ME").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna(subset=["close"])
    monthly.index = monthly.index.strftime("%Y-%m")
    monthly.index.name = "year_month"
    return monthly


def compute_monthly_kd(daily: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """回傳月 K 與 k_val/d_val/prev_k/prev_d/is_partial。

    公式：
        RSV = (收盤 - 近9月最低) / (近9月最高 - 近9月最低) * 100
        K   = 2/3 * K前 + 1/3 * RSV
        D   = 2/3 * D前 + 1/3 * K
    第一期 K、D 設為 50（業界慣例的中性起點，否則遞推沒有前值可用）。
    """
    monthly = aggregate_monthly(daily)
    if monthly.empty:
        return monthly.assign(k_val=[], d_val=[], prev_k=[], prev_d=[], is_partial=[])

    # min_periods=1：上市不滿 9 個月的標的，用已有的月數計算，而不是整段變 NaN
    hh = monthly["high"].rolling(KD_PERIOD, min_periods=1).max()
    ll = monthly["low"].rolling(KD_PERIOD, min_periods=1).min()
    span = hh - ll
    # 高低點相同（如單月只有一筆且無波動）時分母為 0，RSV 取中性值 50 避免除以零
    rsv = ((monthly["close"] - ll) / span * 100).where(span > 0, 50.0)

    # KD 是遞推（今天的值依賴昨天），無法向量化，因此用迴圈
    alpha = 1 / KD_SMOOTH
    ks, ds = [], []
    k_prev = d_prev = 50.0
    for i, r in enumerate(rsv):
        if i == 0:
            k, d = 50.0, 50.0  # 第一期初始值
        else:
            k = (1 - alpha) * k_prev + alpha * r
            d = (1 - alpha) * d_prev + alpha * k
        ks.append(k)
        ds.append(d)
        k_prev, d_prev = k, d

    monthly["k_val"] = ks
    monthly["d_val"] = ds
    monthly["prev_k"] = monthly["k_val"].shift(1)
    monthly["prev_d"] = monthly["d_val"].shift(1)

    # 最後一根若尚未到月底（以今天判斷），標記為未收盤
    today = today or date.today()
    monthly["is_partial"] = False
    if monthly.index[-1] >= today.strftime("%Y-%m"):
        monthly.iloc[-1, monthly.columns.get_loc("is_partial")] = True
    return monthly
