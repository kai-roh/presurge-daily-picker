"""토스증권 인기 미국 종목 거래량 상위 30 fetcher.

공식 API 부재 → Playwright 헤드리스 스크래핑이 W2 산출물.
v0.2 MVP에서는 stub: 환경변수 TOSS_TOP30_TICKERS (CSV) 로 수동 제공 가능.
스크래핑 실패 시 bonus_toss = 0 (전체 시스템 미중단).
"""
from __future__ import annotations

import logging
import os
from datetime import date

logger = logging.getLogger(__name__)


class TossVolumeFetcher:
    def __init__(self) -> None:
        pass

    def fetch_top30(self, on: date) -> list[tuple[int, str]]:
        """오늘의 토스 인기 미국주식 상위 30. (rank, ticker)."""
        env = os.environ.get("TOSS_TOP30_TICKERS", "").strip()
        if env:
            tickers = [t.strip().upper() for t in env.split(",") if t.strip()]
            return list(enumerate(tickers[:30], start=1))

        logger.warning("Toss top30 unavailable for %s — set TOSS_TOP30_TICKERS or implement scraper", on)
        return []

    def close(self) -> None:
        pass
