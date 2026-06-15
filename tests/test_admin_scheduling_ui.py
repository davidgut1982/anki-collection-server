"""
Tests for A7: GET /admin/scheduling (scheduling UI page).

Coverage
--------
- GET /admin/scheduling with valid token -> 200 with expected content.
- GET /admin/scheduling without a token -> 302/401 (redirect to login or 401).

Design notes
------------
- Uses the same _build_test_app() helper pattern established in
  test_admin_browse.py: patches auth state at function level, registers the
  admin blueprint, and stubs waitress.
- The scheduling route renders a static shell (no collection access at render
  time); collection calls happen client-side via acsInvoke.  Therefore we do
  NOT need to mock get_col() for the GET route itself.
- The underlying scheduling actions (getDeckConfigs, updateDeckConfig,
  getFsrsParams, etc.) are already tested exhaustively in
  tests/test_admin_scheduling_actions.py and tests/test_fsrs.py.
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
# Stub waitress before any src.* imports (matches test_admin_browse.py pattern)
# ---------------------------------------------------------------------------
if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "test-scheduling-ui-token"


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
        __name__ + f"_scheduling_test_{id(admin_token)}",
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
    app.register_blueprint(routes_mod.admin_bp)

    return app


@contextmanager
def _client(admin_token: str | None) -> Generator:
    app = _build_test_app(admin_token)
    with app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# GET /admin/scheduling -- auth gate
# ---------------------------------------------------------------------------


class TestSchedulingAuthGate:
    """The /admin/scheduling endpoint must be token-gated."""

    def test_scheduling_without_token_redirects(self) -> None:
        """GET /admin/scheduling without a token redirects to /admin/login (302)
        or returns 401.  Must not return 200.
        """
        with _client(_TOKEN) as c:
            resp = c.get("/admin/scheduling", follow_redirects=False)
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 without token, got {resp.status_code}"
        )
        assert resp.status_code != 200, (
            "Must not return 200 without valid admin token"
        )

    def test_scheduling_without_configured_token_returns_503(self) -> None:
        """When ADMIN_TOKEN is unset, /admin/scheduling returns 503."""
        with _client(None) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": "anything"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )

    def test_scheduling_with_wrong_token_is_rejected(self) -> None:
        """Wrong token in X-Admin-Token header must not return 200."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": "WRONG-TOKEN"},
            )
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 for wrong token, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# GET /admin/scheduling -- rendering
# ---------------------------------------------------------------------------


class TestSchedulingRoute:
    """GET /admin/scheduling auth + rendering with a valid token."""

    def test_scheduling_with_valid_token_returns_200(self) -> None:
        """GET /admin/scheduling with a valid token returns 200."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        assert resp.status_code == 200, (
            f"Expected 200 for /admin/scheduling with valid token, got {resp.status_code}"
        )

    def test_scheduling_page_has_expected_title(self) -> None:
        """The scheduling page title must contain 'Scheduling'."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Scheduling" in body, "Page must contain the word 'Scheduling'"

    def test_scheduling_page_has_preset_selector(self) -> None:
        """The scheduling page must have a preset selector element."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="preset-select"' in body, "Must have preset-select element"

    def test_scheduling_page_has_deck_options_form(self) -> None:
        """The scheduling page must have the deck-options form."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="deck-options-form"' in body, "Must have deck-options-form"

    def test_scheduling_page_has_fsrs_panel(self) -> None:
        """The scheduling page must include the FSRS panel."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="fsrs-card"' in body, "Must have fsrs-card element"

    def test_scheduling_page_has_caveat_banner(self) -> None:
        """The scheduling page must have the FSRS caveat banner."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="fsrs-caveat-banner"' in body, (
            "Must have the FSRS caveat banner element"
        )
        # Banner must mention the headless limitation
        assert "headless" in body.lower() or "desktop" in body.lower(), (
            "Caveat banner must mention the headless/desktop-only limitation"
        )

    def test_scheduling_page_has_enable_fsrs_button(self) -> None:
        """The FSRS panel must include the enable button."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="enable-fsrs-btn"' in body, "Must have enable-fsrs-btn element"

    def test_scheduling_page_has_desired_retention_input(self) -> None:
        """The FSRS panel must include the desired retention input."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="desired-retention"' in body, (
            "Must have desired-retention input element"
        )

    def test_scheduling_page_has_compute_optimal_retention_button(self) -> None:
        """The FSRS panel must include the compute optimal retention button."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert 'id="compute-retention-btn"' in body, (
            "Must have compute-retention-btn element"
        )

    def test_scheduling_page_loads_scheduling_js(self) -> None:
        """The scheduling page must load scheduling.js."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert "scheduling.js" in body, (
            "Page must include scheduling.js script tag"
        )

    def test_scheduling_nav_link_is_wired(self) -> None:
        """The base template nav must contain an active Scheduling link on /admin/scheduling."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/scheduling",
                headers={"X-Admin-Token": _TOKEN},
            )
        body = resp.data.decode()
        assert "/admin/scheduling" in body, (
            "Nav must contain /admin/scheduling link"
        )
        # Active class is set when request.endpoint == 'admin.scheduling'
        assert "active" in body, (
            "Scheduling nav link should have 'active' class when on the scheduling page"
        )

    def test_scheduling_page_cookie_auth(self) -> None:
        """A valid session cookie (obtained via login) grants access to /admin/scheduling."""
        with _client(_TOKEN) as c:
            login_resp = c.post("/admin/login", data={"token": _TOKEN})
            assert login_resp.status_code in (200, 302), (
                f"Login should succeed, got {login_resp.status_code}"
            )
            resp = c.get("/admin/scheduling")

        assert resp.status_code == 200, (
            f"Cookie auth should grant access to /admin/scheduling, got {resp.status_code}"
        )
