from src.score import PatternE


def test_brand_penny_basic(db, today, seed_universe, seed_bars):
    seed_universe("BYND", mcap=80_000_000, historical_max_mcap=14_000_000_000)
    seed_bars("BYND", today, [1.20, 1.10, 1.00])
    res = PatternE().compute("BYND", today, db)
    # recovery ≈ 0.0057 → ~19.9 base score (no mentions/debt bonus)
    assert res.score >= 18.0
    assert res.triggered


def test_recovery_too_high_returns_zero(db, today, seed_universe, seed_bars):
    seed_universe("FOO", mcap=500_000_000, historical_max_mcap=1_000_000_000)
    seed_bars("FOO", today, [3.00])
    res = PatternE().compute("FOO", today, db)
    assert res.score == 0.0


def test_price_out_of_range_returns_zero(db, today, seed_universe, seed_bars):
    seed_universe("FOO", mcap=50_000_000, historical_max_mcap=10_000_000_000)
    seed_bars("FOO", today, [0.30])
    res = PatternE().compute("FOO", today, db)
    assert res.score == 0.0


def test_no_historical_max_returns_zero(db, today, seed_universe, seed_bars):
    seed_universe("FOO", mcap=50_000_000, historical_max_mcap=None)
    seed_bars("FOO", today, [2.00])
    res = PatternE().compute("FOO", today, db)
    assert res.score == 0.0
