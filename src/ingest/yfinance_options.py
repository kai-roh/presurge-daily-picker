"""yfinance 기반 EOD 옵션 활동 snapshot.

가까운 1개 만기 chain의 call/put 거래량 및 open interest 합산.
weekly Sunday cron으로 universe-wide 누적 → 4-8주 후 surge_events vs UOA lift 검증.

historical 옵션 데이터는 yfinance 무료 티어에 없으므로 forward 누적만 가능.
검증되면 Polygon Options Starter ($29/월)로 backtest.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_options_snapshot(ticker: str) -> dict[str, Any] | None:
    """가장 가까운 만기 1개의 call+put 합. 데이터 없으면 None."""
    try:
        tk = yf.Ticker(ticker)
        expiries = tk.options
        if not expiries:
            return None
        nearest = expiries[0]
        chain = tk.option_chain(nearest)
    except Exception as exc:
        logger.debug("yfinance options failed for %s: %s", ticker, exc)
        return None

    try:
        call_vol = int(chain.calls["volume"].fillna(0).sum())
        put_vol = int(chain.puts["volume"].fillna(0).sum())
        call_oi = int(chain.calls["openInterest"].fillna(0).sum())
        put_oi = int(chain.puts["openInterest"].fillna(0).sum())
    except (KeyError, AttributeError):
        return None

    if call_vol == 0 and put_vol == 0:
        return None

    cp_ratio = call_vol / max(put_vol, 1)
    return {
        "expiry": nearest,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "cp_volume_ratio": round(cp_ratio, 3),
    }


def to_db_row(snap_date: date, ticker: str, snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "snap_date": snap_date.isoformat(),
        "ticker": ticker,
        "expiry": snap.get("expiry"),
        "call_volume": snap.get("call_volume"),
        "put_volume": snap.get("put_volume"),
        "call_oi": snap.get("call_oi"),
        "put_oi": snap.get("put_oi"),
        "cp_volume_ratio": snap.get("cp_volume_ratio"),
    }
