"""The reasoning loop: think, run Python, submit an answer in the exact
requested shape.

Two invariants the rest of the system depends on:
  * solve() never raises — it returns None if it could not produce an answer
  * solve() respects the deadline, because a late reply is a zero
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from .config import CONFIG
from .runlog import RunLog
from .sandbox import run_python

log = logging.getLogger(__name__)

_clients: list[AsyncOpenAI] = []
_key_index = 0


def _ensure_clients() -> list[AsyncOpenAI]:
    """One client per key. Multiple comma-separated OPENAI_API_KEY values let a
    key that has hit its usage cap be rotated past mid-question."""
    global _clients
    if not _clients:
        keys = CONFIG.openai_api_keys or (
            [CONFIG.openai_api_key] if CONFIG.openai_api_key else []
        )
        _clients = [
            AsyncOpenAI(
                base_url=CONFIG.openai_base_url,
                api_key=key,
                timeout=90.0,
                max_retries=0,  # retries are handled here, against the deadline
            )
            for key in keys
        ]
    return _clients


def client() -> AsyncOpenAI:
    clients = _ensure_clients()
    if not clients:
        raise RuntimeError("no OPENAI_API_KEY configured")
    return clients[_key_index % len(clients)]


def _rotate_key(runlog: RunLog, reason: str) -> None:
    global _key_index
    _key_index = (_key_index + 1) % max(len(_ensure_clients()), 1)
    runlog.write("key_rotate", to_index=_key_index, reason=reason)


def _is_rate_limited(exc: Exception) -> bool:
    """A capped or throttled key, as opposed to a bad request we should not
    retry on a different key. aipipe reports its $0.1/7d cap as a 429."""
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "quota" in text


SYSTEM = """You are a data-analysis agent answering questions sent over Telegram.

Work every answer out. Do not answer from memory when the question refers to a
dataset — download it with run_python and compute the result. Prefer the exact
source named in the question.

Method:
1. Restate what is being asked and what the answer's type must be.
2. Use run_python to fetch, parse and compute. Print intermediate values so you
   can check them.
3. Sanity-check the result before submitting. If a number looks implausible,
   investigate rather than submitting it.
4. Call submit exactly once with a JSON string in the requested shape.

Shape rules, which decide whether you score at all:
- Exactly the keys requested. No extras, no wrapper, no commentary.
- Placeholders like "<state name>" or [<numbers>] indicate the expected TYPE.
  Replace them with real values.
- Use official spellings for names. Preserve any ordering the question implies.
- Do not round unless asked to.

You are on a hard clock. If the deadline is close, submit your best supported
answer instead of continuing to investigate — a wrong answer and no answer score
the same, but no answer also wastes the attempt.

API discovery, before you commit to a URL shape:
- Don't assume a REST/OData convention is correct. A generic collection name
  like ".../IndicatorData?filter=..." is a guess, not a fact — if it 404s once,
  don't retry the same shape with different filter syntax. Instead probe the
  API's root or docs to learn its real structure (e.g. some OData APIs expose
  one entity per code, like ".../WHOSIS_000001", not a filterable collection).
- Never fetch a full schema/metadata document (like an OData $metadata file) to
  answer one question — those are megabytes of XML you don't need. Ask for a
  single small record first (e.g. a page size of 1) to learn field names cheaply.
- If two consecutive requests to the same API 404 or fail, stop and change
  approach rather than trying a third variation of the same guess."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python 3 and return stdout/stderr. pandas, numpy, "
                "requests, bs4, lxml and openpyxl are available and the network "
                "is reachable. Nothing persists between calls — print anything "
                "you need to keep."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to run."}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "Submit the final answer. Call exactly once, at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer_json": {
                        "type": "string",
                        "description": "The answer as a JSON string matching the requested shape exactly.",
                    }
                },
                "required": ["answer_json"],
            },
        },
    },
]


_FAILURE_MARKERS = (
    "[exit code",
    "traceback (most recent call last)",
    "[timed out after",
    "[out of memory]",
    "[sandbox failed",
)


def _looks_failed(output: str) -> bool:
    """Whether a run_python result was an error rather than a usable answer."""
    low = (output or "").lower()
    return any(marker in low for marker in _FAILURE_MARKERS)


def _has_placeholder(value) -> bool:
    """True if the model echoed a template placeholder like "<country name>"
    instead of substituting a real value."""
    if isinstance(value, str):
        text = value.strip()
        return text.startswith("<") and text.endswith(">")
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    return False


def _build_prompt(history: list[str], shape: str) -> str:
    convo = "\n\n".join(f"[message {i + 1}]\n{m}" for i, m in enumerate(history))
    return (
        f"{convo}\n\n---\n"
        f"Answer the LAST message. Earlier messages are context.\n"
        f"Required reply shape: {shape}\n"
        f"Call submit with a JSON string in exactly that shape."
    )


