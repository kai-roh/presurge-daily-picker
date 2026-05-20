"""장중 시그널 일간/주간 성과 리포트.

signal_events + signal_outcomes 를 묶어 trigger/time-bucket 별 성과를 요약한다.
파라미터 자동 변경은 하지 않고, 표본 기반 추천안만 Telegram 으로 보낸다.
"""
from __future__ import annotations

import argparse
import html
import json
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

SIGNAL_LABELS = {
    "BUY_WATCH": "매수 관찰",
    "TAKE_PROFIT": "익절 관찰",
    "SELL_WATCH": "매도/축소",
    "CAUTION": "주의",
}

TRIGGER_LABELS = {
    "ORB": "장초반 돌파",
    "RVOL_CONT": "거래량 지속",
    "VWAP_RECLAIM": "VWAP 회복",
    "VWAP_LOSS": "VWAP 이탈",
    "EXHAUSTION": "상승 둔화",
    "PRICE_BREAKDOWN": "가격 급락",
    "VWAP_WEAKNESS": "VWAP 약세",
}


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
    title = f"<b>장중 시그널 {window.label} 리포트</b>"
    if not rows:
        return f"{title}\n기간: {window.start}..{window.end}\n\n해당 기간 signal 데이터가 없습니다."

    evaluated = [r for r in rows if r.get("evaluated_at")]
    lines = [
        title,
        f"기간: {window.start}..{window.end}",
        "",
        overview_line(rows, evaluated),
        "",
        "<b>요약 판단</b>",
    ]
    lines.extend(summary_lines(rows))

    lines.extend(["", "<b>신호별 성과</b>"])
    lines.extend(trigger_lines(rows))

    lines.extend(["", "<b>시간대별 흐름</b>"])
    lines.extend(time_bucket_lines(rows))

    lines.extend(["", "<b>눈에 띈 시그널</b>"])
    lines.extend(top_signal_lines(rows))

    lines.extend(["", "<b>운영 추천</b>"])
    lines.extend(recommendation_lines(rows))
    return "\n".join(lines)


def overview_line(rows: list[dict[str, Any]], evaluated: list[dict[str, Any]]) -> str:
    buy_total = sum(1 for r in rows if r["signal_type"] == "BUY_WATCH")
    defensive_total = len(rows) - buy_total
    buy_eval = [r for r in evaluated if r["signal_type"] == "BUY_WATCH"]
    defensive_eval = [r for r in evaluated if r["signal_type"] != "BUY_WATCH"]
    return (
        f"전체 {len(rows)}개 중 {len(evaluated)}개 평가 완료. "
        f"매수 관찰 {buy_total}개({len(buy_eval)}개 평가), "
        f"주의/매도 {defensive_total}개({len(defensive_eval)}개 평가)."
    )


def summary_lines(rows: list[dict[str, Any]]) -> list[str]:
    evaluated = [r for r in rows if r.get("evaluated_at")]
    if not evaluated:
        return ["- 아직 사후 주가 평가가 끝난 시그널이 없습니다."]

    buys = [r for r in evaluated if r["signal_type"] == "BUY_WATCH"]
    defensive = [r for r in evaluated if r["signal_type"] != "BUY_WATCH"]
    out = []
    if buys:
        hit = buy_hit_rate(buys)
        out.append(
            f"- 매수 관찰 신호는 {len(buys)}개 평가됐고, 단기 follow-through는 {ratio_words(hit)}입니다."
        )
    else:
        out.append("- 매수 관찰 신호는 아직 평가 표본이 없습니다.")

    if defensive:
        avoid = defensive_hit_rate(defensive)
        out.append(
            f"- 주의/매도 신호는 {len(defensive)}개 평가됐고, 실제 약세 포착은 {ratio_words(avoid)}입니다."
        )

    strongest = strongest_trigger_line(evaluated)
    if strongest:
        out.append(strongest)
    if len(evaluated) < 30:
        out.append("- 아직 표본이 적어서 설정 변경보다는 관찰을 계속하는 단계입니다.")
    return out


