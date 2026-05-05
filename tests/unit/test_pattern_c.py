from datetime import datetime, timedelta

from src.score import PatternC


def test_dod_contract_high_ratio_scores_max(db, today, seed_universe, seed_filing):
    seed_universe("TNXP", mcap=50_000_000)
    seed_filing(
        "TNXP",
        filed_at=datetime.combine(today, datetime.min.time()) - timedelta(hours=12),
        items="1.01",
        classification="C",
        contract_value_usd=34_000_000,  # 68% of mcap
        counterparty="DOD DTRA",
        key_quote="Department of Defense DTRA five-year contract",
        confidence=0.92,
    )
    res = PatternC().compute("TNXP", today, db)
    # 50 (≥10% ratio) + 5 (gov bonus) clamped to max 50
    assert res.score == 50.0
    assert res.triggered


def test_walmart_partnership_2pct_ratio(db, today, seed_universe, seed_filing):
    seed_universe("BYND", mcap=300_000_000)
    seed_filing(
        "BYND",
        filed_at=datetime.combine(today, datetime.min.time()) - timedelta(hours=4),
        items="1.01",
        classification="C",
        contract_value_usd=12_000_000,  # 4% of mcap → 20점 tier
        counterparty="Walmart",
        key_quote="Walmart distribution expansion to 2,000 stores",
        confidence=0.85,
    )
    res = PatternC().compute("BYND", today, db)
    assert res.score == 20.0
    assert res.triggered


def test_no_filing_returns_zero(db, today, seed_universe):
    seed_universe("FOO", mcap=400_000_000)
    res = PatternC().compute("FOO", today, db)
    assert res.score == 0.0


def test_unscored_contract_falls_back_to_10(db, today, seed_universe, seed_filing):
    seed_universe("FOO", mcap=400_000_000)
    seed_filing(
        "FOO",
        filed_at=datetime.combine(today, datetime.min.time()),
        items="1.01",
        classification="C",
        contract_value_usd=None,
        counterparty="DOD",
        key_quote="contract awarded",
        confidence=0.7,
    )
    res = PatternC().compute("FOO", today, db)
    assert res.score == 10.0
