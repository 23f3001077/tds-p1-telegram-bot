"""Unit tests for the agent loop's own machinery.

test_contract.py stubs solve() out entirely, so none of the logic *inside*
solve() is covered by it. That gap shipped a TypeError to production: the
repeated-failure nudge called runlog.write("nudge", kind=...) which collided
with write()'s own `kind` parameter, aborted the run, and cost ~80s of budget.

Every branch added to agent.py is executed here with a fake LLM, so a mistake
in one shows up as a failing test instead of a fallback in production.

    python -m tools.test_agent

No API key or network required.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.agent as agent                       # noqa: E402
from app.runlog import RunLog                   # noqa: E402


class MemoryLog(RunLog):
    """A RunLog that keeps records in memory but runs the real write() path,
    including its JSON serialisation and signature."""

    def __init__(self):
        self.records: list[dict] = []
        super().__init__(Path("data/logs/_unit_test.jsonl"), "unit-test", "http://x/l")

    def write(self, kind: str, /, **fields) -> None:
        self.records.append({"type": kind, **fields})
        super().write(kind, **fields)

    def kinds(self) -> list[str]:
        return [r["type"] for r in self.records]


def _msg(content=None, tool_calls=None):
    """A stand-in for one SDK chat message."""
    def _dump(**_kw):
        return {"role": "assistant", "content": content}
    return types.SimpleNamespace(
        content=content, tool_calls=tool_calls, model_dump=_dump)


def _call(call_id, name, arguments):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(message):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class FakeLLM:
    """Serves a scripted list of responses, one per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages: list[list] = []

    def install(self):
        outer = self

        class _Completions:
            async def create(self, *, model, messages, tools, **kw):
                outer.seen_messages.append(list(messages))
                if not outer.responses:
                    raise AssertionError("FakeLLM ran out of scripted responses")
                return outer.responses.pop(0)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_Completions()))
        agent.client = lambda: client
        agent._ensure_clients = lambda: [client]


def _sandbox(outputs):
    """Replace the real subprocess sandbox with scripted outputs."""
    seq = list(outputs)
    calls = []

    def fake_run_python(code, timeout, max_output, *a, **k):
        calls.append({"code": code, "timeout": timeout})
        return seq.pop(0) if seq else "done"

    agent.run_python = fake_run_python
    return calls


RESULTS: list[bool] = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f": {detail}" if detail else ""))


async def test_repeated_failure_nudge():
    """Two failing tool calls must inject a change-of-approach message.

    This is the exact path that raised TypeError in production.
    """
    fail = "Traceback (most recent call last):\n[exit code 1]"
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "run_python", {"code": "x"})])),
        _response(_msg(tool_calls=[_call("b", "run_python", {"code": "y"})])),
        _response(_msg(tool_calls=[_call("c", "submit", {"answer_json": '{"v":1}'})])),
    ])
    llm.install()
    _sandbox([fail, fail])
    runlog = MemoryLog()

    answer = await agent.solve(["q"], '{"v": <n>}', runlog, time.time() + 120)

    check("nudge: run completes without raising", answer == {"v": 1}, str(answer))
    check("nudge: 'nudge' record written", "nudge" in runlog.kinds(),
          str(runlog.kinds()))
    nudged = any(
        "Your last two attempts both failed" in m.get("content", "")
        for msgs in llm.seen_messages for m in msgs
        if isinstance(m, dict) and m.get("role") == "user")
    check("nudge: change-of-approach text reached the model", nudged)


async def test_no_nudge_when_failures_not_consecutive():
    ok, fail = "fine", "[exit code 1]"
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "run_python", {"code": "x"})])),
        _response(_msg(tool_calls=[_call("b", "run_python", {"code": "y"})])),
        _response(_msg(tool_calls=[_call("c", "submit", {"answer_json": '{"v":1}'})])),
    ])
    llm.install()
    _sandbox([fail, ok])
    runlog = MemoryLog()

    await agent.solve(["q"], '{"v": <n>}', runlog, time.time() + 120)
    check("no nudge when a success breaks the streak",
          "nudge" not in runlog.kinds(), str(runlog.kinds()))


