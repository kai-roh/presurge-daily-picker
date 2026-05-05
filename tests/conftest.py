from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.db import Database, reset_db_singleton


@pytest.fixture
def db(tmp_path: Path) -> Database:
    reset_db_singleton()
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def today() -> date:
    return date(2026, 5, 5)


@pytest.fixture
def seed_universe(db: Database):
    def _seed(ticker: str, mcap: float = 300_000_000, **kwargs):
        row = {
            "ticker": ticker,
            "name": kwargs.get("name", f"{ticker} Inc."),
            "market_cap_usd": mcap,
            "float_shares": kwargs.get("float_shares", 30_000_000),
            "exchange": kwargs.get("exchange", "XNAS"),
            "sector": kwargs.get("sector", "Technology"),
            "is_common_stock": 1,
            "historical_max_mcap": kwargs.get("historical_max_mcap"),
            "last_refreshed": datetime.utcnow().isoformat(),
        }
        db.upsert_universe([row])

    return _seed


@pytest.fixture
def seed_bars(db: Database):
    def _seed(ticker: str, anchor: date, prices: list[float]):
        # prices[0]은 anchor 일자의 종가, [1]은 -1일 ...
        rows = []
        for i, p in enumerate(prices):
            d = anchor - timedelta(days=i)
            rows.append({
                "ticker": ticker,
                "trade_date": d.isoformat(),
                "open": p,
                "high": p,
                "low": p,
                "close": p,
                "volume": 1_000_000,
                "vwap": p,
            })
        db.upsert_bars(rows)

    return _seed


@pytest.fixture
def seed_filing(db: Database):
    def _seed(ticker: str, filed_at: datetime, items: str = "1.02",
             classification: str = "", contract_value_usd: float | None = None,
             counterparty: str = "", key_quote: str = "",
             confidence: float | None = None,
             accession: str | None = None):
        accession = accession or f"{ticker}-{filed_at.timestamp():.0f}"
        db.upsert_filings([{
            "accession_no": accession,
            "ticker": ticker,
            "cik": "0000000000",
            "filed_at": filed_at.isoformat(),
            "form_type": "8-K",
            "items": items,
            "raw_text_url": f"https://example.com/{accession}",
        }])
        if classification or contract_value_usd is not None or counterparty or key_quote:
            db.update_filing_classification(
                accession,
                classification=classification or "",
                confidence=confidence if confidence is not None else 0.8,
                contract_value_usd=contract_value_usd,
                counterparty=counterparty or None,
                key_quote=key_quote or None,
            )
        return accession

    return _seed
