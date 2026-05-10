"""universe 전체 옵션 활동 weekly snapshot 적재.

historical options 무료 데이터 부재로 forward only 누적. yfinance 가까운 1개
만기 call/put volume + OI를 ticker별 1콜로 가져와 options_activity 테이블에 적재.

Sunday 02:00 KST launchd cron 권장 (PC가 켜져있는 시간 가정).

실행:
    python -m scripts.ingest_options_snapshot
                    [--limit N]      # smoke test
                    [--workers 4]
                    [--mcap-max 5e9] # 시총 cap (대형주 제외)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, Settings
from src.ingest.yfinance_options import fetch_options_snapshot, to_db_row
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mcap-max", type=float, default=MARKET_CAP_MAX_USD)
    parser.add_argument("--mcap-min", type=float, default=MARKET_CAP_MIN_USD)
    parser.add_argument("--checkpoint-every", type=int, default=200)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    universe = db.universe_tickers(args.mcap_min, args.mcap_max)
    if args.limit:
        universe = universe[: args.limit]
    snap_date = date.today()
    logger.info("Snapshotting options for %d tickers (workers=%d)",
                len(universe), args.workers)

    db_lock = threading.Lock()
    progress_lock = threading.Lock()
    counters = {"done": 0, "with_data": 0, "no_data": 0, "fail": 0}
    started = time.monotonic()

    def process(ticker: str) -> None:
        try:
            snap = fetch_options_snapshot(ticker)
        except Exception as exc:
            with progress_lock:
                counters["fail"] += 1
            logger.debug("fetch fail %s: %s", ticker, exc)
            return
        with progress_lock:
            counters["done"] += 1
        if not snap:
            with progress_lock:
                counters["no_data"] += 1
            return
        row = to_db_row(snap_date, ticker, snap)
        with db_lock:
            db.upsert_options_activity([row])
        with progress_lock:
            counters["with_data"] += 1
            done = counters["done"]
        if done % args.checkpoint_every == 0:
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            logger.info(
                "checkpoint @ %d/%d (%.0fs, %.2f/sec): with_data=%d no_data=%d fail=%d",
                done, len(universe), elapsed, rate,
                counters["with_data"], counters["no_data"], counters["fail"],
            )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, t) for t in universe]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                logger.warning("worker exc: %s", exc)

    elapsed = time.monotonic() - started
    logger.info(
        "Done in %.0fs. with_data=%d no_data=%d fail=%d",
        elapsed, counters["with_data"], counters["no_data"], counters["fail"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
