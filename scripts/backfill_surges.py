"""universe 전체 급등 이벤트 retroactive 적재 — recall 학습용.

각 (ticker, T-1일 close) → (ticker, T일 high/close) 페어를 검사하여
- high_1d_10:  T일 high  ≥ T-1일 close × 1.10
- high_1d_20:  T일 high  ≥ T-1일 close × 1.20
- close_1d_10: T일 close ≥ T-1일 close × 1.10
세 종류 surge type 모두 검출. 한 ticker가 같은 날 여러 type trigger 가능.

각 surge에 대해 prev_pss/tier/patterns 를 pss_scores 테이블에서 lookup
(없으면 NULL — 추후 backtest persist로 채울 수 있음).

was_picked 는 watchlist_runs.tier{1,2,3}_json 안에 ticker가 포함되었는지 확인.

실행:
    python -m scripts.backfill_surges --start 2024-05-01 --end 2026-05-08
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from dotenv import load_dotenv

from src.config import Settings
from src.storage.db import Database, get_db

logger = logging.getLogger(__name__)

# 임계: ticker 1d 변동률 (high/prev_close, close/prev_close)
SURGE_THRESHOLDS = (
    ("high_1d_10", "high", 0.10),
    ("high_1d_20", "high", 0.20),
    ("close_1d_10", "close", 0.10),
)


def find_surges(db: Database, start: date, end: date) -> list[dict]:
    """daily_bars 페어를 SQL로 한번에 조회. (ticker, T-1, T) 매 row."""
    sql = """
    WITH paired AS (
        SELECT
            a.ticker AS ticker,
            a.trade_date AS prev_date,
            a.close AS prev_close,
            b.trade_date AS surge_date,
            b.high  AS surge_high,
            b.close AS surge_close
        FROM daily_bars a
        JOIN daily_bars b ON b.ticker = a.ticker
            AND b.trade_date = (
                SELECT MIN(trade_date) FROM daily_bars c
                WHERE c.ticker = a.ticker AND c.trade_date > a.trade_date
            )
        WHERE a.trade_date BETWEEN ? AND ?
          AND a.close > 0
          AND b.high IS NOT NULL
    )
    SELECT * FROM paired
    """
    rows = db.conn.execute(sql, (start.isoformat(), end.isoformat())).fetchall()
    return [dict(r) for r in rows]


def detect_surge_types(row: dict) -> list[tuple[str, float, str]]:
    """단일 row에 대해 매칭되는 surge type/도달률/기준 컬럼을 반환."""
    out: list[tuple[str, float, str]] = []
    prev = row["prev_close"]
    if not prev or prev <= 0:
        return out
    for type_name, col, threshold in SURGE_THRESHOLDS:
        val = row.get(f"surge_{col}")
        if val is None:
            continue
        pct = (val - prev) / prev
        if pct >= threshold:
            out.append((type_name, pct, col))
    return out


def lookup_prev_pss(db: Database, ticker: str, prev_date: str) -> tuple:
    row = db.conn.execute(
        "SELECT pss_total, tier, triggered_patterns FROM pss_scores "
        "WHERE score_date = ? AND ticker = ?",
        (prev_date, ticker),
    ).fetchone()
    if not row:
        return None, None, None
    return row["pss_total"], row["tier"], row["triggered_patterns"]


def lookup_was_picked(db: Database, ticker: str, prev_date: str,
                      cache: dict[str, set[str]]) -> int:
    """watchlist_runs 의 tier1/2/3 JSON에서 ticker 포함 여부 (날짜별 캐시)."""
    if prev_date not in cache:
        row = db.conn.execute(
            "SELECT tier1_json, tier2_json, tier3_json FROM watchlist_runs WHERE run_date = ?",
            (prev_date,),
        ).fetchone()
        tickers: set[str] = set()
        if row:
            for col in ("tier1_json", "tier2_json", "tier3_json"):
                try:
                    for pick in json.loads(row[col] or "[]"):
                        if pick.get("ticker"):
                            tickers.add(pick["ticker"])
                except (json.JSONDecodeError, TypeError):
                    pass
        cache[prev_date] = tickers
    return 1 if ticker in cache[prev_date] else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--checkpoint-every", type=int, default=5000)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    logger.info("Scanning daily_bars pairs %s..%s", start, end)
    pairs = find_surges(db, start, end)
    logger.info("Total ticker-day pairs: %d", len(pairs))

    pick_cache: dict[str, set[str]] = {}
    inserted = 0
    surge_count = 0
    with db.transaction() as conn:
        for i, p in enumerate(pairs, 1):
            types = detect_surge_types(p)
            if not types:
                continue
            surge_count += len(types)
            ticker = p["ticker"]
            prev_date = p["prev_date"]
            surge_date = p["surge_date"]
            pss_total, tier, patterns = lookup_prev_pss(db, ticker, prev_date)
            picked = lookup_was_picked(db, ticker, prev_date, pick_cache)
            for type_name, pct, _col in types:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO surge_events(
                        surge_date, ticker, surge_type, surge_pct,
                        prev_close, surge_high, surge_close,
                        prev_pss_total, prev_tier, prev_patterns, was_picked
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        surge_date, ticker, type_name, pct,
                        p["prev_close"], p["surge_high"], p["surge_close"],
                        pss_total, tier, patterns, picked,
                    ),
                )
                inserted += 1
            if i % args.checkpoint_every == 0:
                logger.info("scanned %d/%d pairs, %d surges so far",
                            i, len(pairs), surge_count)

    logger.info("Done. surges=%d, rows_inserted=%d", surge_count, inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
