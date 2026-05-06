"""Universe 전체 ticker의 최신 SI snapshot 적재.

Yahoo Finance 경로 (전략 §5.3 fallback). 격주 settle_date 기준 1건/ticker.
Universe ~3,740 × 1 sec/call ≈ 60분.

실행:
    python -m scripts.snapshot_short_interest [--limit 50] [--force]

멱등: (ticker, settle_date, source) PK라 같은 settle_date면 업데이트.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, Settings
from src.ingest.yahoo_si import YahooShortInterest
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0=all (universe 전체)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 같은 settle_date로 적재된 ticker도 다시 호출",
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=200, help="N건마다 commit"
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=1.0,
        help="yfinance 호출 간 sleep (Yahoo rate limit 회피)",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)
    universe = db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD)
    if not universe:
        logger.error("universe is empty — run scripts.bootstrap_universe first")
        return 3
    if args.limit:
        universe = universe[: args.limit]
    logger.info("Snapshotting SI for %d tickers", len(universe))

    fetcher = YahooShortInterest(delay_sec=args.delay_sec)
    started = time.monotonic()
    fetched = 0
    no_data = 0
    failed = 0
    buffer: list[dict[str, Any]] = []

    try:
        for i, ticker in enumerate(universe, 1):
            try:
                row = fetcher.fetch(ticker)
            except Exception as exc:
                failed += 1
                logger.warning("fetch failed for %s: %s", ticker, exc)
                continue
            fetched += 1
            if row is None:
                no_data += 1
                continue
            buffer.append(row)

            if len(buffer) >= args.checkpoint_every:
                db.upsert_short_interest(buffer)
                elapsed = time.monotonic() - started
                logger.info(
                    "checkpoint @ %d/%d (%.0fs): rows=%d, no_data=%d, failed=%d",
                    i,
                    len(universe),
                    elapsed,
                    len(buffer),
                    no_data,
                    failed,
                )
                buffer = []

        if buffer:
            db.upsert_short_interest(buffer)

        elapsed = time.monotonic() - started
        logger.info(
            "Done in %.0fs. fetched=%d, with_si=%d, no_data=%d, failed=%d",
            elapsed,
            fetched,
            fetched - no_data,
            no_data,
            failed,
        )
    finally:
        pass
    _ = datetime.utcnow()
    return 0


if __name__ == "__main__":
    sys.exit(main())
