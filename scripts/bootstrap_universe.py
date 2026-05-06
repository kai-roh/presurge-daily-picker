"""universe 테이블 1회성 적재.

Polygon /v3/reference/tickers (list)는 market_cap을 반환하지 않으므로 2단계로 동작:
  Stage 1 — list endpoint 페이지네이션 + 종목 종류/거래소 1차 필터 (저렴, 페이지당 1콜)
  Stage 2 — 살아남은 후보 각각 /v3/reference/tickers/{T} (details) 호출해 market_cap·shares
            획득 후 [MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD] 필터 → SQLite upsert

대규모 호출이라 --checkpoint-every 단위로 부분 commit. 같은 UTC 날짜에 이미 갱신된
ticker는 기본적으로 details 재호출을 건너뜀 (--force 로 우회).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, POLYGON_RPS, Settings
from src.ingest.polygon_bars import PolygonBars
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def is_common_stock(t: dict[str, Any]) -> bool:
    return (t.get("type") or "").upper() in {"CS", "ADRC"}


def in_us_exchange(t: dict[str, Any]) -> bool:
    return (t.get("primary_exchange") or "").upper() in {
        "XNAS", "XNYS", "XASE", "ARCX", "BATS",
    }


def to_candidate(t: dict[str, Any]) -> dict[str, Any] | None:
    """Stage 1: market_cap 없이 가능한 1차 필터."""
    if not t.get("ticker"):
        return None
    if not is_common_stock(t) or not in_us_exchange(t):
        return None
    return t


def enrich_to_universe_row(
    candidate: dict[str, Any], details: dict[str, Any]
) -> dict[str, Any] | None:
    """Stage 2: list+details 결합 + 시총 범위 필터."""
    mcap = details.get("market_cap")
    if not mcap:
        return None
    if not (MARKET_CAP_MIN_USD <= mcap <= MARKET_CAP_MAX_USD):
        return None
    ticker = candidate.get("ticker") or details.get("ticker")
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "name": details.get("name") or candidate.get("name") or "",
        "market_cap_usd": float(mcap),
        "float_shares": details.get("share_class_shares_outstanding"),
        "exchange": details.get("primary_exchange") or candidate.get("primary_exchange"),
        "sector": details.get("sic_description")
        or details.get("type")
        or candidate.get("type"),
        "is_common_stock": 1,
        "historical_max_mcap": None,
        "last_refreshed": datetime.utcnow().isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="0=all. Stage 1 후보 N개까지만 details 호출 (smoke test 용)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 오늘 last_refreshed된 ticker도 다시 details 호출",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=200,
        help="N건 모일 때마다 upsert (크래시 회복용)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.polygon_api_key:
        logger.error("POLYGON_API_KEY required")
        return 2

    db = get_db(settings.database_url)
    pg = PolygonBars(settings.polygon_api_key)
    today_iso = datetime.utcnow().date().isoformat()

    started = time.monotonic()
    total_upserted = 0
    fetched = 0
    skipped_cached = 0
    skipped_outside_range = 0
    failed = 0

    try:
        # ----- Stage 1: list endpoint 1차 필터 -----
        logger.info("Stage 1: paginating /v3/reference/tickers (limit=%d, rps=%d)",
                    args.limit, POLYGON_RPS)
        candidates: list[dict[str, Any]] = []
        for t in pg.list_tickers():
            cand = to_candidate(t)
            if cand:
                candidates.append(cand)
            if args.limit and len(candidates) >= args.limit:
                break
        logger.info("Stage 1 done: %d candidates after type/exchange filter", len(candidates))

        # ----- Resumability: 오늘 이미 갱신된 ticker는 details 호출 스킵 -----
        skip: set[str] = set()
        if not args.force:
            skip = db.universe_refreshed_on(today_iso)
            if skip:
                logger.info(
                    "Skipping %d tickers already refreshed on %s (use --force to override)",
                    len(skip),
                    today_iso,
                )

        to_fetch = [c for c in candidates if c.get("ticker") not in skip]
        logger.info(
            "Stage 2: fetching details for %d tickers (~%.1f min @ %d rps)",
            len(to_fetch),
            len(to_fetch) / max(POLYGON_RPS, 1) / 60.0,
            POLYGON_RPS,
        )

        # ----- Stage 2: details enrichment + mcap 필터 -----
        buffer: list[dict[str, Any]] = []
        for i, cand in enumerate(to_fetch, 1):
            ticker = cand["ticker"]
            try:
                details = pg.ticker_details(ticker)
            except Exception as exc:
                failed += 1
                logger.warning("details fetch failed for %s: %s", ticker, exc)
                continue
            fetched += 1
            row = enrich_to_universe_row(cand, details)
            if row is None:
                skipped_outside_range += 1
                continue
            buffer.append(row)

            if len(buffer) >= args.checkpoint_every:
                n = db.upsert_universe(buffer)
                total_upserted += n
                buffer = []
                elapsed = time.monotonic() - started
                logger.info(
                    "checkpoint @ %d/%d (%.0fs elapsed): upserted=%d, fetched=%d, "
                    "out-of-range=%d, failed=%d",
                    i,
                    len(to_fetch),
                    elapsed,
                    total_upserted,
                    fetched,
                    skipped_outside_range,
                    failed,
                )

        if buffer:
            n = db.upsert_universe(buffer)
            total_upserted += n

        skipped_cached = len(skip & {c["ticker"] for c in candidates})
        elapsed = time.monotonic() - started
        logger.info(
            "Done in %.0fs. candidates=%d, fetched=%d, upserted=%d, "
            "out-of-range=%d, cached_skipped=%d, failed=%d",
            elapsed,
            len(candidates),
            fetched,
            total_upserted,
            skipped_outside_range,
            skipped_cached,
            failed,
        )
    finally:
        pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
