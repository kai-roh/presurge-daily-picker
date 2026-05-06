"""Yahoo 기반 short interest snapshot.

전략 §5.3 의 v0.2 MVP 단순화: FINRA 직배포 API의 OTC 한정 + 필터 미작동 이슈로
Yahoo Finance(yfinance) 스크래핑 경로를 채택한다. 한계:

- 현재 snapshot 1건만 (가장 최근 FINRA settle_date)
- DTC = `shortRatio`, SI%float = `shortPercentOfFloat` (소수, ×1)
- cost_to_borrow는 무료로 미제공 → NULL
- 24개월 backtest 시 historical SI 부재 → Pattern D는 backtest에서 score=0 으로 처리
  (W4에 Ortex 또는 다른 historical 소스 도입 검토)

Rate limit: 1/sec (Yahoo가 너무 빠르면 차단)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

DEFAULT_DELAY_SEC = 1.0


def _epoch_to_iso(epoch: int | float | None) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.utcfromtimestamp(int(epoch)).date().isoformat()
    except (OSError, ValueError, TypeError):
        return None


class YahooShortInterest:
    """Per-ticker SI fetch. yfinance Ticker.info 호출."""

    def __init__(self, delay_sec: float = DEFAULT_DELAY_SEC):
        self.delay_sec = delay_sec

    def fetch(self, ticker: str) -> dict[str, Any] | None:
        """short_interest 테이블 row dict 반환. 데이터 없으면 None."""
        try:
            info = yf.Ticker(ticker).info
        except Exception as exc:
            logger.warning("yfinance failed for %s: %s", ticker, exc)
            return None
        finally:
            if self.delay_sec > 0:
                time.sleep(self.delay_sec)

        si_shares = info.get("sharesShort")
        if si_shares is None:
            return None
        settle_iso = _epoch_to_iso(info.get("dateShortInterest"))
        if not settle_iso:
            return None

        return {
            "ticker": ticker,
            "settle_date": settle_iso,
            "si_shares": int(si_shares),
            "si_pct_float": info.get("shortPercentOfFloat"),  # 소수 (0.31 = 31%)
            "days_to_cover": info.get("shortRatio"),
            "cost_to_borrow": None,  # 무료 미제공
            "source": "yahoo",
        }
