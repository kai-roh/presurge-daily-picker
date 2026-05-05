"""PSS Total 계산 + Tier 분류 + Tier 캡 적용."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from src.config import (
    BONUS_TOSS_TOP30,
    PENALTY_EARNINGS,
    PENALTY_EARNINGS_DAYS,
    PENALTY_RECENT_RUN,
    PENALTY_RECENT_RUN_PCT,
    TIER1_MAX_TICKERS,
    TIER1_PATTERNS_MIN,
    TIER1_PSS_MIN,
    TIER2_MAX_TICKERS,
    TIER2_PSS_MIN,
    TIER3_MAX_TICKERS,
    TIER3_PSS_MIN,
)
from src.score import ALL_SCORERS
from src.score.base import PatternScore
from src.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class TickerScore:
    ticker: str
    pss_total: float
    tier: int | None
    triggered_patterns: list[str]
    breakdown: dict[str, float]
    bonus_toss: float
    penalty_run: float
    penalty_earn: float
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_db_row(self) -> dict[str, Any]:
        return {
            "pattern_a": self.breakdown.get("A", 0.0),
            "pattern_b": self.breakdown.get("B", 0.0),
            "pattern_c": self.breakdown.get("C", 0.0),
            "pattern_d": self.breakdown.get("D", 0.0),
            "pattern_e": self.breakdown.get("E", 0.0),
            "pattern_f": self.breakdown.get("F", 0.0),
            "bonus_toss": self.bonus_toss,
            "penalty_run": self.penalty_run,
            "penalty_earn": self.penalty_earn,
            "pss_total": self.pss_total,
            "tier": self.tier,
            "triggered_patterns": ",".join(self.triggered_patterns),
            "metadata_json": self.signals,
        }


def _tier(pss: float, triggered_count: int) -> int | None:
    if pss >= TIER1_PSS_MIN and triggered_count >= TIER1_PATTERNS_MIN:
        return 1
    if pss >= TIER2_PSS_MIN:
        return 2
    if pss >= TIER3_PSS_MIN:
        return 3
    return None


def compute_for_ticker(ticker: str, as_of: date, db: Database) -> TickerScore:
    breakdown: dict[str, float] = {}
    signals: dict[str, dict[str, Any]] = {}
    triggered: list[str] = []
    base = 0.0

    for scorer_cls in ALL_SCORERS:
        scorer = scorer_cls()
        try:
            ps: PatternScore = scorer.compute(ticker, as_of, db)
        except Exception as exc:  # 한 패턴 실패가 전체 계산을 막지 않도록
            logger.exception("Pattern %s failed for %s: %s", scorer.letter, ticker, exc)
            ps = PatternScore.zero()
        breakdown[scorer.letter] = ps.score
        signals[scorer.letter] = ps.contributing_signals
        base += ps.score
        if ps.triggered:
            triggered.append(scorer.letter)

    bonus = BONUS_TOSS_TOP30 if db.in_toss_top30(ticker, as_of) else 0.0
    pen_run = 0.0
    chg = db.price_change_pct(ticker, as_of, days=30)
    if chg is not None and chg >= PENALTY_RECENT_RUN_PCT:
        pen_run = PENALTY_RECENT_RUN
    pen_earn = 0.0
    if _has_earnings_within(db, ticker, as_of, PENALTY_EARNINGS_DAYS):
        pen_earn = PENALTY_EARNINGS

    total = max(0.0, base + bonus + pen_run + pen_earn)
    tier = _tier(total, len(triggered))

    return TickerScore(
        ticker=ticker,
        pss_total=total,
        tier=tier,
        triggered_patterns=triggered,
        breakdown=breakdown,
        bonus_toss=bonus,
        penalty_run=pen_run,
        penalty_earn=pen_earn,
        signals=signals,
    )


def _has_earnings_within(db: Database, ticker: str, as_of: date, days: int) -> bool:
    """earnings 캘린더는 v0.3에서 정식 적재. MVP는 hook만 두고 False."""
    fn = getattr(db, "has_earnings_within", None)
    if callable(fn):
        return bool(fn(ticker, as_of, days))
    return False


def compute_universe(as_of: date, db: Database) -> list[TickerScore]:
    from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD

    tickers = db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD)
    logger.info("Computing PSS for %d tickers as of %s", len(tickers), as_of)
    out: list[TickerScore] = []
    for t in tickers:
        out.append(compute_for_ticker(t, as_of, db))
    return out


def classify_tiers(scores: list[TickerScore]) -> dict[int, list[TickerScore]]:
    """Tier 캡 적용. tier1 ≤ 3, tier2 ≤ 5, tier3 ≤ 10."""
    by_tier: dict[int, list[TickerScore]] = {1: [], 2: [], 3: []}
    sorted_scores = sorted(scores, key=lambda s: s.pss_total, reverse=True)
    for s in sorted_scores:
        if s.tier in by_tier:
            cap = {1: TIER1_MAX_TICKERS, 2: TIER2_MAX_TICKERS, 3: TIER3_MAX_TICKERS}[s.tier]
            if len(by_tier[s.tier]) < cap:
                by_tier[s.tier].append(s)
    return by_tier


def persist(scores: list[TickerScore], as_of: date, db: Database) -> None:
    for s in scores:
        db.upsert_pss(as_of, s.ticker, s.to_db_row())


def to_dict(s: TickerScore) -> dict[str, Any]:
    return asdict(s)
