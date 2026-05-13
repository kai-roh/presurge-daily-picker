"""일별 실행 엔트리포인트.

GitHub Actions cron (00:00 UTC = 09:00 KST) 또는 수동:
    python -m src.runner --mode=daily

단계는 멱등. 실패한 단계만 재실행 가능 (--skip 플래그).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
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
        # Polygon 무료티어가 "today before end of day"를 403으로 차단하는 경우 한 영업일
        # 거슬러 fallback. 최근 거래일이 폴리곤 기준 'today' 라면 발생.
        rows = []
        for attempt in range(3):
            try:
                rows = pg.grouped_daily(target)
                break
            except Exception as exc:
                msg = str(exc)
                is_too_recent = (
                    "403" in msg or "NOT_AUTHORIZED" in msg or "before end of day" in msg
                )
                if not is_too_recent or attempt == 2:
                    raise
                logger.warning(
                    "bars %s blocked by Polygon free-tier (likely too recent); "
                    "falling back to previous trading day",
                    target,
                )
                target = pg.previous_trading_day(target)
        from src.config import MARKET_CAP_MAX_USD, MARKET_CAP_MIN_USD

        allowed = set(db.universe_tickers(MARKET_CAP_MIN_USD, MARKET_CAP_MAX_USD))
        if allowed:
            rows = pg.filter_universe(rows, allowed)
        logger.info("bars target=%s rows=%d", target, len(rows))
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
    """Retail trending tickers (Toss app 대체).

    fallback chain (위에서 아래로 시도):
    1. Yahoo Finance trending API (query1 → query2 mirror)
    2. ApeWisdom WSB rank top 30 (오늘 적재된 reddit_wsb mention_rank)
    3. TOSS_TOP30_TICKERS env var (manual seed)

    `toss_top_volume` 테이블 + `bonus_toss` 점수 로직은 보존. 의미만 'Korean retail
    volume'에서 'retail interest (WSB + Yahoo trending)'로 변경.
    """
    from src.ingest.yahoo_trending import fetch_trending, to_db_ranks

    today = date.today()
    tickers = fetch_trending(count=30)
    if tickers:
        db.upsert_toss(today, to_db_ranks(tickers))
        return len(tickers)

    # Fallback 1: ApeWisdom WSB ranking
    rows = db.conn.execute(
        "SELECT ticker FROM social_mentions WHERE source = 'reddit_wsb' AND mention_date = ? "
        "AND rank IS NOT NULL ORDER BY rank LIMIT 30",
        (today.isoformat(),),
    ).fetchall()
    if rows:
        ranks = [(i + 1, r["ticker"]) for i, r in enumerate(rows)]
        db.upsert_toss(today, ranks)
        logger.info("trending fallback: WSB rank top %d", len(ranks))
        return len(ranks)

    # Fallback 2: legacy TOSS_TOP30_TICKERS env seed
    fetcher = TossVolumeFetcher()
    legacy = fetcher.fetch_top30(today)
    if legacy:
        db.upsert_toss(today, legacy)
        return len(legacy)
    return 0


def step_ingest_telegram(db: Database, settings: Settings) -> int:
    """Telegram inbound — 사용자가 보낸 /buy /sell 명령을 trade_log에 적재.

    처리 후 사용자에게 처리 결과 ack 메시지를 보냄 (조용히 누락되지 않게).
    """
    from src.ingest.telegram_inbound import ingest_telegram_commands

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return 0
    res = ingest_telegram_commands(
        settings.telegram_bot_token, settings.telegram_chat_id, db
    )
    n = int(res.get("processed", 0))
    if n > 0:
        ack = "Telegram 명령 처리 완료\n\n" + "\n".join(
            f"- {r}" for r in res.get("results", [])
        )
        try:
            TelegramPusher(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                dry_run=settings.dry_run,
            ).send(ack, parse_mode="HTML")
        except Exception as exc:
            logger.warning("ack push failed: %s", exc)
    return n


def step_classify_new_filings(settings: Settings) -> int:
    """미분류 1.01/1.02 8-K filing을 Claude로 분류.

    daily run 시 신규로 들어온 8-K 메타에 패턴 분류를 채워넣어 PSS 계산이 정상 작동하게.
    1일 cap $5 (실제 일평균 1~10건이라 $0.01~0.05 수준).
    """
    if not settings.anthropic_api_key:
        logger.info("ANTHROPIC_API_KEY missing; skipping classify")
        return 0
    cmd = [
        sys.executable,
        "-m",
        "scripts.classify_filings",
        "--workers", "2",
        "--max-cost-usd", "5",
        "--checkpoint-every", "20",
    ]
    proc = subprocess.run(
        cmd, env=os.environ.copy(), capture_output=True, text=True, cwd=os.getcwd()
    )
    if proc.returncode != 0:
        logger.warning("classify subprocess failed (rc=%d): %s",
                        proc.returncode, proc.stderr[-300:])
        return 0
    # stderr 로 logging이 흘러가니 거기서 classified count 추출
    out = proc.stderr or ""
    for line in out.splitlines()[::-1]:
        if "Done in" in line and "classified=" in line:
            # 예: "Done in 9s. classified=8, no_body=0, fail=0, est_cost=$0.02"
            try:
                n = int(line.split("classified=")[1].split(",")[0])
                return n
            except (IndexError, ValueError):
                pass
    return 0


def step_record_surges(db: Database) -> int:
    """어제 미국 close 기준 universe 급등 자동 적재 (recall 학습용).

    bars 적재 직후에 호출되어야 하며, daily_bars 의 가장 최근 2일 페어로 비교.
    backfill_surges 모듈의 핵심 함수 재사용.
    """
    from datetime import date as _date

    from scripts.backfill_surges import (
        detect_surge_types,
        find_surges,
        lookup_prev_pss,
        lookup_was_picked,
    )

    # daily_bars에서 가장 최근 2 영업일을 surge 페어로
    rows = db.conn.execute(
        "SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return 0
    surge_date = _date.fromisoformat(rows[0]["trade_date"])
    prev_date = _date.fromisoformat(rows[1]["trade_date"])

    pairs = find_surges(db, prev_date, prev_date)
    pick_cache: dict[str, set[str]] = {}
    inserted = 0
    with db.transaction() as conn:
        for p in pairs:
            if p["surge_date"] != surge_date.isoformat():
                continue
            types = detect_surge_types(p)
            if not types:
                continue
            pss_total, tier, patterns = lookup_prev_pss(db, p["ticker"], p["prev_date"])
            picked = lookup_was_picked(db, p["ticker"], p["prev_date"], pick_cache)
            for type_name, pct, _col in types:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO surge_events(
                        surge_date, ticker, surge_type, surge_pct,
                        prev_close, surge_high, surge_close,
                        prev_pss_total, prev_tier, prev_patterns, was_picked
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p["surge_date"], p["ticker"], type_name, pct,
                        p["prev_close"], p["surge_high"], p["surge_close"],
                        pss_total, tier, patterns, picked,
                    ),
                )
                inserted += 1
    return inserted


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


def _alert(settings: Settings, message: str) -> None:
    """치명적 에러를 별도 alert chat으로 푸시. 실패해도 main run에 영향 없음."""
    chat_id = settings.telegram_alert_chat_id or settings.telegram_chat_id
    if not settings.telegram_bot_token or not chat_id:
        return
    try:
        TelegramPusher(
            bot_token=settings.telegram_bot_token,
            chat_id=chat_id,
            dry_run=settings.dry_run,
        ).send(message, parse_mode="HTML")
    except Exception as exc:
        logger.warning("alert push failed: %s", exc)


def run_daily(settings: Settings, *, skip: set[str] | None = None) -> int:
    skip = skip or set()
    db = get_db(settings.database_url)
    as_of = date.today()
    logger.info("=== Daily run %s ===", as_of)

    errors: list[str] = []

    def _stage(name: str, fn) -> None:
        if name in skip:
            return
        try:
            n = fn()
            logger.info("%s done: %s", name, n)
        except Exception as exc:
            logger.exception("%s failed: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")

    _stage("telegram_inbound", lambda: step_ingest_telegram(db, settings))
    _stage("filings", lambda: step_ingest_filings(db, settings))
    _stage("classify", lambda: step_classify_new_filings(settings))
    _stage("bars", lambda: step_ingest_bars(db, settings))
    _stage("reddit", lambda: step_ingest_reddit(db))
    _stage("toss", lambda: step_ingest_toss(db))
    _stage("record_surges", lambda: step_record_surges(db))

    scores, tiers = step_score(db, as_of)

    # stocktwits는 Tier 1/2 후보로만 좁혀서 호출 (rate limit 절약)
    if "stocktwits" not in skip:
        candidate_tickers = [s.ticker for s in tiers[1] + tiers[2]]
        if candidate_tickers:
            try:
                n = step_ingest_stocktwits(db, candidate_tickers)
                logger.info("stocktwits upserted: %d", n)
                # stocktwits 후 점수 재계산 (Pattern F mention growth 가 영향 받음)
                scores, tiers = step_score(db, as_of)
            except Exception as exc:
                logger.exception("stocktwits failed: %s", exc)
                errors.append(f"stocktwits: {type(exc).__name__}")

    report_md = step_report(db, settings, as_of, tiers)
    logger.info("Report generated (%d chars)", len(report_md))

    push_ok = True
    if "push" not in skip:
        try:
            step_push(settings, db, as_of, report_md)
        except Exception as exc:
            logger.exception("push failed: %s", exc)
            errors.append(f"push: {type(exc).__name__}")
            push_ok = False

    # 어제/그저께 watchlist의 1d/2d/3d 후 가격을 trade_log에 누적 (forward 학습 데이터)
    if "trade_update" not in skip:
        try:
            from scripts.update_trade_log import process_watchlist_run

            today_d = as_of
            for offset in (1, 2, 3):
                back_date = _shift_business_days(today_d, -offset)
                r = process_watchlist_run(db, back_date)
                if r.get("updated"):
                    logger.info(
                        "trade_log forward update %s: updated=%d skipped=%d horizons=%s",
                        back_date, r["updated"], r["skipped"], r.get("horizons"),
                    )
        except Exception as exc:
            logger.exception("trade_log update failed: %s", exc)
            errors.append(f"trade_update: {type(exc).__name__}")

    if errors:
        msg = (
            f"Daily run {as_of} 부분 실패\n"
            f"errors:\n- " + "\n- ".join(errors) + "\n"
            f"watchlist Tier1={len(tiers[1])} Tier2={len(tiers[2])}"
        )
        _alert(settings, msg)

    return 0 if push_ok else 1


def _shift_business_days(d: date, offset: int) -> date:
    """offset 만큼 영업일 이동 (음수 = 과거)."""
    from datetime import timedelta
    cur = d
    step = 1 if offset > 0 else -1
    moved = 0
    while moved < abs(offset):
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            moved += 1
    return cur


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
