"""24개월 PSS 백테스트 + H1~H4 가설 평가.

Universe × 영업일별 PSS 산출, Tier 1 종목 익일 시가 진입 N일 후 종가 청산 시뮬.
결과를 trade_log 테이블에 적재 + H1~H4 verdict를 stdout/JSON에 출력.

실행:
    python -m scripts.run_backtest --start 2024-05-01 --end 2026-05-01
                                    [--tiers 1,2]
                                    [--out backtest_result.json]

look-ahead bias 가드는 src.backtest.runner에서 처리 (filings.filed_at < as_of 등).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import date

from dotenv import load_dotenv

from src.backtest.hypotheses import evaluate_all
from src.backtest.runner import run_backtest
from src.config import Settings
from src.storage.db import get_db

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--tiers", default="1", help="평가할 tier CSV (예: '1' or '1,2')"
    )
    parser.add_argument(
        "--hold-days", default="1,2,3,5", help="청산 보유일 CSV"
    )
    parser.add_argument("--out", default="backtest_result.json")
    parser.add_argument(
        "--save-trades",
        action="store_true",
        help="trade_log 테이블에 시뮬 결과 저장",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="매일 PSS 점수 전체 + watchlist_runs 까지 DB에 영구 적재 (recall 분석용)",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    tiers = tuple(int(t.strip()) for t in args.tiers.split(",") if t.strip())
    hold_days = tuple(int(h.strip()) for h in args.hold_days.split(",") if h.strip())

    logger.info(
        "Backtest %s..%s tiers=%s hold_days=%s", start, end, tiers, hold_days
    )
    started = time.monotonic()
    result = run_backtest(
        db, start, end, tiers=tiers, hold_days_list=hold_days, persist=args.persist
    )
    elapsed = time.monotonic() - started
    logger.info(
        "Backtest done in %.0fs. trades=%d", elapsed, len(result.trades)
    )

    # 가설 평가
    verdicts = evaluate_all(result)
    logger.info("H1~H4 verdicts:")
    for v in verdicts:
        logger.info(
            "  [%s] %s = %.4f (thr=%.4f, n=%d) %s",
            "PASS" if v.passed else "FAIL",
            v.name,
            v.measured,
            v.threshold,
            v.sample_size,
            v.note,
        )

    # trade_log 저장
    if args.save_trades:
        n = 0
        with db.transaction() as conn:
            for t in result.trades:
                exit_5d = t.exits.get(5)
                conn.execute(
                    """
                    INSERT INTO trade_log(
                        ticker, entry_date, entry_price, entry_pss, entry_tier,
                        triggered_patterns, exit_date, exit_price, pnl_pct,
                        exit_reason, is_paper
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'backtest', 1)
                    """,
                    (
                        t.ticker,
                        t.entry_date.isoformat(),
                        t.entry_price,
                        t.pss_total,
                        t.tier,
                        ",".join(t.triggered_patterns),
                        exit_5d[0].isoformat() if exit_5d else None,
                        exit_5d[1] if exit_5d else None,
                        exit_5d[2] if exit_5d else None,
                    ),
                )
                n += 1
        logger.info("Saved %d trades to trade_log", n)

    # JSON dump
    summary = {
        "start": str(start),
        "end": str(end),
        "tiers": list(tiers),
        "hold_days": list(hold_days),
        "n_trades": len(result.trades),
        "elapsed_sec": round(elapsed, 1),
        "by_tier": {
            str(t): {
                "n": len(result.filter_tier(t)),
                "avg_return_5d": result.avg_return(t, 5),
                "hit_rate_20pct_5d": result.hit_rate(t, 5, 0.20),
            }
            for t in tiers
        },
        "verdicts": [asdict(v) for v in verdicts],
    }
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    logger.info("Result JSON saved to %s", args.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
