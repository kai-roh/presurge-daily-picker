"""로컬 SQLite DB를 Supabase Postgres로 1회 이전.

필수:
    SUPABASE_DATABASE_URL 또는 --target-url 에 Supabase Postgres connection string

권장:
    Supabase dashboard > Connect > Transaction pooler 또는 Session pooler URL 사용.
    Transaction pooler 사용 시 psycopg prepared statements는 db adapter에서 비활성화된다.
"""
from __future__ import annotations

import argparse
import logging
import os
import shlex
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv

from src.storage.db import Database

logger = logging.getLogger(__name__)

TABLES = [
    "universe",
    "daily_bars",
    "filings",
    "short_interest",
    "social_mentions",
    "toss_top_volume",
    "index_inclusion_events",
    "pss_scores",
    "watchlist_runs",
    "options_activity",
    "surge_events",
    "trade_log",
    "signal_events",
    "signal_outcomes",
]

TRUNCATE_ORDER = list(reversed(TABLES))
SEQUENCES = {
    "index_inclusion_events": ("event_id", "index_inclusion_events_event_id_seq"),
    "trade_log": ("trade_id", "trade_log_trade_id_seq"),
    "signal_events": ("signal_id", "signal_events_signal_id_seq"),
}


def _target_url(cli_url: str | None) -> str:
    url = cli_url or os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    url = url.strip().strip('"').strip("'")
    if not url.startswith(("postgres://", "postgresql://")):
        if url.startswith("psql "):
            return _psql_command_to_url(url)
        raise SystemExit(
            "Supabase/Postgres target URL not found. Set SUPABASE_DATABASE_URL to a "
            "postgresql:// URI, or use a psql command plus PGPASSWORD/SUPABASE_DB_PASSWORD."
        )
    return url


def _psql_command_to_url(command: str) -> str:
    parts = shlex.split(command)
    values: dict[str, str] = {}
    flag_map = {
        "-h": "host",
        "--host": "host",
        "-p": "port",
        "--port": "port",
        "-d": "dbname",
        "--dbname": "dbname",
        "-U": "user",
        "--username": "user",
    }
    for i, part in enumerate(parts):
        key = flag_map.get(part)
        if key and i + 1 < len(parts):
            values[key] = parts[i + 1]
    password = os.environ.get("SUPABASE_DB_PASSWORD") or os.environ.get("PGPASSWORD")
    missing = [k for k in ("host", "port", "dbname", "user") if not values.get(k)]
    if missing or not password:
        raise SystemExit(
            "psql command target requires host/port/db/user plus password. "
            "Set PGPASSWORD or SUPABASE_DB_PASSWORD, or paste the Supabase URI form."
        )
    user = quote(values["user"], safe="")
    pw = quote(password, safe="")
    return (
        f"postgresql://{user}:{pw}@{values['host']}:{values['port']}/"
        f"{values['dbname']}?sslmode=require"
    )


def _sqlite_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"SQLite DB not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _insert_sql(table: str, cols: list[str]) -> str:
    col_sql = ", ".join(cols)
    val_sql = ", ".join(f":{c}" for c in cols)
    return f"INSERT INTO {table}({col_sql}) VALUES ({val_sql})"


def migrate(
    source_path: Path,
    target_url: str,
    *,
    replace: bool,
    batch_size: int,
    full: bool,
    window_days: int,
) -> dict[str, int]:
    src = _sqlite_conn(source_path)
    dst = Database(target_url, backend="postgres")
    dst.init_schema()

    if replace:
        logger.info("Truncating Supabase tables")
        dst.conn.execute("TRUNCATE TABLE " + ", ".join(TRUNCATE_ORDER) + " RESTART IDENTITY CASCADE")

    counts: dict[str, int] = {}
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    for table in TABLES:
        cols = _columns(src, table)
        if not cols:
            continue
        sql = _insert_sql(table, cols)
        where, where_params = _where_for(table, cutoff, full)
        total = int(src.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", where_params).fetchone()["n"])
        logger.info("Migrating %s (%d rows)", table, total)
        n = 0
        offset = 0
        while True:
            rows = src.execute(
                f"SELECT * FROM {table} {where} LIMIT ? OFFSET ?",
                (*where_params, batch_size, offset),
            ).fetchall()
            if not rows:
                break
            with dst.transaction() as conn:
                for row in rows:
                    conn.execute(sql, dict(row))
                    n += 1
            offset += batch_size
            if n and n % max(batch_size * 10, 1000) == 0:
                logger.info("  %s: %d/%d", table, n, total)
        counts[table] = n

    _reset_sequences(dst)
    dst.close()
    src.close()
    return counts


def _where_for(table: str, cutoff: str, full: bool) -> tuple[str, tuple[Any, ...]]:
    if full:
        return "", ()
    if table == "daily_bars":
        return "WHERE trade_date >= ?", (cutoff,)
    if table == "filings":
        return "WHERE substr(filed_at, 1, 10) >= ?", (cutoff,)
    if table == "social_mentions":
        return "WHERE mention_date >= ?", (cutoff,)
    if table == "toss_top_volume":
        return "WHERE rank_date >= ?", (cutoff,)
    if table == "pss_scores":
        return "WHERE score_date >= ?", (cutoff,)
    if table == "watchlist_runs":
        return "WHERE run_date >= ?", (cutoff,)
    if table == "options_activity":
        return "WHERE snap_date >= ?", (cutoff,)
    if table == "surge_events":
        return "WHERE surge_date >= ?", (cutoff,)
    return "", ()


def _reset_sequences(db: Database) -> None:
    for table, (pk, seq) in SEQUENCES.items():
        db.conn.execute(
            "SELECT setval(%s, COALESCE((SELECT MAX(" + pk + f") FROM {table}), 1), true)",
            (seq,),
        )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/presurge.db")
    parser.add_argument("--target-url")
    parser.add_argument("--replace", action="store_true", help="TRUNCATE target tables before copy")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--full",
        action="store_true",
        help="migrate full historical DB. Not recommended for Supabase Free Plan.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=220,
        help="operational mode lookback for large time-series tables",
    )
    args = parser.parse_args(argv)

    counts = migrate(
        Path(args.source),
        _target_url(args.target_url),
        replace=args.replace,
        batch_size=args.batch_size,
        full=args.full,
        window_days=args.window_days,
    )
    for table, n in counts.items():
        logger.info("%s=%d", table, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
