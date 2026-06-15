"""
Admin blueprint routes -- A1 scaffold + A6 card/note browser + A7 scheduling.

Routes implemented
------------------
GET  /admin/login       -- Display the login form.
POST /admin/login       -- Validate token; set ``token`` cookie; redirect to /admin.
GET  /admin             -- Dashboard landing page.
GET  /admin/logout      -- Clear the ``token`` cookie; redirect to /admin/login.
POST /admin/api/invoke  -- Token-gated AnkiConnect action proxy (A6).
GET  /admin/browse      -- Card/note browser + triage UI (A6).
GET  /admin/scheduling  -- Deck options + FSRS scheduling panel (A7).

Auth guard
----------
``check_admin_auth()`` is wired as a ``before_request`` hook on the blueprint.
The login route (and its POST) is explicitly exempted.

/admin/api/invoke design
------------------------
The browser never hits the raw unauthenticated ``POST /`` AnkiConnect endpoint.
Instead, every admin page uses ``acsInvoke(action, params)`` in admin.js which
calls ``POST /admin/api/invoke``.  This route is gated by the same
``before_request`` auth hook as all other admin routes.  It imports the merged
``DISPATCH`` table from ``src.server`` and dispatches the action under the
collection lock -- exactly what ``POST /`` does, but token-gated.

Single-writer safety
--------------------
The dashboard reads ``CollectionManager.health()`` which uses a non-blocking
lock acquire -- safe under contention.

The invoke proxy acquires ``_col_lock`` (same lock as the AnkiConnect handler)
before calling each action handler.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Optional

from flask import (
    Blueprint,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.wrappers import Response

from src.admin.auth import (
    ADMIN_TOKEN_CONFIGURED,
    check_admin_auth,
    token_valid,
    _ratelimit_check,
    _ratelimit_record_failure,
    _ratelimit_reset,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint definition
# ---------------------------------------------------------------------------
# template_folder and static_folder are relative to THIS file's directory.
# Flask resolves them to:
#   src/admin/../../templates  ->  <repo>/templates
#   src/admin/../../static     ->  <repo>/static
#
# We use app-level template/static dirs (set in server.py) rather than
# per-blueprint dirs so the layout stays at the repo root and Jinja2's
# template inheritance ({% extends "admin/base.html" %}) works without any
# blueprint URL prefix tricks.
admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin",
)

# ---------------------------------------------------------------------------
# Auth guard -- before every admin request
# ---------------------------------------------------------------------------

# Endpoints that do NOT require authentication.
# Both the GET (login form) and the POST (login submission) are exempt so
# that the user can reach and use the login form before they have a token.
_EXEMPT_ENDPOINTS: frozenset[str] = frozenset({"admin.login", "admin.login_post"})


@admin_bp.before_request
def _require_auth() -> Optional[Response]:
    """Gate every admin route behind the token check.

    The login endpoint is exempt; everything else returns 503 (admin disabled),
    302 to /admin/login (browser GET without a valid token), or 401 (API
    without a valid token).
    """
    if request.endpoint in _EXEMPT_ENDPOINTS:
        return None
    return check_admin_auth()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@admin_bp.get("/login")
def login() -> Any:
    """Render the token login form."""
    return render_template("admin/login.html")


@admin_bp.post("/login")
def login_post() -> Any:
    """Accept a token from the login form.

    On valid token: set an HttpOnly ``token`` cookie and redirect to /admin.
    On invalid token: re-render the login form with an error message.

    Cookie flags:
    - HttpOnly  -- prevents JavaScript from reading it (XSS mitigation).
    - SameSite=Strict -- prevents CSRF (cookie not sent on cross-site requests).
    - Secure    -- cookie only sent over HTTPS.  The app runs behind pfSense TLS
                  termination; ProxyFix (wired in server.py) ensures Flask sees
                  https:// via X-Forwarded-Proto so the flag is honoured.
    """
    if not ADMIN_TOKEN_CONFIGURED:
        return render_template(
            "admin/login.html",
            error="Admin UI is disabled: ADMIN_TOKEN is not set.",
        ), 503

    ip = request.remote_addr or "unknown"

    # Rate-limit: reject if the IP has exceeded the failed-attempt threshold.
    retry_after = _ratelimit_check(ip)
    if retry_after is not None:
        log.warning(
            "Admin login: rate limit exceeded for %s (retry after %ds)",
            ip,
            retry_after,
        )
        response = make_response(
            render_template(
                "admin/login.html",
                error="Too many failed login attempts. Please wait before retrying.",
            ),
            429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    submitted = request.form.get("token", "").strip()
    if not submitted or not token_valid(submitted):
        _ratelimit_record_failure(ip)
        log.warning(
            "Admin login: invalid token attempt from %s",
            ip,
        )
        return render_template(
            "admin/login.html",
            error="Invalid token. Please try again.",
        ), 401

    # Successful login -- reset the failure counter for this IP.
    _ratelimit_reset(ip)
    log.info("Admin login: successful from %s", ip)
    response = make_response(redirect(url_for("admin.index")))
    response.set_cookie(
        "token",
        submitted,
        httponly=True,
        samesite="Strict",
        secure=True,
        # max_age omitted -> session cookie (cleared when browser closes)
    )
    return response


@admin_bp.get("/")
def index() -> Any:
    """Admin dashboard landing page.

    Reads collection health via CollectionManager.health() (non-blocking,
    read-only -- safe to call without holding _col_lock).  Falls back
    gracefully when the collection is not open.
    """
    # Import lazily to avoid a circular import at module level.
    # collection.py is already imported by server.py before the blueprint runs.
    import src.collection as col_mod  # noqa: PLC0415

    health: dict[str, Any] = {}
    health_error: Optional[str] = None
    try:
        health = col_mod.manager.health()
    except RuntimeError as exc:
        health_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        health_error = f"Health check error: {exc}"

    return render_template(
        "admin/index.html",
        health=health,
        health_error=health_error,
    )


@admin_bp.get("/logout")
def logout() -> Any:
    """Clear the session cookie and redirect to the login page."""
    response = make_response(redirect(url_for("admin.login")))
    response.delete_cookie("token")
    log.info("Admin logout from %s", request.remote_addr)
    return response


# ---------------------------------------------------------------------------
# A6: Token-gated AnkiConnect action proxy
# ---------------------------------------------------------------------------


@admin_bp.post("/api/invoke")
def api_invoke() -> Any:
    """Token-gated AnkiConnect action proxy.

    Accepts JSON body: ``{"action": str, "params": dict}``
    Returns the standard AnkiConnect envelope: ``{"result": ..., "error": ...}``

    Auth: enforced by the blueprint's before_request hook (_require_auth).
    The browser must present a valid token (cookie, header, or Basic auth)
    identical to all other /admin/* routes.  The raw POST / endpoint is
    intentionally NOT called from browser admin pages.

    Dispatch: uses the same merged DISPATCH table from src.server so ALL
    registered actions (ACTIONS + GUI_ACTIONS + SYNC_ACTIONS + FSRS_ACTIONS)
    are available under the same access-control umbrella.

    Collection lock: acquired identically to the raw POST / handler -- every
    dispatch call is serialised through _col_lock for single-writer safety.
    """
    # Lazy import to avoid circular dependency at module load time.
    # src.server imports src.admin (this module), so we cannot import
    # src.server at the top of this file.
    import src.collection as col_mod  # noqa: PLC0415
    from src.server import DISPATCH  # noqa: PLC0415

    body: dict[str, Any] = request.get_json(silent=True) or {}
    action: str = body.get("action", "")
    params: dict[str, Any] = body.get("params") or {}

    if not action:
        return jsonify({"result": None, "error": "missing 'action' field"}), 400

    handler = DISPATCH.get(action)
    if handler is None:
        log.warning("admin/api/invoke: unsupported action %r", action)
        return jsonify({"result": None, "error": f"unsupported action: {action}"})

    try:
        with col_mod._col_lock:
            result = handler(params)
        return jsonify({"result": result, "error": None})
    except Exception as exc:  # noqa: BLE001
        log.error(
            "admin/api/invoke: action=%r raised: %s\n%s",
            action,
            exc,
            traceback.format_exc(),
        )
        return jsonify({"result": None, "error": str(exc)})


# ---------------------------------------------------------------------------
# A6: Card/Note browser page
# ---------------------------------------------------------------------------


@admin_bp.get("/browse")
def browse() -> Any:
    """Card and note browser + triage UI.

    Renders the browse page with an initial list of available deck names
    pre-loaded so the change-deck dropdown is populated without an extra
    round-trip on page load.
    """
    import src.collection as col_mod  # noqa: PLC0415

    deck_names: list[str] = []
    model_field_names: list[str] = []
    all_tags: list[str] = []
    try:
        col = col_mod.get_col()
        deck_names = sorted(nid.name for nid in col.decks.all_names_and_ids())
        all_tags = sorted(col.tags.all())
        model_field_names = sorted(
            {f["name"] for nt in col.models.all() for f in nt.get("flds", [])}
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("browse: failed to preload collection data: %s", exc)

    return render_template(
        "admin/browse.html",
        deck_names=deck_names,
        all_tags=all_tags,
        model_field_names=model_field_names,
    )


# ---------------------------------------------------------------------------
# A7: Scheduling page
# ---------------------------------------------------------------------------


@admin_bp.get("/scheduling")
def scheduling() -> Any:
    """Deck options + FSRS scheduling panel.

    Renders a static shell page.  All data (preset list, FSRS status, params)
    is loaded client-side via acsInvoke so the page does not need to open the
    collection at render time.  This keeps the route lightweight and consistent
    with the intent that all admin action calls are token-gated through the
    /admin/api/invoke proxy.
    """
    return render_template("admin/scheduling.html")
