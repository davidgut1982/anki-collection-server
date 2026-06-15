"""
Admin blueprint routes — scaffold step (A1).

Routes implemented in this step
--------------------------------
GET  /admin/login    — Display the login form.
POST /admin/login    — Validate token; set ``token`` cookie; redirect to /admin.
GET  /admin          — Dashboard landing page (collection health summary +
                       nav placeholders for future panels).
GET  /admin/logout   — Clear the ``token`` cookie; redirect to /admin/login.

Auth guard
----------
``check_admin_auth()`` is wired as a ``before_request`` hook on the blueprint.
The login route (and its POST) is explicitly exempted so unauthenticated users
can reach the form.

Single-writer safety
--------------------
The dashboard reads ``CollectionManager.health()`` which uses a non-blocking
lock acquire — it returns immediately whether or not a sync is in progress and
never triggers any collection write.  This is the only collection interaction
in this step.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.wrappers import Response

from src.admin.auth import ADMIN_TOKEN_CONFIGURED, check_admin_auth, _token_valid

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint definition
# ---------------------------------------------------------------------------
# template_folder and static_folder are relative to THIS file's directory.
# Flask resolves them to:
#   src/admin/../../templates  →  <repo>/templates
#   src/admin/../../static     →  <repo>/static
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
# Auth guard — before every admin request
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

    The cookie is HttpOnly to prevent JavaScript from reading it (XSS
    mitigation).  SameSite=Strict prevents CSRF.  The Secure flag is omitted
    here because the sidecar runs behind an internal Docker network (no TLS
    termination at this layer) — the deployer should add Secure if they expose
    the admin port externally over HTTPS.
    """
    if not ADMIN_TOKEN_CONFIGURED:
        return render_template(
            "admin/login.html",
            error="Admin UI is disabled: ADMIN_TOKEN is not set.",
        )

    submitted = request.form.get("token", "").strip()
    if not submitted or not _token_valid(submitted):
        log.warning(
            "Admin login: invalid token attempt from %s",
            request.remote_addr,
        )
        return render_template(
            "admin/login.html",
            error="Invalid token. Please try again.",
        ), 401

    log.info("Admin login: successful from %s", request.remote_addr)
    response = make_response(redirect(url_for("admin.index")))
    response.set_cookie(
        "token",
        submitted,
        httponly=True,
        samesite="Strict",
        # max_age omitted → session cookie (cleared when browser closes)
    )
    return response


@admin_bp.get("/")
def index() -> Any:
    """Admin dashboard landing page.

    Reads collection health via CollectionManager.health() (non-blocking,
    read-only — safe to call without holding _col_lock).  Falls back
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
