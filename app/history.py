"""Per-chat conversation memory.

Multi-turn questions carry context in earlier messages, so history has to
survive a redeploy mid-exchange. It is persisted to the same volume as the
logs, capped in size, and expired by age so a long-running bot cannot grow
without bound.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


class History:
    def __init__(self, path: Path, max_messages: int, ttl_seconds: int):
        self.path = Path(path)
        self.max_messages = max_messages
        self.ttl = ttl_seconds
        self._chats: dict[str, dict] = {}
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._chats = json.loads(self.path.read_text(encoding="utf-8"))
                log.info("restored history for %d chats", len(self._chats))
        except Exception as exc:  # noqa: BLE001 - corrupt state must not block boot
            log.warning("could not restore history: %s", exc)
            self._chats = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._chats), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist history: %s", exc)

    def _expire(self, now: float) -> None:
        stale = [cid for cid, rec in self._chats.items()
                 if now - rec.get("updated", 0) > self.ttl]
        for cid in stale:
            self._chats.pop(cid, None)

    def append(self, chat_id: int, text: str) -> list[str]:
        """Record a message and return the full context for this chat."""
        now = time.time()
        with self._lock:
            self._expire(now)
            key = str(chat_id)
            record = self._chats.setdefault(
                key, {"messages": [], "updated": now, "started": now})
            record.setdefault("started", now)  # records written before this field
            record["messages"].append(text)
            record["messages"] = record["messages"][-self.max_messages:]
            record["updated"] = now
            self._save()
            return list(record["messages"])

    def started_at(self, chat_id: int) -> float | None:
        """When the current exchange's first message arrived, or None.

        The grader's timeout covers the whole exchange, so the deadline for the
        final answer has to be measured from here, not from the last message.
        """
        with self._lock:
            record = self._chats.get(str(chat_id))
            return record.get("started") if record else None

    def clear(self, chat_id: int) -> None:
        """Forget this chat. Called once a question has been answered, so the
        next question does not inherit the previous one's messages as context —
        the graders send every question to the same chat."""
        with self._lock:
            if self._chats.pop(str(chat_id), None) is not None:
                self._save()

    def stats(self) -> dict:
        with self._lock:
            return {"chats": len(self._chats),
                    "messages": sum(len(r["messages"]) for r in self._chats.values())}
