"""Pattern G — Volume Spike (RVOL) 단위 테스트."""
from __future__ import annotations

from datetime import date, timedelta

from src.score import PatternG


def _seed_bars_volumes(db, ticker: str, anchor: date, volumes: list[int]) -> None:
    """anchor부터 과거로 N일 거래량 시드. volumes[0]=anchor 당일."""
    rows = []
    for i, v in enumerate(volumes):
        d = anchor - timedelta(days=i)
        rows.append({
            "ticker": ticker,
            "trade_date": d.isoformat(),
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": v, "vwap": 10.0,
        })
    db.upsert_bars(rows)


def test_rvol_5x_full_score(db, today, seed_universe):
    seed_universe("HOTV")
    # 직전 30영업일 평균 1M, 최근일 6M → RVOL 6
    volumes = [6_000_000] + [1_000_000] * 30
    _seed_bars_volumes(db, "HOTV", today, volumes)

    res = PatternG().compute("HOTV", today, db)
    assert res.score == PatternG.max_score
    assert res.triggered
    assert res.contributing_signals["rvol"] >= 5.0


def test_rvol_3x_75pct(db, today, seed_universe):
    seed_universe("MIDV")
    volumes = [3_500_000] + [1_000_000] * 30  # rvol ~3.5
    _seed_bars_volumes(db, "MIDV", today, volumes)

    res = PatternG().compute("MIDV", today, db)
    assert res.score == PatternG.max_score * 0.75
    assert res.triggered


def test_rvol_2x_50pct(db, today, seed_universe):
    seed_universe("LOWV")
    volumes = [2_200_000] + [1_000_000] * 30
    _seed_bars_volumes(db, "LOWV", today, volumes)

    res = PatternG().compute("LOWV", today, db)
    assert res.score == PatternG.max_score * 0.50
    assert res.triggered  # threshold = 50% = exactly trigger


def test_rvol_below_1_5_zero(db, today, seed_universe):
    seed_universe("FLAT")
    volumes = [1_100_000] + [1_000_000] * 30  # rvol 1.1
    _seed_bars_volumes(db, "FLAT", today, volumes)

    res = PatternG().compute("FLAT", today, db)
    assert res.score == 0.0
    assert not res.triggered


def test_no_baseline_returns_zero(db, today, seed_universe):
    seed_universe("NEW")
    # 1개 거래일만 (baseline 부족)
    _seed_bars_volumes(db, "NEW", today, [5_000_000])

    res = PatternG().compute("NEW", today, db)
    assert res.score == 0.0


def test_zero_volume_returns_zero(db, today, seed_universe):
    seed_universe("DEAD")
    volumes = [0] + [1_000_000] * 30
    _seed_bars_volumes(db, "DEAD", today, volumes)

    res = PatternG().compute("DEAD", today, db)
    assert res.score == 0.0
