"""장중 watchlist monitor."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from src.config import Settings
from src.intraday.calendar import session_for
from src.intraday.market_data import TickerSnapshot, fetch_snapshots
from src.intraday.signals import IntradaySignalEngine, Signal, SignalContext
from src.intraday.watchlist import load_intraday_watchlist
from src.report.telegram_pusher import TelegramPusher
from src.storage.db import Database

logger = logging.getLogger(__name__)


class IntradayMonitor:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.engine = IntradaySignalEngine()

    def run_once(
        self,
        trade_date: date,
        as_of: datetime | None = None,
        dry_run: bool = False,
    ) -> int:
        candidates = load_intraday_watchlist(
            self.db,
            trade_date,
            max_tickers=self.settings.intraday_max_tickers,
            min_tier=self.settings.intraday_min_tier,
        )
        if not candidates:
            logger.warning("No intraday watchlist for %s", trade_date)
            return 0

        tickers = [c.ticker for c in candidates]
        logger.info("Intraday watchlist %s: %s", trade_date, ", ".join(tickers))
        snapshots = fetch_snapshots(
            tickers,
            self.db,
            trade_date,
            self.settings,
            candidates={c.ticker: c for c in candidates},
        )

        sent_or_stored = 0
        alerts_this_loop = 0
        for candidate in candidates:
            snap = snapshots.get(candidate.ticker)
            if not snap or snap.current_price is None:
                logger.debug("No price snapshot for %s", candidate.ticker)
                continue
            ctx = self._context(snap, as_of or datetime.utcnow())
            signals = self.engine.evaluate(snap, ctx)
            for signal in signals:
                if alerts_this_loop >= self.settings.intraday_max_alerts_per_loop:
                    logger.info("alert cap reached for this loop")
                    return sent_or_stored
                if not self._should_store_signal(snap, signal, as_of or datetime.utcnow()):
                    continue
                signal_id = self._store_signal(snap, signal, as_of or datetime.utcnow(), dry_run)
                status = self._push_signal(signal_id, snap, signal, dry_run)
                self.db.mark_signal_telegram(signal_id, status)
                sent_or_stored += 1
                alerts_this_loop += 1
        return sent_or_stored

    def run_loop(self, force_market_closed: bool = False, dry_run: bool = False) -> int:
        total = 0
        while True:
            session = session_for()
            if not session.is_open and not force_market_closed:
                if session.now_et < session.open_et and session.now_et.weekday() < 5:
                    wait = min(
                        self.settings.intraday_interval_seconds,
                        max(30, int((session.open_et - session.now_et).total_seconds())),
                    )
                    logger.info("market closed, waiting %ss until open window", wait)
                    time.sleep(wait)
                    continue
                logger.info("market is closed for %s; exiting intraday monitor", session.trade_date)
                return total

            total += self.run_once(session.trade_date, session.now_et, dry_run=dry_run)

            if force_market_closed:
                time.sleep(self.settings.intraday_interval_seconds)
                continue
            session = session_for()
            if session.now_et >= session.close_et:
                logger.info("market close reached; exiting intraday monitor")
                return total
            time.sleep(self.settings.intraday_interval_seconds)

    def _context(self, snap: TickerSnapshot, as_of: datetime) -> SignalContext:
        latest_buy = self.db.latest_signal(snap.ticker, snap.trade_date, "BUY_WATCH")
        latest_sell = self.db.latest_signal(snap.ticker, snap.trade_date, "SELL_WATCH")
        latest_caution = self.db.latest_signal(snap.ticker, snap.trade_date, "CAUTION")
        active_buy_price: float | None = None
        if latest_buy and _row_ts(latest_buy) >= max(_row_ts(latest_sell), _row_ts(latest_caution)):
            active_buy_price = float(latest_buy["price"])
        return SignalContext(
            as_of=as_of,
            active_buy_price=active_buy_price,
            buy_signal_count=self.db.count_signals(snap.ticker, snap.trade_date, "BUY_WATCH"),
            caution_active=latest_caution is not None and active_buy_price is None,
        )

    def _should_store_signal(self, snap: TickerSnapshot, signal: Signal, as_of: datetime) -> bool:
        if signal.signal_type == "BUY_WATCH":
            if self.db.count_signals(snap.ticker, snap.trade_date, "BUY_WATCH") >= 2:
                return False
            since = (as_of - timedelta(minutes=self.settings.intraday_buy_cooldown_minutes)).isoformat()
        else:
            # TP/SELL/CAUTION은 같은 trigger를 하루 1회만 보낸다.
            since = datetime.combine(snap.trade_date, datetime.min.time(), tzinfo=as_of.tzinfo).isoformat()

        return not self.db.recent_signal_exists(
            snap.ticker,
            snap.trade_date,
            signal.signal_type,
            signal.trigger_code,
            since,
        )

    def _store_signal(
        self,
        snap: TickerSnapshot,
        signal: Signal,
        as_of: datetime,
        dry_run: bool,
    ) -> int:
        metadata: dict[str, Any] = {
            **signal.metadata,
            "snapshot": snap.metadata,
            "day_open": snap.day_open,
            "day_high": snap.day_high,
            "day_low": snap.day_low,
        }
        return self.db.insert_signal_event({
            "signal_ts": as_of.isoformat(),
            "trade_date": snap.trade_date.isoformat(),
            "ticker": snap.ticker,
            "signal_type": signal.signal_type,
            "trigger_code": signal.trigger_code,
            "price": signal.price,
            "ref_price": signal.ref_price,
            "pss_total": snap.pss_total,
            "tier": snap.tier,
            "triggered_patterns": snap.triggered_patterns,
            "source": snap.source,
            "metadata_json": json.dumps(metadata, default=str),
            "telegram_sent_at": None,
            "telegram_status": "dry_run" if dry_run else "pending",
        })

    def _push_signal(
        self,
        signal_id: int,
        snap: TickerSnapshot,
        signal: Signal,
        dry_run: bool,
    ) -> str:
        if dry_run:
            logger.info("[DRY] signal %s %s %s %.2f", snap.ticker, signal.signal_type,
                        signal.trigger_code, signal.price)
            return "dry_run"
        chat_id = self.settings.telegram_alert_chat_id or self.settings.telegram_chat_id
        if not self.settings.telegram_bot_token or not chat_id:
            logger.warning("Telegram config missing; signal %s stored only", signal_id)
            return "stored_no_telegram"
        text = format_signal_message(snap, signal)
        try:
            TelegramPusher(self.settings.telegram_bot_token, chat_id).send(text)
        except Exception as exc:
            logger.warning("Telegram intraday push failed signal=%s: %s", signal_id, exc)
            return f"failed:{type(exc).__name__}"
        return "sent"


def format_signal_message(snap: TickerSnapshot, signal: Signal) -> str:
    pss = f"{snap.pss_total:.1f}" if snap.pss_total is not None else "n/a"
    tier = str(snap.tier) if snap.tier is not None else "n/a"
    patterns = snap.triggered_patterns or "-"
    header = f"[{signal.signal_type}] {snap.ticker}"
    if signal.signal_type == "TAKE_PROFIT":
        pnl = signal.metadata.get("signal_pnl")
        if isinstance(pnl, (int, float)):
            header = f"[TAKE PROFIT] {snap.ticker} {pnl * 100:+.1f}%"
    lines = [
        header,
        f"PSS {pss} / Tier {tier} / {patterns}",
        f"Trigger: {signal.trigger_code}",
        f"Price: {signal.price:.4g}",
    ]
    if signal.ref_price:
        lines.append(f"Ref: {signal.ref_price:.4g}")
    if signal.signal_type == "BUY_WATCH":
        vwap = signal.metadata.get("vwap")
        prev_high = signal.metadata.get("prev_high")
        refs = []
        if prev_high:
            refs.append(f"prev_high {prev_high:.4g}")
        if vwap:
            refs.append(f"VWAP {vwap:.4g}")
        if refs:
            lines.append("Refs: " + " / ".join(refs))
        lines.append("Plan: invalid below ref/VWAP, watch +10%/+20%")
    elif signal.signal_type == "SELL_WATCH":
        pnl = signal.metadata.get("signal_pnl")
        if isinstance(pnl, (int, float)):
            lines.append(f"Signal PnL: {pnl * 100:+.1f}%")
        lines.append("Plan: reduce/exit check")
    elif signal.signal_type == "CAUTION":
        lines.append("Plan: avoid new entry until strength returns")
    return "\n".join(lines)


def _row_ts(row: Any) -> str:
    return row["signal_ts"] if row else ""
