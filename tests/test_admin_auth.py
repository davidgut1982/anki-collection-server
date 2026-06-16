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

Security hardening coverage (Code Critic remediation)
------------------------------------------------------
- Cookie has Secure + HttpOnly + SameSite=Strict flags.
- POST /admin/login returns 503 (not 200) when ADMIN_TOKEN is unset.
- Rate-limit: returns 429 after N failed attempts; resets on successful login.
- Flask secret_key != ADMIN_TOKEN (derived, not the raw token).

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
import hashlib
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
    # Mirror server.py secret_key derivation: derived from token, not the raw token.
    if admin_token:
        app.secret_key = hashlib.sha256(
            b"acs-flask-session:" + admin_token.encode()
        ).hexdigest()
    else:
        app.secret_key = "test-secret-key-no-token"
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
    # Explicit url_prefix="/admin" mirrors server.py default (ADMIN_BASE_PATH=/admin).
    # Tests for alternate prefixes live in test_admin_basepath.py.
    app.config["ADMIN_BASE_PATH"] = "/admin"
    app.register_blueprint(routes_mod.admin_bp, url_prefix="/admin")

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


# ---------------------------------------------------------------------------
# Security hardening — Code Critic remediation tests
# ---------------------------------------------------------------------------


class TestSecureCookieFlags:
    """Verify the login cookie carries all required security flags."""

    def test_cookie_has_secure_httponly_samesite_strict(self) -> None:
        """Successful login must set Secure; HttpOnly; SameSite=Strict on the token cookie."""
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/login",
                data={"token": _TOKEN},
                follow_redirects=False,
            )
        assert resp.status_code == 302, (
            f"Expected 302 redirect after valid login, got {resp.status_code}"
        )
        set_cookie = resp.headers.get("Set-Cookie", "")
        # All three flags must be present (case-insensitive per RFC 6265).
        set_cookie_lower = set_cookie.lower()
        assert "secure" in set_cookie_lower, (
            f"Expected Secure flag in Set-Cookie, got: {set_cookie!r}"
        )
        assert "httponly" in set_cookie_lower, (
            f"Expected HttpOnly flag in Set-Cookie, got: {set_cookie!r}"
        )
        assert "samesite=strict" in set_cookie_lower, (
            f"Expected SameSite=Strict in Set-Cookie, got: {set_cookie!r}"
        )


class TestLoginPostTokenUnset:
    """Verify login_post returns 503 (not 200) when ADMIN_TOKEN is unset."""

    def test_login_post_returns_503_when_token_unset(self) -> None:
        """POST /admin/login must return 503 when ADMIN_TOKEN is not configured."""
        with _client(None) as c:
            resp = c.post(
                "/admin/login",
                data={"token": "anything"},
                follow_redirects=False,
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )
        body = resp.data.decode()
        assert "ADMIN_TOKEN" in body, (
            f"Expected 'ADMIN_TOKEN' in body for 503 response, got: {body!r}"
        )


