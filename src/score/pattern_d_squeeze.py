"""Pattern D — Short Squeeze Setup.

높은 SI + 저float + 신선 카탈리스트 → 강제 커버링.
score = SI%×0.5 + DTC×1.5 + CTB×0.1 + (50-Float_M)×0.1 + (price_30d <= -30% ? 5 : 0)
clamp to PATTERN_D_MAX.
"""
from __future__ import annotations

from datetime import date

from src.config import (
    CTB_MIN,
    DTC_MIN,
    FLOAT_MAX_M,
    PATTERN_D_MAX,
    PRICE_DROP_30D_THRESHOLD,
    SI_PCT_MIN,
)
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


class PatternD(PatternScorer):
    name = "Short Squeeze Setup"
    letter = "D"
    max_score = PATTERN_D_MAX
    trigger_threshold = 18.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        si = db.latest_short_interest(ticker, as_of)
        univ = db.get_universe_row(ticker)
        if not si or not univ:
            return PatternScore.zero()

        si_pct = si["si_pct_float"] or 0.0
        dtc = si["days_to_cover"] or 0.0
        ctb = si["cost_to_borrow"] or 0.0
        float_shares = univ["float_shares"]
        float_m = (float_shares or 0) / 1_000_000

        signals: dict = {
            "si_pct": si_pct,
            "dtc": dtc,
            "ctb": ctb,
            "float_m": float_m,
        }

        # 임계치 미달 시 0
        meets_si = si_pct >= SI_PCT_MIN
        meets_dtc = dtc >= DTC_MIN
        meets_ctb = ctb >= CTB_MIN
        meets_float = 0 < float_m <= FLOAT_MAX_M
        if not (meets_si or meets_dtc or meets_float):
            return PatternScore.zero()

        score = 0.0
        if meets_si:
            score += min(si_pct * 100 * 0.5, 15.0)
        if meets_dtc:
            score += min(dtc * 1.5, 9.0)
        if meets_ctb:
            score += min(ctb * 100 * 0.1, 6.0)
        if meets_float:
            score += max(0.0, (FLOAT_MAX_M - float_m) * 0.1)

        price_30d = db.price_change_pct(ticker, as_of, days=30)
        if price_30d is not None and price_30d <= PRICE_DROP_30D_THRESHOLD:
            score += 5.0
            signals["price_30d_chg"] = price_30d

        return self._result(score, signals)
