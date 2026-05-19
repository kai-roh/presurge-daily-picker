"""Deterministic intraday signal rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.intraday.indicators import (
    avg_volume,
    ema,
    latest_volume_ratio,
    opening_range_high,
    opening_range_low,
    vwap,
)
from src.intraday.market_data import TickerSnapshot


@dataclass(frozen=True)
class Signal:
    signal_type: str
    trigger_code: str
    price: float
    ref_price: float | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalContext:
    as_of: datetime
    active_buy_price: float | None = None
    buy_signal_count: int = 0
    caution_active: bool = False


class IntradaySignalEngine:
    """초기 MVP용 rule engine.

    이 엔진은 "정답"을 맞히는 모델이 아니라 사후 학습 가능한 원시 signal을 만든다.
    threshold는 보수적으로 시작하고 signal_outcomes가 쌓인 뒤 조정한다.
    """

    def evaluate(self, snap: TickerSnapshot, ctx: SignalContext) -> list[Signal]:
        price = snap.current_price
        if price is None or price <= 0:
            return []

        signals: list[Signal] = []
        if ctx.active_buy_price:
            signals.extend(self._exit_signals(snap, ctx, price))
            return signals

        if ctx.caution_active:
            return []

        signals.extend(self._buy_signals(snap, ctx, price))
        if not signals:
            caution = self._caution_signal(snap, price)
            if caution:
                signals.append(caution)
        return signals

    def _buy_signals(
        self,
        snap: TickerSnapshot,
        ctx: SignalContext,
        price: float,
    ) -> list[Signal]:
        if ctx.buy_signal_count >= 2:
            return []

        bars = snap.bars
        out: list[Signal] = []
        pct_prev_close = _pct(price, snap.prev_close)
        vol_ratio = latest_volume_ratio(bars)
        intraday_vwap = vwap(bars)
        or_high = opening_range_high(bars)
        or_low = opening_range_low(bars)

        # BUY-1: Opening Range Breakout. 첫 15분봉 확정 이후만 본다.
        if len(bars) >= 4 and or_high and vol_ratio:
            breaks_reference = (
                (snap.prev_high is not None and price > snap.prev_high)
                or (pct_prev_close is not None and pct_prev_close >= 0.03)
            )
            if price > or_high and breaks_reference and vol_ratio >= 2.0:
                out.append(Signal(
                    signal_type="BUY_WATCH",
                    trigger_code="ORB",
                    price=price,
                    ref_price=or_high,
                    metadata={
                        "opening_range_high": or_high,
                        "opening_range_low": or_low,
                        "prev_high": snap.prev_high,
                        "prev_close": snap.prev_close,
                        "pct_prev_close": pct_prev_close,
                        "volume_ratio_5m": vol_ratio,
                        "vwap": intraday_vwap,
                    },
                ))

        # BUY-2: VWAP Reclaim. 바로 직전 봉이 VWAP 아래, 현재가 위일 때.
        if len(bars) >= 7 and intraday_vwap and vol_ratio:
            prev_bar_close = bars[-2].close
            day_low = min(b.low for b in bars)
            recovered = _pct(price, day_low)
            if (
                prev_bar_close < intraday_vwap <= price
                and vol_ratio >= 1.5
                and pct_prev_close is not None and pct_prev_close >= 0.02
                and recovered is not None and recovered >= 0.05
            ):
                out.append(Signal(
                    signal_type="BUY_WATCH",
                    trigger_code="VWAP_RECLAIM",
                    price=price,
                    ref_price=intraday_vwap,
                    metadata={
                        "vwap": intraday_vwap,
                        "prev_bar_close": prev_bar_close,
                        "volume_ratio_5m": vol_ratio,
                        "pct_prev_close": pct_prev_close,
                        "recovered_from_low": recovered,
                    },
                ))

        # BUY-3: Relative Volume Continuation. Pattern G 후보와 궁합이 좋은 지속 신호.
        rvol = _intraday_rvol(snap)
        closes = [b.close for b in bars]
        ema5 = ema(closes[-10:], span=5) if closes else None
        if len(bars) >= 6 and rvol and pct_prev_close is not None:
            recent_high = max(b.high for b in bars[-4:-1])
            if (
                rvol >= 3.0
                and pct_prev_close >= 0.05
                and ((ema5 is not None and price >= ema5) or price >= recent_high)
            ):
                out.append(Signal(
                    signal_type="BUY_WATCH",
                    trigger_code="RVOL_CONT",
                    price=price,
                    ref_price=ema5,
                    metadata={
                        "intraday_rvol": rvol,
                        "pct_prev_close": pct_prev_close,
                        "ema5": ema5,
                        "recent_high": recent_high,
                        "cumulative_volume": sum(b.volume for b in bars),
                    },
                ))

        return out

    def _exit_signals(
        self,
        snap: TickerSnapshot,
        ctx: SignalContext,
        price: float,
    ) -> list[Signal]:
        assert ctx.active_buy_price is not None
        out: list[Signal] = []
        pnl = _pct(price, ctx.active_buy_price)
        bars = snap.bars
        intraday_vwap = vwap(bars)

        if pnl is not None:
            if pnl >= 0.30:
                out.append(self._take_profit(price, ctx.active_buy_price, pnl, "TP_30"))
            elif pnl >= 0.20:
                out.append(self._take_profit(price, ctx.active_buy_price, pnl, "TP_20"))
            elif pnl >= 0.10:
                out.append(self._take_profit(price, ctx.active_buy_price, pnl, "TP_10"))

        if intraday_vwap and price < intraday_vwap:
            below_count = sum(1 for b in bars[-2:] if b.close < intraday_vwap)
            if below_count >= 2 or (pnl is not None and pnl <= -0.05):
                out.append(Signal(
                    signal_type="SELL_WATCH",
                    trigger_code="VWAP_LOSS",
                    price=price,
                    ref_price=intraday_vwap,
                    metadata={
                        "signal_pnl": pnl,
                        "vwap": intraday_vwap,
                        "below_vwap_bars": below_count,
                    },
                ))

        if pnl is not None and pnl >= 0.10 and len(bars) >= 4:
            highs = [b.high for b in bars[-3:]]
            vol_avg = avg_volume(bars[-7:-1], exclude_last=False)
            latest_vol = bars[-1].volume if bars else None
            if max(highs) <= max(b.high for b in bars[:-3]) and latest_vol and vol_avg and latest_vol < vol_avg:
                out.append(Signal(
                    signal_type="SELL_WATCH",
                    trigger_code="EXHAUSTION",
                    price=price,
                    ref_price=ctx.active_buy_price,
                    metadata={
                        "signal_pnl": pnl,
                        "latest_volume": latest_vol,
                        "avg_prev_volume": vol_avg,
                    },
                ))

        return out

    @staticmethod
    def _take_profit(price: float, entry: float, pnl: float, code: str) -> Signal:
        return Signal(
            signal_type="TAKE_PROFIT",
            trigger_code=code,
            price=price,
            ref_price=entry,
            metadata={"signal_pnl": pnl},
        )

    @staticmethod
    def _caution_signal(snap: TickerSnapshot, price: float) -> Signal | None:
        pct_prev_close = _pct(price, snap.prev_close)
        if pct_prev_close is not None and pct_prev_close <= -0.08:
            return Signal(
                signal_type="CAUTION",
                trigger_code="PRICE_BREAKDOWN",
                price=price,
                ref_price=snap.prev_close,
                metadata={"pct_prev_close": pct_prev_close},
            )

        bars = snap.bars
        intraday_vwap = vwap(bars)
        if intraday_vwap and len(bars) >= 6:
            below = all(b.close < intraday_vwap for b in bars[-6:])
            vol_ratio = latest_volume_ratio(bars)
            if below and (vol_ratio is None or vol_ratio < 1.5):
                return Signal(
                    signal_type="CAUTION",
                    trigger_code="VWAP_WEAKNESS",
                    price=price,
                    ref_price=intraday_vwap,
                    metadata={"vwap": intraday_vwap, "volume_ratio_5m": vol_ratio},
                )
        return None


def _pct(value: float | None, ref: float | None) -> float | None:
    if value is None or ref is None or ref <= 0:
        return None
    return (value - ref) / ref


def _intraday_rvol(snap: TickerSnapshot) -> float | None:
    avg_daily = snap.metadata.get("avg_daily_volume_30d")
    if not avg_daily or not snap.bars:
        return None
    # 정규장 6.5h = 78개 5분봉. 시간대별 볼륨 곡선을 모르는 MVP에서는 균등 추정.
    expected = float(avg_daily) * min(len(snap.bars), 78) / 78
    if expected <= 0:
        return None
    return sum(b.volume for b in snap.bars) / expected

