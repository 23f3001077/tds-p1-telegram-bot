Data-analyst Telegram bot for IIT Madras TDS 2026-05 Project 1 (37.5 marks). A grader messages the bot a data-analysis question; the bot must reply with exactly one JSON object containing the answer and a public log URL.
The grading pipeline — read this before changing anything
Graded by `github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot`. These constraints come from reading `collect.py` and `grade.py`, not from the assignment text. None of them are inferable from this codebase, and each one silently zeroes a question if broken.

1. Exactly one outbound message per inbound message. The grader calls `conv.get_response()`, which returns the FIRST reply after each send. A progress update, an acknowledgement, or a split message becomes the graded answer and the real answer is never seen.
2. The reply is one JSON object and nothing else. Grading does `json.loads(replies[-1].strip())`. A code fence, a preamble, or any trailing text produces `format_error`.
3. Only the last reply is graded. Earlier turns of a multi-turn question just need some reply so `get_response()` returns.
4. One timeout for the entire exchange, not per message — `client.conversation(bot, timeout=timeout_seconds)`, default 300s. A three-turn question shares that budget.
5. Comparison is exact: `answer == expected`. Case-sensitive, extra keys fail, list order matters. `2.0 == 2` is True in Python, so int/float mismatch is safe.
6. `timeout` and `bad_bot` are terminal (`TERMINAL = {"ok","timeout","bad_bot"}`). They are recorded once and never retried; only our-side `error` gets a second run. A bot that is down when its wave runs scores zero with no recourse.

Invariants — do not break these

* `app/main.py::answer()` must send exactly one message on every path, including every `except` branch. If you add a code path, add the send.
* Never send a status, typing, or acknowledgement message before an answer.
* On any failure, still reply with `shape.fallback()`. A wrong answer and a format error both score zero, but a reply records status `ok` instead of the terminal `timeout`.
* `REPLY_BUDGET` must stay well under 300s (default 150) because multi-turn questions share the exchange budget.
* `solve()` must never raise. It returns `None` when it cannot answer.
* A turn carrying no answer shape is acknowledged with `CONTEXT_REPLY`, which is a JSON object rather than prose — if it lands last in an exchange, `grade.py` still parses it.
* A reply over Telegram's 4096-char limit is replaced by `shape.fallback()`. `send_message` refuses to send an over-long message, and sending nothing is a terminal `timeout`.
* `History.clear()` runs after every answered question. The graders send every question to the same chat, so without it each question inherits the previous one's messages as context.
* Anything that can raise belongs inside `answer()`'s try. `RunLog` and `Telegram.send_message` are written not to raise for the same reason.

The reply-shape discrepancy
The public repo's `evals/questions.json` expects a bare `{"state": "..."}` and its `fake_student_bot.py` replies with exactly that. The exam page specifies `{"answer": {...}, "log_url": "..."}`. The repo is the older shape.
`app/shape.py` therefore does not hardcode either. It extracts the JSON template from the incoming message and replies in exactly that shape, adding the `log_url` wrapper only when the message asks for one.
Templates are not always valid JSON — `{"values": [<numbers>]}` does not parse — so `shape.py` uses a tolerant scanner. Do not replace it with `json.loads`.
Deployment constraints (Railway)

* Filesystem is ephemeral. Logs and history live on a volume at `/data`; without it, every redeploy 404s log URLs already handed to the graders.
* `PORT` and `RAILWAY_PUBLIC_DOMAIN` are injected. `config.py` derives `PUBLIC_BASE_URL` from them — do not hardcode either.
* Exactly one replica. Telegram allows a single `getUpdates` consumer; a second returns HTTP 409.
* `log_url` must be fetchable by plain `wget` with no auth. A redirect to a login page fails the offline review.

Testing

```bash
python -m tools.test_contract       # reply contract, no API key needed
python -m tools.test_local          # all cases
python -m tools.test_local -k median

```

`tools/test_local.py` bypasses Telegram (bots cannot message bots) and drives the same handler code. Its `extract_answer()` is copied verbatim from `grade.py` — keep it that way, do not "improve" it. Add cases to `evals/questions.json` using the pipeline's own format.
Every case must finish well under 300s. A case that runs long locally is a terminal `timeout` in production.
`tools/test_contract.py` stubs out `solve()` and asserts the one-reply/valid-JSON invariant under injected failures (agent raises, returns nothing, blows the budget, emits an over-long answer, unwritable log dir). Run it after touching `main.py::answer()` — it is the test that catches a code path added without a send.
`test_local.py` ignores `log_url` when comparing: it is generated per run, so a fixed `expected` can never match it. Everything else is exact, as in `grade.py`.
Conventions

* Standard library plus the pinned deps; no new runtime dependencies without a reason, since Nixpacks builds from `requirements.txt`.
* Secrets only in Railway Variables, never in the repo. `.env` is gitignored.
* Logging goes to stdout for Railway's log viewer.
* `app/sandbox.py` is a resource guard, not a security boundary — the agent needs network access to fetch datasets.
