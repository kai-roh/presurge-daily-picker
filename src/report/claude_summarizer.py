"""Claude API 어댑터 — 8-K 분류 + 일일 리포트 생성.

prompt caching:
- 8-K 분류: system 프롬프트 + 패턴 정의는 cache_control. 일평균 30~50건 호출.
- 일일 리포트: system 프롬프트 cache_control. 1일 1회.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
SYSTEM_REPORT_PATH = TEMPLATE_DIR / "system_report.txt"
SYSTEM_CLASSIFY_PATH = TEMPLATE_DIR / "system_classify.txt"


@dataclass
class ClassificationResult:
    patterns: list[str]
    contract_value_usd: float | None
    counterparty: str | None
    key_quote: str
    confidence: float

    @classmethod
    def empty(cls) -> ClassificationResult:
        return cls(patterns=["none"], contract_value_usd=None, counterparty=None,
                   key_quote="", confidence=0.0)


class ClaudeSummarizer:
    def __init__(
        self,
        api_key: str,
        report_model: str = "claude-haiku-4-5",
        classify_model: str = "claude-sonnet-4-6",
    ):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.report_model = report_model
        self.classify_model = classify_model
        self.jinja = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2", "txt")),
            keep_trailing_newline=True,
        )
        self._system_report = SYSTEM_REPORT_PATH.read_text(encoding="utf-8")
        self._system_classify = SYSTEM_CLASSIFY_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # 8-K classification
    # ------------------------------------------------------------------
    def classify_filing(
        self, ticker: str, items: str, body: str, body_chars: int = 8000
    ) -> ClassificationResult:
        truncated = body[:body_chars]
        try:
            resp = self.client.messages.create(
                model=self.classify_model,
                max_tokens=600,
                system=[
                    {
                        "type": "text",
                        "text": self._system_classify,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"티커: {ticker}\n8-K Items: {items}\n본문:\n{truncated}\n\nJSON만 출력:"
                        ),
                    }
                ],
            )
        except anthropic.APIError as exc:
            logger.warning("Claude classify failed for %s: %s", ticker, exc)
            return ClassificationResult.empty()

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return self._parse_classification(text)

    @staticmethod
    def _parse_classification(text: str) -> ClassificationResult:
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < 0:
                return ClassificationResult.empty()
            data = json.loads(text[start : end + 1])
            patterns = data.get("patterns") or ["none"]
            if not isinstance(patterns, list):
                patterns = ["none"]
            return ClassificationResult(
                patterns=[str(p).upper() for p in patterns],
                contract_value_usd=_to_float(data.get("contract_value_usd")),
                counterparty=data.get("counterparty"),
                key_quote=data.get("key_quote") or "",
                confidence=float(data.get("confidence") or 0.0),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Classification parse failed: %s. raw=%s", exc, text[:200])
            return ClassificationResult.empty()

    # ------------------------------------------------------------------
    # Daily report
    # ------------------------------------------------------------------
    def generate_report(
        self,
        run_date: str,
        tier1: list[dict[str, Any]],
        tier2: list[dict[str, Any]],
        tier3: list[dict[str, Any]],
    ) -> str:
        payload = {
            "run_date": run_date,
            "tier1": tier1,
            "tier2": tier2,
            "tier3": tier3,
        }
        try:
            resp = self.client.messages.create(
                model=self.report_model,
                max_tokens=2000,
                system=[
                    {
                        "type": "text",
                        "text": self._system_report,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": "다음 데이터로 리포트를 작성하라:\n\n"
                        + json.dumps(payload, ensure_ascii=False, indent=2),
                    }
                ],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            if text.strip():
                return text
        except anthropic.APIError as exc:
            logger.warning("Claude report failed: %s — falling back to template", exc)

        return self.fallback_report(run_date, tier1, tier2, tier3)

    def fallback_report(
        self,
        run_date: str,
        tier1: list[dict[str, Any]],
        tier2: list[dict[str, Any]],
        tier3: list[dict[str, Any]],
    ) -> str:
        tmpl = self.jinja.get_template("daily_report.j2")
        return tmpl.render(run_date=run_date, tier1=tier1, tier2=tier2, tier3=tier3)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None
