"""Telegram bot inbound — 사용자가 채팅창에서 매수/매도 명령을 보내면 trade_log에 기록.

지원 명령 (대소문자 무관, 한국어 alias 포함):

    /buy  TICKER PRICE [SHARES] [YYYY-MM-DD]
    매수  TICKER PRICE [SHARES] [YYYY-MM-DD]
    /sell TICKER PRICE [SHARES] [YYYY-MM-DD]
    매도  TICKER PRICE [SHARES] [YYYY-MM-DD]
    /note FREE_TEXT   (마지막 open trade에 메모 추가)

예:
    /buy BNAI 25.50 100
    매수 BNAI 25.5 100 2026-05-08
    /sell BNAI 28.30 100
    /note 5월 8일 단타, +10% 익절

보안:
- 발신 chat_id 가 TELEGRAM_CHAT_ID 일치할 때만 처리.
- update_id 마지막 처리값을 data/.telegram_offset 에 저장 (멱등).

cron 시작 시 step_ingest_telegram() 한 번 실행 → 누적된 명령 일괄 적재.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from src.storage.db import Database

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
OFFSET_FILE = Path(__file__).resolve().parents[2] / "data" / ".telegram_offset"


@dataclass
class TradeCommand:
    action: str  # 'buy' | 'sell' | 'note'
    ticker: str | None
    price: float | None
    shares: int | None
    entry_date: date | None
    note: str | None


_PRICE_RE = re.compile(r"^\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_command(text: str) -> TradeCommand | None:
    """단일 메시지 텍스트를 parse. 인식 실패 시 None."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split()
    head = parts[0].lower().lstrip("/")
    if head in {"buy", "매수"}:
        action = "buy"
    elif head in {"sell", "매도"}:
        action = "sell"
    elif head in {"note", "메모"}:
        return TradeCommand(
            action="note", ticker=None, price=None, shares=None,
            entry_date=None, note=" ".join(parts[1:]).strip() or None,
        )
    else:
        return None

    # buy/sell: TICKER PRICE [SHARES] [DATE]
    if len(parts) < 3:
        return None
    ticker = parts[1].upper()
    if not _PRICE_RE.match(parts[2]):
        return None
    price = float(parts[2])

    shares: int | None = None
    entry_date: date | None = None
    for p in parts[3:]:
        if _DATE_RE.match(p):
            try:
                entry_date = date.fromisoformat(p)
            except ValueError:
                pass
        elif p.isdigit() and shares is None:
            shares = int(p)

    return TradeCommand(
        action=action,
        ticker=ticker,
        price=price,
        shares=shares,
        entry_date=entry_date,
        note=None,
    )


def _read_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return 0


def _write_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset))


def fetch_updates(bot_token: str, since_offset: int) -> list[dict[str, Any]]:
    """getUpdates 단발 호출. since_offset 보다 큰 update만 반환."""
    url = f"{API_BASE}/bot{bot_token}/getUpdates"
    params = {"offset": since_offset + 1, "timeout": 0}
    with httpx.Client(timeout=15.0) as c:
        resp = c.get(url, params=params)
        resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        logger.warning("Telegram getUpdates not ok: %s", body)
        return []
    return body.get("result", []) or []


