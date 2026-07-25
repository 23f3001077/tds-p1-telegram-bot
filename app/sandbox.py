"""Run agent-authored Python in a child process with hard limits.

The agent needs network access (datasets live on the web), so this is a
resource guard rather than a security boundary — it stops runaway loops,
memory blowups and multi-megabyte stdout from taking the bot down.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile

log = logging.getLogger(__name__)

_PRELUDE = (
    "import warnings, sys\n"
    "warnings.filterwarnings('ignore')\n"
    "sys.setrecursionlimit(10000)\n"
)

# Passed through to agent-authored code. This is a whitelist rather than a copy
# of os.environ on purpose: it keeps TELEGRAM_BOT_TOKEN and OPENAI_API_KEY out
# of a subprocess running code the model wrote. SystemRoot/COMSPEC/PATHEXT are
# required for sockets and TLS to work at all on Windows.
_INHERIT_ENV = (
    "PATH", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "WINDIR",
    "LANG", "LC_ALL", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)


def _child_env(workdir: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _INHERIT_ENV if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env.update({
        "HOME": workdir,
        "USERPROFILE": workdir,
        "TMPDIR": workdir,
        "TEMP": workdir,
        "TMP": workdir,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
    })
    return env


def _limits(memory_mb: int, cpu_seconds: int):
    """Applied in the child before exec. POSIX only; a no-op elsewhere."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    def apply():
        soft_mem = memory_mb * 1024 * 1024
        for res, limit in (
            (resource.RLIMIT_AS, soft_mem),
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
            (resource.RLIMIT_NPROC, 256),
        ):
            try:
                resource.setrlimit(res, (limit, limit))
            except (ValueError, OSError):
                pass
        os.setsid()

    return apply


def run_python(code: str, timeout: int = 90, max_output: int = 6000,
               memory_mb: int = 1024) -> str:
    """Execute code, return combined stdout/stderr as text. Never raises."""
    if not code or not code.strip():
        return "[no code supplied]"

    with tempfile.TemporaryDirectory(prefix="agent-") as workdir:
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _PRELUDE + code],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                preexec_fn=_limits(memory_mb, timeout + 5),
                env=_child_env(workdir),
            )
        except subprocess.TimeoutExpired:
            return f"[timed out after {timeout}s — make the code faster or narrow the data]"
        except MemoryError:
            return "[out of memory]"
        except Exception as exc:  # noqa: BLE001 - must never propagate
            log.warning("sandbox failure: %s", exc)
            return f"[sandbox failed: {type(exc).__name__}: {exc}]"

    parts = []
    if proc.stdout:
        parts.append(proc.stdout.rstrip())
    if proc.stderr:
        parts.append("[stderr]\n" + proc.stderr.rstrip())
    if proc.returncode != 0:
        parts.append(f"[exit code {proc.returncode}]")

    out = "\n".join(p for p in parts if p).strip() or "[no output — did you print()?]"
    if len(out) > max_output:
        head = out[: max_output // 2]
        tail = out[-max_output // 2:]
        out = f"{head}\n\n[... {len(out) - max_output} chars trimmed ...]\n\n{tail}"
    return out
