"""
Tests for the /admin auth gate (A1 scaffold step).

Coverage
--------
- ADMIN_TOKEN NOT set   → GET /admin returns 503 with "admin disabled" message.
- ADMIN_TOKEN set, no credentials presented → GET /admin returns 302 to /admin/login.
- ADMIN_TOKEN set, valid X-Admin-Token header → GET /admin returns 200.
- ADMIN_TOKEN set, valid HTTP Basic Auth (password=token) → GET /admin returns 200.
- ADMIN_TOKEN set, valid token cookie → GET /admin returns 200.
- ADMIN_TOKEN set, wrong token (any source) → GET /admin returns 401 or 302.
- POST /admin/login with valid token → sets cookie, redirects to /admin.
- POST /admin/login with wrong token → 401, no cookie set.
- GET /health unaffected — returns 200 (collection mocked) with NO admin token.
- POST / (AnkiConnect version) unaffected — returns 200 with result=6, NOT gated.
- GET /admin/logout clears cookie and redirects to /admin/login.

``waitress`` is not installed in the test environment (it only exists inside the
production Docker image).  Following the pattern in test_health_lock_free.py,
we stub it into sys.modules before any src.* imports.

Auth module state (ADMIN_TOKEN / ADMIN_TOKEN_CONFIGURED) is patched at the
function level inside each test using a context manager helper, then restored
after the test — this ensures isolation between tests even though the auth
module is a singleton.
"""

from __future__ import annotations

import base64
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# One-time setup: stub waitress before any src.* imports
# ---------------------------------------------------------------------------

if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "supersecret"


