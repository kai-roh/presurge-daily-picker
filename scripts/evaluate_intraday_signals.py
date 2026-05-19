"""장중 signal outcome 평가."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from dotenv import load_dotenv

from src.config import Settings
from src.intraday.outcomes import evaluate_pending_signals
from src.storage.db import get_db


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD. 미지정 시 모든 미평가 signal")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)
    trade_date = date.fromisoformat(args.date) if args.date else None
    n = evaluate_pending_signals(db, trade_date)
    logging.info("evaluated intraday signals: %d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

