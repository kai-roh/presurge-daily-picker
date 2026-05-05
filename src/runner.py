"""일별 실행 엔트리포인트.

GitHub Actions cron (00:00 UTC = 09:00 KST) 또는 수동:
    python -m src.runner --mode=daily

단계는 멱등. 실패한 단계만 재실행 가능 (--skip 플래그).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime

from dotenv import load_dotenv

from src.config import Settings
from src.ingest import (
    ApeWisdomFetcher,
    EdgarPoller,
    PolygonBars,
    StockTwitsFetcher,
    TossVolumeFetcher,
)
from src.report import ClaudeSummarizer, TelegramPusher
from src.score import pss_aggregator
from src.storage.db import Database, get_db

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def step_ingest_filings(db: Database, settings: Settings) -> int:
    poller = EdgarPoller(user_agent=settings.sec_user_agent)
    try:
        records = poller.fetch_recent(hours_back=24)
        rows = poller.to_db_rows(records)
        return db.upsert_filings(rows)
    finally:
        poller.close()


def step_ingest_bars(db: Database, settings: Settings) -> int:
    if not settings.polygon_api_key:
        logger.warning("POLYGON_API_KEY missing; skipping bars")
        return 0
    pg = PolygonBars(settings.polygon_api_key)
    try:
        target = pg.previous_trading_day(datetime.now(UTC).date())
        rows = pg.grouped_daily(target)
        from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD

        allowed = set(db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD))
        if allowed:
            rows = pg.filter_universe(rows, allowed)
        return db.upsert_bars(r for r in rows if r.get("ticker"))
    finally:
        pg.close()


def step_ingest_reddit(db: Database) -> int:
    fetcher = ApeWisdomFetcher()
    try:
        raw = fetcher.fetch_top()
        rows = fetcher.to_db_rows(date.today(), raw)
        return db.upsert_social(rows)
    except Exception as exc:
        logger.warning("Reddit ingest failed: %s", exc)
        return 0
    finally:
        fetcher.close()


def step_ingest_stocktwits(db: Database, tickers: list[str]) -> int:
    if not tickers:
        return 0
    fetcher = StockTwitsFetcher()
    n = 0
    today = date.today()
    try:
        for t in tickers:
            try:
                payload = fetcher.fetch_symbol_stream(t)
                summary = fetcher.summarize(payload)
                row = fetcher.to_db_row(t, today, summary)
                db.upsert_social([row])
                n += 1
            except Exception as exc:
                logger.warning("StockTwits %s failed: %s", t, exc)
    finally:
        fetcher.close()
    return n


def step_ingest_toss(db: Database) -> int:
    fetcher = TossVolumeFetcher()
    rows = fetcher.fetch_top30(date.today())
    if rows:
        db.upsert_toss(date.today(), rows)
    return len(rows)


def step_score(db: Database, as_of: date) -> tuple[list, dict]:
    scores = pss_aggregator.compute_universe(as_of, db)
    pss_aggregator.persist(scores, as_of, db)
    tiers = pss_aggregator.classify_tiers(scores)
    logger.info(
        "Scored %d tickers — Tier1=%d Tier2=%d Tier3=%d",
        len(scores), len(tiers[1]), len(tiers[2]), len(tiers[3]),
    )
    return scores, tiers


def step_report(
    db: Database, settings: Settings, as_of: date, tiers: dict
) -> str:
    t1 = [_score_to_dict(s) for s in tiers[1]]
    t2 = [_score_to_dict(s) for s in tiers[2]]
    t3 = [_score_to_dict(s) for s in tiers[3]]

    if settings.anthropic_api_key:
        summ = ClaudeSummarizer(
            api_key=settings.anthropic_api_key,
            report_model=settings.claude_report_model,
            classify_model=settings.claude_classify_model,
        )
        report_md = summ.generate_report(as_of.isoformat(), t1, t2, t3)
    else:
        # API 키 없으면 jinja fallback
        from src.report.claude_summarizer import ClaudeSummarizer as _CS  # noqa
        from jinja2 import Environment, FileSystemLoader
        from pathlib import Path
        env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "report" / "templates")))
        report_md = env.get_template("daily_report.j2").render(
            run_date=as_of.isoformat(), tier1=t1, tier2=t2, tier3=t3
        )

    db.save_watchlist_run(as_of, t1, t2, t3, report_md)
    return report_md


def step_push(settings: Settings, db: Database, as_of: date, report_md: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram credentials missing; skipping push")
        db.mark_pushed(as_of, "skipped_no_creds")
        return
    pusher = TelegramPusher(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        dry_run=settings.dry_run,
    )
    try:
        pusher.send(report_md, parse_mode="Markdown")
        db.mark_pushed(as_of, "success")
    except Exception as exc:
        logger.exception("Telegram push failed: %s", exc)
        db.mark_pushed(as_of, f"error:{type(exc).__name__}")
        raise


def _score_to_dict(s) -> dict:
    return {
        "ticker": s.ticker,
        "pss_total": round(s.pss_total, 1),
        "tier": s.tier,
        "triggered_patterns": s.triggered_patterns,
        "breakdown": s.breakdown,
        "bonus_toss": s.bonus_toss,
        "penalty_run": s.penalty_run,
        "penalty_earn": s.penalty_earn,
    }


def run_daily(settings: Settings, *, skip: set[str] | None = None) -> int:
    skip = skip or set()
    db = get_db(settings.database_url)
    as_of = date.today()
    logger.info("=== Daily run %s ===", as_of)

    if "filings" not in skip:
        try:
            n = step_ingest_filings(db, settings)
            logger.info("filings upserted: %d", n)
        except Exception as exc:
            logger.exception("filings ingest failed: %s", exc)

    if "bars" not in skip:
        try:
            n = step_ingest_bars(db, settings)
            logger.info("bars upserted: %d", n)
        except Exception as exc:
            logger.exception("bars ingest failed: %s", exc)

    if "reddit" not in skip:
        try:
            n = step_ingest_reddit(db)
            logger.info("reddit upserted: %d", n)
        except Exception as exc:
            logger.exception("reddit ingest failed: %s", exc)

    if "toss" not in skip:
        try:
            n = step_ingest_toss(db)
            logger.info("toss upserted: %d", n)
        except Exception as exc:
            logger.exception("toss ingest failed: %s", exc)

    scores, tiers = step_score(db, as_of)

    # stocktwits는 Tier 1/2 후보로만 좁혀서 호출 (rate limit 절약)
    if "stocktwits" not in skip:
        candidate_tickers = [s.ticker for s in tiers[1] + tiers[2]]
        if candidate_tickers:
            try:
                n = step_ingest_stocktwits(db, candidate_tickers)
                logger.info("stocktwits upserted: %d", n)
            except Exception as exc:
                logger.exception("stocktwits failed: %s", exc)

    report_md = step_report(db, settings, as_of, tiers)
    logger.info("Report generated (%d chars)", len(report_md))

    if "push" not in skip:
        try:
            step_push(settings, db, as_of, report_md)
        except Exception as exc:
            logger.exception("push failed: %s", exc)
            return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="presurge-runner")
    parser.add_argument("--mode", choices=["daily"], default="daily")
    parser.add_argument(
        "--skip",
        default="",
        help="CSV of stages to skip: filings,bars,reddit,toss,stocktwits,push",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    _setup_logging(settings.log_level)
    if settings.missing_keys:
        logger.warning("Missing env keys: %s", settings.missing_keys)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    return run_daily(settings, skip=skip)


if __name__ == "__main__":
    sys.exit(main())
