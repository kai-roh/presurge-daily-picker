"""SEC EDGAR 8-K poller.

매일 09:00 KST 실행 시 직전 24h 신규 filing의 atom feed를 폴링.
ticker ↔ CIK 매핑은 SEC company_tickers.json (24h 캐시).
본문은 lazy fetch (스코어링 필요 시).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import feedparser  # type: ignore[import-untyped]
except ImportError:  # sgmllib3k 빌드 실패 환경 대응. lazy import로 사용 시점에 재시도.
    feedparser = None  # type: ignore[assignment]

from src.config import SEC_RPS
from src.ingest._http import HttpClient

logger = logging.getLogger(__name__)

CURRENT_FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ITEM_PATTERN = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)


@dataclass
class FilingRecord:
    accession_no: str
    ticker: str
    cik: str
    filed_at: str
    form_type: str
    items: str
    raw_text_url: str


class EdgarPoller:
    def __init__(self, user_agent: str, cache_dir: Path | None = None):
        self.user_agent = user_agent
        self.http = HttpClient(headers={"User-Agent": user_agent}, rps=SEC_RPS)
        self.cache_dir = cache_dir or Path("data/cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cik_to_ticker: dict[str, str] | None = None

    def _load_ticker_map(self) -> dict[str, str]:
        if self._cik_to_ticker is not None:
            return self._cik_to_ticker
        cache = self.cache_dir / "company_tickers.json"
        # 24h 캐시
        fresh = cache.exists() and (
            datetime.utcnow().timestamp() - cache.stat().st_mtime < 86400
        )
        if fresh:
            import json

            data = json.loads(cache.read_text())
        else:
            resp = self.http.get(COMPANY_TICKERS_URL)
            data = resp.json()
            cache.write_text(resp.text)

        mapping: dict[str, str] = {}
        for v in data.values():
            cik = str(v.get("cik_str", "")).zfill(10)
            ticker = v.get("ticker", "").upper()
            if cik and ticker:
                mapping[cik] = ticker
        self._cik_to_ticker = mapping
        return mapping

    def fetch_recent(self, hours_back: int = 24) -> list[FilingRecord]:
        """직전 N시간 내 8-K filing 리스트."""
        params = {
            "action": "getcurrent",
            "type": "8-K",
            "output": "atom",
            "count": "100",
        }
        resp = self.http.get(CURRENT_FEED_URL, params=params)
        if feedparser is None:
            raise RuntimeError(
                "feedparser is not installed; pip install feedparser to enable EDGAR polling"
            )
        feed = feedparser.parse(resp.text)

        cutoff = datetime.now(UTC) - timedelta(hours=hours_back)
        ticker_map = self._load_ticker_map()
        out: list[FilingRecord] = []

        for entry in feed.entries:
            updated = entry.get("updated_parsed")
            if updated:
                filed_dt = datetime(*updated[:6], tzinfo=UTC)
                if filed_dt < cutoff:
                    continue
            else:
                filed_dt = datetime.now(UTC)

            cik = self._extract_cik(entry)
            ticker = ticker_map.get(cik)
            if not ticker:
                continue

            accession = self._extract_accession(entry)
            if not accession:
                continue

            link = entry.get("link", "")
            out.append(
                FilingRecord(
                    accession_no=accession,
                    ticker=ticker,
                    cik=cik,
                    filed_at=filed_dt.isoformat(),
                    form_type="8-K",
                    items="",  # 본문 fetch 시점에 채움
                    raw_text_url=link,
                )
            )

        logger.info("EDGAR fetched %d 8-K filings (last %dh)", len(out), hours_back)
        return out

    @staticmethod
    def _extract_cik(entry: Any) -> str:
        for tag in entry.get("tags", []) or []:
            term = tag.get("term", "")
            m = re.search(r"\d{10}", term)
            if m:
                return m.group(0)
        link = entry.get("link", "")
        m = re.search(r"CIK=(\d+)", link, re.IGNORECASE)
        if m:
            return m.group(1).zfill(10)
        return ""

    @staticmethod
    def _extract_accession(entry: Any) -> str:
        link = entry.get("link", "")
        m = re.search(r"(\d{10}-\d{2}-\d{6})", link)
        if m:
            return m.group(1)
        m = re.search(r"accession_number=([\d-]+)", link, re.IGNORECASE)
        return m.group(1) if m else ""

    def fetch_filing_text(self, raw_text_url: str) -> str:
        """8-K 본문 첫 페이지 텍스트. 키워드/Item 추출용."""
        resp = self.http.get(raw_text_url)
        return resp.text

    def parse_items(self, body: str) -> list[str]:
        return sorted({m.group(1) for m in ITEM_PATTERN.finditer(body)})

    def to_db_rows(self, records: Iterable[FilingRecord]) -> list[dict[str, Any]]:
        return [
            {
                "accession_no": r.accession_no,
                "ticker": r.ticker,
                "cik": r.cik,
                "filed_at": r.filed_at,
                "form_type": r.form_type,
                "items": r.items,
                "raw_text_url": r.raw_text_url,
            }
            for r in records
        ]

    def close(self) -> None:
        self.http.close()
