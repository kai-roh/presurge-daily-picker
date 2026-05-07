"""universe.historical_max_mcap 채우기 — Pattern E (Brand Penny) 자격 판단용.

yfinance Ticker.history(period='max', auto_adjust=True) 의 종가 max × DB에 저장된
float_shares (Finnhub bootstrap이 채운 share_class_shares_outstanding) 로
historical_max_mcap proxy를 계산.

한계:
- 일부 reverse split이 yfinance 데이터에 누락 → 비현실적 큰 값 (TNXP 등). 이 때문에
  $10T 상한을 두어 sane bound 유지. Pattern E 점수 계산은 ratio 기반이라 절대값
  영향 없음 (recovery_pct = current_mcap / historical_max).
- 30년 이상의 split history가 없는 신생 종목은 24개월 high 정도만 보임 → 자격 못 얻음.

실행:
    python -m scripts.backfill_historical_max_mcap [--limit N] [--workers 4] [--force]

멱등: historical_max_mcap이 이미 채워진 ticker는 --force 없으면 스킵.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf
from dotenv import load_dotenv

from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD, Settings
from src.storage.db import get_db

logger = logging.getLogger(__name__)

# Saudi Aramco peak 약 2T. 10T로 상한 두면 yfinance split 누락 케이스 보호.
MAX_HIST_MCAP_CAP = 10_000_000_000_000.0  # $10T


def fetch_max_close(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="max", auto_adjust=True)
    except Exception as exc:
        logger.warning("history failed %s: %s", ticker, exc)
        return None
    if hist is None or hist.empty:
        return None
    try:
        return float(hist["Close"].max())
    except (KeyError, ValueError):
        return None


def compute_max_mcap(max_close: float, shares: int | float | None) -> float | None:
    if not shares or shares <= 0 or not max_close or max_close <= 0:
        return None
    proxy = float(max_close) * float(shares)
    return min(proxy, MAX_HIST_MCAP_CAP)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0=universe 전체")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--force", action="store_true", help="이미 historical_max_mcap이 있는 ticker도 갱신"
    )
    parser.add_argument("--checkpoint-every", type=int, default=100)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    # universe + float_shares를 한 번에 조회
    rows = db.conn.execute(
        "SELECT ticker, float_shares, historical_max_mcap FROM universe "
        "WHERE market_cap_usd BETWEEN ? AND ? ORDER BY ticker",
        (MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD),
    ).fetchall()

    if not args.force:
        rows = [r for r in rows if r["historical_max_mcap"] is None]
    if args.limit:
        rows = rows[: args.limit]
    logger.info("Backfilling historical_max_mcap for %d tickers (workers=%d)",
                len(rows), args.workers)

    db_lock = threading.Lock()
    progress_lock = threading.Lock()
    counters = {"updated": 0, "no_history": 0, "no_shares": 0, "fail": 0}
    started = time.monotonic()

    def process(row: object) -> None:
        ticker = row["ticker"]
        shares = row["float_shares"]
        try:
            max_close = fetch_max_close(ticker)
        except Exception as exc:
            with progress_lock:
                counters["fail"] += 1
            logger.warning("fetch exc %s: %s", ticker, exc)
            return
        if max_close is None:
            with progress_lock:
                counters["no_history"] += 1
            return
        if not shares:
            with progress_lock:
                counters["no_shares"] += 1
            return
        mcap = compute_max_mcap(max_close, shares)
        if mcap is None:
            with progress_lock:
                counters["no_shares"] += 1
            return
        with db_lock:
            db.conn.execute(
                "UPDATE universe SET historical_max_mcap = ? WHERE ticker = ?",
                (mcap, ticker),
            )
        with progress_lock:
            counters["updated"] += 1
            done = counters["updated"]
        if done % args.checkpoint_every == 0:
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            logger.info(
                "checkpoint @ %d/%d (%.0fs, %.2f/sec): updated=%d no_hist=%d no_shares=%d fail=%d",
                done, len(rows), elapsed, rate,
                counters["updated"], counters["no_history"],
                counters["no_shares"], counters["fail"],
            )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, r) for r in rows]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                logger.warning("worker exc: %s", exc)

    elapsed = time.monotonic() - started
    logger.info(
        "Done in %.0fs. updated=%d no_hist=%d no_shares=%d fail=%d",
        elapsed,
        counters["updated"],
        counters["no_history"],
        counters["no_shares"],
        counters["fail"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