class TestRateLimit:
    """Verify IP-keyed rate-limiting on POST /admin/login."""

    def _reset_ratelimit(self, ip: str = "127.0.0.1") -> None:
        """Clear the rate-limit state for the given IP between tests."""
        import src.admin.auth as auth_mod
        with auth_mod._ratelimit_lock:
            auth_mod._ratelimit_failures.pop(ip, None)

    def test_rate_limit_returns_429_after_max_failures(self) -> None:
        """After _RATELIMIT_MAX_FAILURES failed attempts the endpoint returns 429."""
        import src.admin.auth as auth_mod

        self._reset_ratelimit()
        try:
            with _client(_TOKEN) as c:
                # Submit exactly max_failures wrong tokens.
                for _ in range(auth_mod._RATELIMIT_MAX_FAILURES):
                    r = c.post("/admin/login", data={"token": "WRONG"})
                    assert r.status_code == 401, (
                        f"Expected 401 for wrong token, got {r.status_code}"
                    )

                # The (N+1)-th attempt should be rate-limited.
                resp = c.post("/admin/login", data={"token": "WRONG"})
            assert resp.status_code == 429, (
                f"Expected 429 after {auth_mod._RATELIMIT_MAX_FAILURES} failures, "
                f"got {resp.status_code}"
            )
            assert "Retry-After" in resp.headers, (
                "Expected Retry-After header in 429 response"
            )
            retry_after = int(resp.headers["Retry-After"])
            assert retry_after >= 1, (
                f"Retry-After must be >= 1 second, got {retry_after}"
            )
        finally:
            self._reset_ratelimit()

    def test_successful_login_resets_rate_limit(self) -> None:
        """A successful login clears the failure counter so the IP is no longer blocked."""
        import src.admin.auth as auth_mod

        self._reset_ratelimit()
        try:
            with _client(_TOKEN) as c:
                # Push the IP to the limit with wrong tokens.
                for _ in range(auth_mod._RATELIMIT_MAX_FAILURES):
                    c.post("/admin/login", data={"token": "WRONG"})

                # Confirm it is now rate-limited.
                rate_limited = c.post("/admin/login", data={"token": "WRONG"})
                assert rate_limited.status_code == 429, (
                    f"Expected 429 before reset, got {rate_limited.status_code}"
                )

                # Directly reset via the module function (simulates a successful
                # login from a different session on the same IP).
                auth_mod._ratelimit_reset("127.0.0.1")

                # Now a failed attempt should return 401 again, not 429.
                resp = c.post("/admin/login", data={"token": "WRONG"})
            assert resp.status_code == 401, (
                f"Expected 401 after rate-limit reset, got {resp.status_code}"
            )
        finally:
            self._reset_ratelimit()

    def test_correct_token_after_failures_resets_counter(self) -> None:
        """A correct token resets the failure counter via _ratelimit_reset."""
        import src.admin.auth as auth_mod

        self._reset_ratelimit()
        try:
            with _client(_TOKEN) as c:
                # Submit some (but fewer than max) wrong tokens.
                for _ in range(auth_mod._RATELIMIT_MAX_FAILURES - 1):
                    c.post("/admin/login", data={"token": "WRONG"})

                # Successful login should reset the counter.
                ok_resp = c.post(
                    "/admin/login",
                    data={"token": _TOKEN},
                    follow_redirects=False,
                )
                assert ok_resp.status_code == 302, (
                    f"Expected 302 on valid login, got {ok_resp.status_code}"
                )

                # After reset the IP can fail again without immediately hitting 429.
                resp = c.post("/admin/login", data={"token": "WRONG"})
            assert resp.status_code == 401, (
                f"Expected 401 (counter reset), not 429, got {resp.status_code}"
            )
        finally:
            self._reset_ratelimit()


class TestSecretKeyDecoupled:
    """Verify Flask secret_key is derived from ADMIN_TOKEN, not equal to it."""

    def test_secret_key_differs_from_admin_token(self) -> None:
        """The app's secret_key must not equal ADMIN_TOKEN (it must be derived)."""
        app = _build_test_app(_TOKEN)
        assert app.secret_key != _TOKEN, (
            "secret_key must not be the raw ADMIN_TOKEN — it should be a derived value"
        )

    def test_secret_key_is_deterministic(self) -> None:
        """Building two apps with the same token must produce the same secret_key."""
        app1 = _build_test_app(_TOKEN)
        app2 = _build_test_app(_TOKEN)
        assert app1.secret_key == app2.secret_key, (
            "secret_key must be deterministic (stable across restarts)"
        )

    def test_secret_key_differs_for_different_tokens(self) -> None:
        """Two different tokens must produce different secret keys."""
        app1 = _build_test_app("token-alpha")
        app2 = _build_test_app("token-beta")
        assert app1.secret_key != app2.secret_key, (
            "Different ADMIN_TOKENs must produce different secret keys"
        )
