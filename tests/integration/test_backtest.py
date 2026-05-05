"""백테스트 러너 smoke test — known cases 시드를 시뮬해서 entry가 잡히는지 확인."""
from __future__ import annotations

from datetime import date

import pytest

from scripts.seed_known_cases import seed_all
from src.backtest.runner import run_backtest
from src.storage.db import Database


@pytest.fixture
def seeded_db(tmp_path):
    db = Database(tmp_path / "bt.db")
    db.init_schema()
    seed_all(db)
    yield db
    db.close()


def test_run_backtest_picks_up_known_tier1(seeded_db):
    # BYND surge_date=2025-10-20, TNXP surge_date=2025-08-10 모두 커버
    result = run_backtest(
        seeded_db,
        start=date(2025, 8, 5),
        end=date(2025, 10, 25),
        tiers=(1,),
        hold_days_list=(1, 2, 5),
    )
    tickers_traded = {t.ticker for t in result.trades}
    assert "BYND" in tickers_traded or "TNXP" in tickers_traded, (
        f"expected at least one tier-1 known case; got {tickers_traded}"
    )


def test_backtest_respects_lookahead(seeded_db):
    """entry_date는 score_date 익영업일이고 entry_price > 0 이어야 한다."""
    result = run_backtest(
        seeded_db,
        start=date(2025, 10, 20),  # BYND surge day - C filing visible
        end=date(2025, 10, 22),
        tiers=(1,),
        hold_days_list=(1, 2),
    )
    bynd_trades = [t for t in result.trades if t.ticker == "BYND"]
    assert bynd_trades, "BYND should be Tier 1 on/after C filing"
    for tr in bynd_trades:
        assert tr.entry_date > tr.score_date
        assert tr.entry_price > 0
