"""5분봉 기반 간단 지표."""
from __future__ import annotations

from collections.abc import Sequence

from src.intraday.market_data import IntradayBar


def vwap(bars: Sequence[IntradayBar]) -> float | None:
    vol_sum = sum(max(b.volume or 0, 0) for b in bars)
    if vol_sum <= 0:
        return None
    pv = sum(b.close * max(b.volume or 0, 0) for b in bars)
    return pv / vol_sum


def opening_range_high(bars: Sequence[IntradayBar], minutes: int = 15) -> float | None:
    n = max(1, minutes // 5)
    head = bars[:n]
    if len(head) < n:
        return None
    return max(b.high for b in head)


def opening_range_low(bars: Sequence[IntradayBar], minutes: int = 15) -> float | None:
    n = max(1, minutes // 5)
    head = bars[:n]
    if len(head) < n:
        return None
    return min(b.low for b in head)


def avg_volume(bars: Sequence[IntradayBar], exclude_last: bool = True) -> float | None:
    sample = bars[:-1] if exclude_last else bars
    vols = [b.volume for b in sample if b.volume and b.volume > 0]
    if not vols:
        return None
    return sum(vols) / len(vols)


def latest_volume_ratio(bars: Sequence[IntradayBar], lookback: int = 6) -> float | None:
    if len(bars) < 2:
        return None
    recent = bars[-1]
    sample = bars[max(0, len(bars) - lookback - 1):-1]
    vols = [b.volume for b in sample if b.volume and b.volume > 0]
    if not recent.volume or not vols:
        return None
    return recent.volume / (sum(vols) / len(vols))


def ema(values: Sequence[float], span: int = 5) -> float | None:
    if not values:
        return None
    alpha = 2 / (span + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out

