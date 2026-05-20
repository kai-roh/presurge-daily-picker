"""장중 monitor 실행 entrypoint.

예:
    python -m scripts.run_intraday_monitor --dry-run --once --force-market-closed
    python -m scripts.run_intraday_monitor --loop
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.config import Settings
from src.intraday.calendar import session_for
from src.intraday.monitor import IntradayMonitor
from src.storage.db import get_db


def _setup_logging() -> None:
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("data/intraday_monitor.log"),
            logging.StreamHandler(),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    _setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="한 번만 실행")
    parser.add_argument("--loop", action="store_true", help="정규장 동안 5분 loop")
    parser.add_argument("--dry-run", action="store_true", help="DB 저장은 하되 Telegram 발송 안 함")
    parser.add_argument(
        "--force-market-closed",
        action="store_true",
        help="시장 시간 밖에서도 실행. smoke/dry-run 전용",
    )
    args = parser.parse_args(argv)

    if not args.once and not args.loop:
        parser.error("--once 또는 --loop 중 하나가 필요합니다")

    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True
    if not settings.intraday_enabled and not args.dry_run:
        logging.info("INTRADAY_ENABLED is not set; exiting without live alerts")
        return 0
    db = get_db(settings.database_url)
    monitor = IntradayMonitor(db, settings)

    session = session_for(include_extended=settings.intraday_include_extended_hours)
    if args.once:
        enforce_session = (
            settings.intraday_regular_session_only
            or settings.intraday_include_extended_hours
        )
        if enforce_session and not session.is_open and not args.force_market_closed:
            logging.info("market closed for %s; use --force-market-closed for smoke", session.trade_date)
            return 0
        n = monitor.run_once(session.trade_date, session.now_et, dry_run=args.dry_run)
        logging.info("intraday once done: signals=%d", n)
        return 0

    n = monitor.run_loop(force_market_closed=args.force_market_closed, dry_run=args.dry_run)
    logging.info("intraday loop done: signals=%d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
