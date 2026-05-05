"""universe 테이블 1회성 적재.

Polygon /v3/reference/tickers + 시총 필터 → SQLite 적재.
주 1회 일요일 GitHub Actions로도 실행 가능.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, Settings
from src.ingest.polygon_bars import PolygonBars
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def is_common_stock(t: dict[str, Any]) -> bool:
    return (t.get("type") or "").upper() in {"CS", "ADRC"}


def in_us_exchange(t: dict[str, Any]) -> bool:
    return (t.get("primary_exchange") or "").upper() in {
        "XNAS", "XNYS", "XASE", "ARCX", "BATS",
    }


def to_universe_row(t: dict[str, Any]) -> dict[str, Any]:
    mcap = t.get("market_cap")
    if not mcap or not (MARKET_CAP_MIN_USD <= mcap <= MARKET_CAP_MAX_USD):
        return {}
    if not is_common_stock(t) or not in_us_exchange(t):
        return {}
    return {
        "ticker": t.get("ticker"),
        "name": t.get("name") or "",
        "market_cap_usd": float(mcap),
        "float_shares": t.get("share_class_shares_outstanding"),
        "exchange": t.get("primary_exchange"),
        "sector": t.get("sic_description") or t.get("type"),
        "is_common_stock": 1,
        "historical_max_mcap": None,
        "last_refreshed": datetime.utcnow().isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0=all")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.polygon_api_key:
        logger.error("POLYGON_API_KEY required")
        return 2

    db = get_db(settings.database_url)
    pg = PolygonBars(settings.polygon_api_key)
    try:
        rows: list[dict[str, Any]] = []
        for i, t in enumerate(pg.list_tickers()):
            mapped = to_universe_row(t)
            if mapped and mapped.get("ticker"):
                rows.append(mapped)
            if args.limit and i >= args.limit:
                break
        n = db.upsert_universe(rows)
        logger.info("universe upserted: %d rows", n)
    finally:
        pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
