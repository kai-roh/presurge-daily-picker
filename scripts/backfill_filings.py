"""24개월 historical 8-K 백필 + Claude 분류.

EDGAR full-index (quarterly index files)에서 8-K 메타데이터를 받아
universe 종목만 필터, 본문 lazy fetch + Claude classify.

실행:
    python -m scripts.backfill_filings --start 2024-05-01 --end 2026-05-01
                                        [--no-classify]    # 분류 생략 (메타만)
                                        [--max-classify 5000]

기존 EDGAR full-text search API:
    https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom&...

비용 추정: 50K filings × Claude Sonnet $0.01/call = $500. universe filter 후 5K → $50.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, SEC_RPS, Settings
from src.ingest._http import HttpClient
from src.report.claude_summarizer import ClaudeSummarizer
from src.storage.db import get_db

# efts hits._source.display_names 포맷:
#   "ESSENTIAL PROPERTIES REALTY TRUST, INC.  (EPRT)  (CIK 0001728951)"
#   "GLADSTONE LAND Corp  (LAND, LANDO, LANDP)  (CIK 0001495240)"
# (TICKER[, TICKER, ...]) 직후 (CIK NNNN) 가 붙는 패턴.
_TICKER_GROUP_RE = re.compile(r"\(([^)]+)\)\s*\(CIK\s*\d+\)")


def extract_tickers_from_display_name(display_name: str) -> list[str]:
    m = _TICKER_GROUP_RE.search(display_name or "")
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]

logger = logging.getLogger(__name__)

EDGAR_FULL_TEXT_API = "https://efts.sec.gov/LATEST/search-index"


@dataclass
class FilingMeta:
    accession_no: str
    cik: str
    ticker: str
    filed_at: str
    items: str
    body_url: str


def _iter_chunk(http: HttpClient, start: date, end: date) -> Iterator[dict[str, Any]]:
    """단일 (start, end) 구간의 efts 검색 결과 페이지네이션."""
    page_from = 0
    while True:
        params = {
            "q": "",
            "forms": "8-K",
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": str(page_from),
        }
        try:
            resp = http.get(EDGAR_FULL_TEXT_API, params=params)
        except Exception as exc:
            # efts는 깊은 페이지네이션에서 500 빈발 → 해당 chunk 종료
            logger.warning(
                "efts page failed at %s..%s from=%d: %s — chunk truncated",
                start, end, page_from, exc,
            )
            return
        body = resp.json()
        hits = body.get("hits", {}).get("hits", []) or []
        if not hits:
            return
        yield from hits
        page_from += len(hits)
        total = body.get("hits", {}).get("total", {}).get("value", 0) or 0
        if page_from >= total:
            return
        # efts는 from>=1000 부근에서 cursor depth 제한으로 500 → 보호적으로 break
        if page_from >= 900:
            logger.warning(
                "efts depth guard %s..%s: stopping at from=%d (total=%d), shrink window",
                start, end, page_from, total,
            )
            return
        time.sleep(0.1)


def iter_filings(http: HttpClient, start: date, end: date) -> Iterator[dict[str, Any]]:
    """EDGAR full-text search 페이지네이션. efts depth 제한 회피용 주별 chunk.

    efts는 form=8-K + 24개월 query에서 ~100k건 매칭되며 from>=1000 부근에서 500.
    피크 (earnings season) 시 540 hits/day 까지 관측 → 1일 chunk가 안전.
    """
    chunk_days = 1
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        logger.info("efts chunk %s..%s", cur, chunk_end)
        yield from _iter_chunk(http, cur, chunk_end)
        cur = chunk_end + timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--no-classify", action="store_true")
    parser.add_argument("--max-classify", type=int, default=5000)
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    settings = Settings.from_env()
    db = get_db(settings.database_url)

    universe_set = set(db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD))
    if not universe_set:
        logger.error("universe is empty — run scripts.bootstrap_universe first")
        return 2

    http = HttpClient(headers={"User-Agent": settings.sec_user_agent}, rps=SEC_RPS)
    classifier = None
    if not args.no_classify and settings.anthropic_api_key:
        classifier = ClaudeSummarizer(
            api_key=settings.anthropic_api_key,
            classify_model=settings.claude_classify_model,
        )

    n_inserted = 0
    n_classified = 0
    try:
        for hit in iter_filings(http, start, end):
            src = hit.get("_source") or {}
            tickers = src.get("tickers") or []
            if not tickers:
                # 현 efts schema는 tickers=null. display_names에서 파싱
                for dn in src.get("display_names") or []:
                    tickers.extend(extract_tickers_from_display_name(dn))
            ticker = next((t for t in tickers if t in universe_set), None)
            if not ticker:
                continue
            accession = (hit.get("_id") or "").split(":")[0]
            if not accession:
                continue
            row = {
                "accession_no": accession,
                "ticker": ticker,
                "cik": (src.get("ciks") or [""])[0],
                "filed_at": src.get("file_date") or src.get("ad_sh") or "",
                "form_type": "8-K",
                "items": ",".join(src.get("items") or []),
                "raw_text_url": f"https://www.sec.gov/Archives/edgar/data/{(src.get('ciks') or [''])[0]}/{accession.replace('-','')}",
            }
            db.upsert_filings([row])
            n_inserted += 1

            if classifier and n_classified < args.max_classify:
                try:
                    body_text = http.get(row["raw_text_url"]).text
                    res = classifier.classify_filing(ticker, row["items"], body_text)
                    db.update_filing_classification(
                        accession,
                        classification=",".join(res.patterns),
                        confidence=res.confidence,
                        contract_value_usd=res.contract_value_usd,
                        counterparty=res.counterparty,
                        key_quote=res.key_quote,
                    )
                    n_classified += 1
                except Exception as exc:
                    logger.warning("classify failed for %s: %s", accession, exc)

            if n_inserted % 100 == 0:
                logger.info("inserted=%d classified=%d", n_inserted, n_classified)
    finally:
        http.close()

    logger.info("done. inserted=%d classified=%d", n_inserted, n_classified)
    return 0


if __name__ == "__main__":
    sys.exit(main())