async def test_placeholder_rejected_once():
    llm = FakeLLM([
        _response(_msg(tool_calls=[
            _call("a", "submit", {"answer_json": '{"country": "<country name>"}'})])),
        _response(_msg(tool_calls=[
            _call("b", "submit", {"answer_json": '{"country": "Mexico"}'})])),
    ])
    llm.install()
    _sandbox([])
    runlog = MemoryLog()

    answer = await agent.solve(["q"], '{"country": "<c>"}', runlog, time.time() + 120)
    check("placeholder: rejected then real answer accepted",
          answer == {"country": "Mexico"}, str(answer))
    check("placeholder: rejection logged",
          any(r["type"] == "submit_rejected" and r.get("reason") == "placeholder"
              for r in runlog.records))


async def test_placeholder_accepted_second_time():
    """A stubborn model must not loop forever — take the placeholder rather
    than burn the clock."""
    ph = '{"country": "<country name>"}'
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "submit", {"answer_json": ph})])),
        _response(_msg(tool_calls=[_call("b", "submit", {"answer_json": ph})])),
    ])
    llm.install()
    _sandbox([])
    runlog = MemoryLog()

    answer = await agent.solve(["q"], '{"country": "<c>"}', runlog, time.time() + 120)
    check("placeholder: second attempt accepted, no infinite loop",
          answer == {"country": "<country name>"}, str(answer))


async def test_handler_error_does_not_kill_run():
    """A defect while handling one step must not forfeit the whole budget."""
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "run_python", {"code": "x"})])),
        _response(_msg(tool_calls=[_call("b", "submit", {"answer_json": '{"v":2}'})])),
    ])
    llm.install()

    boom = {"n": 0}

    def exploding(code, timeout, max_output, *a, **k):
        boom["n"] += 1
        raise RuntimeError("simulated internal defect")

    agent.run_python = exploding
    runlog = MemoryLog()

    answer = await agent.solve(["q"], '{"v": <n>}', runlog, time.time() + 120)
    check("handler error: run recovers and still answers",
          answer == {"v": 2}, str(answer))
    check("handler error: logged", "handler_error" in runlog.kinds(),
          str(runlog.kinds()))


async def test_urgency_nudge_on_clock():
    """The time-based nudge must fire even when steps remain."""
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "submit", {"answer_json": '{"v":3}'})])),
    ])
    llm.install()
    _sandbox([])
    runlog = MemoryLog()

    # 30s left: under the 45s threshold, well above the 10s abort floor.
    await agent.solve(["q"], '{"v": <n>}', runlog, time.time() + 30)
    urged = any(
        "before the reply deadline" in m.get("content", "")
        for msgs in llm.seen_messages for m in msgs
        if isinstance(m, dict) and m.get("role") == "user")
    check("urgency: clock-based nudge reaches the model", urged)


async def test_sandbox_budget_is_proportional():
    """One call must never be handed most of the remaining time."""
    llm = FakeLLM([
        _response(_msg(tool_calls=[_call("a", "run_python", {"code": "x"})])),
        _response(_msg(tool_calls=[_call("b", "submit", {"answer_json": '{"v":4}'})])),
    ])
    llm.install()
    calls = _sandbox(["fine"])
    runlog = MemoryLog()

    await agent.solve(["q"], '{"v": <n>}', runlog, time.time() + 48)
    budget = calls[0]["timeout"]
    check("sandbox budget <= half of remaining", budget <= 25, f"{budget}s of ~48s")
    check("sandbox budget still usable", budget >= 5, f"{budget}s")


async def test_runlog_write_accepts_kind_field():
    """The exact collision that broke production: a field literally named
    'kind' must not clash with write()'s own parameter."""
    runlog = MemoryLog()
    try:
        runlog.write("nudge", kind="repeated_tool_failure", step=1)
        ok = True
    except TypeError as exc:
        ok = False
        print(f"         {exc}")
    check("runlog.write tolerates a field named 'kind'", ok)


async def main() -> int:
    tests = [
        ("repeated-failure nudge", test_repeated_failure_nudge),
        ("non-consecutive failures", test_no_nudge_when_failures_not_consecutive),
        ("placeholder rejection", test_placeholder_rejected_once),
        ("placeholder no-loop", test_placeholder_accepted_second_time),
        ("handler error recovery", test_handler_error_does_not_kill_run),
        ("clock-based urgency nudge", test_urgency_nudge_on_clock),
        ("proportional sandbox budget", test_sandbox_budget_is_proportional),
        ("runlog kind collision", test_runlog_write_accepts_kind_field),
    ]
    print("agent loop unit tests\n")
    for title, fn in tests:
        print(f"{title}:")
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            RESULTS.append(False)
            print(f"  [FAIL] raised {type(exc).__name__}: {exc}")
        print()

    print(f"{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
