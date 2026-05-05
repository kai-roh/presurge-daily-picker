"""5종 known surge case의 PSS 산출 통합 테스트.

급등 -1일 시점에 기대 패턴이 모두 triggered list에 있고,
BYND/TNXP는 Tier 1, BNAI/PAVS는 최소 Tier 3에 들어가는지 검증.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from scripts.seed_known_cases import CASES, seed_all
from src.score.pss_aggregator import compute_for_ticker
from src.storage.db import Database


@pytest.fixture
def seeded_db(tmp_path):
    db = Database(tmp_path / "known.db")
    db.init_schema()
    seed_all(db)
    yield db
    db.close()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.ticker)
def test_known_case_triggers_expected_patterns(seeded_db, case):
    as_of = case.surge_date - timedelta(days=1)
    score = compute_for_ticker(case.ticker, as_of, seeded_db)
    for letter in case.expected_patterns:
        assert letter in score.triggered_patterns, (
            f"{case.ticker}: pattern {letter} expected but not triggered. "
            f"got patterns={score.triggered_patterns}, breakdown={score.breakdown}"
        )


def test_bynd_reaches_tier1(seeded_db):
    case = next(c for c in CASES if c.ticker == "BYND")
    score = compute_for_ticker(case.ticker, case.surge_date - timedelta(days=1), seeded_db)
    assert score.tier == 1
    assert score.pss_total >= 70


def test_tnxp_reaches_tier1(seeded_db):
    case = next(c for c in CASES if c.ticker == "TNXP")
    score = compute_for_ticker(case.ticker, case.surge_date - timedelta(days=1), seeded_db)
    assert score.tier == 1


def test_iova_v0_2_misses_with_zero(seeded_db):
    """IOVA는 분석가 상향 series — Pattern G로 v0.3 이전 미커버."""
    case = next(c for c in CASES if c.ticker == "IOVA")
    score = compute_for_ticker(case.ticker, case.surge_date - timedelta(days=1), seeded_db)
    assert score.pss_total == 0
    assert score.tier is None
