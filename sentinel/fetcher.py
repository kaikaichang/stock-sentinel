"""yfinance 抓取層：日線與新聞。"""
import math
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

# 增量同步時往回多抓的天數：覆蓋除權息調整與盤中未定案的收盤價
OVERLAP_DAYS = 5


def fetch_daily(symbol: str, last_date: date | None) -> pd.DataFrame:
    """首次抓全部歷史；之後只抓 last_date 回推 OVERLAP_DAYS 天之後的資料。"""
    kwargs = {"period": "max"} if last_date is None else {
        "start": (last_date - timedelta(days=OVERLAP_DAYS)).isoformat()}
    # auto_adjust=False：同時取得原始價與 Adj Close，KD 用原始價（見 indicators.py）
    df = yf.Ticker(symbol).history(auto_adjust=False, **kwargs)
    if df.empty:
        return df
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Adj Close": "adj_close",
                            "Volume": "volume"})
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    return df[["open", "high", "low", "close", "adj_close", "volume"]]


def fetch_news(symbol: str) -> list[dict]:
    """抓最新新聞；欄位格式因 yfinance 版本而異，抓不到就回傳空清單。"""
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception:
        return []
    items = []
    for n in raw:
        c = n.get("content", n)  # 新版巢狀在 content 內
        link = ((c.get("canonicalUrl") or {}).get("url")
                or (c.get("clickThroughUrl") or {}).get("url") or c.get("link"))
        title = c.get("title")
        if not link or not title:
            continue
        pub = c.get("pubDate")
        try:
            published = (datetime.fromisoformat(pub.replace("Z", "+00:00")).replace(tzinfo=None)
                         if pub else (datetime.fromtimestamp(c["providerPublishTime"])
                                      if c.get("providerPublishTime") else None))
        except (ValueError, TypeError):
            published = None
        items.append(dict(
            title=title, link=link, published_at=published, summary=c.get("summary"),
            publisher=(c.get("provider") or {}).get("displayName") or c.get("publisher")))
    return items


def fetch_name(symbol: str) -> str | None:
    """用 yfinance 的 shortName 自動補名稱；任何失敗都回傳 None，名稱留空即可，不影響主流程。"""
    try:
        info = yf.Ticker(symbol).info or {}
        return info.get("shortName") or info.get("longName")
    except Exception:
        return None


def _num(v) -> float | None:
    """只接受有限的數字；None、NaN、Yahoo 偶爾回的 'Infinity' 字串一律轉 None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def fetch_fundamentals(symbol: str) -> dict:
    """抓價值投資基本面；任何欄位缺漏都容錯為 None，整體失敗則回傳全 None。"""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}

    # 殖利率單位陷阱：Yahoo 的 dividendYield 單位不穩定（實測此版本為百分比：1.02 代表 1.02%，
    # 舊版則是小數），直接用會差 100 倍，讓「殖利率 > 4%」整個失真。
    # 因此自行計算：預估年股利 / 現價，單位固定為小數，數值也與 dividendYield 對得上。
    # 不優先用 trailingAnnualDividendYield：它是「過去 12 個月實際配息」，
    # 季配息或配息時點偏移的股票（如台積電）會被嚴重低估。它只當備援。
    rate = _num(info.get("dividendRate"))
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    dy = rate / price if rate is not None and price else None
    if dy is None:
        dy = _num(info.get("trailingAnnualDividendYield"))

    return dict(
        pe=_num(info.get("trailingPE")),
        forward_pe=_num(info.get("forwardPE")),
        pb=_num(info.get("priceToBook")),
        roe=_num(info.get("returnOnEquity")),
        dividend_yield=dy,
        free_cashflow=_num(info.get("freeCashflow")),
        debt_to_equity=_num(info.get("debtToEquity")),
    )