async def solve(history: list[str], shape: str, runlog: RunLog,
                deadline: float):
    """Returns the parsed answer object, or None. Never raises."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _build_prompt(history, shape)},
    ]
    runlog.write("question", turns=len(history), shape=shape,
                 last_message=history[-1] if history else None)

    consecutive_errors = 0
    urgency_nudged = False
    # Shared with _handle_response: tool outcomes it observes, decisions made
    # here. Keeps the loop able to react without threading return values.
    state = {"tool_failures": 0, "placeholder_rejected": False}

    for step in range(CONFIG.agent_max_steps):
        remaining = deadline - time.time()
        if remaining <= 10:
            runlog.write("abort", reason="deadline", step=step,
                         remaining=round(remaining, 1))
            return None

        if step == CONFIG.agent_max_steps - 2:
            messages.append({
                "role": "user",
                "content": "You are nearly out of steps. Call submit now with your best answer.",
            })
        elif state["tool_failures"] == 2:
            # Two failures in a row means the current approach is wrong, not
            # merely mistyped. Left alone the model tends to re-send the same
            # URL with different filter syntax until the clock runs out.
            state["tool_failures"] = -1  # nudge once per run, not every step
            runlog.write("nudge", kind="repeated_tool_failure", step=step)
            messages.append({
                "role": "user",
                "content": (
                    "Your last two attempts both failed. Do not retry the same "
                    "endpoint or approach with small variations — that is what "
                    "just failed twice. Change strategy: inspect what the source "
                    "actually offers (its index, root listing, or documentation) "
                    "with ONE small cheap request, or switch to a different data "
                    "source entirely. Keep each request small."
                ),
            })
        elif not urgency_nudged and remaining <= 45:
            # Some questions blow the wall clock long before they blow the step
            # count (e.g. slow/unfamiliar external APIs) — the step-based nudge
            # above never fires in that case, so watch the clock too.
            urgency_nudged = True
            messages.append({
                "role": "user",
                "content": (
                    f"Only ~{int(remaining)}s remain before the reply deadline. "
                    "Stop investigating and call submit now with your best "
                    "supported answer — a late reply scores nothing at all, "
                    "which is worse than a guess."
                ),
            })

        response = None
        keys_available = max(len(_ensure_clients()), 1)
        for attempt in range(keys_available):
            remaining = deadline - time.time()
            if remaining <= 10:
                break
            try:
                response = await asyncio.wait_for(
                    client().chat.completions.create(
                        model=CONFIG.model, messages=messages, tools=TOOLS,
                    ),
                    timeout=max(15.0, min(remaining - 5, 90.0)),
                )
                break
            except asyncio.TimeoutError:
                runlog.write("llm_timeout", step=step)
                consecutive_errors += 1
                break
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limited(exc) and attempt < keys_available - 1:
                    runlog.write("llm_rate_limited", step=step,
                                 error=f"{type(exc).__name__}: {exc}")
                    _rotate_key(runlog, reason="rate_limited")
                    continue
                runlog.write("llm_error", step=step, error=f"{type(exc).__name__}: {exc}")
                log.warning("LLM call failed at step %d: %s", step, exc)
                consecutive_errors += 1
                break

        if response is not None:
            consecutive_errors = 0
            result = await _handle_response(response, messages, runlog, step,
                                            deadline, state)
            if result is not _CONTINUE:
                return result
            continue

        if consecutive_errors >= 3:
            runlog.write("abort", reason="three consecutive LLM failures")
            return None
        await asyncio.sleep(min(2 ** consecutive_errors, 8))

    runlog.write("abort", reason="max steps reached")
    return None


_CONTINUE = object()


async def _handle_response(response, messages, runlog: RunLog, step: int,
                           deadline: float, state: dict):
    choice = response.choices[0].message
    try:
        messages.append(choice.model_dump(exclude_none=True))
    except Exception:  # noqa: BLE001 - SDK shape drift must not be fatal
        messages.append({"role": "assistant", "content": choice.content or ""})

    if choice.content:
        runlog.write("thinking", step=step, text=choice.content)

    calls = choice.tool_calls or []
    if not calls:
        runlog.write("no_tool_call", step=step)
        messages.append({
            "role": "user",
            "content": "Reply only by calling run_python or submit.",
        })
        return _CONTINUE

    for call in calls:
        name = getattr(call.function, "name", "")
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if name == "submit":
            raw = (args.get("answer_json") or "").strip()
            # Models occasionally wrap the JSON in a fence despite instructions.
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
                raw = raw.rsplit("```", 1)[0].strip()
            runlog.write("submit", step=step, raw=raw)
            try:
                answer = json.loads(raw)
            except json.JSONDecodeError as exc:
                runlog.write("submit_rejected", error=str(exc))
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": f"That is not valid JSON ({exc}). Send only the raw JSON value.",
                })
                continue
            if _has_placeholder(answer) and not state["placeholder_rejected"]:
                # The template was echoed back instead of answered. Push back
                # once — never twice, or a stubborn model burns the whole clock.
                state["placeholder_rejected"] = True
                runlog.write("submit_rejected", reason="placeholder", raw=raw)
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": (
                        "That still contains a template placeholder in angle "
                        "brackets. Replace it with the real computed value."
                    ),
                })
                continue
            runlog.write("final", answer=answer)
            return answer

        if name == "run_python":
            code = args.get("code", "")
            runlog.write("tool_call", step=step, tool="run_python", code=code)
            # Proportional, not just "whatever is left minus 10". A single call
            # may take at most half the remaining time, so a slow fetch late in
            # the run cannot burn the endgame and leave nothing to recover with
            # — the failure mode where a 38s $metadata fetch ate the last 48s.
            remaining = deadline - time.time()
            budget = max(5, min(CONFIG.py_timeout,
                                int(remaining * 0.5),
                                int(remaining) - 15))
            output = await asyncio.to_thread(
                run_python, code, budget, CONFIG.py_max_output,
            )
            failed = _looks_failed(output)
            runlog.write("tool_result", step=step, output=output, failed=failed)
            if state["tool_failures"] >= 0:  # -1 means "already nudged"
                state["tool_failures"] = state["tool_failures"] + 1 if failed else 0
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": output})
            continue

        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": f"No such tool: {name}"})

    return _CONTINUE
