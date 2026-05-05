"""ApeWisdom Reddit 멘션 fetcher (무료)."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from src.ingest._http import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://apewisdom.io"


class ApeWisdomFetcher:
    def __init__(self) -> None:
        self.http = HttpClient(base_url=BASE_URL, rps=2)

    def fetch_top(self, page: int = 1, source: str = "wallstreetbets") -> list[dict[str, Any]]:
        path = f"/api/v1.0/filter/{source}/page/{page}"
        resp = self.http.get(path)
        body = resp.json()
        return body.get("results", []) or []

    def to_db_rows(self, mention_date: date, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i, r in enumerate(rows, start=1):
            ticker = (r.get("ticker") or "").upper()
            if not ticker:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "mention_date": mention_date.isoformat(),
                    "source": "reddit_wsb",
                    "mentions": r.get("mentions"),
                    "bullish_pct": None,
                    "rank": r.get("rank") or i,
                }
            )
        return out

    def close(self) -> None:
        self.http.close()
