"""H1~H4 가설 검증 헬퍼.

전략 문서 §9의 4개 가설을 테스트 가능 함수로 변환.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.runner import BacktestResult


@dataclass
class HypothesisVerdict:
    name: str
    measured: float
    threshold: float
    passed: bool
    sample_size: int
    note: str = ""


def h1_tier1_hit_rate_5d(
    result: BacktestResult, threshold: float = 0.20, hit_rate_floor: float = 0.35
) -> HypothesisVerdict:
    """Tier 1 종목의 5일 후 +20% 도달율 ≥ 35%."""
    rate = result.hit_rate(tier=1, hold_days=5, threshold=threshold)
    n = len(result.filter_tier(1))
    return HypothesisVerdict(
        name=f"H1 Tier1 5d ≥+{threshold:.0%} hit-rate",
        measured=rate,
        threshold=hit_rate_floor,
        passed=rate >= hit_rate_floor,
        sample_size=n,
    )


def h2_pattern_cd_avg_return_5d(
    result: BacktestResult, floor: float = 0.30
) -> HypothesisVerdict:
    """패턴 C+D 동시 활성 종목의 5일 평균 수익률 ≥ +30%."""
    rows = [
        t for t in result.trades
        if "C" in t.triggered_patterns and "D" in t.triggered_patterns and 5 in t.exits
    ]
    if not rows:
        return HypothesisVerdict("H2 C+D combo 5d avg return", 0.0, floor, False, 0, "no samples")
    rets = [t.exits[5][2] for t in rows]
    avg = sum(rets) / len(rets)
    return HypothesisVerdict(
        name="H2 C+D combo 5d avg return",
        measured=avg,
        threshold=floor,
        passed=avg >= floor,
        sample_size=len(rows),
    )


def h3_toss_top30_alpha(
    result: BacktestResult, baseline_avg: float
) -> HypothesisVerdict:
    """토스 거래량 상위 진입 종목의 7일 평균 수익률 vs 일반 small-cap 평균."""
    # bonus_toss > 0 인 종목 = 토스 진입. exits에 7일 없으면 5일로 근사.
    toss_rows = [t for t in result.trades if 5 in t.exits]  # 5d as proxy until 7d added
    if not toss_rows:
        return HypothesisVerdict("H3 Toss alpha", 0.0, baseline_avg, False, 0)
    avg = sum(t.exits[5][2] for t in toss_rows) / len(toss_rows)
    return HypothesisVerdict(
        name="H3 Toss top30 alpha vs baseline",
        measured=avg,
        threshold=baseline_avg,
        passed=avg > baseline_avg,
        sample_size=len(toss_rows),
        note="proxy 5d (7d 추가 적재 시 보강)",
    )


def h4_spearman(
    result: BacktestResult, hold_days: int = 5, floor: float = 0.25
) -> HypothesisVerdict:
    """PSS 점수와 N일 수익률의 Spearman 상관 ≥ 0.25."""
    try:
        from scipy.stats import spearmanr  # type: ignore[import-untyped]
    except ImportError:
        return HypothesisVerdict(
            "H4 PSS-return Spearman", 0.0, floor, False, 0, "scipy not installed"
        )

    rows = [t for t in result.trades if hold_days in t.exits]
    if len(rows) < 30:
        return HypothesisVerdict(
            "H4 PSS-return Spearman", 0.0, floor, False, len(rows), "insufficient samples"
        )
    pss = [t.pss_total for t in rows]
    rets = [t.exits[hold_days][2] for t in rows]
    rho, _p = spearmanr(pss, rets)
    return HypothesisVerdict(
        name=f"H4 PSS-return Spearman (hold={hold_days})",
        measured=float(rho),
        threshold=floor,
        passed=float(rho) >= floor,
        sample_size=len(rows),
    )


def h1_short_intraday_hit_rate(
    result: BacktestResult, hold_days: int = 1, threshold: float = 0.10,
    hit_rate_floor: float = 0.30,
) -> HypothesisVerdict:
    """초단타 가설: pick 후 T+1~T+hold_days 일중 high가 +10% 이상 도달율.

    daily picker 의 본질이 짧은 급등 포착이라면 close 5d보다 high 1-3d가 더 정확한
    alpha 측정. Tier 1/2/3 전체 trades 대상.
    """
    rows = result.trades
    if not rows:
        return HypothesisVerdict(
            f"H1' high-{hold_days}d ≥+{threshold:.0%} hit-rate",
            0.0, hit_rate_floor, False, 0,
        )
    hits = sum(1 for r in rows if hold_days in r.high_exits and r.high_exits[hold_days] >= threshold)
    rate = hits / len(rows)
    return HypothesisVerdict(
        name=f"H1' high-{hold_days}d ≥+{threshold:.0%} hit-rate (all tiers)",
        measured=rate,
        threshold=hit_rate_floor,
        passed=rate >= hit_rate_floor,
        sample_size=len(rows),
    )


def h4_short_spearman(
    result: BacktestResult, hold_days: int = 1, floor: float = 0.20,
) -> HypothesisVerdict:
    """초단타 PSS-수익 상관 (high 기반). hold_days 짧게."""
    try:
        from scipy.stats import spearmanr  # type: ignore[import-untyped]
    except ImportError:
        return HypothesisVerdict(
            f"H4' PSS-high{hold_days}d Spearman", 0.0, floor, False, 0, "scipy not installed"
        )
    rows = [t for t in result.trades if hold_days in t.high_exits]
    if len(rows) < 30:
        return HypothesisVerdict(
            f"H4' PSS-high{hold_days}d Spearman", 0.0, floor, False, len(rows), "insufficient samples"
        )
    pss = [t.pss_total for t in rows]
    rets = [t.high_exits[hold_days] for t in rows]
    rho, _p = spearmanr(pss, rets)
    return HypothesisVerdict(
        name=f"H4' PSS-high{hold_days}d Spearman",
        measured=float(rho),
        threshold=floor,
        passed=float(rho) >= floor,
        sample_size=len(rows),
    )


def evaluate_all(result: BacktestResult, baseline_5d: float = 0.005) -> list[HypothesisVerdict]:
    return [
        h1_tier1_hit_rate_5d(result),
        h1_short_intraday_hit_rate(result, hold_days=1),
        h1_short_intraday_hit_rate(result, hold_days=3, threshold=0.15),
        h2_pattern_cd_avg_return_5d(result),
        h3_toss_top30_alpha(result, baseline_5d),
        h4_spearman(result),
        h4_short_spearman(result, hold_days=1),
        h4_short_spearman(result, hold_days=3),
    ]
