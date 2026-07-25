"""HTTP surface. Its only jobs are serving run logs at a public URL and
answering Railway's health check.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .config import CONFIG

log = logging.getLogger(__name__)

STARTED = time.time()
STATE: dict = {"bot_username": None, "history": None, "runs": 0, "failures": 0}

app = FastAPI(title="TDS P1 Data Analyst Bot", docs_url=None, redoc_url=None)


@app.get("/")
def root():
    return {"service": "tds-p1-telegram-bot", "status": "up"}


@app.get("/health")
def health():
    stats = STATE["history"].stats() if STATE.get("history") else {}
    return {
        "ok": True,
        "uptime_seconds": round(time.time() - STARTED, 1),
        "bot": STATE.get("bot_username"),
        "public_base_url": CONFIG.public_base_url,
        "runs": STATE.get("runs", 0),
        "failures": STATE.get("failures", 0),
        **stats,
    }


@app.get("/logs/{name}")
def get_log(name: str):
    """Serve a run log. Must stay public and unauthenticated — the graders
    fetch these with plain wget."""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".jsonl"):
        return JSONResponse({"error": "bad name"}, status_code=400)
    path = Path(CONFIG.log_dir) / name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", path, exc)
        return Response(status_code=500)
    return PlainTextResponse(body, media_type="application/x-ndjson",
                             headers={"Cache-Control": "public, max-age=300"})


@app.get("/logs")
def list_logs():
    directory = Path(CONFIG.log_dir)
    if not directory.exists():
        return {"logs": []}
    names = sorted((p.name for p in directory.glob("*.jsonl")), reverse=True)[:100]
    return {"count": len(names),
            "logs": [f"{CONFIG.public_base_url}/logs/{n}" for n in names]}
