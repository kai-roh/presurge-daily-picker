"""DB에 적재된 미분류 8-K filing을 Claude로 분류 (멀티스레드).

backfill_filings 메타가 64K건 적재된 상태에서, items 1.01 / 1.02 (Pattern A/C 핵심)만
대상으로 분류한다. 8.01은 v0.2에서 keyword-matching fallback (PATTERN_A_KEYWORDS 등)
으로 처리하고 분류 비용에서 제외.

비용 추정 (Haiku 4.5):
- 9,140 filings × 약 $0.0026/call (3K input + 200 output) ≈ **$23**
- prompt caching (system 프롬프트 캐시) 활용 시 입력 토큰 90%+ 캐시 적용

병렬: --workers N (기본 4). SEC RateLimiter는 thread-safe 하므로 단일 HttpClient 공유.
DB upsert는 자체 lock으로 직렬화.

실행:
    python -m scripts.classify_filings [--limit N] [--items 1.01,1.02]
                                        [--workers 4] [--max-cost-usd 50]
                                        [--dry-run]

멱등: classification IS NULL 인 row만 처리. 이미 분류된 건 스킵.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from dotenv import load_dotenv

from src.config import SEC_RPS, Settings
from src.ingest._http import HttpClient
from src.report.claude_summarizer import ClaudeSummarizer
from src.storage.db import get_db

logger = logging.getLogger(__name__)

# Haiku 4.5 단가 (USD/1M tokens) - 2026년 추정
HAIKU_INPUT_PER_M = 0.80
HAIKU_OUTPUT_PER_M = 4.00
SONNET_INPUT_PER_M = 3.00
SONNET_OUTPUT_PER_M = 15.00


def estimate_cost_per_call(input_tokens: int = 2200, output_tokens: int = 200,
                            haiku: bool = True) -> float:
    if haiku:
        return (
            input_tokens * HAIKU_INPUT_PER_M / 1_000_000
            + output_tokens * HAIKU_OUTPUT_PER_M / 1_000_000
        )
    return (
        input_tokens * SONNET_INPUT_PER_M / 1_000_000
        + output_tokens * SONNET_OUTPUT_PER_M / 1_000_000
    )


def _build_body_url(cik_raw: str, accession: str) -> str:
    cik_int = (cik_raw or "").lstrip("0") or (cik_raw or "")
    accession_dashless = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_dashless}/{accession}.txt"
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--items", default="1.01,1.02")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-cost-usd", type=float, default=50.0)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4, help="병렬 worker 수")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY required")
        return 2

    items_filter = [it.strip() for it in args.items.split(",") if it.strip()]
    if not items_filter:
        logger.error("--items must include at least one")
        return 2

    db = get_db(settings.database_url)

    where_clauses = " OR ".join(["items LIKE ?" for _ in items_filter])
    params: list[Any] = [f"%{it}%" for it in items_filter]
    sql = (
        f"SELECT accession_no, ticker, cik, items, raw_text_url FROM filings "
        f"WHERE classification IS NULL AND ({where_clauses}) "
        f"ORDER BY filed_at"
    )
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = db.conn.execute(sql, params).fetchall()
    logger.info(
        "Found %d unclassified filings matching items=%s", len(rows), items_filter
    )

    haiku = args.model.startswith("claude-haiku")
    cost_per = estimate_cost_per_call(haiku=haiku)
    est_total = cost_per * len(rows)
    logger.info(
        "Estimate per call ≈ $%.5f (model=%s, workers=%d) → total $%.2f for %d filings",
        cost_per, args.model, args.workers, est_total, len(rows),
    )
    if args.dry_run:
        logger.info("DRY RUN — exit before any HTTP/Claude call")
        return 0
    if est_total > args.max_cost_usd:
        logger.error(
            "Estimated cost $%.2f > --max-cost-usd $%.2f. Reduce scope or raise cap.",
            est_total, args.max_cost_usd,
        )
        return 4

    classifier = ClaudeSummarizer(
        api_key=settings.anthropic_api_key,
        classify_model=args.model,
    )
    sec_http = HttpClient(headers={"User-Agent": settings.sec_user_agent}, rps=SEC_RPS)

    db_lock = threading.Lock()
    progress_lock = threading.Lock()
    cost_stop_event = threading.Event()
    counters = {
        "classified": 0,
        "no_body": 0,
        "fail": 0,
        "cost": 0.0,
    }
    started = time.monotonic()

    def process_one(row: Any) -> None:
        if cost_stop_event.is_set():
            return
        accession = row["accession_no"]
        ticker = row["ticker"]
        items = row["items"] or ""
        body_url = _build_body_url(row["cik"] or "", accession)
        try:
            body_text = sec_http.get(body_url).text
        except Exception as exc:
            logger.warning("body fetch failed %s: %s", accession, exc)
            with progress_lock:
                counters["no_body"] += 1
            return
        try:
            res = classifier.classify_filing(ticker, items, body_text)
        except Exception as exc:
            logger.warning("classify exc for %s: %s", accession, exc)
            with progress_lock:
                counters["fail"] += 1
            return
        with db_lock:
            db.update_filing_classification(
                accession,
                classification=",".join(res.patterns),
                confidence=res.confidence,
                contract_value_usd=res.contract_value_usd,
                counterparty=res.counterparty,
                key_quote=res.key_quote,
            )
        with progress_lock:
            counters["classified"] += 1
            counters["cost"] += cost_per
            cur_cost = counters["cost"]
            cur_done = counters["classified"]
            cur_no_body = counters["no_body"]
            cur_fail = counters["fail"]
        if cur_cost > args.max_cost_usd:
            cost_stop_event.set()
            logger.error("Cost cap hit at $%.2f, draining workers", cur_cost)
        if cur_done % args.checkpoint_every == 0:
            elapsed = time.monotonic() - started
            rate = cur_done / elapsed if elapsed > 0 else 0
            logger.info(
                "checkpoint @ %d/%d (%.0fs, %.2f/sec, %d workers): est_cost=$%.2f, "
                "no_body=%d, fail=%d",
                cur_done, len(rows), elapsed, rate, args.workers, cur_cost,
                cur_no_body, cur_fail,
            )

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(process_one, row) for row in rows]
            for f in as_completed(futures):
                # 예외는 process_one 내부에서 잡지만 안전망
                exc = f.exception()
                if exc is not None:
                    logger.warning("worker exc: %s", exc)
    finally:
        sec_http.close()

    elapsed = time.monotonic() - started
    logger.info(
        "Done in %.0fs. classified=%d, no_body=%d, fail=%d, est_cost=$%.2f",
        elapsed, counters["classified"], counters["no_body"], counters["fail"],
        counters["cost"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
