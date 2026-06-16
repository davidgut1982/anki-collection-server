"""
Tests for A8: GET /admin/maintenance (database & media health UI page).

Coverage
--------
- GET /admin/maintenance with valid token -> 200 with expected content.
- GET /admin/maintenance without a token -> 302/401/503 (redirect to login or 401).

Design notes
------------
- Uses the same _build_test_app() helper pattern established in
  test_admin_scheduling_ui.py: patches auth state at function level, registers
  the admin blueprint, and stubs waitress.
- The maintenance route renders a static shell (no collection access at render
  time); collection calls happen client-side via acsInvoke.  Therefore we do
  NOT need to mock get_col() for the GET route itself.
- The underlying maintenance actions (checkDatabase, getEmptyCards,
  optimizeCollection, fixIntegrity, removeEmptyCards, mediaCheck,
  mediaDirSize, deleteUnusedMedia) are already tested in
  tests/test_admin_db_media_actions.py.
  This file covers the UI route auth and basic page structure only.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub waitress before any src.* imports (matches test_admin_scheduling_ui.py pattern)
# ---------------------------------------------------------------------------
if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "test-maintenance-ui-token"


def _build_test_app(admin_token: str | None) -> Any:
    """Build a fresh Flask test app wired identically to server.py.

    Patches auth state, registers the admin blueprint, and mounts the
    AnkiConnect dispatch + /health routes.
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

    # Patch auth module state.
    auth_mod.ADMIN_TOKEN = admin_token
    auth_mod.ADMIN_TOKEN_CONFIGURED = admin_token is not None

    DISPATCH: dict[str, Any] = {
        **ACTIONS,
        **GUI_ACTIONS,
        **SYNC_ACTIONS,
        **FSRS_ACTIONS,
    }

    app = Flask(
        __name__ + f"_maintenance_test_{id(admin_token)}",
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

    @app.post("/")
    def anki_connect() -> Any:  # type: ignore[return]
        body: dict[str, Any] = flask_request.get_json(silent=True) or {}
        action: str = body.get("action", "")
        params: dict[str, Any] = body.get("params") or {}
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

    # Reload routes module to clear any stale blueprint registration state.
    import src.admin.routes as routes_mod

    importlib.reload(routes_mod)
    app.config["ADMIN_BASE_PATH"] = "/admin"
    app.register_blueprint(routes_mod.admin_bp, url_prefix="/admin")

    return app


@contextmanager
def _client(admin_token: str | None) -> Generator:
    app = _build_test_app(admin_token)
    with app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# GET /admin/maintenance -- auth gate
# ---------------------------------------------------------------------------


class TestMaintenanceAuthGate:
    """The /admin/maintenance endpoint must be token-gated."""

    def test_maintenance_without_token_redirects(self) -> None:
        """GET /admin/maintenance without a token redirects to /admin/login (302)
        or returns 401.  Must not return 200.
        """
        with _client(_TOKEN) as c:
            resp = c.get("/admin/maintenance", follow_redirects=False)
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 without token, got {resp.status_code}"
        )
        assert resp.status_code != 200, (
            "Must not return 200 without valid admin token"
        )

    def test_maintenance_without_configured_token_returns_503(self) -> None:
        """When ADMIN_TOKEN is unset, /admin/maintenance returns 503."""
        with _client(None) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": "anything"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )

    def test_maintenance_with_wrong_token_is_rejected(self) -> None:
        """Wrong token in X-Admin-Token header must not return 200."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": "WRONG-TOKEN"},
            )
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 for wrong token, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# GET /admin/maintenance -- rendering
# ---------------------------------------------------------------------------


class TestMaintenanceRoute:
    """GET /admin/maintenance auth + rendering with a valid token."""

    def test_maintenance_with_valid_token_returns_200(self) -> None:
        """GET /admin/maintenance with a valid token returns 200."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        assert resp.status_code == 200, (
            f"Expected 200 for /admin/maintenance with valid token, got {resp.status_code}"
        )

    def test_maintenance_page_has_expected_title(self) -> None:
        """The maintenance page must contain 'DB'."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "DB" in body, "Page must contain 'DB'"

    def test_maintenance_page_has_db_section(self) -> None:
        """The maintenance page must have the check-db-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="check-db-btn"' in body, "Must have check-db-btn element"

    def test_maintenance_page_has_media_section(self) -> None:
        """The maintenance page must have the media-check-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="media-check-btn"' in body, "Must have media-check-btn element"

    def test_maintenance_page_has_optimize_btn(self) -> None:
        """The maintenance page must have the optimize-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="optimize-btn"' in body, "Must have optimize-btn element"

    def test_maintenance_page_has_fix_integrity_btn(self) -> None:
        """The maintenance page must have the fix-integrity-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="fix-integrity-btn"' in body, "Must have fix-integrity-btn element"

    def test_maintenance_page_has_remove_empty_btn(self) -> None:
        """The maintenance page must have the remove-empty-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="remove-empty-btn"' in body, "Must have remove-empty-btn element"

    def test_maintenance_page_has_delete_unused_btn(self) -> None:
        """The maintenance page must have the delete-unused-btn element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="delete-unused-btn"' in body, "Must have delete-unused-btn element"

    def test_maintenance_page_has_confirm_overlay(self) -> None:
        """The maintenance page must have the confirm-overlay modal."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="confirm-overlay"' in body, "Must have confirm-overlay element"

    def test_maintenance_page_loads_maintenance_js(self) -> None:
        """The maintenance page must load maintenance.js."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert "maintenance.js" in body, (
            "Page must include maintenance.js script tag"
        )

    def test_maintenance_nav_link_is_wired(self) -> None:
        """The base template nav must contain a link to /admin/maintenance."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/maintenance",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert "/admin/maintenance" in body, (
            "Nav must contain /admin/maintenance link"
        )

    def test_maintenance_page_cookie_auth(self) -> None:
        """A valid session cookie (obtained via login) grants access to /admin/maintenance."""
        with _client(_TOKEN) as c:
            login_resp = c.post("/admin/login", data={"token": _TOKEN})
            assert login_resp.status_code in (200, 302), (
                f"Login should succeed, got {login_resp.status_code}"
            )
            resp = c.get("/admin/maintenance")

        assert resp.status_code == 200, (
            f"Cookie auth should grant access to /admin/maintenance, got {resp.status_code}"
        )
