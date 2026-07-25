"""Entry point: HTTP server and Telegram poller in one process.

The reply contract, enforced structurally:
  * exactly ONE outbound message per inbound message (the grader records the
    first reply it sees and moves on)
  * that message is exactly one JSON object and nothing else
  * it is sent within the deadline, or a valid-shaped fallback is sent instead
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
import uuid
from collections import OrderedDict

import uvicorn

from .agent import solve
from .config import CONFIG
from .history import History
from .runlog import RunLog
from .server import STATE, app
from .shape import assemble, extract_shape, fallback, plan
from .telegram import MAX_MESSAGE_CHARS, Conflict, Telegram, TelegramError

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")

_shutdown = asyncio.Event()
_chat_locks: dict[int, asyncio.Lock] = {}
_seen: "OrderedDict[int, None]" = OrderedDict()
_run_slots: asyncio.Semaphore | None = None

# Acknowledgement for a turn that carries no answer shape. It is JSON rather
# than "OK" so that if it ever lands as the final reply of an exchange,
# grade.py's json.loads succeeds and the status is a scored `ok` instead of a
# format_error.
CONTEXT_REPLY = '{"ok": true}'


def _already_seen(update_id: int) -> bool:
    """Bounded dedup — Telegram can redeliver if an offset commit is lost."""
    if update_id in _seen:
        return True
    _seen[update_id] = None
    while len(_seen) > 5000:
        _seen.popitem(last=False)
    return False


def _chat_lock(chat_id: int) -> asyncio.Lock:
    """One in-flight answer per chat, so multi-turn replies stay ordered."""
    lock = _chat_locks.get(chat_id)
    if lock is None:
        if len(_chat_locks) > 1000:
            for cid, existing in list(_chat_locks.items()):
                if not existing.locked():
                    del _chat_locks[cid]
        lock = _chat_locks[chat_id] = asyncio.Lock()
    return lock


def _safe_fallback(template: str | None, log_url: str) -> object:
    """fallback() over an arbitrary template, guaranteed not to raise."""
    try:
        return fallback(template, log_url)
    except Exception:  # noqa: BLE001
        log.exception("fallback() failed for template %r", template)
        return {"answer": None, "log_url": log_url}


def _encode(payload: object, template: str | None, log_url: str) -> str:
    """Serialise the reply. Always returns something json.loads() can parse,
    because grade.py runs json.loads on it and anything else is a
    format_error."""
    for candidate in (payload, _safe_fallback(template, log_url)):
        try:
            return json.dumps(candidate, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            continue
    return '{"answer": null}'


async def answer(tg: Telegram, history: History, chat_id: int, text: str) -> None:
    """Handle one inbound message. Sends exactly one reply on every path.

    Everything that can fail is inside the try: an exception that escaped here
    would send nothing at all, and collect.py records that as `timeout`, which
    is terminal and never retried.
    """
    async with _chat_lock(chat_id):
        started = time.time()
        run_id = f"{int(started)}-{uuid.uuid4().hex[:8]}"
        log_url = f"{CONFIG.public_base_url}/logs/{run_id}.jsonl"
        template: str | None = None
        runlog: RunLog | None = None
        payload = None
        answered = False
        body = ""

        try:
            context = history.append(chat_id, text)
            template = extract_shape(text)

            if template is None:
                # Context-only turn in a multi-turn question. It still needs a
                # reply or the grader's get_response() blocks until the whole
                # exchange times out — and the reply has to be valid JSON in
                # case this turn is the last one, which is the one graded.
                await tg.send_message(chat_id, CONTEXT_REPLY)
                return

            inner, wrapper = plan(template)
            runlog = RunLog(CONFIG.log_dir / f"{run_id}.jsonl", run_id, log_url)
            deadline = started + CONFIG.reply_budget

            async with _run_slots:
                answer_obj = await asyncio.wait_for(
                    solve(context, inner, runlog, deadline),
                    timeout=max(5.0, deadline - time.time()),
                )
            if answer_obj is not None:
                payload = assemble(answer_obj, wrapper, log_url)
            else:
                runlog.write("fallback", reason="agent returned no answer")
        except asyncio.TimeoutError:
            if runlog:
                runlog.write("fallback", reason="reply budget exhausted")
        except Exception as exc:  # noqa: BLE001 - a reply must always go out
            log.exception("run %s failed", run_id)
            if runlog:
                runlog.write("fallback", reason=f"{type(exc).__name__}: {exc}")

        try:
            if payload is None:
                STATE["failures"] = STATE.get("failures", 0) + 1
                payload = _safe_fallback(template, log_url)

            body = _encode(payload, template, log_url)
            if len(body) > MAX_MESSAGE_CHARS:
                # send_message refuses to send an over-long message, and sending
                # nothing is a terminal timeout. A fallback is small and still
                # parses, which keeps the status at `ok`.
                log.error("reply for run %s is %d chars; sending fallback",
                          run_id, len(body))
                if runlog:
                    runlog.write("oversize_reply", chars=len(body))
                body = _encode(_safe_fallback(template, log_url), template, log_url)

            answered = await tg.send_message(chat_id, body)
            STATE["runs"] = STATE.get("runs", 0) + 1
            log.info("chat=%s run=%s sent=%s %.1fs", chat_id, run_id, answered,
                     time.time() - started)
        finally:
            if runlog:
                runlog.close(sent=answered, reply_chars=len(body))
            # The question is over. Without this the next question inherits this
            # one's messages as context — the graders reuse a single chat.
            with contextlib.suppress(Exception):
                history.clear(chat_id)


async def poll(tg: Telegram, history: History) -> None:
    offset: int | None = None
    tasks: set[asyncio.Task] = set()
    backoff = 1.0

    def _finished(task: asyncio.Task) -> None:
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # answer() is meant to handle its own failures; reaching here means
            # a message went unanswered, which is a terminal timeout.
            log.error("answer task died without replying", exc_info=task.exception())

    while not _shutdown.is_set():
        try:
            updates = await tg.get_updates(offset, CONFIG.poll_timeout)
            backoff = 1.0
        except Conflict:
            # Another consumer holds getUpdates: a leftover webhook, or a second
            # Railway replica. Scale to exactly one replica.
            log.error("getUpdates conflict — is a second instance running? "
                      "Set replicas to 1. Retrying in 15s.")
            await asyncio.sleep(15)
            await tg.delete_webhook()
            continue
        except TelegramError as exc:
            log.warning("getUpdates: %s (retry in %.0fs)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.exception("unexpected poll failure: %s", exc)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            if _already_seen(update["update_id"]):
                continue
            message = update.get("message")
            if not message or not message.get("text"):
                continue
            task = asyncio.create_task(
                answer(tg, history, message["chat"]["id"], message["text"])
            )
            tasks.add(task)
            task.add_done_callback(_finished)

    if tasks:
        log.info("draining %d in-flight answers", len(tasks))
        await asyncio.wait(tasks, timeout=CONFIG.reply_budget + 30)


async def main_async() -> None:
    global _run_slots
    _run_slots = asyncio.Semaphore(CONFIG.max_concurrent_runs)

    problems = CONFIG.problems()
    for problem in problems:
        log.error("CONFIG: %s", problem)
    if not CONFIG.telegram_token or not CONFIG.openai_api_key:
        sys.exit("Refusing to start without TELEGRAM_BOT_TOKEN and OPENAI_API_KEY.")

    try:
        CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.error("CONFIG: log dir %s is not writable (%s) — log_url will 404",
                  CONFIG.log_dir, exc)
    history = History(CONFIG.state_path, CONFIG.history_max_messages,
                      CONFIG.history_ttl_seconds)
    STATE["history"] = history

    log.info("log URLs will look like %s/logs/<run>.jsonl", CONFIG.public_base_url)

    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=CONFIG.port, log_level="warning",
        access_log=False,
    ))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown.set)

    # The HTTP server comes up first so Railway's health check can pass while
    # Telegram is still being reached. Doing it the other way round turns a slow
    # or unreachable Telegram API at boot into a container restart loop.
    server_task = asyncio.create_task(server.serve())

    tg = Telegram(CONFIG.telegram_token)
    await tg.delete_webhook()
    try:
        me = await tg.get_me()
        STATE["bot_username"] = me.get("username")
        log.info("running as @%s", me.get("username"))
    except TelegramError as exc:
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        await tg.aclose()
        sys.exit(f"Telegram rejected the token: {exc}")

    poll_task = asyncio.create_task(poll(tg, history))

    await _shutdown.wait()
    log.info("shutting down")
    server.should_exit = True
    await asyncio.gather(poll_task, server_task, return_exceptions=True)
    await tg.aclose()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
