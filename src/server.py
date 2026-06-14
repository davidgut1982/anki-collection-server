"""
AnkiConnect-compatible HTTP entrypoint for anki-collection-server.

This module implements the AnkiConnect JSON wire protocol on port 8765:
  - POST /  : accepts {"action": str, "params": dict, "version": int}
              and returns {"result": <value>, "error": <str|null>}
  - GET /health : liveness probe — returns CollectionManager.health() or 503

Startup sequence (before any requests are accepted):
  1. CollectionManager.open() is called with the path from the
     ANKI_COLLECTION_PATH environment variable
     (default: /config/.local/share/Anki2/User 1/collection.anki2).
  2. If open() fails for any reason (lock contention, corrupt file,
     FileNotFoundError) the error is logged and the process exits with code 1
     so that Docker's restart policy does not spin-loop silently.

Graceful shutdown:
  SIGTERM and SIGINT are intercepted to close the collection before the
  process exits.  This flushes the WAL, releases the SQLite handle and the
  fcntl advisory lock.

Single-worker constraint:
  The SQLite-backed Anki collection is a single-writer database. This server
  MUST run as a single OS process with exactly ONE thread so that only one
  Collection handle exists at a time. We use waitress (threads=1) instead of
  the Flask development server. Do NOT change threads to >1, do NOT run behind
  a multi-worker WSGI server (gunicorn -w N with N>1, etc.).

Dispatch table:
  DISPATCH is built by merging all action dicts:
    ACTIONS       — CRUD/media/stats (src/actions.py)
    GUI_ACTIONS   — gui* review session (src/review_session.py)
    SYNC_ACTIONS  — sync (src/sync.py)
    FSRS_ACTIONS  — enableFsrs / isFsrsEnabled (src/fsrs.py)

  Each handler has signature (params: dict) -> Any.  The server wraps every
  dispatch call in the collection threading lock as defense-in-depth
  (waitress threads=1 means this never actually contends, but keeps the door
  open to future reconfiguration without silent data races).

AnkiConnect wire protocol:
  - "version" field in the request envelope: accepted and ignored.
    AnkiConnect clients always send version=6; we are compatible.
  - "apiKey" field: accepted and ignored.  This server operates on an
    internal Docker network and requires no key.
  - Unknown action: {"result": null, "error": "unsupported action: <name>"}
    with HTTP 200 (matching AnkiConnect behaviour).
  - Handler exception: {"result": null, "error": "<str(exc)>"}
    with HTTP 200 (traceback logged at ERROR level).
  - Success: {"result": <value>, "error": null} with HTTP 200.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import traceback
from typing import Any

from flask import Flask, jsonify, request
from waitress import serve

import src.collection as col_mod
from src.actions import ACTIONS
from src.fsrs import FSRS_ACTIONS
from src.review_session import GUI_ACTIONS
from src.sync import SYNC_ACTIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Unified action dispatch table
# ---------------------------------------------------------------------------

# Build once at import time.  Merge order: ACTIONS is the base; the
# specialised dicts may shadow entries from the base (they do not currently
# overlap, but explicit order makes intent clear).
DISPATCH: dict[str, Any] = {
    **ACTIONS,
    **GUI_ACTIONS,
    **SYNC_ACTIONS,
    **FSRS_ACTIONS,
}

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
# Routes
# ---------------------------------------------------------------------------


@app.post("/")
def anki_connect() -> Any:
    """Handle an AnkiConnect request envelope.

    Parses {"action", "params", "version", "apiKey"} from the JSON body.
    "version" and "apiKey" are accepted and ignored.

    Dispatches to DISPATCH[action](params) while holding the collection lock.
    Returns the AnkiConnect result envelope as JSON with HTTP 200.
    """
    body: dict[str, Any] = request.get_json(silent=True) or {}
    action: str = body.get("action", "")
    params: dict[str, Any] = body.get("params", {}) or {}
    # "version" and "apiKey" are part of the AnkiConnect wire protocol.
    # We accept them to stay compatible with real AnkiConnect clients but
    # do not act on them.
    version: int = body.get("version", ANKI_CONNECT_VERSION)

    log.debug("action=%r version=%s params=%r", action, version, params)

    handler = DISPATCH.get(action)
    if handler is None:
        log.warning("unsupported action: %r", action)
        return jsonify(err(f"unsupported action: {action}"))

    # Wrap every dispatch in the collection lock (defense-in-depth; the lock
    # is never contended with waitress threads=1 but prevents silent races if
    # the thread count is ever raised).
    try:
        with col_mod._col_lock:
            result = handler(params)
        return jsonify(ok(result))
    except Exception as exc:  # noqa: BLE001
        log.error(
            "action=%r raised: %s\n%s",
            action,
            exc,
            traceback.format_exc(),
        )
        return jsonify(err(str(exc)))


@app.get("/health")
def health() -> Any:
    """Liveness probe — delegates to CollectionManager.health().

    Returns 200 with the health dict when the collection is open and the
    SQLite handle is alive.  Returns 503 when the collection is not open
    (e.g. startup failed or close() was called).
    """
    try:
        h = col_mod.manager.health()
        return jsonify(h), 200
    except RuntimeError as exc:
        # Collection not open (RuntimeError from CollectionManager.col property)
        log.warning("health check: collection not open — %s", exc)
        return jsonify({"status": "unavailable", "error": str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        # SQLite probe failed or other unexpected error
        log.error("health check failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 503


# ---------------------------------------------------------------------------
# Graceful shutdown helpers
# ---------------------------------------------------------------------------


def _shutdown(signum: int, frame: object) -> None:  # noqa: ARG001
    """Signal handler: close collection then exit.

    Called on SIGTERM (Docker stop) and SIGINT (Ctrl-C / keyboard interrupt).
    Closing the collection flushes the WAL, commits any pending writes, and
    releases the fcntl advisory lock so the next process can open cleanly.
    """
    sig_name = signal.Signals(signum).name
    log.info("Received %s — closing collection and shutting down.", sig_name)
    try:
        col_mod.manager.close()
        log.info("Collection closed cleanly.")
    except Exception as exc:  # noqa: BLE001
        log.error("Error during shutdown close: %s", exc)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _open_collection_or_exit() -> None:
    """Open the Anki collection; exit with code 1 on any failure.

    The collection path is taken from the ANKI_COLLECTION_PATH environment
    variable (default: /config/.local/share/Anki2/User 1/collection.anki2).

    Failing fast (sys.exit(1)) on startup errors prevents Docker from
    entering a silent restart-loop when the collection is locked or corrupt.
    """
    path: str = os.environ.get(
        "ANKI_COLLECTION_PATH",
        "/config/.local/share/Anki2/User 1/collection.anki2",
    )
    try:
        col_mod.manager.open(path)
    except FileNotFoundError as exc:
        log.critical("Collection file not found: %s — exiting.", exc)
        sys.exit(1)
    except RuntimeError as exc:
        # Covers: lock contention ("already locked by another process"),
        # "Collection is already open", etc.
        log.critical("Collection open failed: %s — exiting.", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.critical(
            "Unexpected error opening collection: %s — exiting.\n%s",
            exc,
            traceback.format_exc(),
        )
        sys.exit(1)

    log.info("Collection opened successfully.")


if __name__ == "__main__":
    # 1. Register signal handlers BEFORE opening the collection so that even a
    #    slow open() can be interrupted cleanly.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 2. Open the collection.  Exits with code 1 on failure (fail fast).
    _open_collection_or_exit()

    # 3. Start the server (blocks until killed).
    log.info(
        "Starting anki-collection-server on 0.0.0.0:8765 (single-threaded, waitress)"
    )
    # threads=1 is REQUIRED: the Anki Collection is not thread-safe and SQLite
    # is single-writer. One request must fully complete before the next begins.
    # Never increase threads; never add workers; never use a multi-worker server.
    serve(app, host="0.0.0.0", port=8765, threads=1)

    # 4. waitress.serve() returns when the server is stopped externally.
    #    Ensure the collection is closed on normal exit as well.
    log.info("waitress exited — closing collection.")
    col_mod.manager.close()
