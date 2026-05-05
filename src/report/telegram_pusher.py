"""Telegram Bot 푸시. 4096자 제한 → 청크 분할."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
CHUNK_LIMIT = 3800  # 안전 마진


class TelegramPusher:
    def __init__(self, bot_token: str, chat_id: str, dry_run: bool = False):
        if not bot_token or not chat_id:
            raise ValueError("Telegram bot token and chat id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.dry_run = dry_run

    def send(self, text: str, parse_mode: str = "Markdown") -> dict[str, Any]:
        if self.dry_run:
            logger.info("[DRY] Telegram send (%d chars)", len(text))
            return {"ok": True, "dry_run": True}

        url = f"{API_BASE}/bot{self.bot_token}/sendMessage"
        last: dict[str, Any] = {}
        for chunk in self._chunks(text):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
                resp.raise_for_status()
            last = resp.json()
        return last

    @staticmethod
    def _chunks(text: str) -> list[str]:
        if len(text) <= CHUNK_LIMIT:
            return [text]
        parts: list[str] = []
        buf: list[str] = []
        size = 0
        for line in text.splitlines(keepends=True):
            if size + len(line) > CHUNK_LIMIT and buf:
                parts.append("".join(buf))
                buf = []
                size = 0
            buf.append(line)
            size += len(line)
        if buf:
            parts.append("".join(buf))
        return parts
