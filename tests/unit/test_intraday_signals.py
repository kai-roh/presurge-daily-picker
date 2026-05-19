from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.intraday.market_data import IntradayBar, TickerSnapshot
from src.intraday.signals import IntradaySignalEngine, SignalContext

ET = ZoneInfo("America/New_York")


def _bar(i: int, open_: float, high: float, low: float, close: float, volume: int) -> IntradayBar:
    return IntradayBar(
        ts=datetime(2026, 5, 18, 9, 30, tzinfo=ET) + timedelta(minutes=5 * i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_orb_buy_signal():
    bars = [
        _bar(0, 10.0, 10.2, 9.9, 10.1, 1000),
        _bar(1, 10.1, 10.3, 10.0, 10.2, 1000),
        _bar(2, 10.2, 10.35, 10.1, 10.25, 1000),
        _bar(3, 10.3, 10.8, 10.3, 10.75, 5000),
    ]
    snap = TickerSnapshot(
        ticker="TEST",
        trade_date=date(2026, 5, 18),
        bars=bars,
        current_price=10.75,
        prev_close=10.0,
        prev_high=10.4,
        pss_total=55,
        tier=2,
        triggered_patterns="E,G",
        source="yfinance",
    )

    signals = IntradaySignalEngine().evaluate(
        snap, SignalContext(as_of=bars[-1].ts, buy_signal_count=0)
    )

    assert any(s.signal_type == "BUY_WATCH" and s.trigger_code == "ORB" for s in signals)


def test_take_profit_signal_after_active_buy():
    bars = [
        _bar(0, 10.0, 10.2, 9.9, 10.1, 1000),
        _bar(1, 10.1, 11.2, 10.1, 11.1, 1000),
    ]
    snap = TickerSnapshot(
        ticker="TEST",
        trade_date=date(2026, 5, 18),
        bars=bars,
        current_price=11.1,
        prev_close=10.0,
        prev_high=10.4,
        source="yfinance",
    )

    signals = IntradaySignalEngine().evaluate(
        snap, SignalContext(as_of=bars[-1].ts, active_buy_price=10.0, buy_signal_count=1)
    )

    assert any(s.signal_type == "TAKE_PROFIT" and s.trigger_code == "TP_10" for s in signals)

