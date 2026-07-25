"""Thin async Telegram Bot API client.

Handles the failure modes that actually occur in a long-lived deployment:
429 flood waits, 409 getUpdates conflicts, transient network errors, and the
4096-character message ceiling.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4096


class TelegramError(Exception):
    pass


class Conflict(TelegramError):
    """Another getUpdates consumer is running — usually a second replica."""


class Telegram:
    def __init__(self, token: str, timeout: float = 70.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            limits=httpx.Limits(max_connections=20),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, *, params=None, json=None, retries: int = 3):
        url = f"{self.base}/{method}"
        delay = 1.0
        for attempt in range(retries):
            try:
                response = await self._client.post(url, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == retries - 1:
                    raise TelegramError(f"{method}: {type(exc).__name__}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 409:
                raise Conflict(response.text[:200])

            if response.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(response.json()["parameters"]["retry_after"])
                except Exception:  # noqa: BLE001
                    pass
                log.warning("%s rate limited, sleeping %ss", method, retry_after)
                await asyncio.sleep(retry_after + 1)
                continue

            if response.status_code >= 500:
                if attempt == retries - 1:
                    raise TelegramError(f"{method}: HTTP {response.status_code}")
                await asyncio.sleep(delay)
                delay *= 2
                continue

            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - proxies can return HTML
                raise TelegramError(
                    f"{method}: non-JSON response (HTTP {response.status_code})"
                ) from exc
            if not payload.get("ok"):
                raise TelegramError(f"{method}: {payload.get('description')}")
            return payload.get("result")

        raise TelegramError(f"{method}: retries exhausted")

    async def get_me(self):
        return await self._call("getMe")

    async def delete_webhook(self):
        """getUpdates returns 409 while a webhook is registered."""
        try:
            return await self._call("deleteWebhook",
                                    json={"drop_pending_updates": False})
        except TelegramError as exc:
            log.warning("deleteWebhook: %s", exc)
            return None

    async def get_updates(self, offset: int | None, timeout: int):
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", params=params, retries=2) or []

    async def send_message(self, chat_id: int, text: str) -> bool:
        """One message, one attempt path. Returns success rather than raising,
        because the caller has already decided what to say."""
        if len(text) > MAX_MESSAGE_CHARS:
            log.error("reply is %d chars, over Telegram's %d limit",
                      len(text), MAX_MESSAGE_CHARS)
            return False
        try:
            await self._call("sendMessage",
                             json={"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True})
            return True
        except Exception as exc:  # noqa: BLE001 - the caller owes a reply and
            # has nothing left to fall back to; never turn this into a raise.
            log.error("sendMessage to %s failed: %s", chat_id, exc)
            return False
