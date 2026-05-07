"""Forward 자동 trade_log 갱신 — 초단타 alpha 측정용.

매일 cron 끝에 호출. 어제/그저께/3일전 watchlist의 종목을 trade_log에 적재.
- entry_price = pick된 날의 close (다음날 시초 미진입 가정으로 보수적)
- exit 1d/2d/3d:
    pnl_close = (T+N close - entry) / entry
    pnl_high  = (T+N high  - entry) / entry  ← 일중 최고가 도달률 (max profit)
- 멱등: (ticker, entry_date) UNIQUE — 같은 trade는 update만

실행:
    python -m scripts.update_trade_log [--lookback 5]    # 최근 5일치 watchlist
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from src.config import Settings
from src.storage.db import Database, get_db

logger = logging.getLogger(__name__)


def _bar_high_close(db: Database, ticker: str, target: date) -> tuple[float, float] | None:
    """target 일자 또는 그 이후 첫 영업일의 (high, close)."""
    row = db.conn.execute(
        "SELECT trade_date, high, close FROM daily_bars "
        "WHERE ticker = ? AND trade_date >= ? AND high IS NOT NULL "
        "ORDER BY trade_date ASC LIMIT 1",
        (ticker, target.isoformat()),
    ).fetchone()
    if not row or row["close"] is None:
        return None
    return float(row["high"]), float(row["close"])


def _next_n_business_days(d: date, n: int) -> date:
    cur = d
    added = 0
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _entry_close(db: Database, ticker: str, entry_date: date) -> float | None:
    row = db.conn.execute(
        "SELECT close FROM daily_bars WHERE ticker = ? AND trade_date = ?",
        (ticker, entry_date.isoformat()),
    ).fetchone()
    if not row or row["close"] is None:
        return None
    return float(row["close"])


def upsert_trade(
    db: Database,
    *,
    ticker: str,
    entry_date: date,
    entry_pss: float | None,
    entry_tier: int | None,
    triggered_patterns: str,
    entry_price: float,
    pnl: dict[str, float | None],
) -> None:
    """기존 row 있으면 pnl만 update, 없으면 insert."""
    existing = db.conn.execute(
        "SELECT trade_id FROM trade_log WHERE ticker = ? AND entry_date = ? AND is_paper = 1",
        (ticker, entry_date.isoformat()),
    ).fetchone()
    if existing:
        db.conn.execute(
            """
            UPDATE trade_log SET
                pnl_high_1d_pct = COALESCE(?, pnl_high_1d_pct),
                pnl_close_1d_pct = COALESCE(?, pnl_close_1d_pct),
                pnl_high_2d_pct = COALESCE(?, pnl_high_2d_pct),
                pnl_close_2d_pct = COALESCE(?, pnl_close_2d_pct),
                pnl_high_3d_pct = COALESCE(?, pnl_high_3d_pct),
                pnl_close_3d_pct = COALESCE(?, pnl_close_3d_pct)
            WHERE trade_id = ?
            """,
            (
                pnl.get("h1"), pnl.get("c1"),
                pnl.get("h2"), pnl.get("c2"),
                pnl.get("h3"), pnl.get("c3"),
                existing["trade_id"],
            ),
        )
    else:
        db.conn.execute(
            """
            INSERT INTO trade_log(
                ticker, entry_date, entry_price, entry_pss, entry_tier,
                triggered_patterns, exit_reason, is_paper,
                pnl_high_1d_pct, pnl_close_1d_pct,
                pnl_high_2d_pct, pnl_close_2d_pct,
                pnl_high_3d_pct, pnl_close_3d_pct
            ) VALUES (?, ?, ?, ?, ?, ?, 'forward', 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, entry_date.isoformat(), entry_price, entry_pss, entry_tier,
                triggered_patterns,
                pnl.get("h1"), pnl.get("c1"),
                pnl.get("h2"), pnl.get("c2"),
                pnl.get("h3"), pnl.get("c3"),
            ),
        )


def process_watchlist_run(db: Database, run_date: date) -> dict[str, int]:
    """주어진 run_date의 watchlist를 가져와 가능한 1d/2d/3d pnl을 채움."""
    row = db.conn.execute(
        "SELECT tier1_json, tier2_json, tier3_json FROM watchlist_runs WHERE run_date = ?",
        (run_date.isoformat(),),
    ).fetchone()
    if not row:
        return {"updated": 0, "skipped": 0}

    picks: list[dict] = []
    for col in ("tier1_json", "tier2_json", "tier3_json"):
        try:
            picks.extend(json.loads(row[col] or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

    if not picks:
        return {"updated": 0, "skipped": 0}

    # 진입 기준일: pick된 날의 close (= run_date 시점 가용 종가)
    today = date.today()
    days_elapsed = (today - run_date).days
    horizons = [(n, f"h{n}", f"c{n}") for n in (1, 2, 3) if days_elapsed >= n]

    updated = 0
    skipped = 0
    with db.transaction():
        for pick in picks:
            ticker = pick.get("ticker")
            if not ticker:
                continue
            entry_close = _entry_close(db, ticker, run_date)
            if entry_close is None:
                skipped += 1
                continue
            pnl: dict[str, float | None] = {}
            for n, hkey, ckey in horizons:
                target = _next_n_business_days(run_date, n)
                hc = _bar_high_close(db, ticker, target)
                if hc is None:
                    pnl[hkey] = None
                    pnl[ckey] = None
                else:
                    high, close = hc
                    pnl[hkey] = (high - entry_close) / entry_close
                    pnl[ckey] = (close - entry_close) / entry_close
            upsert_trade(
                db,
                ticker=ticker,
                entry_date=run_date,
                entry_pss=pick.get("pss_total"),
                entry_tier=pick.get("tier"),
                triggered_patterns=",".join(pick.get("triggered_patterns") or []),
                entry_price=entry_close,
                pnl=pnl,
            )
            updated += 1
    return {"updated": updated, "skipped": skipped, "horizons": [n for n, _, _ in horizons]}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback", type=int, default=5,
                        help="최근 N영업일치 watchlist run을 모두 갱신")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    today = date.today()
    cur = today
    counted = 0
    grand_total = {"updated": 0, "skipped": 0}
    while counted < args.lookback:
        cur -= timedelta(days=1)
        if cur.weekday() >= 5:
            continue
        counted += 1
        result = process_watchlist_run(db, cur)
        if result["updated"] or result["skipped"]:
            logger.info("run_date=%s horizons=%s updated=%d skipped=%d",
                        cur, result.get("horizons"), result["updated"], result["skipped"])
            grand_total["updated"] += result["updated"]
            grand_total["skipped"] += result["skipped"]

    logger.info("Done. total updated=%d skipped=%d", grand_total["updated"], grand_total["skipped"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
