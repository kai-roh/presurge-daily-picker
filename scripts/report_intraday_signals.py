"""장중 시그널 일간/주간 성과 리포트.

signal_events + signal_outcomes 를 묶어 trigger/time-bucket 별 성과를 요약한다.
파라미터 자동 변경은 하지 않고, 표본 기반 추천안만 Telegram 으로 보낸다.
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.config import Settings
from src.intraday.outcomes import evaluate_pending_signals
from src.report.telegram_pusher import TelegramPusher
from src.storage.db import Database, get_db

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    label: str


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--period", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--date", help="period end date in ET trade-date format (YYYY-MM-DD)")
    parser.add_argument("--push", action="store_true", help="send report to Telegram")
    parser.add_argument("--skip-evaluate", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db = get_db(settings.database_url)

    if not args.skip_evaluate:
        evaluated = evaluate_pending_signals(db)
        logger.info("evaluated pending intraday signals: %d", evaluated)

    end = date.fromisoformat(args.date) if args.date else latest_signal_trade_date(db)
    if end is None:
        report = "장중 시그널 리포트\n\n아직 signal_events 데이터가 없습니다."
    else:
        window = report_window(args.period, end)
        rows = load_signal_rows(db, window)
        report = build_report(window, rows)

    print(report)
    if args.push:
        chat_id = settings.telegram_alert_chat_id or settings.telegram_chat_id
        if not settings.telegram_bot_token or not chat_id:
            logger.warning("Telegram credentials missing; report printed only")
            return 0
        TelegramPusher(settings.telegram_bot_token, chat_id).send(report, parse_mode="HTML")
    return 0


def latest_signal_trade_date(db: Database) -> date | None:
    row = db.conn.execute("SELECT MAX(trade_date) AS d FROM signal_events").fetchone()
    if not row or not row["d"]:
        return None
    return date.fromisoformat(row["d"])


def report_window(period: str, end: date) -> Window:
    if period == "weekly":
        return Window(start=end - timedelta(days=6), end=end, label="주간")
    return Window(start=end, end=end, label="일간")


def load_signal_rows(db: Database, window: Window) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT
            s.signal_id, s.signal_ts, s.trade_date, s.ticker, s.signal_type,
            s.trigger_code, s.price, s.pss_total, s.tier, s.triggered_patterns,
            s.source, s.telegram_status, s.metadata_json,
            o.max_10m_pct, o.close_10m_pct, o.max_30m_pct, o.close_30m_pct,
            o.max_60m_pct, o.close_60m_pct, o.max_eod_pct, o.close_eod_pct,
            o.min_after_pct, o.evaluated_at
        FROM signal_events s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.signal_id
        WHERE s.trade_date BETWEEN ? AND ?
        ORDER BY s.signal_ts, s.ticker
        """,
        (window.start.isoformat(), window.end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def build_report(window: Window, rows: list[dict[str, Any]]) -> str:
    title = f"장중 시그널 {window.label} 리포트 ({window.start}..{window.end})"
    if not rows:
        return f"{title}\n\n해당 기간 signal_events 데이터가 없습니다."

    evaluated = [r for r in rows if r.get("evaluated_at")]
    by_status = counts(rows, "telegram_status")
    lines = [
        title,
        "",
        f"전체 시그널: {len(rows)}개 / 성과평가 완료: {len(evaluated)}개",
        f"전송 상태: {format_counts(by_status)}",
        "",
        "Trigger별 성과",
    ]
    for line in trigger_lines(rows):
        lines.append(line)

    lines.extend(["", "시간대별 성과"])
    for line in time_bucket_lines(rows):
        lines.append(line)

    lines.extend(["", "상위/주의 시그널"])
    lines.extend(top_signal_lines(rows))

    lines.extend(["", "운영 추천"])
    lines.extend(recommendation_lines(rows))
    return "\n".join(lines)


def trigger_lines(rows: list[dict[str, Any]]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[(r["signal_type"], r["trigger_code"])].append(r)

    out = []
    for (signal_type, trigger), sample in sorted(grouped.items()):
        ev = [r for r in sample if r.get("evaluated_at")]
        buy_like = signal_type == "BUY_WATCH"
        hit = buy_hit_rate(ev) if buy_like else defensive_hit_rate(ev)
        avg_eod = avg_pct([r.get("close_eod_pct") for r in ev])
        avg_max = avg_pct([r.get("max_eod_pct") for r in ev])
        avg_min = avg_pct([r.get("min_after_pct") for r in ev])
        hit_label = "hit" if buy_like else "avoid"
        out.append(
            f"- {signal_type}/{trigger}: n={len(sample)}, eval={len(ev)}, "
            f"{hit_label}={fmt_pct(hit)}, avgMax={fmt_pct(avg_max)}, "
            f"avgClose={fmt_pct(avg_eod)}, avgMin={fmt_pct(avg_min)}"
        )
    return out


def time_bucket_lines(rows: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[time_bucket(r.get("signal_ts"))].append(r)
    order = ["17-20 pre", "20-03 core", "03-06 late", "06-11 post", "other"]
    out = []
    for bucket in order:
        sample = grouped.get(bucket, [])
        if not sample:
            continue
        ev = [r for r in sample if r.get("evaluated_at")]
        buys = [r for r in ev if r["signal_type"] == "BUY_WATCH"]
        out.append(
            f"- {bucket}: n={len(sample)}, eval={len(ev)}, "
            f"BUY hit={fmt_pct(buy_hit_rate(buys))}, avgClose={fmt_pct(avg_pct([r.get('close_eod_pct') for r in ev]))}"
        )
    return out or ["- 평가 가능한 시간대 데이터가 아직 없습니다."]


def top_signal_lines(rows: list[dict[str, Any]]) -> list[str]:
    evaluated = [r for r in rows if r.get("evaluated_at")]
    if not evaluated:
        return ["- 아직 outcome 평가가 완료된 시그널이 없습니다."]

    best = sorted(
        evaluated,
        key=lambda r: _num(r.get("max_eod_pct")) if r.get("max_eod_pct") is not None else -999,
        reverse=True,
    )[:3]
    weak = sorted(
        evaluated,
        key=lambda r: _num(r.get("min_after_pct")) if r.get("min_after_pct") is not None else 999,
    )[:3]
    out = ["- follow-through TOP"]
    out.extend(f"  {signal_label(r)} maxEOD={fmt_pct(r.get('max_eod_pct'))} closeEOD={fmt_pct(r.get('close_eod_pct'))}" for r in best)
    out.append("- adverse move TOP")
    out.extend(f"  {signal_label(r)} minAfter={fmt_pct(r.get('min_after_pct'))} closeEOD={fmt_pct(r.get('close_eod_pct'))}" for r in weak)
    return out


def recommendation_lines(rows: list[dict[str, Any]]) -> list[str]:
    evaluated = [r for r in rows if r.get("evaluated_at")]
    if len(evaluated) < 30:
        return [
            f"- 표본 {len(evaluated)}개: 파라미터 변경 보류. 최소 30개, 가능하면 100개 이상까지 관찰.",
            "- 현재 단계에서는 시그널 품질 기록과 시간대별 성과 확인이 우선.",
        ]

    out = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in evaluated:
        grouped[(r["signal_type"], r["trigger_code"])].append(r)
    for (signal_type, trigger), sample in sorted(grouped.items()):
        if len(sample) < 10:
            continue
        if signal_type == "BUY_WATCH":
            hit = buy_hit_rate(sample)
            if hit is not None and hit < 0.2:
                out.append(f"- {trigger}: BUY hit 낮음({fmt_pct(hit)}). volume/price 조건 강화 후보.")
            elif hit is not None and hit >= 0.45:
                out.append(f"- {trigger}: BUY hit 양호({fmt_pct(hit)}). 유지 또는 후보 우선순위 상향 후보.")
        else:
            avoid = defensive_hit_rate(sample)
            if avoid is not None and avoid >= 0.6:
                out.append(f"- {trigger}: 회피 신호 양호({fmt_pct(avoid)}). SELL/CAUTION 유지 후보.")
    if not out:
        out.append("- 특정 trigger 조정 권고 없음. 현 설정 유지.")
    return out


def signal_label(row: dict[str, Any]) -> str:
    return (
        f"{row['trade_date']} {row['ticker']} "
        f"{row['signal_type']}/{row['trigger_code']} @{float(row['price']):.4g}"
    )


def buy_hit_rate(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    hits = 0
    for r in rows:
        max30 = _num(r.get("max_30m_pct"))
        max60 = _num(r.get("max_60m_pct"))
        max_eod = _num(r.get("max_eod_pct"))
        if max(max30 or -999, max60 or -999) >= 0.05 or (max_eod is not None and max_eod >= 0.10):
            hits += 1
    return hits / len(rows)


def defensive_hit_rate(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    hits = 0
    for r in rows:
        close60 = _num(r.get("close_60m_pct"))
        close_eod = _num(r.get("close_eod_pct"))
        min_after = _num(r.get("min_after_pct"))
        if (close60 is not None and close60 <= -0.02) or (close_eod is not None and close_eod <= 0) or (min_after is not None and min_after <= -0.05):
            hits += 1
    return hits / len(rows)


def time_bucket(signal_ts: str | None) -> str:
    if not signal_ts:
        return "other"
    try:
        ts = datetime.fromisoformat(signal_ts)
    except ValueError:
        return "other"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    t = ts.astimezone(KST).time()
    hour = t.hour + t.minute / 60
    if 17 <= hour < 20:
        return "17-20 pre"
    if hour >= 20 or hour < 3:
        return "20-03 core"
    if 3 <= hour < 6:
        return "03-06 late"
    if 6 <= hour < 11:
        return "06-11 post"
    return "other"


def counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[str(r.get(key) or "none")] += 1
    return dict(out)


def format_counts(values: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(values.items()))


def avg_pct(values: list[Any]) -> float | None:
    nums = [_num(v) for v in values if v is not None]
    nums = [v for v in nums if v is not None]
    return statistics.fmean(nums) if nums else None


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_pct(value: Any) -> str:
    n = _num(value)
    if n is None:
        return "n/a"
    return f"{n * 100:+.1f}%"


if __name__ == "__main__":
    sys.exit(main())
