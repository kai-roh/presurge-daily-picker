"""SQLite 추상화. URL 파싱 + 스키마 init + 헬퍼 쿼리."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


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


class Database:
    """경량 SQLite 래퍼. 멀티 프로세스 동시성은 GitHub Actions concurrency group으로 직렬화."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(sql)

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
    def upsert_pss(self, score_date: date, ticker: str, breakdown: dict[str, Any]) -> None:
        sql = """
        INSERT OR REPLACE INTO pss_scores(
            score_date, ticker, pattern_a, pattern_b, pattern_c, pattern_d, pattern_e, pattern_f,
            bonus_toss, penalty_run, penalty_earn, pss_total, tier, triggered_patterns, metadata_json
        ) VALUES (
            :score_date, :ticker, :pattern_a, :pattern_b, :pattern_c, :pattern_d, :pattern_e, :pattern_f,
            :bonus_toss, :penalty_run, :penalty_earn, :pss_total, :tier, :triggered_patterns, :metadata_json
        )
        """
        params = {"score_date": score_date.isoformat(), "ticker": ticker, **breakdown}
        if "metadata_json" in params and not isinstance(params["metadata_json"], str):
            params["metadata_json"] = json.dumps(params["metadata_json"])
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


_DB_SINGLETON: Database | None = None


def get_db(url: str | None = None) -> Database:
    global _DB_SINGLETON
    if _DB_SINGLETON is None:
        url = url or os.environ.get("DATABASE_URL", "sqlite:///data/presurge.db")
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
