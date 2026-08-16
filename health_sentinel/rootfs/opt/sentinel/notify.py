"""Outbound alerting, independent of Home Assistant.

Home Assistant's own notify services cannot tell you that Home Assistant is
down. This add-on can, because it is still running when Core is not — so it
talks to Telegram directly rather than going through Core.

Recording is never contingent on delivery. Every alert is written to the local
event log first; if the network is also down (which, during a real incident, it
may well be) the evidence still survives.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from config import Config
from storage import Storage

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Don't repeat the same alert inside this window.
_DEDUPE_SECONDS = 600
# Absolute ceiling, so one bad night can't produce hundreds of messages.
_MAX_PER_HOUR = 20

_EMOJI = {
    "critical": "\U0001f6a8",  # rotating light
    "error": "⚠️",  # warning sign
    "warning": "⚠️",
    "info": "ℹ️",  # information
}


class Notifier:
    def __init__(self, config: Config, storage: Storage) -> None:
        self._config = config
        self._storage = storage
        self._session: aiohttp.ClientSession | None = None
        self._recent: dict[str, float] = {}
        self._hour_started = time.monotonic()
        self._hour_count = 0
        self._suppressed = 0

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    @property
    def enabled(self) -> bool:
        return self._config.telegram_ready or bool(self._config.webhook_url)

    def describe(self) -> dict[str, Any]:
        return {
            "telegram": self._config.telegram_ready,
            "webhook": bool(self._config.webhook_url),
            "sent_this_hour": self._hour_count,
            "suppressed": self._suppressed,
        }

    # ------------------------------------------------------------------ alert

    async def alert(
        self, title: str, message: str, severity: str = "warning"
    ) -> None:
        """Record an alert locally, then try to deliver it."""
        # Local record first, always — delivery is best-effort, this is not.
        await asyncio.to_thread(
            self._storage.add_event,
            "alert",
            f"{title}: {message}",
            severity,
            "notify",
            {"title": title, "body": message},
        )

        if not self.enabled:
            return
        if not self._admit(title, severity):
            return

        text = f"{_EMOJI.get(severity, '')} {title}\n\n{message}".strip()
        await asyncio.gather(
            self._send_telegram(text),
            self._send_webhook(title, message, severity),
            return_exceptions=True,
        )

    def _admit(self, title: str, severity: str) -> bool:
        now = time.monotonic()

        if now - self._hour_started >= 3600:
            self._hour_started = now
            self._hour_count = 0
            self._suppressed = 0

        # Critical alerts bypass the dedupe window but still respect the cap.
        if severity != "critical":
            last = self._recent.get(title)
            if last is not None and now - last < _DEDUPE_SECONDS:
                self._suppressed += 1
                return False

        if self._hour_count >= _MAX_PER_HOUR:
            self._suppressed += 1
            if self._suppressed == 1:
                _LOGGER.warning(
                    "Alert rate limit reached (%d/hour); suppressing further "
                    "notifications until the hour rolls over",
                    _MAX_PER_HOUR,
                )
            return False

        self._recent[title] = now
        self._hour_count += 1
        return True

    # ---------------------------------------------------------------- senders

    async def _send_telegram(self, text: str) -> None:
        if not self._config.telegram_ready:
            return
        url = f"https://api.telegram.org/bot{self._config.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self._config.telegram_chat_id,
            # Plain text: message bodies contain log lines full of characters
            # that Markdown and HTML parse modes would choke on.
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        try:
            async with self._http.post(url, json=payload, timeout=_TIMEOUT) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    _LOGGER.warning(
                        "Telegram rejected the message (HTTP %s): %s",
                        response.status,
                        body,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Telegram delivery failed: %s", err)

    async def _send_webhook(self, title: str, message: str, severity: str) -> None:
        if not self._config.webhook_url:
            return
        payload = {
            "title": title,
            "message": message,
            "severity": severity,
            "timestamp": int(time.time()),
            "source": "ha-health-sentinel",
        }
        try:
            async with self._http.post(
                self._config.webhook_url, json=payload, timeout=_TIMEOUT
            ) as response:
                if response.status >= 400:
                    _LOGGER.warning(
                        "Webhook returned HTTP %s", response.status
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Webhook delivery failed: %s", err)

    # ------------------------------------------------------------ diagnostics

    async def test(self) -> dict[str, Any]:
        """Send a test message, bypassing dedupe. Used by the UI."""
        if not self.enabled:
            return {"ok": False, "reason": "No Telegram or webhook configured"}
        text = (
            "✅ Health Sentinel test\n\n"
            "If you can read this, alerts will reach you when Home Assistant "
            "cannot send them itself."
        )
        await asyncio.gather(
            self._send_telegram(text),
            self._send_webhook("Health Sentinel test", text, "info"),
            return_exceptions=True,
        )
        return {"ok": True, **self.describe()}
