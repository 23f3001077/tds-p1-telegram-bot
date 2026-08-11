"""Append-only JSONL run log — the artifact behind log_url.

Written line by line and flushed immediately, so a crash mid-run still leaves a
readable log. Every value is coerced to something JSON-serialisable, because a
logging failure must never take down an answer.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FIELD_CHARS = 8000
MAX_LOG_BYTES = 8 * 1024 * 1024


def _safe(value, limit: int = MAX_FIELD_CHARS):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…[+{len(value) - limit}]"
    if isinstance(value, dict):
        return {str(k): _safe(v, limit) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_safe(v, limit) for v in list(value)[:64]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe(repr(value), limit)


class RunLog:
    def __init__(self, path: Path, run_id: str, url: str):
        self.path = Path(path)
        self.run_id = run_id
        self.url = url
        self.started = time.time()
        self._bytes = 0
        self._closed = False
        # Must not raise: the caller is on the path that owes Telegram a reply,
        # and an unwritable log directory is not a reason to answer nothing.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not create log dir %s: %s", self.path.parent, exc)
        self.write("run_start", run_id=run_id, log_url=url)

    def write(self, kind: str, **fields) -> None:
        if self._closed or self._bytes > MAX_LOG_BYTES:
            return
        record = {
            "ts": round(time.time() - self.started, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "type": kind,
            **{k: _safe(v) for k, v in fields.items()},
        }
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        except Exception:  # noqa: BLE001
            line = json.dumps({"ts": record["ts"], "type": kind,
                               "note": "unserialisable payload"}) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            self._bytes += len(line)
        except Exception as exc:  # noqa: BLE001 - logging must never break a run
            log.warning("run log write failed (%s): %s", self.path, exc)

    def close(self, **fields) -> None:
        self.write("run_end", elapsed=round(time.time() - self.started, 3), **fields)
        self._closed = True


def prune(log_dir, retention_days: int, keep_newest: int = 500) -> int:
    """Delete run logs older than retention_days, always keeping the newest
    `keep_newest` regardless of age.

    Railway volumes are finite; a bot left running for months would otherwise
    fill one and start failing to write logs at all. Retention is deliberately
    long — a log_url handed to a grader must still resolve weeks later.
    Never raises: this is housekeeping, not part of answering.
    """
    if retention_days <= 0:
        return 0
    try:
        files = sorted(Path(log_dir).glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("log prune could not list %s: %s", log_dir, exc)
        return 0

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in files[keep_newest:]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception:  # noqa: BLE001 - a locked/vanished file is not fatal
            continue
    if removed:
        log.info("pruned %d run logs older than %d days", removed, retention_days)
    return removed
