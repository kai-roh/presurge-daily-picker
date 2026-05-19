"""오늘 장중 감시 후보 추출."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.storage.db import Database

PATTERN_PRIORITY = {"G": 0, "E": 1, "D": 2, "C": 3, "A": 4, "F": 5, "B": 6}


@dataclass(frozen=True)
class WatchCandidate:
    ticker: str
    pss_total: float
    tier: int | None
    triggered_patterns: str
    name: str | None = None

    @property
    def pattern_set(self) -> set[str]:
        return {p.strip() for p in self.triggered_patterns.split(",") if p.strip()}


def _loads_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return body if isinstance(body, list) else []


def _ticker_from_item(item: dict[str, Any]) -> str | None:
    for key in ("ticker", "symbol"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().upper()
    return None


def _candidate_from_pss(row: sqlite3.Row, db: Database) -> WatchCandidate:
    uni = db.get_universe_row(row["ticker"])
    return WatchCandidate(
        ticker=row["ticker"],
        pss_total=float(row["pss_total"] or 0),
        tier=row["tier"],
        triggered_patterns=row["triggered_patterns"] or "",
        name=uni["name"] if uni else None,
    )


def _pattern_rank(c: WatchCandidate) -> tuple[int, ...]:
    if not c.pattern_set:
        return (99,)
    return tuple(sorted(PATTERN_PRIORITY.get(p, 50) for p in c.pattern_set))


def load_intraday_watchlist(
    db: Database,
    trade_date: date,
    max_tickers: int = 20,
    min_tier: int = 3,
) -> list[WatchCandidate]:
    """watchlist_runs 기준으로 최대 max_tickers 후보를 반환.

    해당 날짜 run이 없으면 가장 최근 run_date <= trade_date를 사용한다. JSON 안의
    항목 형태가 바뀌어도 pss_scores를 단일 출처로 다시 조회해 정규화한다.
    """
    row = db.conn.execute(
        "SELECT * FROM watchlist_runs WHERE run_date <= ? ORDER BY run_date DESC LIMIT 1",
        (trade_date.isoformat(),),
    ).fetchone()
    if not row:
        return _fallback_top_pss(db, trade_date, max_tickers, min_tier)

    run_date = date.fromisoformat(row["run_date"])
    tickers: list[str] = []
    for key in ("tier1_json", "tier2_json", "tier3_json"):
        for item in _loads_list(row[key]):
            ticker = _ticker_from_item(item)
            if ticker and ticker not in tickers:
                tickers.append(ticker)

    candidates: list[WatchCandidate] = []
    for ticker in tickers:
        pss = db.get_pss(run_date, ticker)
        if pss and (pss["tier"] is None or int(pss["tier"]) <= min_tier):
            candidates.append(_candidate_from_pss(pss, db))

    if not candidates:
        return _fallback_top_pss(db, run_date, max_tickers, min_tier)

    tier12 = [c for c in candidates if c.tier in (1, 2)]
    tier3 = [c for c in candidates if c.tier == 3]
    tier3.sort(key=lambda c: (_pattern_rank(c), -c.pss_total, c.ticker))
    merged = tier12 + tier3
    return merged[:max_tickers]


def _fallback_top_pss(
    db: Database,
    score_date: date,
    max_tickers: int,
    min_tier: int,
) -> list[WatchCandidate]:
    rows = db.conn.execute(
        "SELECT * FROM pss_scores WHERE score_date <= ? AND tier IS NOT NULL "
        "AND tier <= ? ORDER BY score_date DESC, tier ASC, pss_total DESC LIMIT ?",
        (score_date.isoformat(), min_tier, max_tickers),
    ).fetchall()
    return [_candidate_from_pss(r, db) for r in rows]

