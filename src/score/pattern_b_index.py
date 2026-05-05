"""Pattern B — Index / ETF Inclusion.

Russell 2000/3000, MEME ETF, AI 테마 ETF 신규 편입 시 패시브 매수 유입.
편입 발표일~effective day 사이 +25, effective 후 1주일 +15, 시총<$500M 보너스 +5.

MVP에서는 편입 이벤트 데이터 소스가 별도 적재되어야 함. v0.2 W2에서 ETF 운용사 csv
fetcher 추가 시 db.index_inclusion_events(ticker, as_of) 헬퍼를 도입한다. 본 모듈은
그 헬퍼가 없으면 0을 반환한다.
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
    trigger_threshold = 15.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        events_fn = getattr(db, "index_inclusion_events", None)
        if not callable(events_fn):
            return PatternScore.zero()

        events = events_fn(ticker, as_of)  # list[dict]
        if not events:
            return PatternScore.zero()

        signals: dict = {"events": events}
        score = 0.0
        for ev in events:
            announce = ev.get("announced_at")
            effective = ev.get("effective_at")
            if announce and (effective is None or as_of <= effective):
                score = max(score, 25.0)
            elif effective and (as_of - effective).days <= 7:
                score = max(score, 15.0)

        mcap = db.get_market_cap(ticker) or MARKET_CAP_MAX_USD
        if score > 0 and mcap < 500_000_000:
            score += 5.0
            signals["small_cap_bonus"] = True

        return self._result(score, signals)
