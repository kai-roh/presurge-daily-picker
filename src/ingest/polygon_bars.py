"""Polygon.io 일봉 fetcher.

grouped daily endpoint (/v2/aggs/grouped/locale/us/market/stocks/{date})
로 1콜에 전체 시장 어제 종가를 받는다. universe 매칭은 호출자 책임.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from src.config import POLYGON_RPS
from src.ingest._http import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://api.polygon.io"


class PolygonBars:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.http = HttpClient(base_url=BASE_URL, rps=POLYGON_RPS)

    def grouped_daily(self, trade_date: date, adjusted: bool = True) -> list[dict[str, Any]]:
        """미국 주식 시장 전체 종가. 주말/휴일은 빈 결과."""
        params = {
            "adjusted": "true" if adjusted else "false",
            "apiKey": self.api_key,
        }
        url = f"/v2/aggs/grouped/locale/us/market/stocks/{trade_date.isoformat()}"
        resp = self.http.get(url, params=params)
        body = resp.json()
        results = body.get("results", []) or []
        out = []
        for r in results:
            out.append(
                {
                    "ticker": r.get("T"),
                    "trade_date": trade_date.isoformat(),
                    "open": r.get("o"),
                    "high": r.get("h"),
                    "low": r.get("l"),
                    "close": r.get("c"),
                    "volume": r.get("v"),
                    "vwap": r.get("vw"),
                }
            )
        logger.info("Polygon grouped daily %s -> %d rows", trade_date, len(out))
        return out

    def filter_universe(
        self, rows: Iterable[dict[str, Any]], allowed: set[str]
    ) -> list[dict[str, Any]]:
        return [r for r in rows if r.get("ticker") in allowed]

    def ticker_details(self, ticker: str) -> dict[str, Any]:
        params = {"apiKey": self.api_key}
        url = f"/v3/reference/tickers/{ticker}"
        resp = self.http.get(url, params=params)
        return resp.json().get("results", {}) or {}

    def list_tickers(
        self, market: str = "stocks", active: bool = True, limit: int = 1000
    ) -> Iterable[dict[str, Any]]:
        """페이지네이션. universe bootstrap 용."""
        params: dict[str, Any] = {
            "market": market,
            "active": "true" if active else "false",
            "limit": limit,
            "apiKey": self.api_key,
        }
        url = "/v3/reference/tickers"
        while True:
            resp = self.http.get(url, params=params)
            body = resp.json()
            yield from (body.get("results", []) or [])
            next_url = body.get("next_url")
            if not next_url:
                break
            url = next_url
            params = {"apiKey": self.api_key}

    def previous_trading_day(self, anchor: date) -> date:
        """주말/공휴일 단순 처리 (월요일이면 금요일). 정밀 캘린더는 v0.3."""
        d = anchor - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def close(self) -> None:
        self.http.close()


def utc_today() -> date:
    return datetime.utcnow().date()
