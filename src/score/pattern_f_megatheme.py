"""Pattern F — Megatheme + AI keyword.

회사명/사업 설명 + 30일내 8-K Item 8.01 + WSB 멘션 5x 증가.
키워드 stuffing 방지: 분류 confidence < 0.6은 감점.
"""
from __future__ import annotations

from datetime import date

from src.config import MEGATHEME_KEYWORDS, PATTERN_F_MAX, WSB_MENTION_GROWTH_MIN
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


def _matches_megatheme(text: str | None) -> list[str]:
    if not text:
        return []
    low = text.lower()
    return [kw for kw in MEGATHEME_KEYWORDS if kw in low]


class PatternF(PatternScorer):
    name = "Megatheme"
    letter = "F"
    max_score = PATTERN_F_MAX
    trigger_threshold = 12.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        univ = db.get_universe_row(ticker)
        if not univ:
            return PatternScore.zero()

        signals: dict = {}
        score = 0.0

        sector_hits = _matches_megatheme(univ["sector"]) + _matches_megatheme(univ["name"])
        if sector_hits:
            score += min(len(sector_hits) * 5.0, 15.0)
            signals["sector_keywords"] = sector_hits

        pivot_filings = db.query_filings(
            ticker, as_of, hours_back=24 * 30, items=["8.01"], keywords=list(MEGATHEME_KEYWORDS),
        )
        if pivot_filings:
            top = pivot_filings[0]
            conf = top["classification_confidence"] or 0.0
            if conf >= 0.6 or "F" in (top["classification"] or ""):
                score += 5.0
                signals["pivot_filing"] = top["accession_no"]
            else:
                score -= 3.0
                signals["weak_pivot_filing"] = top["accession_no"]

        wsb_growth = db.mention_growth(ticker, as_of, source="reddit_wsb")
        if wsb_growth and wsb_growth >= WSB_MENTION_GROWTH_MIN:
            score += 5.0
            signals["wsb_growth"] = wsb_growth

        return self._result(score, signals)
