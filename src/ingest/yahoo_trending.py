"""Yahoo Finance 미국 trending tickers — 일간 retail interest 시그널.

토스앱 거래량 상위 시그널을 받기 어려운 환경에서 대체로 채택. Yahoo finance trending
API는 사용자 검색/조회수 기반이라 거래량과 정확히 일치하지 않지만, retail 관심 종목
변화 시그널로는 충분히 작동한다.

API:
  GET https://query1.finance.yahoo.com/v1/finance/trending/US?count=30
  필수: User-Agent 헤더 (없으면 429)

응답에 crypto (XXX-USD), forex (XXX=X) 같은 비주식 심볼도 포함되므로 필터링.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_URLS = (
    "https://query1.finance.yahoo.com/v1/finance/trending/US",
    "https://query2.finance.yahoo.com/v1/finance/trending/US",
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch_trending(count: int = 30, timeout: float = 15.0) -> list[str]:
    """Yahoo trending US top N. query1/query2 mirror 순차 시도."""
    params = {"count": count}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    last_err: Exception | None = None
    for url in API_URLS:
        try:
            with httpx.Client(timeout=timeout) as c:
                resp = c.get(url, params=params, headers=headers)
                resp.raise_for_status()
            body = resp.json()
            break
        except Exception as exc:
            last_err = exc
            logger.debug("Yahoo trending %s failed: %s", url, exc)
    if body is None:
        logger.warning("Yahoo trending all mirrors failed: %s", last_err)
        return []

    result = (body.get("finance") or {}).get("result") or []
    if not result:
        return []
    quotes = result[0].get("quotes") or []

    tickers: list[str] = []
    for q in quotes:
        sym = (q.get("symbol") or "").upper().strip()
        if not sym:
            continue
        # crypto/forex/futures 제외 (-USD, =X, =F 같은 패턴)
        if any(ch in sym for ch in ("-", "=", "^")):
            continue
        tickers.append(sym)
    logger.info("Yahoo trending US -> %d tickers", len(tickers))
    return tickers


def to_db_ranks(tickers: list[str]) -> list[tuple[int, str]]:
    return [(i + 1, t) for i, t in enumerate(tickers)]
