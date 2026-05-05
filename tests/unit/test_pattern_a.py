from datetime import datetime, timedelta

from src.score import PatternA


def test_recent_24h_termination_scores_30(db, today, seed_universe, seed_filing):
    seed_universe("BNAI", mcap=50_000_000)
    seed_filing(
        "BNAI",
        filed_at=datetime.combine(today, datetime.min.time()),
        items="1.02",
        key_quote="ATM termination of standby equity purchase agreement",
    )
    res = PatternA().compute("BNAI", today, db)
    assert res.score == 30.0
    assert res.triggered


def test_7d_termination_scores_20(db, today, seed_universe, seed_filing):
    seed_universe("PAVS", mcap=20_000_000)
    seed_filing(
        "PAVS",
        filed_at=datetime.combine(today, datetime.min.time()) - timedelta(days=4),
        items="1.02",
        key_quote="equity purchase agreement terminated",
    )
    res = PatternA().compute("PAVS", today, db)
    assert res.score == 20.0
    assert res.triggered


def test_no_match_returns_zero(db, today, seed_universe):
    seed_universe("XYZ")
    res = PatternA().compute("XYZ", today, db)
    assert res.score == 0.0
    assert not res.triggered
