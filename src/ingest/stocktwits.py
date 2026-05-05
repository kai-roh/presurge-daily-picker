"""StockTwits 멘션/sentiment fetcher (무료, 200 req/h)."""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from src.config import STOCKTWITS_RPH
from src.ingest._http import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://api.stocktwits.com"
KOREAN_RE = re.compile(r"[가-힣]")


class StockTwitsFetcher:
    def __init__(self) -> None:
        rps = max(STOCKTWITS_RPH // 3600, 1)
        self.http = HttpClient(base_url=BASE_URL, rps=rps)

    def fetch_symbol_stream(self, ticker: str) -> dict[str, Any]:
        path = f"/api/2/streams/symbol/{ticker}.json"
        resp = self.http.get(path)
        return resp.json()

    def summarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages") or []
        bull = bear = neutral = 0
        ko_msgs = 0
        for m in messages:
            sentiment = (m.get("entities") or {}).get("sentiment") or {}
            basic = (sentiment.get("basic") or "").lower()
            if basic == "bullish":
                bull += 1
            elif basic == "bearish":
                bear += 1
            else:
                neutral += 1
            body = m.get("body") or ""
            if KOREAN_RE.search(body):
                ko_msgs += 1
        total_directional = bull + bear
        bullish_pct = (bull / total_directional) if total_directional else None
        return {
            "mentions": len(messages),
            "bullish_pct": bullish_pct,
            "korean_messages": ko_msgs,
        }

    def to_db_row(
        self, ticker: str, mention_date: date, summary: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "mention_date": mention_date.isoformat(),
            "source": "stocktwits",
            "mentions": summary.get("mentions"),
            "bullish_pct": summary.get("bullish_pct"),
            "rank": None,
        }

    def close(self) -> None:
        self.http.close()
