"""장중 5분봉/quote fetch + DB 기준가 결합."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.config import Settings
from src.ingest.finnhub import Finnhub
from src.intraday.watchlist import WatchCandidate
from src.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntradayBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class TickerSnapshot:
    ticker: str
    trade_date: date
    bars: list[IntradayBar] = field(default_factory=list)
    current_price: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    prev_high: float | None = None
    pss_total: float | None = None
    tier: int | None = None
    triggered_patterns: str = ""
    source: str = "none"
    metadata: dict[str, Any] = field(default_factory=dict)


def fetch_snapshots(
    tickers: list[str],
    db: Database,
    trade_date: date,
    settings: Settings,
    candidates: dict[str, WatchCandidate] | None = None,
) -> dict[str, TickerSnapshot]:
    """yfinance 5분봉 우선, 실패/누락 ticker는 Finnhub quote로 보강."""
    candidates = candidates or {}
    snapshots = {
        ticker: _base_snapshot(ticker, db, trade_date, candidates.get(ticker))
        for ticker in tickers
    }

    missing = set(tickers)
    if settings.intraday_use_yfinance:
        yf_bars = _fetch_yfinance_bars(
            tickers,
            prepost=settings.intraday_yfinance_prepost,
        )
        for ticker, bars in yf_bars.items():
            if not bars:
                continue
            snap = snapshots[ticker]
            snap.bars = bars
            snap.current_price = bars[-1].close
            snap.day_open = bars[0].open
            snap.day_high = max(b.high for b in bars)
            snap.day_low = min(b.low for b in bars)
            snap.source = "yfinance"
            snap.metadata["bar_count"] = len(bars)
            missing.discard(ticker)

    if missing and settings.intraday_use_finnhub_fallback and settings.finnhub_api_key:
        _fill_finnhub_quotes(sorted(missing), snapshots, settings)

    return snapshots


def _base_snapshot(
    ticker: str,
    db: Database,
    trade_date: date,
    candidate: WatchCandidate | None,
) -> TickerSnapshot:
    prev = db.latest_bar(ticker, trade_date)
    pss_total = candidate.pss_total if candidate else None
    tier = candidate.tier if candidate else None
    patterns = candidate.triggered_patterns if candidate else ""
    if candidate is None:
        pss = db.get_pss(trade_date, ticker)
        if pss:
            pss_total = float(pss["pss_total"] or 0)
            tier = pss["tier"]
            patterns = pss["triggered_patterns"] or ""
    snap = TickerSnapshot(
        ticker=ticker,
        trade_date=trade_date,
        prev_close=float(prev["close"]) if prev and prev["close"] is not None else None,
        prev_high=float(prev["high"]) if prev and prev["high"] is not None else None,
        pss_total=pss_total,
        tier=tier,
        triggered_patterns=patterns,
    )
    avg30 = db.avg_volume(ticker, trade_date, 30)
    if avg30:
        snap.metadata["avg_daily_volume_30d"] = avg30
    return snap


def _fetch_yfinance_bars(
    tickers: list[str],
    *,
    prepost: bool = False,
) -> dict[str, list[IntradayBar]]:
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; skipping intraday OHLCV fetch")
        return {}

    try:
        df = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval="5m",
            group_by="ticker",
            auto_adjust=False,
            prepost=prepost,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        logger.warning("yfinance intraday batch failed: %s", exc)
        return {}

    if df is None or df.empty:
        return {}

    out: dict[str, list[IntradayBar]] = {}
    for ticker in tickers:
        tdf = _extract_ticker_frame(df, ticker, len(tickers) == 1)
        bars = _frame_to_bars(tdf)
        if bars:
            out[ticker] = bars
    return out


def _extract_ticker_frame(df: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame:
    if single and not isinstance(df.columns, pd.MultiIndex):
        return df
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.get_level_values(0):
            return df[ticker]
        if ticker in df.columns.get_level_values(1):
            return df.xs(ticker, axis=1, level=1)
    return pd.DataFrame()


def _frame_to_bars(df: pd.DataFrame) -> list[IntradayBar]:
    if df.empty:
        return []
    cols = {str(c).lower(): c for c in df.columns}
    required = ("open", "high", "low", "close", "volume")
    if not all(k in cols for k in required):
        return []
    bars: list[IntradayBar] = []
    for idx, row in df.dropna(subset=[cols["close"]]).iterrows():
        try:
            volume_raw = row[cols["volume"]]
            volume = 0 if pd.isna(volume_raw) else int(volume_raw)
            bars.append(
                IntradayBar(
                    ts=idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx,
                    open=float(row[cols["open"]]),
                    high=float(row[cols["high"]]),
                    low=float(row[cols["low"]]),
                    close=float(row[cols["close"]]),
                    volume=volume,
                )
            )
        except (TypeError, ValueError):
            continue
    return bars


def _fill_finnhub_quotes(
    tickers: list[str],
    snapshots: dict[str, TickerSnapshot],
    settings: Settings,
) -> None:
    client = Finnhub(settings.finnhub_api_key)
    try:
        for ticker in tickers:
            try:
                q = client.quote(ticker)
            except Exception as exc:
                logger.debug("Finnhub quote failed %s: %s", ticker, exc)
                continue
            price = q.get("c")
            if not price:
                continue
            snap = snapshots[ticker]
            snap.current_price = float(price)
            snap.day_open = _num(q.get("o"))
            snap.day_high = _num(q.get("h"))
            snap.day_low = _num(q.get("l"))
            snap.prev_close = snap.prev_close or _num(q.get("pc"))
            snap.source = "finnhub"
            snap.metadata["quote"] = {
                "o": q.get("o"),
                "h": q.get("h"),
                "l": q.get("l"),
                "pc": q.get("pc"),
                "t": q.get("t"),
            }
    finally:
        client.close()


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
