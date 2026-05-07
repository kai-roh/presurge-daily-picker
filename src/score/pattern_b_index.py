"""Pattern B — Index / ETF Inclusion.

Russell 2000/3000, MEME ETF 등 신규 편입 시 패시브 매수 유입.

W4 #5 검증 결과: Russell 2000 reconstitution 자체로는 5d 단위 alpha 거의 없음
(H4 Spearman 0.261 → 0.04 폭락, n=704). PATTERN_B_MAX 를 25 → 5로 다운하면서
세부 가중치도 비례 축소 (announced=5, post-effective=3, small-cap bonus=+1).
"""
from __future__ import annotations

from datetime import date

from src.config import MARKET_CAP_MAX_USD, PATTERN_B_MAX
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


class PatternB(PatternScorer):
    name = "Index Inclusion"
    letter = "B"
    max_score = PATTERN_B_MAX
    trigger_threshold = max_score * 0.6  # 5에서 3.0, 25에서 15.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        events_fn = getattr(db, "index_inclusion_events", None)
        if not callable(events_fn):
            return PatternScore.zero()

        events = events_fn(ticker, as_of)  # list[dict]
        if not events:
            return PatternScore.zero()

        # 가중치 비례: PATTERN_B_MAX=5일 때 (5, 3, +1), =25일 때 (25, 15, +5)
        scale = self.max_score / 25.0
        full_score = 25.0 * scale
        post_effective_score = 15.0 * scale
        small_cap_bonus = 5.0 * scale

        signals: dict = {"events": events}
        score = 0.0
        for ev in events:
            announce = ev.get("announced_at")
            effective = ev.get("effective_at")
            if announce and (effective is None or as_of <= effective):
                score = max(score, full_score)
            elif effective and (as_of - effective).days <= 7:
                score = max(score, post_effective_score)

        mcap = db.get_market_cap(ticker) or MARKET_CAP_MAX_USD
        if score > 0 and mcap < 500_000_000:
            score += small_cap_bonus
            signals["small_cap_bonus"] = True

        return self._result(score, signals)
