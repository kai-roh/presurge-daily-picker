"""Pattern G — Volume Spike (RVOL).

가설: 카탈리스트 발표 또는 retail 누적 관심으로 거래량이 평소 대비 급증한 종목은
다음날 급등 확률이 높다.

24mo 데이터 검증 (surge_events vs universe baseline):
- RVOL ≥ 5: surge prev-day 발생 확률 5.4% (baseline 1.2%) → **4.6x lift**
- RVOL ≥ 3: 4.8% (baseline 1.8%) → 2.7x
- RVOL ≥ 2: 7.8% (baseline 3.8%) → 2.1x

Score curve (max 20):
- RVOL ≥ 5.0: 20
- RVOL ≥ 3.0: 15
- RVOL ≥ 2.0: 10
- RVOL ≥ 1.5: 5
- < 1.5: 0

trigger_threshold: 10 (RVOL ≥ 2)

baseline 30일 평균 거래량 대비. 신생 IPO 등 baseline 데이터 부족 시 score=0.
"""
from __future__ import annotations

from datetime import date

from src.config import PATTERN_G_MAX, PATTERN_G_RVOL_LOOKBACK_DAYS
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database

# RVOL → score 테이블 (max=20 기준 비례 스케일)
_RVOL_BUCKETS = (
    (5.0, 1.0),    # RVOL >= 5: max_score
    (3.0, 0.75),   # RVOL >= 3: 75% of max
    (2.0, 0.50),   # RVOL >= 2: 50% of max
    (1.5, 0.25),   # RVOL >= 1.5: 25% of max
)


class PatternG(PatternScorer):
    name = "Volume Spike"
    letter = "G"
    max_score = PATTERN_G_MAX
    trigger_threshold = max_score * 0.5  # RVOL >= 2 = 50%

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        latest_v = db.latest_volume(ticker, as_of)
        if latest_v is None or latest_v <= 0:
            return PatternScore.zero()

        avg_v = db.avg_volume(ticker, as_of, PATTERN_G_RVOL_LOOKBACK_DAYS)
        if avg_v is None or avg_v <= 0:
            return PatternScore.zero()

        rvol = latest_v / avg_v
        score = 0.0
        for threshold, frac in _RVOL_BUCKETS:
            if rvol >= threshold:
                score = self.max_score * frac
                break

        signals = {"rvol": round(rvol, 2), "latest_volume": latest_v, "avg30_volume": int(avg_v)}
        return self._result(score, signals)
