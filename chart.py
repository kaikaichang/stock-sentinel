"""月 K 線與月 KD 互動圖表檢視器。

用法：
    python chart.py --symbol GRAB
    python chart.py --symbol 2330.TW --no-open   # 只產檔，不開瀏覽器
"""
import argparse
import sys
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import select

from sentinel.db import SessionLocal
from sentinel.models import MonthlyIndicator, Watchlist

CHART_DIR = Path(__file__).resolve().parent / "charts"

# 台灣看盤習慣：紅漲綠跌（與歐美相反）
UP_COLOR = "#e53935"
DOWN_COLOR = "#00a152"
# K、D 線刻意避開紅/綠，避免與上圖漲跌色混淆
K_COLOR = "#1e88e5"
D_COLOR = "#fb8c00"


def load_data(symbol: str) -> tuple[pd.DataFrame, str | None]:
    """從資料庫讀出該標的全部月線與月 KD，另回傳公司名稱。"""
    with SessionLocal() as s:
        rows = s.execute(
            select(MonthlyIndicator.year_month, MonthlyIndicator.open,
                   MonthlyIndicator.high, MonthlyIndicator.low, MonthlyIndicator.close,
                   MonthlyIndicator.k_val, MonthlyIndicator.d_val, MonthlyIndicator.is_partial)
            .where(MonthlyIndicator.symbol == symbol)
            .order_by(MonthlyIndicator.year_month)).all()
        name = s.scalar(select(Watchlist.name).where(Watchlist.symbol == symbol))
    df = pd.DataFrame(rows, columns=["year_month", "open", "high", "low", "close",
                                     "k_val", "d_val", "is_partial"])
    return df, name


def build_title(symbol: str, name: str | None, latest: pd.Series) -> str:
    head = f"{symbol} {name}" if name else symbol
    title = f"{head} ｜ 月K {latest.k_val:.1f} ｜ 月D {latest.d_val:.1f}"
    if latest.is_partial:
        title += f"（{latest.year_month} 當月未收盤，動態月線）"
    return title


def build_figure(df: pd.DataFrame, symbol: str, name: str | None) -> go.Figure:
    # 資料庫存 'YYYY-MM' 字串；轉成當月 1 日的日期，才能使用真正的時間軸（縮放、刻度才會正確）
    x = pd.to_datetime(df["year_month"] + "-01")

    # shared_xaxes=True：上下圖共用同一條時間軸，縮放或拖曳其中一張，另一張同步移動，
    # 這樣才能把某根月 K 與「同一個月」的 KD 位置對照著看
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3], vertical_spacing=0.04)

    fig.add_trace(go.Candlestick(
        x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="月K",
        increasing=dict(line=dict(color=UP_COLOR), fillcolor=UP_COLOR),
        decreasing=dict(line=dict(color=DOWN_COLOR), fillcolor=DOWN_COLOR),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=x, y=df["k_val"], name="K", mode="lines",
                             line=dict(color=K_COLOR, width=1.8)), row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["d_val"], name="D", mode="lines",
                             line=dict(color=D_COLOR, width=1.8)), row=2, col=1)

    # 20 = 超賣、80 = 超買 參考線，只畫在下圖
    for y in (20, 80):
        fig.add_hline(y=y, line_dash="dash", line_color="gray", line_width=1,
                      row=2, col=1)

    fig.update_layout(
        title=build_title(symbol, name, df.iloc[-1]),
        # Candlestick 預設會附帶 range slider，會擠壓版面，這裡關掉
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        showlegend=False,
        height=800,
        margin=dict(t=80, b=40),
    )
    fig.update_yaxes(title_text="價格", row=1, col=1)
    fig.update_yaxes(title_text="KD", range=[0, 100], row=2, col=1)  # KD 值域固定 0~100
    return fig


def main() -> None:
    p = argparse.ArgumentParser(description="月 K 線與月 KD 互動圖表")
    p.add_argument("--symbol", required=True, help="股票代號，例如 GRAB、2330.TW")
    p.add_argument("--no-open", action="store_true", help="只產生檔案，不自動開啟瀏覽器")
    args = p.parse_args()

    symbol = args.symbol.upper()
    df, name = load_data(symbol)
    if df.empty:
        print(f"找不到 {symbol} 的月線資料。請先執行：python sync.py --add {symbol}")
        sys.exit(1)

    fig = build_figure(df, symbol, name)
    CHART_DIR.mkdir(exist_ok=True)
    out = CHART_DIR / f"{symbol}_monthly_kd.html"
    # include_plotlyjs="cdn"：檔案只有數十 KB，開啟時才從 CDN 載入 plotly.js（需連網）
    fig.write_html(out, include_plotlyjs="cdn")
    print(f"已產生：{out}")

    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
