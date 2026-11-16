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


# ---- S&P 500 成分股池 ----

SP500_CSV_URL = ("https://raw.githubusercontent.com/datasets/"
                 "s-and-p-500-companies/main/data/constituents.csv")


def fetch_sp500_list() -> list[tuple[str, str]]:
    """抓 S&P 500 成分股 [(代號, 名稱)]。

    代號的 "." 要換成 "-"（BRK.B → BRK-B）：Yahoo 用連字號表示股別，
    不轉的話 yfinance 會查無資料而被當成下市股。
    選 GitHub 上的 CSV 而非 Wikipedia：純 CSV 不需 HTML 解析套件與偽裝 User-Agent，較穩。
    """
    df = pd.read_csv(SP500_CSV_URL)
    df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    return list(dict.fromkeys(zip(df["Symbol"], df["Security"].astype(str))))


def fetch_daily_batch(symbols: list[str], start: date | None = None,
                      period: str = "2y") -> tuple[dict[str, pd.DataFrame], list[str]]:
    """用 yf.download 一次打包多檔日線，回傳 ({代號: 日線表}, 抓不到的代號)。

    為什麼打包：逐檔呼叫每檔都是一次獨立 HTTP 往返，500 檔太慢；
    download 內部多執行緒並行，50 檔一批的總時間遠小於逐檔相加。
    欄位與 fetch_daily 完全一致，讓同一套 upsert_daily / recompute_monthly 可直接使用。
    只給 start 時（增量重跑）改用起始日，period 才是首次的 2 年。
    """
    kwargs = {"start": start.isoformat()} if start else {"period": period}
    # auto_adjust=False：與 fetch_daily 相同，保留原始價（KD 用）與 Adj Close
    raw = yf.download(symbols, group_by="ticker", auto_adjust=False,
                      threads=True, progress=False, **kwargs)
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out, list(symbols)
    for sym in symbols:
        try:
            # 多檔時第一層是代號；只有單檔且欄位不是 MultiIndex 時直接用整張表
            df = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Adj Close": "adj_close",
                                "Volume": "volume"})
        # 下市或代號錯誤的標的整欄都是 NaN；某檔在其他檔有交易日卻沒資料時也會出現全 NaN 列
        df = df[["open", "high", "low", "close", "adj_close", "volume"]].dropna(subset=["close"])
        if df.empty:
            continue
        df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
        out[sym] = df
    return out, [s for s in symbols if s not in out]
