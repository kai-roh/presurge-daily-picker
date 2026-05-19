"""SQLite 추상화. URL 파싱 + 스키마 init + 헬퍼 쿼리."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
POSTGRES_SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"

UPSERT_CONFLICTS = {
    "daily_bars": ("ticker", "trade_date"),
    "filings": ("accession_no",),
    "short_interest": ("ticker", "settle_date", "source"),
    "social_mentions": ("ticker", "mention_date", "source"),
    "toss_top_volume": ("rank_date", "rank"),
    "pss_scores": ("score_date", "ticker"),
    "watchlist_runs": ("run_date",),
    "options_activity": ("snap_date", "ticker"),
    "surge_events": ("surge_date", "ticker", "surge_type"),
    "signal_events": ("trade_date", "ticker", "signal_type", "trigger_code", "signal_ts"),
    "signal_outcomes": ("signal_id",),
}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _resolve_db_path(url: str) -> Path:
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///")).resolve()
    if url.startswith("sqlite://"):
        return Path(url.removeprefix("sqlite://")).resolve()
    return Path(url).resolve()


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


class PostgresConnection:
    """sqlite-ish execute facade over psycopg.

    The codebase historically uses sqlite qmark parameters and a few SQLite
    insert forms. This adapter keeps those call sites small while DATABASE_URL
    can point at Supabase Postgres.
    """

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Postgres DATABASE_URL requires psycopg. Install requirements.txt first."
            ) from exc

        self._psycopg = psycopg
        self._conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        # Supabase transaction pooler is incompatible with prepared statements.
        self._conn.prepare_threshold = None

    def execute(self, sql: str, params: Any = None) -> Any:
        sql, params = self._translate(sql, params)
        return self._conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        for stmt in _split_sql_statements(sql):
            self.execute(stmt)

    def close(self) -> None:
        self._conn.close()

    def _translate(self, sql: str, params: Any) -> tuple[str, Any]:
        out = sql.strip()
        out = self._translate_insert_or(out, params)
        if isinstance(params, dict):
            out = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", out)
        else:
            out = out.replace("?", "%s")
        return out, params

    def _translate_insert_or(self, sql: str, params: Any) -> str:
        m = re.match(
            r"INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
            r"\((.*?)\)\s*VALUES\s*\((.*?)\)\s*$",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return sql
        mode, table, cols_raw, values_raw = m.groups()
        cols = [c.strip() for c in cols_raw.replace("\n", " ").split(",")]
        base = f"INSERT INTO {table}({cols_raw}) VALUES ({values_raw})"
        if mode.upper() == "IGNORE":
            return base + " ON CONFLICT DO NOTHING"
        conflict = UPSERT_CONFLICTS.get(table)
        if not conflict:
            return base
        update_cols = [c for c in cols if c not in conflict]
        if not update_cols:
            return base + f" ON CONFLICT ({', '.join(conflict)}) DO NOTHING"
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        return base + f" ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}"


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    for ch in sql:
        if ch == "'":
            in_single = not in_single
        if ch == ";" and not in_single:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class Database:
    """경량 SQLite 래퍼. 멀티 프로세스 동시성은 GitHub Actions concurrency group으로 직렬화."""

    def __init__(self, path: Path | str, backend: str = "sqlite"):
        self.backend = backend
        self.url = str(path)
        self.path = Path(path) if backend == "sqlite" else Path("<postgres>")
        if backend == "sqlite":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Any | None = None

    @property
    def conn(self) -> Any:
        if self._conn is None:
            if self.backend == "postgres":
                self._conn = PostgresConnection(self.url)
                return self._conn
            # check_same_thread=False — 멀티스레드 워커가 같은 connection을 쓸 수
            # 있도록 허용. 쓰기 직렬화는 호출자가 lock으로 책임.
            self._conn = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_schema(self) -> None:
        schema_path = POSTGRES_SCHEMA_PATH if self.backend == "postgres" else SCHEMA_PATH
        sql = schema_path.read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self._migrate_in_place()

    def _migrate_in_place(self) -> None:
        """기존 DB에 누락된 컬럼/인덱스를 비파괴적으로 추가."""
        # trade_log: short-horizon pnl 컬럼 (W5+ 추가)
        existing = self._table_columns("trade_log")
        new_cols = (
            "pnl_high_1d_pct",
            "pnl_close_1d_pct",
            "pnl_high_2d_pct",
            "pnl_close_2d_pct",
            "pnl_high_3d_pct",
            "pnl_close_3d_pct",
        )
        for col in new_cols:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col} REAL")

        # pss_scores: pattern_g 컬럼 (v0.3 RVOL 추가)
        pss_cols = self._table_columns("pss_scores")
        if "pattern_g" not in pss_cols:
            self.conn.execute(
                "ALTER TABLE pss_scores ADD COLUMN pattern_g REAL NOT NULL DEFAULT 0"
            )

    def _table_columns(self, table: str) -> set[str]:
        if self.backend == "postgres":
            rows = self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            ).fetchall()
            return {r["column_name"] for r in rows}
        return {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # universe
    # ------------------------------------------------------------------
    def upsert_universe(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT INTO universe(ticker, name, market_cap_usd, float_shares, exchange,
                             sector, is_common_stock, historical_max_mcap, last_refreshed)
        VALUES (:ticker, :name, :market_cap_usd, :float_shares, :exchange,
                :sector, :is_common_stock, :historical_max_mcap, :last_refreshed)
        ON CONFLICT(ticker) DO UPDATE SET
            name = excluded.name,
            market_cap_usd = excluded.market_cap_usd,
            float_shares = excluded.float_shares,
            exchange = excluded.exchange,
            sector = excluded.sector,
            is_common_stock = excluded.is_common_stock,
            historical_max_mcap = COALESCE(excluded.historical_max_mcap, universe.historical_max_mcap),
            last_refreshed = excluded.last_refreshed
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def get_market_cap(self, ticker: str) -> float | None:
        row = self.conn.execute(
            "SELECT market_cap_usd FROM universe WHERE ticker = ?", (ticker,)
        ).fetchone()
        return row["market_cap_usd"] if row else None

    def get_universe_row(self, ticker: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM universe WHERE ticker = ?", (ticker,)
        ).fetchone()

    def universe_tickers(self, mcap_min: float, mcap_max: float) -> list[str]:
        rows = self.conn.execute(
            "SELECT ticker FROM universe WHERE market_cap_usd BETWEEN ? AND ? "
            "AND is_common_stock = 1 ORDER BY ticker",
            (mcap_min, mcap_max),
        ).fetchall()
        return [r["ticker"] for r in rows]

    def universe_refreshed_on(self, iso_date: str) -> set[str]:
        """Tickers whose last_refreshed timestamp falls on the given UTC date (YYYY-MM-DD)."""
        rows = self.conn.execute(
            "SELECT ticker FROM universe WHERE substr(last_refreshed, 1, 10) = ?",
            (iso_date,),
        ).fetchall()
        return {r["ticker"] for r in rows}

    # ------------------------------------------------------------------
    # daily bars
    # ------------------------------------------------------------------
    def upsert_bars(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT OR REPLACE INTO daily_bars(ticker, trade_date, open, high, low, close, volume, vwap)
        VALUES (:ticker, :trade_date, :open, :high, :low, :close, :volume, :vwap)
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def get_close(self, ticker: str, on: date) -> float | None:
        row = self.conn.execute(
            "SELECT close FROM daily_bars WHERE ticker = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (ticker, on.isoformat()),
        ).fetchone()
        return row["close"] if row else None

    def latest_volume(self, ticker: str, as_of: date) -> int | None:
        """as_of 이전 또는 당일의 가장 최근 거래일 volume."""
        row = self.conn.execute(
            "SELECT volume FROM daily_bars WHERE ticker = ? AND trade_date <= ? "
            "AND volume IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
            (ticker, as_of.isoformat()),
        ).fetchone()
        return int(row["volume"]) if row and row["volume"] else None

    def avg_volume(self, ticker: str, as_of: date, lookback_days: int) -> float | None:
        """as_of 이전 lookback_days 영업일 평균 거래량 (최근 거래일 자체는 제외)."""
        # 최근 거래일 제외 → as_of 이전의 가장 최근 거래일 직전 N일 평균
        latest = self.conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_bars WHERE ticker = ? AND trade_date <= ?",
            (ticker, as_of.isoformat()),
        ).fetchone()
        if not latest or not latest["d"]:
            return None
        # latest["d"] 이전 ~ -1.5 * lookback_days 범위에서 가장 가까운 lookback_days 영업일
        # 단순화: 캘린더 일수 lookback_days * 1.5 만큼 윈도우
        from datetime import timedelta as _td
        latest_d = date.fromisoformat(latest["d"])
        window_start = latest_d - _td(days=int(lookback_days * 1.5))
        row = self.conn.execute(
            "SELECT AVG(volume) AS v FROM daily_bars "
            "WHERE ticker = ? AND trade_date >= ? AND trade_date < ? "
            "AND volume IS NOT NULL AND volume > 0",
            (ticker, window_start.isoformat(), latest["d"]),
        ).fetchone()
        return float(row["v"]) if row and row["v"] else None

    def price_change_pct(self, ticker: str, as_of: date, days: int) -> float | None:
        end = self.get_close(ticker, as_of)
        if end is None:
            return None
        start_date = as_of - timedelta(days=days)
        start_row = self.conn.execute(
            "SELECT close FROM daily_bars WHERE ticker = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (ticker, start_date.isoformat()),
        ).fetchone()
        if not start_row or not start_row["close"]:
            return None
        return (end - start_row["close"]) / start_row["close"]

    # ------------------------------------------------------------------
    # filings
    # ------------------------------------------------------------------
    def upsert_filings(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT OR IGNORE INTO filings(accession_no, ticker, cik, filed_at, form_type,
                                       items, raw_text_url)
        VALUES (:accession_no, :ticker, :cik, :filed_at, :form_type, :items, :raw_text_url)
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def update_filing_classification(
        self,
        accession_no: str,
        classification: str,
        confidence: float,
        contract_value_usd: float | None,
        counterparty: str | None,
        key_quote: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE filings SET
                classification = ?,
                classification_confidence = ?,
                contract_value_usd = ?,
                counterparty = ?,
                key_quote = ?,
                classified_at = ?
            WHERE accession_no = ?
            """,
            (
                classification,
                confidence,
                contract_value_usd,
                counterparty,
                key_quote,
                datetime.utcnow().isoformat(),
                accession_no,
            ),
        )

    def query_filings(
        self,
        ticker: str,
        as_of: date,
        hours_back: int,
        items: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        cutoff = (datetime.combine(as_of, datetime.min.time()) - timedelta(hours=hours_back)).isoformat()
        sql = "SELECT * FROM filings WHERE ticker = ? AND filed_at >= ? AND filed_at <= ?"
        params: list[Any] = [ticker, cutoff, as_of.isoformat() + "T23:59:59"]
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            row_items = (r["items"] or "").split(",")
            if items and not any(it in row_items for it in items):
                continue
            if keywords:
                hay = " ".join(filter(None, [r["key_quote"], r["counterparty"], r["raw_text_url"]])).lower()
                if not any(k.lower() in hay for k in keywords):
                    continue
            out.append(r)
        return out

    # ------------------------------------------------------------------
    # short interest
    # ------------------------------------------------------------------
    def upsert_short_interest(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT OR REPLACE INTO short_interest(ticker, settle_date, si_shares, si_pct_float,
                                               days_to_cover, cost_to_borrow, source)
        VALUES (:ticker, :settle_date, :si_shares, :si_pct_float, :days_to_cover, :cost_to_borrow, :source)
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def latest_short_interest(self, ticker: str, as_of: date) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM short_interest WHERE ticker = ? AND settle_date <= ? "
            "ORDER BY settle_date DESC LIMIT 1",
            (ticker, as_of.isoformat()),
        ).fetchone()

    # ------------------------------------------------------------------
    # social
    # ------------------------------------------------------------------
    def upsert_social(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT OR REPLACE INTO social_mentions(ticker, mention_date, source, mentions, bullish_pct, rank)
        VALUES (:ticker, :mention_date, :source, :mentions, :bullish_pct, :rank)
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def avg_mentions(self, ticker: str, as_of: date, days: int, source: str) -> float | None:
        start = (as_of - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT AVG(mentions) AS m FROM social_mentions "
            "WHERE ticker = ? AND source = ? AND mention_date BETWEEN ? AND ?",
            (ticker, source, start, as_of.isoformat()),
        ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def mention_growth(self, ticker: str, as_of: date, source: str) -> float | None:
        today = self.conn.execute(
            "SELECT mentions FROM social_mentions WHERE ticker = ? AND source = ? AND mention_date = ?",
            (ticker, source, as_of.isoformat()),
        ).fetchone()
        prev = self.conn.execute(
            "SELECT mentions FROM social_mentions WHERE ticker = ? AND source = ? "
            "AND mention_date = ?",
            (ticker, source, (as_of - timedelta(days=1)).isoformat()),
        ).fetchone()
        if not today or not prev or not prev["mentions"]:
            return None
        return today["mentions"] / prev["mentions"]

    # ------------------------------------------------------------------
    # toss
    # ------------------------------------------------------------------
    def upsert_toss(self, rank_date: date, ranks: list[tuple[int, str]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM toss_top_volume WHERE rank_date = ?", (rank_date.isoformat(),))
            for rank, ticker in ranks:
                conn.execute(
                    "INSERT INTO toss_top_volume(rank_date, rank, ticker) VALUES (?, ?, ?)",
                    (rank_date.isoformat(), rank, ticker),
                )

    def in_toss_top30(self, ticker: str, as_of: date) -> bool:
        # 직전 7일 내 1번이라도 top 30 진입했으면 True (단발 노이즈 vs 안정 인기 분리는 v0.3)
        start = (as_of - timedelta(days=7)).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM toss_top_volume WHERE ticker = ? AND rank <= 30 "
            "AND rank_date BETWEEN ? AND ? LIMIT 1",
            (ticker, start, as_of.isoformat()),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Index inclusion events
    # ------------------------------------------------------------------
    def upsert_index_event(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO index_inclusion_events(ticker, index_name, announced_at, effective_at, source, notes)
            VALUES (:ticker, :index_name, :announced_at, :effective_at, :source, :notes)
            """,
            row,
        )

    def index_inclusion_events(self, ticker: str, as_of: date) -> list[dict[str, Any]]:
        """Pattern B 헬퍼. announced_at <= as_of, effective_at >= as_of - 7d 인 이벤트만."""
        cutoff = (as_of - timedelta(days=7)).isoformat()
        rows = self.conn.execute(
            """
            SELECT ticker, index_name, announced_at, effective_at, source
            FROM index_inclusion_events
            WHERE ticker = ?
              AND (announced_at IS NULL OR announced_at <= ?)
              AND (effective_at IS NULL OR effective_at >= ?)
            """,
            (ticker, as_of.isoformat(), cutoff),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "ticker": r["ticker"],
                "index_name": r["index_name"],
                "announced_at": _parse_date(r["announced_at"]),
                "effective_at": _parse_date(r["effective_at"]),
                "source": r["source"],
            })
        return out

    def has_earnings_within(self, ticker: str, as_of: date, days: int) -> bool:
        """earnings 캘린더는 v0.3 Finnhub 적재. MVP에서는 항상 False."""
        _ = (ticker, as_of, days)
        return False

    # ------------------------------------------------------------------
    # PSS scores
    # ------------------------------------------------------------------
    def upsert_options_activity(self, rows: Iterable[dict[str, Any]]) -> int:
        sql = """
        INSERT OR REPLACE INTO options_activity(
            snap_date, ticker, expiry, call_volume, put_volume,
            call_oi, put_oi, cp_volume_ratio
        ) VALUES (
            :snap_date, :ticker, :expiry, :call_volume, :put_volume,
            :call_oi, :put_oi, :cp_volume_ratio
        )
        """
        n = 0
        with self.transaction() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        return n

    def upsert_pss(self, score_date: date, ticker: str, breakdown: dict[str, Any]) -> None:
        sql = """
        INSERT OR REPLACE INTO pss_scores(
            score_date, ticker, pattern_a, pattern_b, pattern_c, pattern_d, pattern_e, pattern_f,
            pattern_g, bonus_toss, penalty_run, penalty_earn, pss_total, tier,
            triggered_patterns, metadata_json
        ) VALUES (
            :score_date, :ticker, :pattern_a, :pattern_b, :pattern_c, :pattern_d, :pattern_e, :pattern_f,
            :pattern_g, :bonus_toss, :penalty_run, :penalty_earn, :pss_total, :tier,
            :triggered_patterns, :metadata_json
        )
        """
        params = {"score_date": score_date.isoformat(), "ticker": ticker, **breakdown}
        if "metadata_json" in params and not isinstance(params["metadata_json"], str):
            # Pattern B 의 events 안에 date 객체가 들어있을 수 있어 default=str 필요
            params["metadata_json"] = json.dumps(params["metadata_json"], default=str)
        self.conn.execute(sql, params)

    def get_pss(self, score_date: date, ticker: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pss_scores WHERE score_date = ? AND ticker = ?",
            (score_date.isoformat(), ticker),
        ).fetchone()

    def top_pss(self, score_date: date, tier: int, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pss_scores WHERE score_date = ? AND tier = ? "
            "ORDER BY pss_total DESC LIMIT ?",
            (score_date.isoformat(), tier, limit),
        ).fetchall()

    # ------------------------------------------------------------------
    # watchlist runs
    # ------------------------------------------------------------------
    def save_watchlist_run(
        self,
        run_date: date,
        tier1: list[dict[str, Any]],
        tier2: list[dict[str, Any]],
        tier3: list[dict[str, Any]],
        report_md: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO watchlist_runs(run_date, tier1_json, tier2_json, tier3_json, report_md)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_date.isoformat(),
                json.dumps(tier1),
                json.dumps(tier2),
                json.dumps(tier3),
                report_md,
            ),
        )

    def mark_pushed(self, run_date: date, status: str) -> None:
        self.conn.execute(
            "UPDATE watchlist_runs SET pushed_at = ?, push_status = ? WHERE run_date = ?",
            (datetime.utcnow().isoformat(), status, run_date.isoformat()),
        )

    # ------------------------------------------------------------------
    # Intraday signals
    # ------------------------------------------------------------------
    def latest_bar(self, ticker: str, on_or_before: date) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM daily_bars WHERE ticker = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (ticker, on_or_before.isoformat()),
        ).fetchone()

    def insert_signal_event(self, row: dict[str, Any]) -> int:
        params = dict(row)
        if "metadata_json" in params and not isinstance(params["metadata_json"], str):
            params["metadata_json"] = json.dumps(params["metadata_json"], default=str)
        sql = """
        INSERT INTO signal_events(
            signal_ts, trade_date, ticker, signal_type, trigger_code, price, ref_price,
            pss_total, tier, triggered_patterns, source, metadata_json,
            telegram_sent_at, telegram_status
        ) VALUES (
            :signal_ts, :trade_date, :ticker, :signal_type, :trigger_code, :price, :ref_price,
            :pss_total, :tier, :triggered_patterns, :source, :metadata_json,
            :telegram_sent_at, :telegram_status
        )
        """
        if self.backend == "postgres":
            row_id = self.conn.execute(sql + " RETURNING signal_id", params).fetchone()
            return int(row_id["signal_id"])
        self.conn.execute(sql, params)
        return int(self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def mark_signal_telegram(self, signal_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE signal_events SET telegram_sent_at = ?, telegram_status = ? "
            "WHERE signal_id = ?",
            (datetime.utcnow().isoformat(), status, signal_id),
        )

    def recent_signal_exists(
        self,
        ticker: str,
        trade_date: date,
        signal_type: str,
        trigger_code: str,
        since_ts: str,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM signal_events
            WHERE ticker = ? AND trade_date = ? AND signal_type = ?
              AND trigger_code = ? AND signal_ts >= ?
            LIMIT 1
            """,
            (ticker, trade_date.isoformat(), signal_type, trigger_code, since_ts),
        ).fetchone()
        return row is not None

    def count_signals(
        self,
        ticker: str,
        trade_date: date,
        signal_type: str | None = None,
    ) -> int:
        if signal_type:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM signal_events "
                "WHERE ticker = ? AND trade_date = ? AND signal_type = ?",
                (ticker, trade_date.isoformat(), signal_type),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM signal_events WHERE ticker = ? AND trade_date = ?",
                (ticker, trade_date.isoformat()),
            ).fetchone()
        return int(row["n"] or 0)

    def latest_signal(
        self,
        ticker: str,
        trade_date: date,
        signal_type: str | None = None,
    ) -> sqlite3.Row | None:
        if signal_type:
            return self.conn.execute(
                "SELECT * FROM signal_events WHERE ticker = ? AND trade_date = ? "
                "AND signal_type = ? ORDER BY signal_ts DESC LIMIT 1",
                (ticker, trade_date.isoformat(), signal_type),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM signal_events WHERE ticker = ? AND trade_date = ? "
            "ORDER BY signal_ts DESC LIMIT 1",
            (ticker, trade_date.isoformat()),
        ).fetchone()

    def upsert_signal_outcome(self, signal_id: int, outcome: dict[str, Any]) -> None:
        params = {"signal_id": signal_id, **outcome}
        self.conn.execute(
            """
            INSERT OR REPLACE INTO signal_outcomes(
                signal_id, max_10m_pct, close_10m_pct, max_30m_pct, close_30m_pct,
                max_60m_pct, close_60m_pct, max_eod_pct, close_eod_pct,
                min_after_pct, evaluated_at
            ) VALUES (
                :signal_id, :max_10m_pct, :close_10m_pct, :max_30m_pct, :close_30m_pct,
                :max_60m_pct, :close_60m_pct, :max_eod_pct, :close_eod_pct,
                :min_after_pct, :evaluated_at
            )
            """,
            params,
        )

    def unevaluated_signals(self, trade_date: date | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT s.* FROM signal_events s "
            "LEFT JOIN signal_outcomes o ON o.signal_id = s.signal_id "
            "WHERE o.signal_id IS NULL"
        )
        params: list[Any] = []
        if trade_date:
            sql += " AND s.trade_date = ?"
            params.append(trade_date.isoformat())
        sql += " ORDER BY s.signal_ts"
        return self.conn.execute(sql, params).fetchall()


_DB_SINGLETON: Database | None = None


def get_db(url: str | None = None) -> Database:
    global _DB_SINGLETON
    if _DB_SINGLETON is None:
        url = url or os.environ.get("DATABASE_URL", "sqlite:///data/presurge.db")
        if _is_postgres_url(url):
            _DB_SINGLETON = Database(url, backend="postgres")
        else:
            path = _resolve_db_path(url)
            _DB_SINGLETON = Database(path)
        _DB_SINGLETON.init_schema()
    return _DB_SINGLETON


def reset_db_singleton() -> None:
    """테스트용. 새 DB 인스턴스가 필요할 때 호출."""
    global _DB_SINGLETON
    if _DB_SINGLETON is not None:
        _DB_SINGLETON.close()
    _DB_SINGLETON = None


def main() -> None:
    """python -m src.storage.db --init"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="Initialize schema")
    parser.add_argument("--url", help="Override DATABASE_URL")
    args = parser.parse_args()

    db = get_db(args.url)
    if args.init:
        db.init_schema()
        print(f"Schema initialized at {db.path}")


if __name__ == "__main__":
    main()
