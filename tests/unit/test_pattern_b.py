from datetime import date, timedelta

from src.score import PatternB


def test_announced_pre_effective_scores_25(db, today, seed_universe):
    seed_universe("TNXP", mcap=60_000_000)
    db.upsert_index_event({
        "ticker": "TNXP",
        "index_name": "Russell 2000",
        "announced_at": (today - timedelta(days=3)).isoformat(),
        "effective_at": (today + timedelta(days=2)).isoformat(),
        "source": "manual_seed",
        "notes": "",
    })
    res = PatternB().compute("TNXP", today, db)
    # 25 + 5 (mcap < 500M bonus)
    assert res.score == PatternB.max_score
    assert res.triggered


def test_post_effective_window_scores_15(db, today, seed_universe):
    seed_universe("FOO", mcap=800_000_000)
    db.upsert_index_event({
        "ticker": "FOO",
        "index_name": "MEME ETF",
        "announced_at": (today - timedelta(days=10)).isoformat(),
        "effective_at": (today - timedelta(days=3)).isoformat(),
        "source": "roundhill",
        "notes": "",
    })
    res = PatternB().compute("FOO", today, db)
    assert res.score == 15.0
    assert res.triggered


def test_no_event_returns_zero(db, today, seed_universe):
    seed_universe("FOO")
    res = PatternB().compute("FOO", today, db)
    assert res.score == 0.0


def test_old_effective_returns_zero(db, today, seed_universe):
    seed_universe("FOO")
    db.upsert_index_event({
        "ticker": "FOO",
        "index_name": "Russell 2000",
        "announced_at": (today - timedelta(days=60)).isoformat(),
        "effective_at": (today - timedelta(days=30)).isoformat(),
        "source": "manual",
        "notes": "",
    })
    res = PatternB().compute("FOO", today, db)
    assert res.score == 0.0


def test_db_helper_filters_correctly(db):
    db.upsert_index_event({
        "ticker": "AAA",
        "index_name": "X",
        "announced_at": "2026-01-01",
        "effective_at": "2026-01-15",
        "source": "manual",
        "notes": "",
    })
    rows = db.index_inclusion_events("AAA", date(2026, 1, 10))
    assert len(rows) == 1
    rows = db.index_inclusion_events("AAA", date(2026, 2, 1))  # well past effective + 7d
    assert len(rows) == 0
