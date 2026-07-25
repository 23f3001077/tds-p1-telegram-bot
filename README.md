# TDS P1 — Data Analyst Telegram Bot

An LLM agent that receives a data-analysis question on Telegram, works the
answer out with Python, and replies with exactly one JSON object plus a public
link to its run log.

Built against the published grading pipeline
(`Jivraj-18/tds-p1-t2-2026-telegram-bot`), not just the question text.

---

## The contract that decides your mark

These come from `collect.py` and `grade.py`. Each one silently zeroes a
question if you get it wrong.

| Rule | Source |
|---|---|
| **One reply per inbound message** | `conv.get_response()` records the **first** reply after each send. A "thinking…" message becomes your graded answer. |
| **The reply is one JSON object, nothing else** | `json.loads(replies[-1].strip())` — a code fence, a greeting, or a second sentence gives `format_error`. |
| **Only the last reply is graded** | `extract_answer` reads `replies[-1]`. Earlier turns just need *some* reply. |
| **One timeout for the whole exchange** | `client.conversation(bot, timeout=timeout_seconds)`, default 300s — not per message. |
| **Comparison is exact** | `answer == expected`. Case-sensitive, extra keys fail, list order matters. (`2.0 == 2` is fine.) |
| **`timeout` and `bad_bot` are terminal** | `TERMINAL = {"ok","timeout","bad_bot"}` — never retried. Only our-side `error` gets a second run. |

That last row is the one that costs marks: **if your bot is down when your wave
runs, that is your grade.** There is no second attempt.

### How this repo satisfies each rule

- `app/main.py::answer()` sends exactly one message on every code path,
  including every exception path.
- A per-chat `asyncio.Lock` keeps multi-turn replies ordered.
- If the agent fails or runs out of time, `shape.fallback()` still emits a
  structurally valid JSON object. A wrong answer and a format error both score
  zero — but a reply keeps the status at `ok` instead of the terminal `timeout`.
- `REPLY_BUDGET` defaults to 150s so a three-turn question still fits inside 300s.

### The shape discrepancy

The public repo's `questions.json` expects a bare `{"state": "..."}` and its
`fake_student_bot.py` replies with exactly that. The exam page specifies
`{"answer": {...}, "log_url": "..."}`. The repo is the older shape.

Nothing here hardcodes either. `app/shape.py` extracts the JSON template from
the incoming message and replies in exactly that shape, adding the `log_url`
wrapper only when the message asks for one. It tolerates templates that are not
valid JSON (`{"values": [<numbers>]}` does not parse).

---

## Layout

```
app/
  config.py     env config, Railway auto-detection, startup validation
  main.py       poller + one-reply-per-message orchestration
  agent.py      LLM tool loop (run_python, submit), deadline-aware
  sandbox.py    subprocess Python with CPU/memory/output limits
  shape.py      tolerant parser for the requested reply template
  telegram.py   Bot API client: 429, 409, retries, 4096-char guard
  history.py    per-chat context, capped + TTL'd + persisted
  runlog.py     JSONL run log behind log_url
  server.py     FastAPI: /health, /logs/{name}, /logs
tools/test_local.py   local harness using grade.py's own comparison
evals/questions.json  your test cases
```

## Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in the two tokens
python -m tools.test_contract # reply contract; no key or network needed
python -m tools.test_local    # real answers; needs a key, no Telegram
python -m app.main            # go live
```

`.env` is read automatically by `app/config.py`, so a fresh clone needs nothing
beyond filling it in.

`tools/test_local.py` calls the handler directly — bots cannot message bots, so
this is the only way to exercise the answer path without a second Telegram
account. Its `extract_answer` is copied verbatim from `grade.py`. It ignores
`log_url` when comparing, since that is generated per run and no fixed
`expected` could match it; everything else is exact.

`tools/test_contract.py` stubs the agent out and asserts the invariant that
decides whether a question scores at all — one reply per message, always valid
JSON — under injected failures: the agent raising, returning nothing, exceeding
the budget, producing an answer past Telegram's 4096-char limit, and an
unwritable log directory. It needs no API key and runs in a second.

---

## Deploying to Railway

### 1. Create the bot

Message [@BotFather](https://t.me/BotFather) → `/newbot`. The username must end
in `bot` and match `^[a-zA-Z][a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$` (5–32 chars) or the
exam page will reject it. Copy the token.

### 2. Push to GitHub — public

```bash
git init && git add . && git commit -m "TDS P1 telegram bot"
gh repo create tds-p1-bot --public --source=. --push
```

The exam page calls `api.github.com/repos/<owner>/<repo>`; a 404 blocks
submission, so private will not work.

### 3. Create the Railway project

[railway.app](https://railway.app) → **New Project** → **Deploy from GitHub
repo** → pick the repo. Nixpacks detects Python from `requirements.txt` and
`.python-version`; `railway.toml` supplies the start command and health check.

### 4. Add a volume — do not skip this

Railway's filesystem is **ephemeral**. Without a volume, every redeploy wipes
your logs and every `log_url` you have already handed out 404s.

Service → **Variables** tab → **Volumes** → **New Volume**, mount path `/data`.

### 5. Set variables

Service → **Variables** → **Raw Editor**:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
OPENAI_API_KEY=eyJhbGciOi...
OPENAI_BASE_URL=https://aipipe.org/openai/v1
AGENT_MODEL=gpt-5-mini
LOG_DIR=/data/logs
STATE_PATH=/data/state.json
REPLY_BUDGET=150
```

`OPENAI_API_KEY` accepts several comma-separated keys
(`key_one,key_two,key_three`). aipipe's free tier caps a token at `$0.1 / 7
days`; when the active key gets a 429, `app/agent.py` rotates to the next one
automatically and logs a `key_rotate` entry, instead of falling back on every
question until the cap resets.

Do **not** set `PORT` or `PUBLIC_BASE_URL` — Railway injects `PORT` and
`RAILWAY_PUBLIC_DOMAIN`, and `config.py` derives the base URL from them.

### 6. Generate a domain

Settings → **Networking** → **Generate Domain**. Until you do this,
`RAILWAY_PUBLIC_DOMAIN` is unset, `log_url` falls back to `localhost`, and the
startup log prints a `CONFIG:` warning saying so.

### 7. Keep exactly one replica

Settings → **Replicas** → `1`. Telegram allows one `getUpdates` consumer; a
second replica produces HTTP 409 and the poller logs
`getUpdates conflict — is a second instance running?`

### 8. Verify before you register

```bash
curl https://<your-domain>/health
# {"ok":true,"bot":"your_bot","public_base_url":"https://<your-domain>",...}
```

Then message the bot from your own Telegram account:

```
What is 17 * 23? Reply with ONLY a JSON object like {"value": <number>}
```

You must get **exactly one** reply and it must be `{"value": 391}` — nothing
before it, nothing after it. If two messages arrive, that is the failure mode
that costs the most marks.

Finally, confirm the log is publicly fetchable from a machine that has never
authenticated:

```bash
curl https://<your-domain>/logs        # lists recent run URLs
wget -O- https://<your-domain>/logs/<run>.jsonl
```

A redirect to a sign-in page fails the offline review.

---

## Pre-submission checklist

- [ ] Bot username ends in `bot`, 5–32 chars
- [ ] GitHub repo is **public**
- [ ] Railway volume mounted at `/data`, `LOG_DIR=/data/logs`
- [ ] Domain generated; `/health` shows a non-localhost `public_base_url`
- [ ] Replicas = 1
- [ ] `wget` on a fresh `log_url` returns JSONL
- [ ] `python -m tools.test_local` passes, every case well under 300s
- [ ] Live bot: one reply, pure JSON
- [ ] Startup logs show no `CONFIG:` errors

Registering on the exam page auto-awards **0.1 marks**. The other 37.4 come
from your live bot and repository after the deadline.

## Operating notes

- **Watch the deploy logs on the first boot.** `config.py` prints every problem
  it finds as `CONFIG: ...` before anything starts.
- **`/health` is your monitor.** It reports uptime, run count and failure count.
  A climbing `failures` number means the agent is falling back rather than
  answering — check a recent log at `/logs`.
- **Cost control.** `MAX_CONCURRENT_RUNS` caps parallel agent runs;
  `AGENT_MAX_STEPS` caps tool calls per question.
- **If you must redeploy during the grading window,** do it fast. A restart
  drops in-flight conversations, and `timeout` is terminal.
