"""yfinance 抓取層：日線與新聞。"""
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