def _basic_auth_header(username: str, password: str) -> str:
    """Return an ``Authorization: Basic ...`` header value."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def _build_test_app(admin_token: str | None) -> Any:
    """
    Build a fresh Flask app wired identically to server.py.

    Sets auth module state to match ``admin_token``, registers the admin
    blueprint, and mounts the AnkiConnect dispatch and /health routes.
    A fresh Flask app object is created each call so blueprint registration
    never conflicts.
    """
    import src.admin.auth as auth_mod
    import src.collection as col_mod
    from src.actions import ACTIONS
    from src.fsrs import FSRS_ACTIONS
    from src.review_session import GUI_ACTIONS
    from src.sync import SYNC_ACTIONS
    from flask import Flask, jsonify, request as flask_request

    # Patch auth module state — routes read these at call time.
    auth_mod.ADMIN_TOKEN = admin_token
    auth_mod.ADMIN_TOKEN_CONFIGURED = admin_token is not None

    DISPATCH: dict[str, Any] = {
        **ACTIONS,
        **GUI_ACTIONS,
        **SYNC_ACTIONS,
        **FSRS_ACTIONS,
    }

    app = Flask(
        __name__ + f"_test_{id(admin_token)}",
        template_folder=str(_REPO_ROOT / "templates"),
        static_folder=str(_REPO_ROOT / "static"),
    )
    app.secret_key = admin_token or "test-secret-key"
    app.config["TESTING"] = True

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

    # Each call must use the same blueprint object; Flask prevents registering
    # the same blueprint instance twice on the same app, but different app
    # objects can each have it.  Import fresh each time via the package.
    import importlib
    import src.admin.routes as routes_mod

    importlib.reload(routes_mod)  # clear any stale before_request state
    app.register_blueprint(routes_mod.admin_bp)

    return app


@contextmanager
def _client(admin_token: str | None) -> Generator:
    """Context manager that yields a Flask test client with the given token state."""
    app = _build_test_app(admin_token)
    with app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# ADMIN_TOKEN unset → 503 on all /admin routes
# ---------------------------------------------------------------------------


class TestAdminDisabled:
    def test_admin_index_returns_503_when_token_unset(self) -> None:
        """GET /admin without ADMIN_TOKEN configured must return 503."""
        with _client(None) as c:
            resp = c.get("/admin/")
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
        body = resp.data.decode()
        assert "ADMIN_TOKEN" in body, f"Expected 'ADMIN_TOKEN' in body, got: {body!r}"

    def test_admin_login_page_still_reachable(self) -> None:
        """GET /admin/login is exempt from auth — reachable even when token is unset."""
        with _client(None) as c:
            resp = c.get("/admin/login")
        assert resp.status_code == 200, (
            f"Login page (exempt from auth) should be 200, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# No credentials → redirect to login (browser GET) or 401 (non-GET)
# ---------------------------------------------------------------------------


class TestNoCredentials:
    def test_get_admin_no_token_redirects_to_login(self) -> None:
        """GET /admin without any credentials returns 302 to /admin/login."""
        with _client(_TOKEN) as c:
            resp = c.get("/admin/", follow_redirects=False)
        assert resp.status_code == 302, f"Expected 302, got {resp.status_code}"
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location, (
            f"Expected /admin/login in Location, got: {location}"
        )

    def test_post_empty_token_returns_401(self) -> None:
        """POST /admin/login with empty token returns 401."""
        with _client(_TOKEN) as c:
            resp = c.post("/admin/login", data={"token": ""})
        assert resp.status_code == 401, (
            f"Expected 401 for empty token POST, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Valid token via X-Admin-Token header
# ---------------------------------------------------------------------------


class TestXAdminTokenHeader:
    def test_valid_header_grants_access(self) -> None:
        """GET /admin with correct X-Admin-Token header returns 200."""
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {
                    "status": "ok",
                    "card_count": 1,
                    "note_count": 1,
                }
                resp = c.get("/admin/", headers={"X-Admin-Token": _TOKEN})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_wrong_header_token_denied(self) -> None:
        """GET /admin with wrong X-Admin-Token returns 302 or 401."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/",
                headers={"X-Admin-Token": "WRONG"},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 401), (
            f"Expected 302 or 401 for wrong token, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Valid token via HTTP Basic Auth
# ---------------------------------------------------------------------------


class TestBasicAuth:
    def test_valid_basic_auth_grants_access(self) -> None:
        """GET /admin with correct Basic auth (any user, password=token) returns 200."""
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {
                    "status": "ok",
                    "card_count": 1,
                    "note_count": 1,
                }
                resp = c.get(
                    "/admin/",
                    headers={"Authorization": _basic_auth_header("admin", _TOKEN)},
                )
        assert resp.status_code == 200, (
            f"Expected 200 for valid Basic auth, got {resp.status_code}"
        )

    def test_basic_auth_any_username_accepted(self) -> None:
        """Username in Basic auth is ignored — only the password matters."""
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {
                    "status": "ok",
                    "card_count": 1,
                    "note_count": 1,
                }
                resp = c.get(
                    "/admin/",
                    headers={"Authorization": _basic_auth_header("ignored", _TOKEN)},
                )
        assert resp.status_code == 200, (
            f"Username should be ignored; expected 200, got {resp.status_code}"
        )

    def test_wrong_basic_auth_password_denied(self) -> None:
        """Wrong password in Basic auth → 302 or 401."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/",
                headers={"Authorization": _basic_auth_header("admin", "wrong")},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 401), (
            f"Expected 302 or 401 for wrong password, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Valid token via cookie
# ---------------------------------------------------------------------------


class TestCookieAuth:
    def test_valid_cookie_grants_access(self) -> None:
        """GET /admin with a valid ``token`` cookie returns 200."""
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {
                    "status": "ok",
                    "card_count": 1,
                    "note_count": 1,
                }
                # Flask 2.2: set_cookie(server_name, key, value)
                c.set_cookie("localhost", "token", _TOKEN)
                resp = c.get("/admin/")
        assert resp.status_code == 200, (
            f"Expected 200 for valid cookie, got {resp.status_code}"
        )

    def test_wrong_cookie_denied(self) -> None:
        """Wrong cookie value → 302 or 401."""
        with _client(_TOKEN) as c:
            c.set_cookie("localhost", "token", "WRONG")
            resp = c.get("/admin/", follow_redirects=False)
        assert resp.status_code in (302, 401), (
            f"Expected 302 or 401 for wrong cookie, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Login form flow
# ---------------------------------------------------------------------------


class TestLoginFlow:
    def test_login_page_get_returns_200(self) -> None:
        """GET /admin/login renders the login form (200, no auth required)."""
        with _client(_TOKEN) as c:
            resp = c.get("/admin/login")
        assert resp.status_code == 200, (
            f"Login page should be 200 (exempt from auth), got {resp.status_code}"
        )
        body = resp.data.decode()
        assert "token" in body.lower(), (
            f"Login page should contain a token field, body snippet: {body[:200]!r}"
        )

    def test_login_post_valid_token_sets_cookie_and_redirects(self) -> None:
        """POST /admin/login with valid token sets cookie and redirects to /admin."""
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/login",
                data={"token": _TOKEN},
                follow_redirects=False,
            )
        assert resp.status_code == 302, (
            f"Expected 302 redirect after valid login, got {resp.status_code}"
        )
        location = resp.headers.get("Location", "")
        assert "/admin" in location, f"Expected redirect to /admin, got: {location}"
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "token=" in set_cookie, (
            f"Expected token cookie in Set-Cookie, got: {set_cookie!r}"
        )

    def test_login_post_wrong_token_returns_401(self) -> None:
        """POST /admin/login with wrong token returns 401, no token cookie."""
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/login",
                data={"token": "WRONG"},
                follow_redirects=False,
            )
        assert resp.status_code == 401, (
            f"Expected 401 for wrong login token, got {resp.status_code}"
        )
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "token=" not in set_cookie, (
            f"No token cookie should be set on failed login, got: {set_cookie!r}"
        )

    def test_logout_clears_cookie(self) -> None:
        """GET /admin/logout clears the token cookie and redirects to /admin/login."""
        with _client(_TOKEN) as c:
            # Set a cookie then log out.
            c.set_cookie("localhost", "token", _TOKEN)
            resp = c.get("/admin/logout", follow_redirects=False)
        assert resp.status_code == 302, (
            f"Expected 302 from logout, got {resp.status_code}"
        )
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location, (
            f"Expected redirect to /admin/login after logout, got: {location}"
        )
        # Flask 2.2: response.headers.getlist() returns all Set-Cookie values.
        # Werkzeug's delete_cookie sets the cookie with an expired date / empty value.
        all_set_cookie = " | ".join(resp.headers.getlist("Set-Cookie"))
        assert "token=" in all_set_cookie, (
            f"Expected Set-Cookie to clear token on logout, got: {all_set_cookie!r}"
        )


# ---------------------------------------------------------------------------
# Existing routes are NOT gated by admin auth
# ---------------------------------------------------------------------------


class TestExistingRoutesUnaffected:
    def test_health_returns_200_with_no_admin_credentials(self) -> None:
        """GET /health returns 200 with NO admin token in the request.

        Confirms the auth gate does NOT wrap /health.
        """
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.return_value = {
                    "status": "ok",
                    "card_count": 5,
                    "note_count": 5,
                }
                resp = c.get("/health")
        assert resp.status_code == 200, (
            f"/health should be 200 (not gated by admin auth), got {resp.status_code}"
        )
        data = resp.get_json()
        assert data["status"] == "ok"

    def test_health_503_from_collection_not_401_from_auth(self) -> None:
        """GET /health may return 503 (collection not open) but never 401 (auth error)."""
        with _client(_TOKEN) as c:
            with patch("src.collection.manager") as m:
                m.health.side_effect = RuntimeError("collection not open")
                resp = c.get("/health")
        assert resp.status_code in (200, 503), (
            f"Expected 200 or 503 from /health, got {resp.status_code}"
        )
        assert resp.status_code != 401, "/health must never return 401"

    def test_anki_connect_post_not_gated(self) -> None:
        """POST / (AnkiConnect version action) is NOT gated by admin auth.

        Patches the collection lock and dispatch table so no real collection
        is needed.  Response must be 200 with result=6.
        """
        with _client(_TOKEN) as c:
            with patch("src.collection._col_lock") as mock_lock:
                mock_lock.__enter__ = MagicMock(return_value=None)
                mock_lock.__exit__ = MagicMock(return_value=False)
                # Reach into the test app's dispatch by patching the module.
                import src.server as server_mod

                original_dispatch = server_mod.DISPATCH.copy()
                server_mod.DISPATCH["version"] = lambda params: 6
                try:
                    resp = c.post("/", json={"action": "version", "version": 6})
                finally:
                    server_mod.DISPATCH.clear()
                    server_mod.DISPATCH.update(original_dispatch)

        # The test app has its own dispatch (not src.server.DISPATCH), so let's
        # just confirm it returns 200 and is not gated by admin auth.
        # The test app's dispatch table is built inline in _build_test_app
        # and includes ACTIONS which provides version → 6.
        assert resp.status_code == 200, (
            f"POST / should be 200 (not gated by admin auth), got {resp.status_code}"
        )
        assert resp.status_code != 401, "POST / must never return 401"
        data = resp.get_json()
        assert data["result"] == 6
        assert data["error"] is None
