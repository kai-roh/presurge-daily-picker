"""백테스트 러너.

전체 universe × 영업일별 PSS 계산 → entry simulation → 수익률 매트릭스 산출.
look-ahead bias 방지: filings.filed_at < as_of, settle_date <= as_of 엄격 가드.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.score.pss_aggregator import (
    classify_tiers,
    compute_universe,
)
from src.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    score_date: date
    ticker: str
    pss_total: float
    tier: int | None
    triggered_patterns: list[str]
    entry_date: date
    entry_price: float
    # close 기반 exit: hold_days -> (exit_date, exit_close, ret_close)
    exits: dict[int, tuple[date, float, float]] = field(default_factory=dict)
    # high 기반 exit (일중 최고가 도달률 — 초단타 alpha 측정):
    #   hold_days -> ret_high  (= (max(high over [entry_date+1, entry_date+hold_days]) - entry) / entry)
    high_exits: dict[int, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    start: date
    end: date

    def filter_tier(self, tier: int) -> list[TradeRecord]:
        return [t for t in self.trades if t.tier == tier]

    def hit_rate(self, tier: int, hold_days: int, threshold: float) -> float:
        rows = self.filter_tier(tier)
        if not rows:
            return 0.0
        hits = sum(
            1
            for r in rows
            if hold_days in r.exits and r.exits[hold_days][2] >= threshold
        )
        return hits / len(rows)

    def hit_rate_high(self, tier: int, hold_days: int, threshold: float) -> float:
        """일중 high 기반 hit rate — 초단타 max profit 도달율."""
        rows = self.filter_tier(tier)
        if not rows:
            return 0.0
        hits = sum(
            1
            for r in rows
            if hold_days in r.high_exits and r.high_exits[hold_days] >= threshold
        )
        return hits / len(rows)

    def avg_return(self, tier: int, hold_days: int) -> float:
        rows = self.filter_tier(tier)
        if not rows:
            return 0.0
        rets = [r.exits[hold_days][2] for r in rows if hold_days in r.exits]
        return sum(rets) / len(rets) if rets else 0.0

    def avg_return_high(self, tier: int, hold_days: int) -> float:
        rows = self.filter_tier(tier)
        if not rows:
            return 0.0
        rets = [r.high_exits[hd] for r in rows for hd in [hold_days] if hd in r.high_exits]
        return sum(rets) / len(rets) if rets else 0.0


def trading_days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _next_trading_day(d: date) -> date:
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def _entry_price(db: Database, ticker: str, target: date) -> float | None:
    """target 일자의 open 가격 (없으면 None). 첫 영업일에 익일 시가 진입 가정."""
    row = db.conn.execute(
        "SELECT open FROM daily_bars WHERE ticker = ? AND trade_date = ?",
        (ticker, target.isoformat()),
    ).fetchone()
    if not row or row["open"] is None:
        return None
    return float(row["open"])


def _exit_price(db: Database, ticker: str, target: date) -> tuple[date, float] | None:
    """target 일자 또는 그 이후 첫 영업일의 close. (실 종가 일자, close)."""
    row = db.conn.execute(
        "SELECT trade_date, close FROM daily_bars WHERE ticker = ? AND trade_date >= ? "
        "ORDER BY trade_date ASC LIMIT 1",
        (ticker, target.isoformat()),
    ).fetchone()
    if not row:
        return None
    return date.fromisoformat(row["trade_date"]), float(row["close"])


def _max_high_in_window(
    db: Database, ticker: str, start: date, end: date
) -> float | None:
    """[start, end] 영업일 윈도우의 max(high) — 일중 최고가 도달률 측정용."""
    row = db.conn.execute(
        "SELECT MAX(high) AS h FROM daily_bars "
        "WHERE ticker = ? AND trade_date BETWEEN ? AND ? AND high IS NOT NULL",
        (ticker, start.isoformat(), end.isoformat()),
    ).fetchone()
    if not row or row["h"] is None:
        return None
    return float(row["h"])


def run_backtest(
    db: Database,
    start: date,
    end: date,
    *,
    tiers: tuple[int, ...] = (1,),
    hold_days_list: tuple[int, ...] = (1, 2, 3, 5),
    persist: bool = False,
) -> BacktestResult:
    """tier 1 종목을 영업일별로 추출, 익일 시가 진입 + N일 후 종가 청산 시뮬.

    persist=True 시 매일 PSS 점수 전체와 watchlist_runs를 DB에 적재.
    surge recall 분석 등 retroactive feature lookup 용.
    """
    trades: list[TradeRecord] = []

    for d in trading_days(start, end):
        scores = compute_universe(d, db)
        if not scores:
            continue
        by_tier = classify_tiers(scores)

        if persist:
            import json as _json

            from src.score.pss_aggregator import persist as _persist
            _persist(scores, d, db)
            t1 = [
                {"ticker": s.ticker, "pss_total": s.pss_total, "tier": s.tier,
                 "triggered_patterns": s.triggered_patterns}
                for s in by_tier.get(1, [])
            ]
            t2 = [
                {"ticker": s.ticker, "pss_total": s.pss_total, "tier": s.tier,
                 "triggered_patterns": s.triggered_patterns}
                for s in by_tier.get(2, [])
            ]
            t3 = [
                {"ticker": s.ticker, "pss_total": s.pss_total, "tier": s.tier,
                 "triggered_patterns": s.triggered_patterns}
                for s in by_tier.get(3, [])
            ]
            db.conn.execute(
                """
                INSERT OR REPLACE INTO watchlist_runs(run_date, tier1_json, tier2_json, tier3_json,
                                                     report_md, push_status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    d.isoformat(),
                    _json.dumps(t1), _json.dumps(t2), _json.dumps(t3),
                    "(backtest persist)", "backtest",
                ),
            )

        for tier in tiers:
            for s in by_tier.get(tier, []):
                entry_d = _next_trading_day(d)
                entry_p = _entry_price(db, s.ticker, entry_d)
                if entry_p is None or entry_p <= 0:
                    continue
                rec = TradeRecord(
                    score_date=d,
                    ticker=s.ticker,
                    pss_total=s.pss_total,
                    tier=s.tier,
                    triggered_patterns=s.triggered_patterns,
                    entry_date=entry_d,
                    entry_price=entry_p,
                )
                for hd in hold_days_list:
                    exit_target = entry_d + timedelta(days=hd)
                    res = _exit_price(db, s.ticker, exit_target)
                    if res is None:
                        continue
                    exit_date, exit_p = res
                    ret = (exit_p - entry_p) / entry_p
                    rec.exits[hd] = (exit_date, exit_p, ret)
                    # high 기반 — entry_d 부터 exit_date 까지의 최고가
                    max_high = _max_high_in_window(db, s.ticker, entry_d, exit_date)
                    if max_high is not None and entry_p > 0:
                        rec.high_exits[hd] = (max_high - entry_p) / entry_p
                trades.append(rec)

    return BacktestResult(trades=trades, start=start, end=end)
