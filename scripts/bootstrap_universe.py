"""universe 테이블 1회성 적재 — Finnhub 경로.

Polygon 무료 티어가 details 호출(5/min)에서 즉시 429를 내므로 universe bootstrap은
Finnhub로 우회한다. (Polygon은 grouped daily 한 콜만 쓰는 daily_pick에 계속 사용)

Stage 1 — /stock/symbol?exchange=US (1 콜) → type ∈ {Common Stock, ADR}
          + mic ∈ {XNAS, XNYS, XASE, ARCX, BATS} 로 1차 필터.
Stage 2 — 후보별 /stock/profile2?symbol=X 로 marketCapitalization·shareOutstanding 획득
          → [MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD] 필터 후 SQLite upsert.

특징:
- --checkpoint-every N: N건마다 부분 commit (크래시/중단 회복)
- --force 미지정 시 같은 UTC 날짜에 이미 last_refreshed된 ticker는 enrichment 스킵
- --limit N: Stage 1 후보 N개까지만 enrichment (smoke test용)
- Finnhub marketCapitalization 단위는 **백만 USD** → mcap_usd = value × 1_000_000
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from src.config import (
    FINNHUB_RPS,
    MARKET_CAP_MAX_USD,
    MARKET_CAP_MIN_USD,
    Settings,
)
from src.ingest.finnhub import Finnhub
from src.storage.db import get_db

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"Common Stock", "ADR"}
ALLOWED_MICS = {"XNAS", "XNYS", "XASE", "ARCX", "BATS"}

# Finnhub mcap 단위가 백만 USD라 floor/ceiling을 USD → M 으로 환산
MCAP_MIN_M = MARKET_CAP_MIN_USD / 1_000_000.0
MCAP_MAX_M = MARKET_CAP_MAX_USD / 1_000_000.0


def to_candidate(s: dict[str, Any]) -> dict[str, Any] | None:
    """Stage 1: type/mic 1차 필터 (mcap 없음)."""
    symbol = s.get("symbol") or s.get("displaySymbol")
    if not symbol:
        return None
    if s.get("type") not in ALLOWED_TYPES:
        return None
    if s.get("mic") not in ALLOWED_MICS:
        return None
    return {
        "ticker": symbol,
        "name": s.get("description") or "",
        "exchange": s.get("mic"),
        "type": s.get("type"),
    }


def enrich_to_universe_row(
    candidate: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    """Stage 2: profile2 결합 + 시총 범위 필터."""
    mcap_m = profile.get("marketCapitalization")
    if mcap_m is None or mcap_m <= 0:
        return None
    if not (MCAP_MIN_M <= mcap_m <= MCAP_MAX_M):
        return None
    ticker = candidate["ticker"]
    shares_m = profile.get("shareOutstanding")
    float_shares = int(shares_m * 1_000_000) if shares_m else None
    return {
        "ticker": ticker,
        "name": profile.get("name") or candidate.get("name") or "",
        "market_cap_usd": float(mcap_m) * 1_000_000.0,
        "float_shares": float_shares,
        "exchange": candidate.get("exchange"),
        "sector": profile.get("finnhubIndustry") or candidate.get("type"),
        "is_common_stock": 1 if candidate.get("type") == "Common Stock" else 0,
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
        help="0=all. Stage 1 후보 N개까지만 profile2 호출 (smoke test 용)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 오늘 last_refreshed된 ticker도 다시 profile2 호출",
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
    if not settings.finnhub_api_key:
        logger.error("FINNHUB_API_KEY required")
        return 2

    db = get_db(settings.database_url)
    fh = Finnhub(settings.finnhub_api_key, rps=FINNHUB_RPS)
    today_iso = datetime.utcnow().date().isoformat()

    started = time.monotonic()
    total_upserted = 0
    fetched = 0
    skipped_outside_range = 0
    failed = 0

    try:
        # ----- Stage 1: /stock/symbol?exchange=US (1 call) -----
        logger.info("Stage 1: /stock/symbol?exchange=US")
        all_symbols = fh.stock_symbols(exchange="US")
        candidates: list[dict[str, Any]] = []
        for s in all_symbols:
            cand = to_candidate(s)
            if cand:
                candidates.append(cand)
            if args.limit and len(candidates) >= args.limit:
                break
        logger.info(
            "Stage 1 done: %d candidates after type/mic filter (from %d total)",
            len(candidates),
            len(all_symbols),
        )

        # ----- Resumability -----
        skip: set[str] = set()
        if not args.force:
            skip = db.universe_refreshed_on(today_iso)
            if skip:
                logger.info(
                    "Skipping %d tickers already refreshed on %s (use --force to override)",
                    len(skip),
                    today_iso,
                )

        to_fetch = [c for c in candidates if c["ticker"] not in skip]
        eta_min = len(to_fetch) / max(FINNHUB_RPS * 60, 1)
        logger.info(
            "Stage 2: profile2 enrichment for %d tickers (~%.1f min @ %d/sec)",
            len(to_fetch),
            eta_min,
            FINNHUB_RPS,
        )

        # ----- Stage 2: profile2 enrichment + mcap 필터 -----
        buffer: list[dict[str, Any]] = []
        for i, cand in enumerate(to_fetch, 1):
            ticker = cand["ticker"]
            try:
                profile = fh.company_profile2(ticker)
            except Exception as exc:
                failed += 1
                logger.warning("profile2 failed for %s: %s", ticker, exc)
                continue
            fetched += 1
            row = enrich_to_universe_row(cand, profile)
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
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
