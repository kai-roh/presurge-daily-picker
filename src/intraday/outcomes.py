"""장중 signal 이후 성과 평가."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from src.intraday.market_data import _fetch_yfinance_bars
from src.storage.db import Database

logger = logging.getLogger(__name__)


def evaluate_pending_signals(db: Database, trade_date: date | None = None) -> int:
    signals = db.unevaluated_signals(trade_date)
    if not signals:
        return 0

    tickers = sorted({r["ticker"] for r in signals})
    bars_by_ticker = _fetch_yfinance_bars(tickers)
    n = 0
    for row in signals:
        bars = bars_by_ticker.get(row["ticker"], [])
        if not bars:
            continue
        try:
            signal_ts = datetime.fromisoformat(row["signal_ts"])
        except ValueError:
            continue
        entry = float(row["price"])
        after = [b for b in bars if _bar_after_signal(b.ts, signal_ts)]
        if not after:
            continue
        outcome = _outcome(entry, signal_ts, after)
        db.upsert_signal_outcome(int(row["signal_id"]), outcome)
        n += 1
    return n


def _bar_after_signal(bar_ts: datetime, signal_ts: datetime) -> bool:
    if bar_ts.tzinfo is None or signal_ts.tzinfo is None:
        return bar_ts.replace(tzinfo=None) >= signal_ts.replace(tzinfo=None)
    return bar_ts >= signal_ts


def _outcome(entry: float, signal_ts: datetime, bars: list[Any]) -> dict[str, Any]:
    def window(minutes: int) -> list[Any]:
        until = signal_ts + timedelta(minutes=minutes)
        if signal_ts.tzinfo is None:
            until_cmp = until.replace(tzinfo=None)
            return [b for b in bars if b.ts.replace(tzinfo=None) <= until_cmp]
        return [b for b in bars if b.ts <= until]

    def metrics(sample: list[Any]) -> tuple[float | None, float | None]:
        if not sample:
            return None, None
        max_pct = (max(b.high for b in sample) - entry) / entry
        close_pct = (sample[-1].close - entry) / entry
        return max_pct, close_pct

    max10, close10 = metrics(window(10))
    max30, close30 = metrics(window(30))
    max60, close60 = metrics(window(60))
    max_eod, close_eod = metrics(bars)
    min_after = (min(b.low for b in bars) - entry) / entry
    return {
        "max_10m_pct": max10,
        "close_10m_pct": close10,
        "max_30m_pct": max30,
        "close_30m_pct": close30,
        "max_60m_pct": max60,
        "close_60m_pct": close60,
        "max_eod_pct": max_eod,
        "close_eod_pct": close_eod,
        "min_after_pct": min_after,
        "evaluated_at": datetime.utcnow().isoformat(),
    }

