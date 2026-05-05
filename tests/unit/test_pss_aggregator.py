from datetime import datetime, timedelta

from src.score.pss_aggregator import (
    classify_tiers,
    compute_for_ticker,
    compute_universe,
    persist,
)


def test_tier1_requires_two_patterns(db, today, seed_universe, seed_filing, seed_bars):
    # Pattern A (24h ATM termination) + Pattern C (DOD contract)
    seed_universe("TNXP", mcap=50_000_000, float_shares=7_000_000)
    seed_bars("TNXP", today, [3.0] * 35)
    seed_filing(
        "TNXP",
        filed_at=datetime.combine(today, datetime.min.time()),
        items="1.02",
        key_quote="ATM termination",
        accession="acc-a",
    )
    seed_filing(
        "TNXP",
        filed_at=datetime.combine(today, datetime.min.time()) - timedelta(hours=10),
        items="1.01",
        classification="C",
        contract_value_usd=34_000_000,
        counterparty="DOD",
        key_quote="DOD contract",
        accession="acc-c",
    )

    score = compute_for_ticker("TNXP", today, db)
    assert "A" in score.triggered_patterns
    assert "C" in score.triggered_patterns
    assert score.pss_total >= 70
    assert score.tier == 1


def test_recent_run_penalty(db, today, seed_universe, seed_filing, seed_bars):
    seed_universe("FOO", mcap=300_000_000)
    # +60% in 30 days → -30 penalty
    seed_bars("FOO", today, [1.6] + [1.0] * 35)
    seed_filing(
        "FOO",
        filed_at=datetime.combine(today, datetime.min.time()),
        items="1.02",
        key_quote="equity purchase agreement terminated",
    )
    score = compute_for_ticker("FOO", today, db)
    assert score.penalty_run == -30.0


def test_classify_tiers_caps(db, today):
    # Build a synthetic list of TickerScore-like objects using compute_for_ticker stubs.
    from src.score.pss_aggregator import TickerScore

    scores = [
        TickerScore(
            ticker=f"T{i}",
            pss_total=100 - i,
            tier=1 if i < 5 else (2 if i < 12 else 3),
            triggered_patterns=["A", "C"] if i < 5 else (["A"] if i < 12 else []),
            breakdown={},
            bonus_toss=0,
            penalty_run=0,
            penalty_earn=0,
        )
        for i in range(20)
    ]
    tiers = classify_tiers(scores)
    assert len(tiers[1]) == 3
    assert len(tiers[2]) == 5
    assert len(tiers[3]) == 8  # 20 - 5 - 7 = 8 tier-3 candidates, cap 10 → 8


def test_persist_and_compute_universe(db, today, seed_universe):
    seed_universe("AAA", mcap=300_000_000)
    seed_universe("BBB", mcap=400_000_000)
    scores = compute_universe(today, db)
    assert len(scores) == 2
    persist(scores, today, db)
    row = db.get_pss(today, "AAA")
    assert row is not None
    assert row["pss_total"] == 0.0
