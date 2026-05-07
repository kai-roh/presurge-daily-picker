"""W4 #3 — Russell 2000 reconstitution events seed.

iShares IWM (Russell 2000 ETF) 공식 endpoint가 asOfDate 파라미터를 지원하는 점을 이용:
  https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/...?asOfDate=YYYYMMDD

reconstitution 전/후 holdings diff = 새로 편입된 ticker.
2024 reconstitution: announced 2024-05-24, effective 2024-06-28.
2025 reconstitution: announced 2025-05-23, effective 2025-06-27.

universe와 교집합인 ticker만 index_inclusion_events에 적재 (Pattern B 활성화).
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from datetime import date

import httpx
from dotenv import load_dotenv

from src.storage.db import get_db

logger = logging.getLogger(__name__)

IWM_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
    "1467271812596.ajax"
)


# Russell 2000 reconstitution 일정 (FTSE Russell 공식)
RECONSTITUTIONS = [
    {
        "year": 2024,
        "pre_date": date(2024, 5, 31),     # reconstitution 전 holdings
        "post_date": date(2024, 7, 1),      # 후 holdings
        "announced_at": date(2024, 5, 24),  # 공식 발표일
        "effective_at": date(2024, 6, 28),  # 적용일
    },
    {
        "year": 2025,
        "pre_date": date(2025, 5, 30),
        "post_date": date(2025, 7, 1),
        "announced_at": date(2025, 5, 23),
        "effective_at": date(2025, 6, 27),
    },
]


def fetch_holdings(as_of: date) -> set[str]:
    """iShares IWM holdings 받아 ticker set 반환."""
    params = {
        "fileType": "csv",
        "fileName": "IWM_holdings",
        "dataType": "fund",
        "asOfDate": as_of.strftime("%Y%m%d"),
    }
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        resp = c.get(IWM_HOLDINGS_URL, params=params)
        resp.raise_for_status()
        text = resp.text
    # 첫 10줄은 메타. 11번째 줄부터 헤더 + ticker rows
    lines = text.splitlines()
    # "Ticker,Name,..." header line 찾기
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("Ticker,")), None
    )
    if header_idx is None:
        return set()
    csv_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))
    tickers: set[str] = set()
    for row in reader:
        t = (row.get("Ticker") or "").strip()
        if t and t != "-":
            tickers.add(t.upper())
    return tickers


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db = get_db()
    universe_tickers = {
        r["ticker"]
        for r in db.conn.execute("SELECT ticker FROM universe").fetchall()
    }
    logger.info("universe size: %d", len(universe_tickers))

    inserted_total = 0
    for recon in RECONSTITUTIONS:
        logger.info("Russell %s reconstitution: pre=%s post=%s",
                    recon["year"], recon["pre_date"], recon["post_date"])
        try:
            pre = fetch_holdings(recon["pre_date"])
            post = fetch_holdings(recon["post_date"])
        except Exception as exc:
            logger.warning("fetch failed for %s: %s", recon["year"], exc)
            continue
        added = post - pre
        added_in_universe = added & universe_tickers
        logger.info(
            "  pre=%d post=%d added=%d  (intersect universe = %d)",
            len(pre), len(post), len(added), len(added_in_universe),
        )

        if args.dry_run:
            sample = sorted(added_in_universe)[:10]
            logger.info("  dry-run sample additions: %s", sample)
            continue

        for ticker in sorted(added_in_universe):
            row = {
                "ticker": ticker,
                "index_name": "Russell 2000",
                "announced_at": recon["announced_at"].isoformat(),
                "effective_at": recon["effective_at"].isoformat(),
                "source": "ishares_iwm_diff",
                "notes": f"Russell 2000 {recon['year']} reconstitution",
            }
            try:
                db.upsert_index_event(row)
                inserted_total += 1
            except Exception as exc:
                logger.warning("upsert failed %s: %s", ticker, exc)

    logger.info("Total inserted: %d events", inserted_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