def apply_command(db: Database, cmd: TradeCommand) -> str:
    """파싱된 command를 trade_log에 반영."""
    today = date.today()
    if cmd.action == "buy":
        if not cmd.ticker or cmd.price is None:
            return "skip: buy needs ticker+price"
        entry_d = cmd.entry_date or today
        # 같은 (ticker, entry_date, is_paper=0) 이미 있으면 update, 아니면 insert
        existing = db.conn.execute(
            "SELECT trade_id FROM trade_log "
            "WHERE ticker = ? AND entry_date = ? AND is_paper = 0",
            (cmd.ticker, entry_d.isoformat()),
        ).fetchone()
        if existing:
            db.conn.execute(
                "UPDATE trade_log SET entry_price = ?, size_pct_capital = NULL, "
                "notes = COALESCE(notes, '') || ? WHERE trade_id = ?",
                (cmd.price, f"\n[buy update {today.isoformat()}]", existing["trade_id"]),
            )
            return f"updated buy: {cmd.ticker} @ {cmd.price}"
        # entry_pss/tier는 그날 watchlist_runs 에서 lookup
        pss, tier, patterns = _lookup_pick(db, cmd.ticker, entry_d)
        db.conn.execute(
            """
            INSERT INTO trade_log(
                ticker, entry_date, entry_price, entry_pss, entry_tier,
                triggered_patterns, exit_reason, is_paper,
                size_pct_capital, notes
            ) VALUES (?, ?, ?, ?, ?, ?, 'live', 0, ?, ?)
            """,
            (
                cmd.ticker, entry_d.isoformat(), cmd.price, pss, tier,
                patterns, cmd.shares, f"telegram /buy",
            ),
        )
        return f"buy: {cmd.ticker} @ {cmd.price} x{cmd.shares or '?'}"

    if cmd.action == "sell":
        if not cmd.ticker or cmd.price is None:
            return "skip: sell needs ticker+price"
        exit_d = cmd.entry_date or today
        # 가장 최근 open buy (exit_price IS NULL) 찾아서 close
        row = db.conn.execute(
            "SELECT trade_id, entry_price FROM trade_log "
            "WHERE ticker = ? AND is_paper = 0 AND exit_price IS NULL "
            "ORDER BY entry_date DESC LIMIT 1",
            (cmd.ticker,),
        ).fetchone()
        if not row:
            return f"skip: no open buy for {cmd.ticker}"
        entry_p = float(row["entry_price"])
        pnl = (cmd.price - entry_p) / entry_p
        db.conn.execute(
            """
            UPDATE trade_log SET
                exit_date = ?, exit_price = ?, pnl_pct = ?, exit_reason = 'live'
            WHERE trade_id = ?
            """,
            (exit_d.isoformat(), cmd.price, pnl, row["trade_id"]),
        )
        return f"sell: {cmd.ticker} @ {cmd.price} pnl={pnl:.2%}"

    if cmd.action == "note" and cmd.note:
        # 가장 최근 trade의 notes에 append
        row = db.conn.execute(
            "SELECT trade_id, notes FROM trade_log "
            "WHERE is_paper = 0 ORDER BY entry_date DESC LIMIT 1",
        ).fetchone()
        if not row:
            return "skip: no live trade to note"
        new_notes = (row["notes"] or "") + f"\n[note] {cmd.note}"
        db.conn.execute(
            "UPDATE trade_log SET notes = ? WHERE trade_id = ?",
            (new_notes, row["trade_id"]),
        )
        return f"note added: {cmd.note[:40]}"

    return "skip: unknown"


def _lookup_pick(db: Database, ticker: str, entry_date: date) -> tuple:
    """해당 날짜 watchlist_runs에서 ticker 정보 조회 (있으면 PSS/Tier/패턴 채움)."""
    row = db.conn.execute(
        "SELECT pss_total, tier, triggered_patterns FROM pss_scores "
        "WHERE score_date = ? AND ticker = ?",
        (entry_date.isoformat(), ticker),
    ).fetchone()
    if row:
        return float(row["pss_total"] or 0), row["tier"], row["triggered_patterns"]
    return None, None, None


def ingest_telegram_commands(
    bot_token: str, allowed_chat_id: str, db: Database
) -> dict[str, Any]:
    """전체 흐름: offset 읽기 → getUpdates → 권한 체크 → parse → apply → offset 저장."""
    if not bot_token or not allowed_chat_id:
        return {"processed": 0, "reason": "missing bot creds"}
    last_offset = _read_offset()
    try:
        updates = fetch_updates(bot_token, last_offset)
    except Exception as exc:
        logger.warning("Telegram getUpdates failed: %s", exc)
        return {"processed": 0, "error": str(exc)}

    processed = 0
    skipped = 0
    new_offset = last_offset
    results: list[str] = []
    for u in updates:
        new_offset = max(new_offset, int(u.get("update_id", 0)))
        msg = u.get("message") or u.get("channel_post") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(allowed_chat_id):
            skipped += 1
            continue
        text = msg.get("text") or ""
        cmd = parse_command(text)
        if cmd is None:
            continue
        try:
            res = apply_command(db, cmd)
            results.append(res)
            processed += 1
            logger.info("telegram cmd: %s -> %s", text[:60], res)
        except Exception as exc:
            logger.warning("apply_command failed for %r: %s", text, exc)

    if new_offset > last_offset:
        _write_offset(new_offset)
    return {
        "processed": processed,
        "skipped_other_chat": skipped,
        "new_offset": new_offset,
        "results": results,
    }
