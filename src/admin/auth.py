"""
Token authentication helpers for the admin blueprint.

Auth scheme
-----------
All /admin/* routes call ``require_admin_token()`` before handling the request.

Token resolution order (first match wins):
  1. ``X-Admin-Token`` HTTP header   — preferred for API/curl usage.
  2. HTTP Basic Auth password field  — any username; password == token.
     Allows ``curl -u :TOKEN http://host/admin/...`` and browser basic-auth.
  3. ``token`` cookie                — set by POST /admin/login so the browser
     can navigate /admin/* without re-sending credentials each time.

``ADMIN_TOKEN`` is read ONCE at import time from ``os.environ``.  If it is not
set, ``ADMIN_TOKEN_CONFIGURED`` is ``False`` and every /admin request returns
503 immediately — the admin UI is intentionally disabled rather than open.

Constant-time comparison
------------------------
``hmac.compare_digest`` is used for every token comparison to prevent
timing-side-channel attacks (an attacker probing byte-by-byte until they get a
faster or slower response).

Both sides are encoded to ``bytes`` (UTF-8) before the call, because
``hmac.compare_digest`` requires both arguments to be the same type and
``str`` inputs are only accepted when both are ``str`` — using ``bytes``
throughout avoids any type mismatch.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Optional

from flask import Request, make_response, redirect, request, url_for
from flask.wrappers import Response

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level: read ADMIN_TOKEN once at startup
# ---------------------------------------------------------------------------

ADMIN_TOKEN: Optional[str] = os.environ.get("ADMIN_TOKEN") or None
ADMIN_TOKEN_CONFIGURED: bool = ADMIN_TOKEN is not None

if ADMIN_TOKEN_CONFIGURED:
    log.info("Admin UI: ADMIN_TOKEN is set — /admin routes are enabled.")
else:
    log.warning(
        "Admin UI: ADMIN_TOKEN is not set — /admin routes will return 503. "
        "Set ADMIN_TOKEN in the container environment to enable the admin UI."
    )


# ---------------------------------------------------------------------------
# Login rate-limiter (in-memory, IP-keyed, single-process safe)
# ---------------------------------------------------------------------------
# This server runs under waitress with a fixed thread count and a single
# process — module-level state is shared safely across threads via _ratelimit_lock.
#
# Policy: max 10 failed attempts per IP in any rolling 5-minute window.
# On success the counter for that IP is reset.  A 429 response with a
# Retry-After header is returned when the limit is exceeded.

_RATELIMIT_MAX_FAILURES: int = 10
_RATELIMIT_WINDOW_SECONDS: int = 300  # 5 minutes

# Map: ip_address → list of failure timestamps (float, epoch seconds)
_ratelimit_failures: dict[str, list[float]] = defaultdict(list)
_ratelimit_lock: threading.Lock = threading.Lock()


def _ratelimit_check(ip: str) -> Optional[int]:
    """Return None if the IP is within the allowed limit, or seconds-to-wait.

    Prunes timestamps older than the window before counting.
    Thread-safe via _ratelimit_lock.
    """
    now = time.monotonic()
    window_start = now - _RATELIMIT_WINDOW_SECONDS
    with _ratelimit_lock:
        # Prune stale entries
        _ratelimit_failures[ip] = [
            ts for ts in _ratelimit_failures[ip] if ts >= window_start
        ]
        if len(_ratelimit_failures[ip]) >= _RATELIMIT_MAX_FAILURES:
            oldest = _ratelimit_failures[ip][0]
            retry_after = int(oldest + _RATELIMIT_WINDOW_SECONDS - now) + 1
            return max(retry_after, 1)
    return None


def _ratelimit_record_failure(ip: str) -> None:
    """Record a failed login attempt for the given IP."""
    with _ratelimit_lock:
        _ratelimit_failures[ip].append(time.monotonic())


def _ratelimit_reset(ip: str) -> None:
    """Reset the failure counter for the given IP (call on successful login)."""
    with _ratelimit_lock:
        _ratelimit_failures.pop(ip, None)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def _extract_token(req: Request) -> Optional[str]:
    """Extract the presented token from the request using the priority order.

    1. ``X-Admin-Token`` header.
    2. HTTP Basic Auth password (any username).
    3. ``token`` session cookie.

    Returns the raw token string, or ``None`` if none of the three sources
    provide a non-empty value.
    """
    # 1. X-Admin-Token header
    header_token = req.headers.get("X-Admin-Token", "").strip()
    if header_token:
        return header_token

    # 2. HTTP Basic Auth — decode Authorization: Basic <base64(user:pass)>
    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode(
                "utf-8", errors="replace"
            )
            # Split on the FIRST colon only — usernames may not contain colons
            # but passwords may.  We accept any username; only the password is
            # compared against ADMIN_TOKEN.
            _username, sep, password = decoded.partition(":")
            if sep and password:
                return password
        except Exception:  # noqa: BLE001
            pass  # Malformed base64 — fall through to next source

    # 3. Cookie
    cookie_token = req.cookies.get("token", "").strip()
    if cookie_token:
        return cookie_token

    return None


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------


def token_valid(presented: str) -> bool:
    """Return True iff ``presented`` matches ``ADMIN_TOKEN`` in constant time.

    Both strings are encoded to UTF-8 bytes before ``hmac.compare_digest`` is
    called — the function requires both arguments to be the same type.

    This is a public function so that all auth paths (header, cookie, form
    submission) go through a single, auditable comparison point.  Future auth
    additions (e.g. TOTP second factor) should extend this function rather than
    adding a second comparison elsewhere.
    """
    if not ADMIN_TOKEN_CONFIGURED or not ADMIN_TOKEN:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8"),
        ADMIN_TOKEN.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Public guard function
# ---------------------------------------------------------------------------


def check_admin_auth() -> Optional[Response]:
    """Validate the request carries a valid admin token.

    Returns
    -------
    None
        Authentication passed; the caller should proceed normally.
    Response
        Authentication failed (or admin is disabled); return this response
        directly from the view.  The response is one of:
        - HTTP 503  — ADMIN_TOKEN not configured.
        - HTTP 302  — redirect to /admin/login (for browser GET requests).
        - HTTP 401  — invalid / missing token (for API / non-GET requests).

    Usage inside a before_request hook
    -----------------------------------
    ::

        @admin_bp.before_request
        def _require_auth() -> Optional[Response]:
            # Login page and its POST are exempt from auth.
            if request.endpoint in ("admin.login",):
                return None
            return check_admin_auth()
    """
    # The login route must be exempt — it IS the way to get a token.
    # Callers are responsible for exempting non-auth endpoints (e.g. the login
    # GET/POST) via _EXEMPT_ENDPOINTS in routes.py before calling this function.

    # 503 — admin disabled
    if not ADMIN_TOKEN_CONFIGURED:
        response = make_response(
            "Admin UI disabled: set the ADMIN_TOKEN environment variable to "
            "enable the /admin console.\n",
            503,
        )
        response.content_type = "text/plain; charset=utf-8"
        return response

    presented = _extract_token(request)
    if presented is None or not token_valid(presented):
        if presented is not None:
            log.warning(
                "Admin auth: invalid token presented from %s",
                request.remote_addr,
            )
        # Browser GET → redirect to login; API / non-GET → 401
        if request.method == "GET":
            return redirect(url_for("admin.login"))  # type: ignore[return-value]
        response = make_response(
            "Unauthorized: valid ADMIN_TOKEN required.\n",
            401,
        )
        response.content_type = "text/plain; charset=utf-8"
        return response

    return None
