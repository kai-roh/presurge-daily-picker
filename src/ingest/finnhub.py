"""Finnhub client (free tier).

용도:
- Universe bootstrap의 Stage 1/2 (Polygon 무료 티어가 details 호출에 5/min만 허용해
  대안으로 Finnhub /stock/symbol + /stock/profile2 사용).
- /stock/profile2 의 marketCapitalization, shareOutstanding 단위는 **백만 USD** / **백만 주**.

Rate limit (free tier): 60 calls/min — RateLimiter는 rps=1 (=60/min steady) 로 사용.
"""
from __future__ import annotations

import logging
from typing import Any

from src.ingest._http import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
DEFAULT_RPS = 1  # 60/min on free tier


class Finnhub:
    def __init__(self, api_key: str, rps: int = DEFAULT_RPS):
        self.api_key = api_key
        # /stock/symbol 는 S3 정적 파일로 302 리다이렉트 → follow_redirects 필요
        self.http = HttpClient(base_url=BASE_URL, rps=rps, follow_redirects=True)

    def stock_symbols(self, exchange: str = "US") -> list[dict[str, Any]]:
        """1콜로 거래소 전체 심볼 반환. exchange='US'면 OTC 포함 ~30k건."""
        params = {"exchange": exchange, "token": self.api_key}
        resp = self.http.get("/stock/symbol", params=params)
        body = resp.json()
        if not isinstance(body, list):
            logger.warning("unexpected /stock/symbol body type: %s", type(body))
            return []
        logger.info("Finnhub stock/symbol %s -> %d rows", exchange, len(body))
        return body

    def company_profile2(self, symbol: str) -> dict[str, Any]:
        """심볼별 마이크로 정보. marketCapitalization (M USD), shareOutstanding (M)."""
        params = {"symbol": symbol, "token": self.api_key}
        resp = self.http.get("/stock/profile2", params=params)
        body = resp.json()
        if not isinstance(body, dict):
            return {}
        return body

    def quote(self, symbol: str) -> dict[str, Any]:
        """현재 quote. c=current, h/l/o=day high/low/open, pc=previous close."""
        params = {"symbol": symbol, "token": self.api_key}
        resp = self.http.get("/quote", params=params)
        body = resp.json()
        if not isinstance(body, dict):
            return {}
        return body

    def close(self) -> None:
        self.http.close()
