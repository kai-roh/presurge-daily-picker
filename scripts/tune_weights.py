"""W4 #5 — 패턴 weight + tier threshold sensitivity sweep.

각 config를 ENV 변수로 override해서 별도 subprocess로 24mo backtest를 돌리고
H1~H4 verdict를 비교한다. config.py가 import 시점에 ENV를 캡처하므로 별도 process가
가장 안정적.

실행:
    python -m scripts.tune_weights --start 2024-05-01 --end 2026-05-01

출력: 각 config의 trades / Tier 1 n / H4 Spearman / H3 Toss alpha 비교 표.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

# 시도할 weight/threshold 조합. 각 dict는 ENV override 변수 → 값.
CONFIGS: list[dict[str, str]] = [
    {"name": "baseline"},
    # Tier 1 진입 임계치 낮춤 → H1 sample 늘리기
    {"name": "tier1_60", "TIER1_PSS_MIN_OVERRIDE": "60"},
    {"name": "tier1_50", "TIER1_PSS_MIN_OVERRIDE": "50"},
    # Pattern E max 다운 — 대부분 데이터 한계로 0% recovery 통과해 동일 점수 받는 노이즈
    {"name": "E_max_15", "PATTERN_E_MAX_OVERRIDE": "15"},
    # 위 둘 결합
    {"name": "E15_tier60", "PATTERN_E_MAX_OVERRIDE": "15", "TIER1_PSS_MIN_OVERRIDE": "60"},
    # Pattern A 강조 (8-K 검증된 신호)
    {"name": "A_boost", "PATTERN_A_MAX_OVERRIDE": "40"},
    # Pattern C 강조 (재분류 후 정확)
    {"name": "C_boost", "PATTERN_C_MAX_OVERRIDE": "70"},
    # 가장 공격적 시나리오: Tier1=50, A+C boost
    {
        "name": "aggressive",
        "TIER1_PSS_MIN_OVERRIDE": "50",
        "PATTERN_A_MAX_OVERRIDE": "40",
        "PATTERN_C_MAX_OVERRIDE": "70",
        "PATTERN_E_MAX_OVERRIDE": "15",
    },
]


def run_one(start: str, end: str, cfg: dict[str, str]) -> dict[str, Any]:
    """단일 config로 backtest 실행, 결과 dict 반환."""
    env = os.environ.copy()
    for k, v in cfg.items():
        if k != "name":
            env[k] = v
    out_path = f"/tmp/tune_{cfg['name']}.json"
    cmd = [
        "python3",
        "-m",
        "scripts.run_backtest",
        "--start", start,
        "--end", end,
        "--tiers", "1,2,3",
        "--out", out_path,
    ]
    started = time.monotonic()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, cwd=os.getcwd()
    )
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        logger.warning("config %s failed: %s", cfg["name"], proc.stderr[-500:])
        return {"name": cfg["name"], "error": proc.stderr[-200:]}
    with open(out_path) as f:
        result = json.load(f)
    summary = {
        "name": cfg["name"],
        "elapsed_sec": round(elapsed, 1),
        "n_trades": result["n_trades"],
        "tier1_n": result["by_tier"].get("1", {}).get("n", 0),
        "tier2_n": result["by_tier"].get("2", {}).get("n", 0),
        "tier3_n": result["by_tier"].get("3", {}).get("n", 0),
    }
    for v in result["verdicts"]:
        if "H1" in v["name"]:
            summary["h1_hit_rate"] = v["measured"]
            summary["h1_n"] = v["sample_size"]
        elif "H3" in v["name"]:
            summary["h3_alpha"] = v["measured"]
            summary["h3_pass"] = v["passed"]
        elif "H4" in v["name"]:
            summary["h4_spearman"] = v["measured"]
            summary["h4_pass"] = v["passed"]
            summary["h4_n"] = v["sample_size"]
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-05-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default="/tmp/tune_summary.json")
    args = parser.parse_args(argv)

    results: list[dict[str, Any]] = []
    for i, cfg in enumerate(CONFIGS, 1):
        logger.info("[%d/%d] running %s with overrides=%s",
                    i, len(CONFIGS), cfg["name"],
                    {k: v for k, v in cfg.items() if k != "name"})
        r = run_one(args.start, args.end, cfg)
        logger.info("  -> %s", r)
        results.append(r)
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)

    # 결과 표
    logger.info("=== Summary ===")
    logger.info(
        "%-16s %6s %6s %6s %8s %8s %8s",
        "config", "trades", "T1_n", "h4_n", "h4_spear", "h3_alpha", "h1_hit"
    )
    for r in results:
        if "error" in r:
            logger.info("%-16s ERROR: %s", r["name"], r["error"])
            continue
        logger.info(
            "%-16s %6d %6d %6d %8.4f %8.4f %8.4f",
            r["name"],
            r.get("n_trades", 0),
            r.get("tier1_n", 0),
            r.get("h4_n", 0),
            r.get("h4_spearman", 0.0),
            r.get("h3_alpha", 0.0),
            r.get("h1_hit_rate", 0.0),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
