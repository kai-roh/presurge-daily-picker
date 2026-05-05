"""24개월 historical 일봉 백필.

universe 적재 후 실행. Polygon grouped daily endpoint를 영업일별로 호출하여
universe 종목만 필터해 daily_bars에 적재.

실행:
    python -m scripts.backfill_bars --start 2024-05-01 --end 2026-05-01

Polygon free tier: 5 req/min → 504 영업일 / 5 = ~100분 소요. Stocks Starter는 즉시.
멱등: 이미 있는 (ticker, trade_date)는 INSERT OR REPLACE.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, Settings
from src.ingest.polygon_bars import PolygonBars
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def trading_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="ISO date e.g. 2024-05-01")
    parser.add_argument("--end", required=True, help="ISO date")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = trading_days(start, end)
    logger.info("Backfilling %d trading days from %s to %s", len(days), start, end)

    settings = Settings.from_env()
    if not settings.polygon_api_key:
        logger.error("POLYGON_API_KEY required")
        return 2

    db = get_db(settings.database_url)
    allowed = set(db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD))
    if not allowed:
        logger.error("universe is empty — run scripts.bootstrap_universe first")
        return 3

    pg = PolygonBars(settings.polygon_api_key)
    total = 0
    try:
        for i, d in enumerate(days, start=1):
            try:
                rows = pg.grouped_daily(d)
                rows = pg.filter_universe(rows, allowed)
                inserted = db.upsert_bars(r for r in rows if r.get("ticker"))
                total += inserted
                if i % 20 == 0:
                    logger.info("Progress %d/%d days, %d bars inserted", i, len(days), total)
            except Exception as exc:
                logger.warning("Skipping %s: %s", d, exc)
    finally:
        pg.close()

    logger.info("Backfill complete: %d total bar rows", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
