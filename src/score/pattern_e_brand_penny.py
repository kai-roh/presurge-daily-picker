"""Pattern E — Brand Penny.

historical max 시총 대비 90% 이상 추락 + $1~$5 가격 + retail 인지도 안정.
score = (1 - recovery) * 20 + min(mentions/20, 5) + (debt resolved ? 5 : 0)
"""
from __future__ import annotations

from datetime import date

from src.config import (
    BRAND_PENNY_MENTIONS_FLOOR,
    BRAND_PENNY_PRICE_MAX,
    BRAND_PENNY_PRICE_MIN,
    BRAND_PENNY_RECOVERY_MAX,
    PATTERN_E_MAX,
)
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


class PatternE(PatternScorer):
    name = "Brand Penny"
    letter = "E"
    max_score = PATTERN_E_MAX
    trigger_threshold = 15.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        univ = db.get_universe_row(ticker)
        if not univ:
            return PatternScore.zero()

        hist_max = univ["historical_max_mcap"]
        cur_mcap = univ["market_cap_usd"]
        if not hist_max or not cur_mcap or hist_max <= 0:
            return PatternScore.zero()

        recovery = cur_mcap / hist_max
        if recovery > BRAND_PENNY_RECOVERY_MAX:
            return PatternScore.zero()

        price = db.get_close(ticker, as_of)
        if price is None or not (BRAND_PENNY_PRICE_MIN <= price <= BRAND_PENNY_PRICE_MAX):
            return PatternScore.zero()

        signals: dict = {
            "recovery": recovery,
            "price": price,
            "historical_max_mcap": hist_max,
        }

        score = (1.0 - recovery) * 20.0  # 0.10 → 18, 0.05 → 19, 0.02 → 19.6

        avg_mentions = (
            db.avg_mentions(ticker, as_of, days=90, source="stocktwits")
            or db.avg_mentions(ticker, as_of, days=90, source="reddit_wsb")
            or 0
        )
        if avg_mentions >= BRAND_PENNY_MENTIONS_FLOOR:
            score += min(avg_mentions * 0.05, 5.0)
            signals["avg_mentions_90d"] = avg_mentions

        debt_resolved = bool(
            db.query_filings(
                ticker, as_of, hours_back=24 * 180,
                keywords=["debt swap", "refinancing", "covenant", "exchange agreement"],
            )
        )
        if debt_resolved:
            score += 5.0
            signals["debt_resolved"] = True

        return self._result(score, signals)
