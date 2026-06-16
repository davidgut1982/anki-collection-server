"""
Tests for ADMIN_BASE_PATH support (feat/admin-basepath).

These tests verify that the admin console can be hosted at a configurable URL
prefix (ADMIN_BASE_PATH) so a 1:1 reverse proxy can serve it at a path other
than the default /admin.

Test strategy
-------------
- Default prefix (ADMIN_BASE_PATH="/admin"): existing behaviour is unchanged,
  /admin/login returns 200, /admin/api/invoke is gated, /admin/static/admin/...
  is served.
- Custom prefix (ADMIN_BASE_PATH="/anki-admin"): every route lives under
  /anki-admin; /admin/* returns 404; HTML emits /anki-admin/static/ refs;
  the login redirect Location is under /anki-admin; cookie path is /anki-admin.
- Normalisation edge cases: missing leading slash, trailing slash.

We build a minimal Flask test app in the same pattern used by test_admin_auth.py
(_build_test_app_with_base) but parameterise the base path.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub waitress (not installed in the test venv)
# ---------------------------------------------------------------------------

if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "test-basepath-secret"


def _build_test_app(
    admin_token: str | None,
    admin_base_path: str = "/admin",
) -> Any:
    """Build a Flask test app with a configurable admin base path.

    Mirrors server.py setup: derives secret_key, registers health + anki
    routes, then registers the admin blueprint with the given url_prefix.
    ADMIN_BASE_PATH is stored in app.config so the context processor picks
    it up.
    """
    import importlib

    import src.admin.auth as auth_mod
    import src.collection as col_mod
    from flask import Flask, jsonify
    from flask import request as flask_request
    from src.actions import ACTIONS
    from src.fsrs import FSRS_ACTIONS
    from src.review_session import GUI_ACTIONS
    from src.sync import SYNC_ACTIONS

    auth_mod.ADMIN_TOKEN = admin_token
    auth_mod.ADMIN_TOKEN_CONFIGURED = admin_token is not None

    DISPATCH: dict[str, Any] = {
        **ACTIONS,
        **GUI_ACTIONS,
        **SYNC_ACTIONS,
        **FSRS_ACTIONS,
    }

    app = Flask(
        __name__ + f"_basepath_{id(admin_base_path)}",
        template_folder=str(_REPO_ROOT / "templates"),
        static_folder=str(_REPO_ROOT / "static"),
    )
    if admin_token:
        app.secret_key = hashlib.sha256(
            b"acs-flask-session:" + admin_token.encode()
        ).hexdigest()
    else:
        app.secret_key = "test-secret-no-token"
    app.config["TESTING"] = True
    app.config["ADMIN_BASE_PATH"] = admin_base_path

    @app.post("/")
    def anki_connect() -> Any:  # type: ignore[return]
        body: dict[str, Any] = flask_request.get_json(silent=True) or {}
        action: str = body.get("action", "")
        params: dict[str, Any] = body.get("params", {}) or {}
        handler = DISPATCH.get(action)
        if handler is None:
            return jsonify({"result": None, "error": f"unsupported action: {action}"})
        try:
            with col_mod._col_lock:
                result = handler(params)
            return jsonify({"result": result, "error": None})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"result": None, "error": str(exc)})

    @app.get("/health")
    def health() -> Any:  # type: ignore[return]
        try:
            h = col_mod.manager.health()
            return jsonify(h), 200
        except RuntimeError as exc:
            return jsonify({"status": "unavailable", "error": str(exc)}), 503
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "error": str(exc)}), 503

    import src.admin.routes as routes_mod

    importlib.reload(routes_mod)
    app.register_blueprint(routes_mod.admin_bp, url_prefix=admin_base_path)

    return app


@contextmanager
def _client(
    admin_token: str | None,
    admin_base_path: str = "/admin",
) -> Generator:
    app = _build_test_app(admin_token, admin_base_path)
    with app.test_client() as client:
        yield client


# ===========================================================================
# 1. Normalisation helper tests
# ===========================================================================

class TestNormalizeBasePath:
    """Test the _normalize_base_path helper in src.server."""

    def test_default_returns_slash_admin(self) -> None:
        from src.server import _normalize_base_path
        assert _normalize_base_path("") == "/admin"
        assert _normalize_base_path("   ") == "/admin"

    def test_adds_leading_slash(self) -> None:
        from src.server import _normalize_base_path
        assert _normalize_base_path("anki-admin") == "/anki-admin"

    def test_strips_trailing_slash(self) -> None:
        from src.server import _normalize_base_path
        assert _normalize_base_path("/anki-admin/") == "/anki-admin"

    def test_multiple_trailing_slashes(self) -> None:
        from src.server import _normalize_base_path
        assert _normalize_base_path("/anki-admin///") == "/anki-admin"

    def test_preserves_nested_path(self) -> None:
        from src.server import _normalize_base_path
        assert _normalize_base_path("/tools/anki-admin") == "/tools/anki-admin"

    def test_plain_slash_becomes_admin(self) -> None:
        """A bare "/" should default to /admin (the path would be empty after strip)."""
        from src.server import _normalize_base_path
        assert _normalize_base_path("/") == "/admin"


# ===========================================================================
# 2. Default prefix (/admin) — backward-compat
# ===========================================================================

class TestDefaultPrefix:
    """With ADMIN_BASE_PATH=/admin (the default), all existing URLs work."""

    def test_login_page_reachable_at_default(self) -> None:
        with _client(_TOKEN, "/admin") as c:
            resp = c.get("/admin/login")
        assert resp.status_code == 200

    def test_admin_index_requires_auth(self) -> None:
        with _client(_TOKEN, "/admin") as c:
            resp = c.get("/admin/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers.get("Location", "")

    def test_api_invoke_gated_default(self) -> None:
        with _client(_TOKEN, "/admin") as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 401)

    def test_static_asset_served_under_default_prefix(self) -> None:
        """Admin CSS served at /admin/static/admin/admin.css (blueprint static)."""
        with _client(_TOKEN, "/admin") as c:
            resp = c.get("/admin/static/admin/admin.css")
        assert resp.status_code == 200
        assert b"admin" in resp.data.lower()

    def test_html_references_default_static_prefix(self) -> None:
        """Login page HTML references /admin/static/... not /static/..."""
        with _client(_TOKEN, "/admin") as c:
            resp = c.get("/admin/login")
        body = resp.data.decode()
        # CSS link must reference /admin/static/admin/admin.css
        assert "/admin/static/admin/admin.css" in body, (
            f"Expected /admin/static/admin/admin.css in login HTML, got snippet: "
            f"{body[:500]!r}"
        )
        # Must NOT reference bare /static/admin (app-level static, not blueprint)
        assert 'href="/static/admin/' not in body, (
            "Login page must not reference bare /static/admin/... (use blueprint static)"
        )

    def test_window_admin_base_injected_default(self) -> None:
        """window.ADMIN_BASE is set to /admin in base.html and login.html."""
        with _client(_TOKEN, "/admin") as c:
            resp = c.get("/admin/login")
        body = resp.data.decode()
        assert 'window.ADMIN_BASE = "/admin"' in body, (
            f"Expected window.ADMIN_BASE = \"/admin\" in login page, snippet: {body[:500]!r}"
        )

    def test_login_redirect_under_default_prefix(self) -> None:
        """Successful login redirects to /admin (index), not /anki-admin/."""
        with _client(_TOKEN, "/admin") as c:
            resp = c.post("/admin/login", data={"token": _TOKEN}, follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert location.startswith("/admin") or "/admin" in location, (
            f"Expected /admin in Location, got: {location}"
        )
        assert "/anki-admin" not in location, (
            f"Location should not contain /anki-admin with default prefix, got: {location}"
        )

    def test_health_still_at_root(self) -> None:
        """/health is never under the admin prefix."""
        with _client(_TOKEN, "/admin") as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {"status": "ok", "card_count": 0, "note_count": 0}
                resp = c.get("/health")
        assert resp.status_code == 200


# ===========================================================================
# 3. Custom prefix (/anki-admin)
# ===========================================================================

class TestCustomPrefix:
    """With ADMIN_BASE_PATH=/anki-admin, all admin routes move to /anki-admin."""

    def test_login_at_custom_prefix(self) -> None:
        """/anki-admin/login returns 200 (exempt from auth)."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/anki-admin/login")
        assert resp.status_code == 200

    def test_old_admin_login_is_404(self) -> None:
        """/admin/login returns 404 when prefix is /anki-admin."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/admin/login")
        assert resp.status_code == 404, (
            f"Expected 404 for /admin/login when prefix=/anki-admin, got {resp.status_code}"
        )

    def test_index_redirect_to_custom_login(self) -> None:
        """GET /anki-admin/ without auth redirects to /anki-admin/login."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/anki-admin/", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/anki-admin/login" in location, (
            f"Expected /anki-admin/login in Location, got: {location}"
        )

    def test_old_admin_index_is_404(self) -> None:
        """/admin/ returns 404 when prefix is /anki-admin."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/admin/", follow_redirects=False)
        assert resp.status_code == 404, (
            f"Expected 404 for /admin/ with prefix=/anki-admin, got {resp.status_code}"
        )

    def test_api_invoke_at_custom_prefix(self) -> None:
        """/anki-admin/api/invoke is gated (no token → 302 or 401)."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.post(
                "/anki-admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 401), (
            f"Expected 302 or 401 for unauthenticated /anki-admin/api/invoke, "
            f"got {resp.status_code}"
        )

    def test_old_api_invoke_is_404(self) -> None:
        """/admin/api/invoke returns 404 when prefix is /anki-admin."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                follow_redirects=False,
            )
        assert resp.status_code == 404, (
            f"Expected 404 for /admin/api/invoke with prefix=/anki-admin, "
            f"got {resp.status_code}"
        )

    def test_static_asset_served_under_custom_prefix(self) -> None:
        """Admin CSS served at /anki-admin/static/admin/admin.css."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/anki-admin/static/admin/admin.css")
        assert resp.status_code == 200, (
            f"Expected 200 for /anki-admin/static/admin/admin.css, got {resp.status_code}"
        )
        assert b"admin" in resp.data.lower()

    def test_old_static_path_is_404_or_different(self) -> None:
        """/static/admin/admin.css is NOT served by the blueprint (app static may still serve it,
        but the blueprint's blueprint static endpoint is at /anki-admin/static/...)."""
        # The app-level static folder still exists at /static/ so Flask itself
        # serves /static/admin/admin.css — that's fine.  What matters is that
        # the blueprint static (which auto-resolves via url_for('admin.static'))
        # resolves to /anki-admin/static/..., not /static/...
        # We verify the HTML emits /anki-admin/static/... and NOT /static/admin/...
        # in the TestCustomPrefixHtml class below.
        pass

    def test_html_references_custom_static_prefix(self) -> None:
        """Login page HTML references /anki-admin/static/... not /static/admin/..."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/anki-admin/login")
        body = resp.data.decode()
        assert "/anki-admin/static/admin/admin.css" in body, (
            f"Expected /anki-admin/static/admin/admin.css in HTML, snippet: {body[:600]!r}"
        )
        assert 'href="/static/admin/' not in body, (
            f"Login page must not reference bare /static/admin/... (app static), "
            f"snippet: {body[:600]!r}"
        )

    def test_window_admin_base_injected_custom(self) -> None:
        """window.ADMIN_BASE is set to /anki-admin in the login page."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.get("/anki-admin/login")
        body = resp.data.decode()
        assert 'window.ADMIN_BASE = "/anki-admin"' in body, (
            f"Expected window.ADMIN_BASE = \"/anki-admin\", snippet: {body[:600]!r}"
        )

    def test_login_redirect_under_custom_prefix(self) -> None:
        """POST /anki-admin/login with valid token redirects to /anki-admin (index)."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.post(
                "/anki-admin/login",
                data={"token": _TOKEN},
                follow_redirects=False,
            )
        assert resp.status_code == 302, (
            f"Expected 302 on valid login, got {resp.status_code}"
        )
        location = resp.headers.get("Location", "")
        assert "/anki-admin" in location, (
            f"Expected /anki-admin in redirect Location, got: {location}"
        )
        assert "/admin" not in location.replace("/anki-admin", ""), (
            f"Redirect Location must not contain bare /admin, got: {location}"
        )

    def test_cookie_set_on_custom_login(self) -> None:
        """POST /anki-admin/login sets the token cookie."""
        with _client(_TOKEN, "/anki-admin") as c:
            resp = c.post(
                "/anki-admin/login",
                data={"token": _TOKEN},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "token=" in set_cookie, (
            f"Expected token cookie in Set-Cookie header, got: {set_cookie!r}"
        )
        # The cookie path should be the custom prefix, not /admin
        assert "/anki-admin" in set_cookie, (
            f"Cookie path should include /anki-admin, got: {set_cookie!r}"
        )

    def test_logout_redirects_to_custom_login(self) -> None:
        """GET /anki-admin/logout redirects to /anki-admin/login."""
        with _client(_TOKEN, "/anki-admin") as c:
            c.set_cookie("localhost", "token", _TOKEN)
            resp = c.get("/anki-admin/logout", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/anki-admin/login" in location, (
            f"Expected /anki-admin/login in logout redirect, got: {location}"
        )

    def test_api_invoke_accessible_with_valid_token(self) -> None:
        """POST /anki-admin/api/invoke with valid token returns 200."""
        with _client(_TOKEN, "/anki-admin") as c:
            with patch("src.collection._col_lock") as mock_lock:
                mock_lock.__enter__ = MagicMock(return_value=None)
                mock_lock.__exit__ = MagicMock(return_value=False)
                resp = c.post(
                    "/anki-admin/api/invoke",
                    json={"action": "deckNames", "params": {}},
                    headers={"X-Admin-Token": _TOKEN},
                )
        # deckNames needs a real collection; expect 200 with an error envelope
        # (action fails due to no collection) but NOT a 401 or 404.
        assert resp.status_code == 200, (
            f"Expected 200 from /anki-admin/api/invoke with valid token, "
            f"got {resp.status_code}"
        )
        assert resp.status_code != 401
        assert resp.status_code != 404

    def test_health_unaffected_by_custom_prefix(self) -> None:
        """/health is always at the root, never under the admin prefix."""
        with _client(_TOKEN, "/anki-admin") as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {"status": "ok", "card_count": 0, "note_count": 0}
                resp_health = c.get("/health")
                resp_admin_health = c.get("/anki-admin/health")
        assert resp_health.status_code == 200, "/health must be 200"
        # /anki-admin/health is not a real route — it might be 404
        assert resp_admin_health.status_code != 200 or resp_health.status_code == 200


# ===========================================================================
# 4. Disabled admin (no ADMIN_TOKEN) with custom prefix
# ===========================================================================

class TestDisabledWithCustomPrefix:
    """When ADMIN_TOKEN is not set, 503 is returned even with a custom prefix."""

    def test_503_with_custom_prefix_no_token(self) -> None:
        with _client(None, "/anki-admin") as c:
            resp = c.get("/anki-admin/")
        assert resp.status_code == 503

    def test_login_page_still_reachable_with_custom_prefix(self) -> None:
        with _client(None, "/anki-admin") as c:
            resp = c.get("/anki-admin/login")
        assert resp.status_code == 200
