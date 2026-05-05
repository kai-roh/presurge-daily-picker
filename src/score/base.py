"""패턴 스코어러 공통 인터페이스."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.storage.db import Database


@dataclass
class PatternScore:
    score: float
    contributing_signals: dict[str, Any] = field(default_factory=dict)
    triggered: bool = False

    @classmethod
    def zero(cls) -> PatternScore:
        return cls(score=0.0, triggered=False)


class PatternScorer(ABC):
    name: str = ""
    letter: str = ""
    max_score: float = 0.0
    trigger_threshold: float = 0.0

    @abstractmethod
    def compute(self, ticker: str, as_of: date, db: Database) -> PatternScore: ...

    def _clamp(self, score: float) -> float:
        return max(0.0, min(score, self.max_score))

    def _result(self, raw: float, signals: dict[str, Any]) -> PatternScore:
        score = self._clamp(raw)
        return PatternScore(
            score=score,
            contributing_signals=signals,
            triggered=score >= self.trigger_threshold,
        )