def strongest_trigger_line(rows: list[dict[str, Any]]) -> str | None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[(r["signal_type"], r["trigger_code"])].append(r)
    candidates = []
    for key, sample in grouped.items():
        if len(sample) < 3:
            continue
        signal_type, trigger = key
        rate = buy_hit_rate(sample) if signal_type == "BUY_WATCH" else defensive_hit_rate(sample)
        if rate is None:
            continue
        candidates.append((rate, len(sample), signal_type, trigger))
    if not candidates:
        return "- 아직 특정 신호를 대표로 꼽기에는 trigger별 표본이 작습니다."
    rate, n, signal_type, trigger = sorted(candidates, reverse=True)[0]
    return (
        f"- 현재 가장 눈에 띄는 신호는 {trigger_name(trigger)} "
        f"({signal_name(signal_type)})입니다. {n}개 평가에서 {ratio_words(rate)} 수준입니다."
    )


def trigger_lines(rows: list[dict[str, Any]]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grouped[(r["signal_type"], r["trigger_code"])].append(r)

    out = []
    ranked = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
    for (signal_type, trigger), sample in ranked:
        ev = [r for r in sample if r.get("evaluated_at")]
        buy_like = signal_type == "BUY_WATCH"
        hit = buy_hit_rate(ev) if buy_like else defensive_hit_rate(ev)
        avg_max = avg_pct([r.get("max_eod_pct") for r in ev])
        avg_close = avg_pct([r.get("close_eod_pct") for r in ev])
        verdict = trigger_verdict(signal_type, hit, len(ev))
        out.append(
            f"- {trigger_name(trigger)} ({signal_name(signal_type)}): "
            f"{len(sample)}번 발생, {len(ev)}번 평가. {verdict} "
            f"이후 최고 {fmt_pct(avg_max)}, 종가 기준 {fmt_pct(avg_close)}."
        )
    return out[:6]


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
        avg_close = avg_pct([r.get("close_eod_pct") for r in ev])
        out.append(
            f"- {time_bucket_name(bucket)}: {len(sample)}개 신호, "
            f"평가 {len(ev)}개. 매수 follow-through {ratio_words(buy_hit_rate(buys))}, "
            f"종가 흐름 {fmt_pct(avg_close)}."
        )
    return out or ["- 평가 가능한 시간대 데이터가 아직 없습니다."]


def top_signal_lines(rows: list[dict[str, Any]]) -> list[str]:
    evaluated = [r for r in rows if r.get("evaluated_at")]
    if not evaluated:
        return ["- 아직 outcome 평가가 완료된 시그널이 없습니다."]

    buys = [r for r in evaluated if r["signal_type"] == "BUY_WATCH"]
    best_buy = sorted(
        buys,
        key=lambda r: _num(r.get("max_eod_pct")) if r.get("max_eod_pct") is not None else -999,
        reverse=True,
    )[:2]
    defensive = [r for r in evaluated if r["signal_type"] != "BUY_WATCH"]
    useful_defensive = sorted(
        defensive,
        key=lambda r: _num(r.get("min_after_pct")) if r.get("min_after_pct") is not None else 999,
    )[:2]

    out = []
    if best_buy:
        out.append("- 매수 관찰 신호")
        out.extend(f"  {signal_sentence(r, buy=True)}" for r in best_buy)
    else:
        out.append("- 매수 관찰 신호: 아직 평가 표본이 없습니다.")

    if useful_defensive:
        out.append("- 주의/매도 신호")
        out.extend(f"  {signal_sentence(r, buy=False)}" for r in useful_defensive)
    return out


def recommendation_lines(rows: list[dict[str, Any]]) -> list[str]:
    evaluated = [r for r in rows if r.get("evaluated_at")]
    if len(evaluated) < 30:
        return [
            f"- 평가 표본이 {len(evaluated)}개라 아직 자동 조정은 이릅니다. 최소 30개, 가능하면 100개 이상까지 관찰합니다.",
            "- 지금은 설정 유지. 다음 리포트에서 반복적으로 좋은 신호와 나쁜 신호가 갈리는지 보겠습니다.",
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
                out.append(f"- {trigger_name(trigger)} 매수 신호는 약합니다. 거래량/가격 조건 강화 후보입니다.")
            elif hit is not None and hit >= 0.45:
                out.append(f"- {trigger_name(trigger)} 매수 신호는 유지 후보입니다. 표본이 더 쌓이면 우선순위 상향을 검토합니다.")
        else:
            avoid = defensive_hit_rate(sample)
            if avoid is not None and avoid >= 0.6:
                out.append(f"- {trigger_name(trigger)} 주의/매도 신호는 유효해 보입니다. 유지 후보입니다.")
    if not out:
        out.append("- 아직 특정 신호를 올리거나 내릴 만큼 뚜렷한 차이는 없습니다. 현 설정 유지.")
    return out


def trigger_verdict(signal_type: str, rate: float | None, n: int) -> str:
    if n == 0:
        return "아직 판단할 사후 데이터가 없습니다."
    if n < 5:
        return "표본이 작아 관찰 단계입니다."
    if rate is None:
        return "판정 보류."
    if signal_type == "BUY_WATCH":
        if rate >= 0.45:
            return "좋은 편입니다."
        if rate >= 0.25:
            return "보통입니다."
        return "약한 편입니다."
    if rate >= 0.6:
        return "회피/축소 신호로 쓸 만합니다."
    if rate >= 0.35:
        return "보통입니다."
    return "효과가 약합니다."


def signal_sentence(row: dict[str, Any], *, buy: bool) -> str:
    label = (
        f"{html.escape(str(row['ticker']))} "
        f"{trigger_name(str(row['trigger_code']))} "
        f"({signal_time_kst(row.get('signal_ts'))})"
    )
    if buy:
        outcome = (
            f"이후 최고 {fmt_pct(row.get('max_eod_pct'))}, "
            f"종가 {fmt_pct(row.get('close_eod_pct'))}"
        )
    else:
        outcome = (
            f"이후 최저 {fmt_pct(row.get('min_after_pct'))}, "
            f"종가 {fmt_pct(row.get('close_eod_pct'))}"
        )
    meta = metadata_summary(row)
    return f"{label}: {outcome}. {meta}"


def metadata_summary(row: dict[str, Any]) -> str:
    md = metadata(row)
    snap = md.get("snapshot") if isinstance(md.get("snapshot"), dict) else {}
    parts = []
    if row.get("tier") is not None:
        parts.append(f"Tier {row['tier']}")
    if row.get("pss_total") is not None:
        parts.append(f"PSS {float(row['pss_total']):.1f}")
    raw_session = snap.get("session") or time_bucket(row.get("signal_ts"))
    session = time_bucket_name(str(raw_session))
    if session:
        parts.append(f"세션 {session}")
    active = snap.get("active_position") if isinstance(snap.get("active_position"), dict) else None
    if active and active.get("entry_price") is not None:
        parts.append(f"실진입 {float(active['entry_price']):.4g}")
    for key, label in (
        ("intraday_rvol", "RVOL"),
        ("volume_ratio_5m", "5분거래량"),
    ):
        val = _num(md.get(key))
        if val is not None:
            parts.append(f"{label} {val:.1f}x")
    pct_prev = _num(md.get("pct_prev_close"))
    if pct_prev is not None:
        parts.append(f"전일비 {fmt_pct(pct_prev)}")
    vwap_val = _num(md.get("vwap"))
    if vwap_val is not None:
        parts.append(f"VWAP {vwap_val:.4g}")
    return "메타: " + ", ".join(parts[:6]) if parts else "메타: 없음"


def metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json") or "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def trigger_name(trigger: str) -> str:
    return html.escape(TRIGGER_LABELS.get(trigger, trigger))


def signal_name(signal_type: str) -> str:
    return html.escape(SIGNAL_LABELS.get(signal_type, signal_type))


def time_bucket_name(bucket: str) -> str:
    return {
        "17-20 pre": "17-20시 프리장 초반",
        "20-03 core": "20-03시 프리장 후반/정규장 핵심",
        "03-06 late": "03-06시 정규장 후반",
        "06-11 post": "06-11시 포스트장",
        "other": "기타",
        "premarket": "프리장",
        "regular": "정규장",
        "postmarket": "포스트장",
        "closed": "장외",
    }.get(bucket, bucket)


def signal_time_kst(signal_ts: str | None) -> str:
    if not signal_ts:
        return "시간 n/a"
    try:
        ts = datetime.fromisoformat(signal_ts)
    except ValueError:
        return "시간 n/a"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(KST).strftime("%m/%d %H:%M KST")


def ratio_words(value: float | None) -> str:
    if value is None:
        return "아직 판단 불가"
    if value >= 0.7:
        return "강함"
    if value >= 0.45:
        return "양호"
    if value >= 0.25:
        return "보통"
    return "약함"


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
