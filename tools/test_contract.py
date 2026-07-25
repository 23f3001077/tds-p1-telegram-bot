"""Failure-injection tests for the reply contract.

test_local.py checks that answers are *correct*. This checks the property that
decides whether a question is scored at all, under conditions the happy path
never reaches: exactly one outbound message per inbound message, and that
message always parses as JSON.

Every case here corresponds to a way collect.py records a terminal status:
sending nothing is `timeout`, sending prose is `format_error`, and neither is
ever retried.

    python -m tools.test_contract

No API key or network required — solve() is stubbed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as m                              # noqa: E402
from app.history import History                   # noqa: E402

SHAPE = 'Reply with ONLY {"answer": {"sd": <number>}, "log_url": "<url>"}'
STATE_PATH = Path("data/state_contract_test.json")


class FakeTelegram:
    """Records outbound messages instead of sending them."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text):
        self.sent.append(text)
        return True


def _set(field, value):
    object.__setattr__(m.CONFIG, field, value)   # CONFIG is a frozen dataclass


async def drive(stub_solve, messages, log_dir=None):
    """Feed messages through the real handler with solve() stubbed out."""
    _set("log_dir", Path(log_dir) if log_dir else Path("data/logs"))
    m._run_slots = asyncio.Semaphore(4)
    m._chat_locks.clear()
    m.solve = stub_solve

    tg = FakeTelegram()
    history = History(STATE_PATH, 20, 7200)
    history.clear(1)
    for text in messages:
        await m.answer(tg, history, 1, text)
    return tg.sent, history


def check(name, sent, expect_replies=1):
    ok = len(sent) == expect_replies
    detail = f"{len(sent)} reply/replies"
    if ok:
        try:
            json.loads(sent[-1].strip())          # exactly what grade.py does
            detail += ", last parses as JSON"
        except json.JSONDecodeError as exc:
            ok = False
            detail += f", last is NOT JSON ({exc})"
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if sent:
        print(f"         {sent[-1][:100]}")
    return ok


async def main() -> int:
    async def raises(*a, **k):
        raise RuntimeError("agent exploded")

    async def returns_none(*a, **k):
        return None

    async def hangs(*a, **k):
        await asyncio.sleep(999)

    async def oversize(*a, **k):
        return {"sd": "x" * 9000}                 # over Telegram's 4096 limit

    async def good(*a, **k):
        return {"sd": 2.0}

    results = []
    print("reply contract under failure injection\n")

    for name, stub in (
        ("solve() raises", raises),
        ("solve() returns None", returns_none),
        ("solve() returns an oversize answer", oversize),
        ("normal answer", good),
    ):
        sent, _ = await drive(stub, [SHAPE])
        results.append(check(name, sent))

    sent, _ = await drive(good, ["here are some numbers: 1, 2, 3"])
    results.append(check("message with no answer shape", sent))

    # A log directory that cannot be created must not stop the reply: the run
    # log is best-effort, the reply is not.
    unwritable = "Z:/nonexistent-volume/logs" if sys.platform == "win32" \
        else "/proc/nonexistent/logs"
    sent, _ = await drive(good, [SHAPE], log_dir=unwritable)
    results.append(check("unwritable log directory", sent))

    # Multi-turn: one reply per inbound message, context kept until answered,
    # then cleared so the next question does not inherit it.
    sent, history = await drive(good, [
        "I am going to give you some numbers.",
        "The numbers are 4, 8, 15.",
        SHAPE,
    ])
    results.append(check("multi-turn: one reply each", sent, expect_replies=3))
    cleared = history.stats()["chats"] == 0
    print(f"  [{'PASS' if cleared else 'FAIL'}] history cleared after answering: "
          f"{history.stats()}")
    results.append(cleared)

    _set("reply_budget", 6.0)
    sent, _ = await drive(hangs, [SHAPE])
    results.append(check("solve() exceeds the reply budget", sent))

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
