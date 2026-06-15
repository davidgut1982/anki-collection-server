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

Thread count (threads=2):
  The SQLite-backed Anki collection is a single-writer database.  All
  collection access (every dispatched action) is serialised through
  ``_col_lock`` (RLock) so only one operation runs at a time.

  We run waitress with ``threads=2`` — NOT 1 — specifically so that
  ``GET /health`` can be served on the second thread while a long-running
  sync or write holds ``_col_lock`` on the first thread.  Without this,
  the single worker thread is blocked by the sync and Docker's 5-second
  HEALTHCHECK times out, marking the container unhealthy mid-sync.

  ``health()`` uses a non-blocking lock acquire and returns
  ``{"status": "ok", "syncing": true, ...}`` immediately when the lock is
  held — it never blocks and never touches the collection under contention.

  Single-writer safety guarantee: only the dispatch path (``POST /``)
  acquires ``_col_lock`` for collection work.  The health path uses
  ``acquire(blocking=False)`` and performs NO collection writes.

  Do NOT run behind a multi-worker WSGI server (gunicorn -w N with N>1,
  etc.) — one process, two threads, one collection handle.

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

import hashlib
import logging
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

import src.collection as col_mod
from src.actions import ACTIONS
from src.admin import admin_bp
from src.fsrs import FSRS_ACTIONS
from src.review_session import GUI_ACTIONS
from src.sync import SYNC_ACTIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve repo-root template and static directories.
# ---------------------------------------------------------------------------
# server.py lives at <repo>/src/server.py, so two levels up is <repo>/.
# We set these explicitly rather than relying on Flask's default (which
# resolves relative to the *package* directory, i.e. <repo>/src/) so that
# the shared templates/ and static/ directories at the repo root are found
# correctly whether the app is run via ``python -m src.server`` or from an
# installed wheel.
_REPO_ROOT = Path(__file__).parent.parent
_TEMPLATE_FOLDER = str(_REPO_ROOT / "templates")
_STATIC_FOLDER = str(_REPO_ROOT / "static")

app = Flask(
    __name__,
    template_folder=_TEMPLATE_FOLDER,
    static_folder=_STATIC_FOLDER,
)

# Secret key is required for Flask's session/cookie machinery (used by the
# admin login flow to set signed cookies).  In production the value is
# irrelevant for our auth scheme because we use a plain HttpOnly cookie
# (not Flask's signed session cookie), but Flask requires it to be set.
#
# INVARIANT: secret_key is derived, not the raw ADMIN_TOKEN.
# Using ADMIN_TOKEN directly as the secret key would expose it via Flask's
# session-signing machinery.  We use a separate FLASK_SECRET_KEY env var if
# set, otherwise derive a stable key from ADMIN_TOKEN via SHA-256 with a
# domain-separation prefix so the two values are always distinct.
_admin_token = os.environ.get("ADMIN_TOKEN", "")
_flask_secret_key = os.environ.get("FLASK_SECRET_KEY", "")
if _flask_secret_key:
    app.secret_key = _flask_secret_key
elif _admin_token:
    app.secret_key = hashlib.sha256(
        b"acs-flask-session:" + _admin_token.encode()
    ).hexdigest()
else:
    app.secret_key = os.urandom(32)

# Trust the X-Forwarded-Proto and X-Forwarded-Host headers set by the reverse
# proxy (pfSense / nginx TLS termination) so that Flask sees https:// and sets
# the Secure flag correctly on cookies.  x_proto=1 and x_host=1 mean we trust
# exactly one hop of proxying.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[method-assign]

# ---------------------------------------------------------------------------
# Register the admin blueprint
# ---------------------------------------------------------------------------
# The blueprint gates itself at /admin and /admin/* — it does NOT touch
# POST / (AnkiConnect) or GET /health.
app.register_blueprint(admin_bp)

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

    # Wrap every dispatch in the collection lock (single-writer guarantee;
    # with waitress threads=2 the lock is what serialises all collection
    # operations — health() uses acquire(blocking=False) so it can answer
    # immediately on the second thread while this lock is held).
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
    log.info("Starting anki-collection-server on 0.0.0.0:8765 (waitress threads=2)")
    # threads=2: one thread for collection operations (POST /), one for health
    # checks (GET /health).  All collection access is serialised by _col_lock;
    # health() uses acquire(blocking=False) so it never blocks on a sync.
    # Never add more than 2 threads or multiple workers — one collection handle.
    serve(app, host="0.0.0.0", port=8765, threads=2)

    # 4. waitress.serve() returns after the SIGTERM handler calls sys.exit(0):
    #    _shutdown() → col_mod.manager.close() → sys.exit(0)
    #    → waitress catches SystemExit → serve() returns.
    #    The close() below is an idempotent no-op safety net for any other
    #    exit path (e.g. SIGINT handled differently by the OS).
    log.info("waitress exited — closing collection (idempotent safety net).")
    col_mod.manager.close()
