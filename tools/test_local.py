"""Test the answer path locally, using the grader's own comparison logic.

Bots cannot message bots, so this bypasses Telegram entirely and drives the
same code the live handler uses. extract_answer() and the comparison are copied
verbatim from grade.py — a pass here is a pass there.

    python3 -m tools.test_local
    python3 -m tools.test_local -k median
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import solve                                   # noqa: E402
from app.config import CONFIG                                 # noqa: E402
from app.runlog import RunLog                                 # noqa: E402
from app.shape import assemble, extract_shape, fallback, plan  # noqa: E402

EXCHANGE_BUDGET = 300  # collect.py default, for the whole conversation


# ---- verbatim from grade.py; do not "improve" -----------------------------
def extract_answer(replies):
    if not replies:
        return None
    try:
        return json.loads(replies[-1].strip())
    except json.JSONDecodeError:
        return None
# ---------------------------------------------------------------------------


async def one_message(history, text):
    """Exactly one inbound message -> exactly one reply string."""
    history.append(text)
    template = extract_shape(text)
    if template is None:
        return "OK"

    run_id = uuid.uuid4().hex[:10]
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_url = f"{CONFIG.public_base_url}/logs/{run_id}.jsonl"
    runlog = RunLog(CONFIG.log_dir / f"{run_id}.jsonl", run_id, log_url)
    inner, wrapper = plan(template)

    try:
        answer = await asyncio.wait_for(
            solve(history, inner, runlog, time.time() + CONFIG.reply_budget),
            timeout=CONFIG.reply_budget,
        )
        payload = assemble(answer, wrapper, log_url) if answer is not None \
            else fallback(template, log_url)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! {type(exc).__name__}: {exc}")
        payload = fallback(template, log_url)
    runlog.close()
    return json.dumps(payload, ensure_ascii=False)


def _compare(answer, expected):
    """Match grade.py's exact-match rule, but log_url is randomly generated per
    run so a fixed key.json can never contain the real one. If the reply is
    wrapper-shaped ({"answer": ..., "log_url": ...}) and expected has no
    log_url key of its own, compare the inner "answer" and just sanity-check
    that log_url is a plausible public URL instead of matching it verbatim.
    """
    if answer == expected:
        return True, None
    if (isinstance(answer, dict) and isinstance(expected, dict)
            and "log_url" in answer and "log_url" not in expected):
        url = answer.get("log_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False, f"log_url is not a public URL: {url!r}"
        stripped = {k: v for k, v in answer.items() if k != "log_url"}
        if stripped == expected:
            return True, None
        return False, f"expected {expected}, got {stripped} (log_url ignored)"
    return False, f"expected {expected}, got {answer}"


async def run_case(question):
    history, replies = [], []
    started = time.time()
    for message in question["messages"]:
        replies.append(await one_message(history, message))
    elapsed = time.time() - started

    expected = question.get("expected")
    answer = extract_answer(replies)

    if answer is None:
        return "FAIL", elapsed, replies, "format_error — reply was not pure JSON"
    if expected is None:
        return "SKIP", elapsed, replies, f"no expected set; got {answer}"
    ok, detail = _compare(answer, expected)
    if ok:
        return "PASS", elapsed, replies, "ok"
    return "FAIL", elapsed, replies, detail


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default="evals/questions.json")
    parser.add_argument("-k", default="", help="substring filter on question id")
    args = parser.parse_args()

    questions = [q for q in json.load(open(args.questions))
                 if args.k in q["id"]]
    if not questions:
        sys.exit("no questions matched")

    passed = failed = 0
    for question in questions:
        verdict, elapsed, replies, detail = await run_case(question)
        passed += verdict == "PASS"
        failed += verdict == "FAIL"
        print(f"\n[{verdict}] {question['id']}  ({elapsed:.1f}s, "
              f"{len(replies)} replies)")
        print(f"  final: {replies[-1][:240] if replies else '(none)'}")
        print(f"  {detail}")
        budget = question.get("timeout_seconds", EXCHANGE_BUDGET)
        if elapsed > budget:
            print(f"  !! {elapsed:.0f}s over the {budget}s exchange budget — "
                  f"this records as 'timeout', which is terminal and never retried")

    print(f"\n{passed} passed, {failed} failed, {len(questions)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
