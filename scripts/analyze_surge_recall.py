"""실제 급등 종목 대비 PSS 잡힌 비율 (Recall) 분석.

trade_log = precision (우리 picks 중 적중률).
surge_events = recall (실제 급등 중 PSS가 잡은 비율).

리포트:
1. 전체 recall: surges 중 was_picked 비율
2. surge_type 별 분해 (high_1d_10 / high_1d_20 / close_1d_10)
3. PSS 임계치별 recall: prev_pss_total >= N 인 surge가 전체 surge 중 차지하는 비율
4. 패턴별 recall: 어떤 패턴이 trigger 됐던 surge 비율
5. miss 분석: PSS=0인 surge top tickers (현재 패턴이 잡지 못하는 catalyst)

실행:
    python -m scripts.analyze_surge_recall [--start 2024-05-01] [--end 2026-05-08]
                                            [--type high_1d_10]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from src.config import Settings
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def report_overall(db, start: str, end: str, surge_type: str | None) -> dict:
    where = "WHERE surge_date BETWEEN ? AND ?"
    params: list = [start, end]
    if surge_type:
        where += " AND surge_type = ?"
        params.append(surge_type)
    base_query = f"FROM surge_events {where}"
    total = db.conn.execute(f"SELECT COUNT(*) AS n {base_query}", params).fetchone()["n"]
    picked = db.conn.execute(
        f"SELECT COUNT(*) AS n {base_query} AND was_picked = 1", params
    ).fetchone()["n"]
    pss_present = db.conn.execute(
        f"SELECT COUNT(*) AS n {base_query} AND prev_pss_total IS NOT NULL", params
    ).fetchone()["n"]
    return {
        "total_surges": total,
        "picked": picked,
        "recall": picked / total if total else 0.0,
        "pss_data_coverage": pss_present / total if total else 0.0,
    }


def report_by_type(db, start: str, end: str) -> list[dict]:
    rows = db.conn.execute(
        """
        SELECT surge_type, COUNT(*) AS total,
               SUM(was_picked) AS picked,
               SUM(CASE WHEN prev_pss_total IS NOT NULL THEN 1 ELSE 0 END) AS pss_present
        FROM surge_events
        WHERE surge_date BETWEEN ? AND ?
        GROUP BY surge_type
        ORDER BY total DESC
        """,
        (start, end),
    ).fetchall()
    return [
        {
            "type": r["surge_type"],
            "total": r["total"],
            "picked": r["picked"],
            "recall": (r["picked"] / r["total"]) if r["total"] else 0.0,
            "pss_data_coverage": (r["pss_present"] / r["total"]) if r["total"] else 0.0,
        }
        for r in rows
    ]


def report_pss_thresholds(db, start: str, end: str, surge_type: str) -> list[dict]:
    """prev_pss_total >= threshold 인 surge가 전체 중 차지하는 비율 (= 그 임계로 풀했을 때 recall)."""
    rows = db.conn.execute(
        """
        SELECT prev_pss_total, was_picked
        FROM surge_events
        WHERE surge_date BETWEEN ? AND ? AND surge_type = ?
        """,
        (start, end, surge_type),
    ).fetchall()
    if not rows:
        return []
    total = len(rows)
    out = []
    for thr in (10, 20, 30, 40, 50, 60, 70):
        count_at_thr = sum(
            1 for r in rows
            if (r["prev_pss_total"] or 0) >= thr
        )
        out.append({
            "pss_threshold": thr,
            "surges_above_threshold": count_at_thr,
            "recall_if_all_above_picked": count_at_thr / total,
        })
    return out


def report_by_pattern(db, start: str, end: str, surge_type: str) -> list[dict]:
    """surge 시점 (전 영업일) 어떤 pattern triggered 였는지 분포."""
    rows = db.conn.execute(
        """
        SELECT prev_patterns
        FROM surge_events
        WHERE surge_date BETWEEN ? AND ? AND surge_type = ?
          AND prev_patterns IS NOT NULL AND prev_patterns != ''
        """,
        (start, end, surge_type),
    ).fetchall()
    if not rows:
        return []
    counter: dict[str, int] = {}
    for r in rows:
        pats = (r["prev_patterns"] or "").split(",")
        for p in pats:
            p = p.strip().upper()
            if p:
                counter[p] = counter.get(p, 0) + 1
    return sorted(
        [{"pattern": k, "n_surges_with_pattern": v} for k, v in counter.items()],
        key=lambda x: -x["n_surges_with_pattern"],
    )


def report_misses(db, start: str, end: str, surge_type: str, limit: int = 15) -> list[dict]:
    """PSS=0 또는 NULL인 surge 중 도달률 큰 순. 이게 우리가 놓치는 catalyst."""
    rows = db.conn.execute(
        """
        SELECT ticker, surge_date, surge_pct, prev_pss_total
        FROM surge_events
        WHERE surge_date BETWEEN ? AND ? AND surge_type = ?
          AND (prev_pss_total IS NULL OR prev_pss_total < 10)
          AND was_picked = 0
        ORDER BY surge_pct DESC
        LIMIT ?
        """,
        (start, end, surge_type, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-05-01")
    parser.add_argument("--end", default="2026-05-08")
    parser.add_argument("--type", default="high_1d_10",
                        help="기본 분석 대상 surge_type")
    parser.add_argument("--out", default="/tmp/surge_recall.json")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    overall = report_overall(db, args.start, args.end, args.type)
    by_type = report_by_type(db, args.start, args.end)
    thresholds = report_pss_thresholds(db, args.start, args.end, args.type)
    by_pattern = report_by_pattern(db, args.start, args.end, args.type)
    misses = report_misses(db, args.start, args.end, args.type, 15)

    print("=" * 60)
    print(f"SURGE RECALL — type={args.type}  range={args.start}..{args.end}")
    print("=" * 60)
    print()
    print("[Overall]")
    print(f"  total surges: {overall['total_surges']:,}")
    print(f"  picked (was in watchlist): {overall['picked']:,}")
    print(f"  recall: {overall['recall']:.2%}")
    print(f"  PSS data coverage: {overall['pss_data_coverage']:.2%}")
    print()
    print("[By surge_type]")
    print(f"  {'type':18s} {'total':>8s} {'picked':>8s} {'recall':>8s} {'pss_cov':>8s}")
    for r in by_type:
        print(f"  {r['type']:18s} {r['total']:>8,} {r['picked']:>8,} "
              f"{r['recall']:>7.2%} {r['pss_data_coverage']:>7.2%}")
    print()
    print(f"[PSS threshold sensitivity — {args.type}]")
    print(f"  {'PSS≥':>6s} {'surges':>10s} {'cumulative recall':>20s}")
    for r in thresholds:
        print(f"  {r['pss_threshold']:>6d} {r['surges_above_threshold']:>10,} "
              f"{r['recall_if_all_above_picked']:>19.2%}")
    print()
    print(f"[Pattern presence in surges — {args.type}]")
    print(f"  surges with at least one prev_pattern: {sum(p['n_surges_with_pattern'] for p in by_pattern)}")
    for p in by_pattern:
        print(f"  pattern {p['pattern']:5s} {p['n_surges_with_pattern']:>7,}")
    print()
    print(f"[Top 15 misses — {args.type} (PSS<10, not picked)]")
    print(f"  {'date':12s} {'ticker':8s} {'surge%':>8s} {'prev_pss':>9s}")
    for m in misses:
        pss = m['prev_pss_total'] or 0
        print(f"  {m['surge_date']} {m['ticker']:8s} {m['surge_pct']:>7.1%} {pss:>9.1f}")

    out = {
        "overall": overall,
        "by_type": by_type,
        "pss_thresholds": thresholds,
        "by_pattern": by_pattern,
        "misses_top15": misses,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nJSON saved: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
