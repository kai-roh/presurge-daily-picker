"""Pattern C — Government / Tier-1 Contract.

8-K Item 1.01 (Material Definitive Agreement)에서 정부/대형 retailer 계약을 식별.
계약 규모/시총 비율로 점수: ≥10% → 50, ≥5% → 35, ≥2% → 20, >0 → 10.
classification == 'C' 인 filing이 있으면 contract_value_usd / market_cap 으로 산출.
"""
from __future__ import annotations

from datetime import date

from src.config import (
    PATTERN_C_GOV_KEYWORDS,
    PATTERN_C_MAX,
    PATTERN_C_RATIOS,
    PATTERN_C_RETAIL_KEYWORDS,
)
from src.score.base import PatternScore, PatternScorer
from src.storage.db import Database


def _matches_keywords(text: str | None, keywords: tuple[str, ...]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in keywords)


class PatternC(PatternScorer):
    name = "Tier-1 Contract"
    letter = "C"
    max_score = PATTERN_C_MAX
    trigger_threshold = 20.0

    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore:
        rows = db.query_filings(ticker, as_of, hours_back=24 * 7, items=["1.01"])
        if not rows:
            return PatternScore.zero()

        mcap = db.get_market_cap(ticker)
        if not mcap or mcap <= 0:
            return PatternScore.zero()

        best_score = 0.0
        signals: dict = {}
        for r in rows:
            cls = (r["classification"] or "").upper()
            counterparty = r["counterparty"] or ""
            kquote = r["key_quote"] or ""
            text = f"{counterparty} {kquote}"
            is_gov = _matches_keywords(text, PATTERN_C_GOV_KEYWORDS)
            is_retail = _matches_keywords(text, PATTERN_C_RETAIL_KEYWORDS)
            if "C" not in cls and not (is_gov or is_retail):
                continue

            cv = r["contract_value_usd"]
            if cv is None or cv <= 0:
                # 분류는 됐는데 금액 추출 실패 → 보수적 10점
                if best_score < 10.0:
                    best_score = 10.0
                    signals["unscored_contract"] = r["accession_no"]
                continue

            ratio = cv / mcap
            tier_score = 0.0
            for thr, pts in PATTERN_C_RATIOS:
                if ratio >= thr:
                    tier_score = pts
                    break
            if is_gov:
                tier_score = min(tier_score + 5.0, self.max_score)

            if tier_score > best_score:
                best_score = tier_score
                signals.update({
                    "accession_no": r["accession_no"],
                    "counterparty": counterparty,
                    "contract_value_usd": cv,
                    "ratio": ratio,
                    "is_gov": is_gov,
                    "is_retail": is_retail,
                })

        return self._result(best_score, signals)
