"""Pattern A — Dilution Shutdown.

ATM, standby equity 등 만성 매도 압력 메커니즘이 종료되면 매도벽 제거.
24h 내 종료 공시 → +30, 7일 내 → +20, 발행주식수 증가율 둔화 → +5.
"""
from __future__ import annotations

from datetime import date

from src.config import (
    PATTERN_A_DILUTION_LOW_RATE,
    PATTERN_A_ITEMS,
    PATTERN_A_KEYWORDS,
    PATTERN_A_MAX,
)
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


class PatternA(PatternScorer):
    name = "Dilution Shutdown"
    letter = "A"
    max_score = PATTERN_A_MAX
    trigger_threshold = 20.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        signals: dict = {}
        score = 0.0

        recent_24h = db.query_filings(
            ticker, as_of, hours_back=24,
            items=list(PATTERN_A_ITEMS), keywords=list(PATTERN_A_KEYWORDS),
        )
        if recent_24h:
            score = 30.0
            signals["recent_24h"] = recent_24h[0]["accession_no"]
        else:
            recent_7d = db.query_filings(
                ticker, as_of, hours_back=24 * 7,
                items=list(PATTERN_A_ITEMS), keywords=list(PATTERN_A_KEYWORDS),
            )
            if recent_7d:
                score = 20.0
                signals["recent_7d"] = recent_7d[0]["accession_no"]

        if score > 0:
            growth = self._share_growth_rate_proxy(ticker, as_of, db)
            if growth is not None and growth < PATTERN_A_DILUTION_LOW_RATE:
                score += 5.0
                signals["low_dilution_rate"] = growth

        return self._result(score, signals)

    def _share_growth_rate_proxy(self, ticker: str, as_of: date, db: Database) -> float | None:
        """발행주식수 증가율 프록시. universe.float_shares 변화 (v0.3에서 historical 적재 후 정확화)."""
        # MVP: float_shares 단일 시점만 보유 → None 반환. W2에서 historical로 보강.
        _ = (ticker, as_of, db)
        return None
