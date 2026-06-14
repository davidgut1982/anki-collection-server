"""
AnkiConnect-compatible HTTP entrypoint for anki-collection-server.

This module implements the AnkiConnect JSON wire protocol on port 8765:
  - POST /  : accepts {"action": str, "params": dict, "version": int}
              and returns {"result": <value>, "error": <str|null>}
  - GET /health : liveness probe

In this scaffold step (Step 2) only the `version` action is implemented.
All other actions return {"result": null, "error": "not implemented: <action>"}.

Dispatch to the full action table (actions.py) and stateful review sessions
(review_session.py) are wired in Steps 5–7.

Single-worker constraint:
  The SQLite-backed Anki collection is a single-writer database. This server
  MUST run as a single OS process with exactly ONE thread so that only one
  Collection handle exists at a time. We use waitress (threads=1) instead of
  the Flask development server. Do NOT change threads to >1, do NOT run behind
  a multi-worker WSGI server (gunicorn -w N with N>1, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request
from waitress import serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Wire-protocol helpers
# ---------------------------------------------------------------------------

ANKI_CONNECT_VERSION = 6


def ok(result: Any) -> dict[str, Any]:
    """Return a successful AnkiConnect envelope."""
    return {"result": result, "error": None}


def err(message: str) -> dict[str, Any]:
    """Return an error AnkiConnect envelope."""
    return {"result": None, "error": message}


# ---------------------------------------------------------------------------
# Stub action dispatch (Step 2 only — real dispatch added in Step 5)
# ---------------------------------------------------------------------------


def dispatch(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Route an action to its handler.

    Only `version` is implemented in this scaffold step.
    """
    if action == "version":
        return ok(ANKI_CONNECT_VERSION)

    return err(f"not implemented: {action}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/")
def anki_connect() -> Any:
    """Handle an AnkiConnect request envelope."""
    body = request.get_json(silent=True) or {}
    action: str = body.get("action", "")
    params: dict[str, Any] = body.get("params", {}) or {}
    version: int = body.get("version", ANKI_CONNECT_VERSION)

    log.debug("action=%r version=%s params=%r", action, version, params)

    result = dispatch(action, params)
    return jsonify(result)


@app.get("/health")
def health() -> Any:
    """Liveness probe — returns 200 while the process is alive."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting anki-collection-server on 0.0.0.0:8765 (single-threaded)")
    # threads=1 is REQUIRED: the Anki Collection is not thread-safe and SQLite
    # is single-writer. One request must fully complete before the next begins.
    # Never increase threads; never add workers; never use a multi-worker server.
    serve(app, host="0.0.0.0", port=8765, threads=1)
