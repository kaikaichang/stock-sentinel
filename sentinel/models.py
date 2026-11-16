"""資料表定義（SQLAlchemy 2.0 風格）。"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Watchlist(Base):
    """自選關注清單。"""
    __tablename__ = "watchlist"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100))
    market: Mapped[str] = mapped_column(String(2))  # 'US' / 'TW'
    target_low: Mapped[float | None] = mapped_column(Float)   # 低價警示
    target_high: Mapped[float | None] = mapped_column(Float)  # 高價警示
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 'core'＝使用者手動關注的核心自選；'sp500'＝批次匯入的成分股掃描池。
    # 分開存放是為了讓預設同步、警示價只作用在核心自選，不會被 500 檔拖慢或誤觸限流。
    category: Mapped[str] = mapped_column(String(10), default="core",
                                          server_default="core", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DailyPrice(Base):
    """日線價量。複合主鍵 (symbol, trade_date) 讓 Upsert 可以防止重複寫入。"""
    __tablename__ = "daily_prices"

    symbol: Mapped[str] = mapped_column(ForeignKey("watchlist.symbol"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(Integer)


class MonthlyIndicator(Base):
    """月 K 棒與月 KD。year_month 格式 'YYYY-MM'。"""
    __tablename__ = "monthly_indicators"

    symbol: Mapped[str] = mapped_column(ForeignKey("watchlist.symbol"), primary_key=True)
    year_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    k_val: Mapped[float | None] = mapped_column(Float)
    d_val: Mapped[float | None] = mapped_column(Float)
    prev_k: Mapped[float | None] = mapped_column(Float)
    prev_d: Mapped[float | None] = mapped_column(Float)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)  # 當月尚未收盤

    # 篩選條件固定是「某月份 + K/D 範圍」，複合索引讓這類查詢不必全表掃描
    __table_args__ = (Index("ix_monthly_screen", "year_month", "k_val", "d_val"),)


class StockNews(Base):
    """新聞情報。ai_analysis_json 預留給下一階段 Claude 結構化分析。"""
    __tablename__ = "stock_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("watchlist.symbol"), index=True)
    title: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(100))
    link: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ai_analysis_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("symbol", "link", name="uq_news_symbol_link"),)


class Fundamentals(Base):
    """價值投資基本面「最新快照」。

    為什麼 symbol 單獨當主鍵：基本面是當下的一組數字而非時間序列，
    每檔只留一列，重抓時直接覆蓋（Upsert），不會累積過期資料。
    所有數值欄位皆可為 NULL：Yahoo 對虧損股（無 PE）、不配息股（無殖利率）、ETF 都會缺欄位。
    """
    __tablename__ = "fundamentals"

    symbol: Mapped[str] = mapped_column(ForeignKey("watchlist.symbol"), primary_key=True)
    pe: Mapped[float | None] = mapped_column(Float)              # trailingPE
    forward_pe: Mapped[float | None] = mapped_column(Float)      # forwardPE
    pb: Mapped[float | None] = mapped_column(Float)              # priceToBook
    roe: Mapped[float | None] = mapped_column(Float)             # 小數：0.15 = 15%
    dividend_yield: Mapped[float | None] = mapped_column(Float)  # 小數：0.04 = 4%
    free_cashflow: Mapped[float | None] = mapped_column(Float)   # 原幣別絕對值
    debt_to_equity: Mapped[float | None] = mapped_column(Float)  # Yahoo 原值為百分比：150 = 1.5 倍
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
