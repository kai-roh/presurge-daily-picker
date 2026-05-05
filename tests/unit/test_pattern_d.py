from datetime import date

from src.score import PatternD


def test_high_si_low_float_scores(db, today, seed_universe, seed_bars):
    seed_universe("BYND", mcap=300_000_000, float_shares=40_000_000)
    seed_bars("BYND", today, [0.50, 0.60, 0.70, 0.80, 0.90, 1.00] + [1.00] * 30)
    db.upsert_short_interest([{
        "ticker": "BYND",
        "settle_date": (today.replace(day=15) if today.day > 15 else today.replace(day=1)).isoformat(),
        "si_shares": 25_000_000,
        "si_pct_float": 0.63,         # 63% SI
        "days_to_cover": 7.0,
        "cost_to_borrow": 0.40,
        "source": "finra",
    }])
    res = PatternD().compute("BYND", today, db)
    # SI 31.5 (capped 15) + DTC 10.5 (capped 9) + CTB 4 + Float 1 + drop bonus 5
    assert res.score >= 25.0
    assert res.triggered


def test_low_si_returns_zero(db, today, seed_universe):
    seed_universe("FOO", float_shares=200_000_000)
    db.upsert_short_interest([{
        "ticker": "FOO",
        "settle_date": today.isoformat(),
        "si_shares": 1_000_000,
        "si_pct_float": 0.05,
        "days_to_cover": 1.0,
        "cost_to_borrow": 0.02,
        "source": "finra",
    }])
    res = PatternD().compute("FOO", today, db)
    assert res.score == 0.0


def test_no_si_returns_zero(db, today, seed_universe):
    seed_universe("FOO")
    res = PatternD().compute("FOO", date.today(), db)
    assert res.score == 0.0
