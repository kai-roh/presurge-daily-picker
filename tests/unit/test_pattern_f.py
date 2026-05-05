from datetime import datetime, timedelta

from src.score import PatternF


def test_sector_keyword_match_scores(db, today, seed_universe):
    seed_universe("AICO", sector="AI Software", name="AI Corp")
    res = PatternF().compute("AICO", today, db)
    # name "ai corp" + sector "ai software" → 2 hits → 10
    # 10 < trigger_threshold (12) — sector 단독은 trigger 안 함 (pivot 또는 WSB 추가 필요)
    assert res.score == 10.0
    assert not res.triggered


def test_pivot_filing_high_confidence_adds_5(db, today, seed_universe, seed_filing):
    seed_universe("AICO", sector="AI Software")
    seed_filing(
        "AICO",
        filed_at=datetime.combine(today, datetime.min.time()) - timedelta(days=2),
        items="8.01",
        classification="F",
        confidence=0.85,
        key_quote="company pivots to artificial intelligence platform",
    )
    res = PatternF().compute("AICO", today, db)
    # sector ai (5) + pivot (5) = 10
    assert res.score >= 10.0


def test_weak_pivot_filing_penalized(db, today, seed_universe, seed_filing):
    seed_universe("FOO", sector="Technology", name="Foo Corp")
    seed_filing(
        "FOO",
        filed_at=datetime.combine(today, datetime.min.time()),
        items="8.01",
        classification="",
        confidence=0.3,
        key_quote="we will explore AI capabilities in the future",
    )
    res = PatternF().compute("FOO", today, db)
    # No sector hit, pivot exists but confidence low → -3 → clamp 0
    assert res.score == 0.0


def test_no_megatheme_returns_zero(db, today, seed_universe):
    seed_universe("FOO", sector="Construction", name="Foo Inc")
    res = PatternF().compute("FOO", today, db)
    assert res.score == 0.0


def test_wsb_growth_adds_5(db, today, seed_universe):
    seed_universe("FOO", sector="AI Robotics", name="Foo")
    db.upsert_social([{
        "ticker": "FOO",
        "mention_date": (today - timedelta(days=1)).isoformat(),
        "source": "reddit_wsb",
        "mentions": 30,
        "bullish_pct": None,
        "rank": 50,
    }, {
        "ticker": "FOO",
        "mention_date": today.isoformat(),
        "source": "reddit_wsb",
        "mentions": 200,
        "bullish_pct": None,
        "rank": 5,
    }])
    res = PatternF().compute("FOO", today, db)
    # 2 sector hits (10) + WSB growth bonus (5) = 15
    assert res.score >= 15.0
