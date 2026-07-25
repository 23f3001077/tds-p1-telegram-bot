"""Configuration, loaded once at import and validated before anything starts.

Railway injects PORT and RAILWAY_PUBLIC_DOMAIN automatically, so PUBLIC_BASE_URL
is derived rather than required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env before anything reads the environment, so `python -m app.main`
# behaves the same as the test harness. Real deployments set variables directly
# and simply have no .env to find.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional at runtime
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _api_keys() -> list[str]:
    """OPENAI_API_KEY may hold one key or several comma-separated keys, so a
    key that hits its usage cap (aipipe's free tier is $0.1/7 days) can be
    rotated past instead of failing every question until it resets."""
    raw = os.getenv("OPENAI_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys


def _public_base_url(port: int) -> str:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Railway sets this once you generate a domain under Settings -> Networking.
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return f"http://localhost:{port}"


@dataclass(frozen=True)
class Config:
    telegram_token: str
    openai_api_key: str
    openai_api_keys: list[str]
    openai_base_url: str
    model: str
    port: int
    public_base_url: str
    log_dir: Path
    state_path: Path
    reply_budget: float
    agent_max_steps: int
    py_timeout: int
    py_max_output: int
    max_concurrent_runs: int
    history_max_messages: int
    history_ttl_seconds: int
    poll_timeout: int
    log_level: str
    errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        port = _int("PORT", 8000)
        log_dir = Path(os.getenv("LOG_DIR", "data/logs"))
        keys = _api_keys()
        cfg = cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            openai_api_key=keys[0] if keys else "",
            openai_api_keys=keys,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://aipipe.org/openai/v1").strip(),
            model=os.getenv("AGENT_MODEL", "gpt-5-mini").strip(),
            port=port,
            public_base_url=_public_base_url(port),
            log_dir=log_dir,
            state_path=Path(os.getenv("STATE_PATH", str(log_dir.parent / "state.json"))),
            reply_budget=_float("REPLY_BUDGET", 150.0),
            agent_max_steps=_int("AGENT_MAX_STEPS", 12),
            py_timeout=_int("PY_TIMEOUT", 90),
            py_max_output=_int("PY_MAX_OUTPUT", 6000),
            max_concurrent_runs=_int("MAX_CONCURRENT_RUNS", 4),
            history_max_messages=_int("HISTORY_MAX_MESSAGES", 20),
            history_ttl_seconds=_int("HISTORY_TTL_SECONDS", 7200),
            poll_timeout=_int("POLL_TIMEOUT", 50),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
        return cfg

    def problems(self) -> list[str]:
        out = []
        if not self.telegram_token:
            out.append("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather)")
        if not self.openai_api_keys:
            out.append("OPENAI_API_KEY is not set")
        if os.getenv("RAILWAY_PUBLIC_DOMAIN") and not self.log_dir.is_absolute():
            out.append(
                f"LOG_DIR={self.log_dir} is a relative path on Railway, whose "
                "filesystem is ephemeral — every redeploy 404s log_urls already "
                "handed to the graders. Mount a volume at /data and set "
                "LOG_DIR=/data/logs."
            )
        if self.public_base_url.startswith("http://localhost"):
            out.append(
                "PUBLIC_BASE_URL resolves to localhost — the graders must be able "
                "to wget your log_url. On Railway, generate a domain under "
                "Settings > Networking, or set PUBLIC_BASE_URL explicitly."
            )
        if self.reply_budget >= 290:
            out.append(
                f"REPLY_BUDGET={self.reply_budget}s leaves no room inside the "
                "grader's 300s exchange budget"
            )
        return out


CONFIG = Config.load()
