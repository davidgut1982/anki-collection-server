"""
Tests for A9: GET /admin/diagnostics (diagnostics dashboard UI page).

Coverage
--------
Auth gate:
  - GET /admin/diagnostics with a valid token -> 200.
  - GET /admin/diagnostics without a token -> 302/401 (not 200).
  - GET /admin/diagnostics when ADMIN_TOKEN is unset -> 503.
  - GET /admin/diagnostics with wrong token -> 302/401.

Page content (with valid token):
  - HTTP 200 response.
  - Page title contains "Diagnostics".
  - Time-range selector present.
  - Chart canvases present for all 8 stat charts.
  - diagnostics.js loaded.
  - Vendored Chart.js referenced in the page.
  - Nav link "Diagnostics" is active.

Static asset:
  - GET /static/admin/vendor/chart.min.js -> 200 (vendor bundle served).

Design notes
------------
Uses the same _build_test_app() helper pattern as test_admin_maintenance_ui.py.
The diagnostics route renders a static shell; no collection access happens at
render time, so we do not need to mock get_col().  All stat action calls happen
client-side via acsInvoke (tested separately in test_admin_stats.py).
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub waitress before any src.* imports (matches existing test pattern)
# ---------------------------------------------------------------------------
if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "test-diagnostics-ui-token-a9"

# ---------------------------------------------------------------------------
# Test app builder
# ---------------------------------------------------------------------------


def _build_test_app(admin_token: str | None) -> Any:
    """Build a fresh Flask test app wired identically to server.py.

    Patches auth state, registers the admin blueprint, and mounts the
    AnkiConnect dispatch + /health routes.  Static folder is set to the
    real repo static/ directory so the vendored chart.min.js is served.
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
        __name__ + f"_diag_test_{id(admin_token)}",
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


def _authed_get(admin_token: str, path: str) -> Any:
    """GET a path with a valid X-Admin-Token header, no follow_redirects."""
    with _client(admin_token) as c:
        return c.get(path, headers={"X-Admin-Token": admin_token})


def _authed_get_text(admin_token: str, path: str) -> str:
    """GET a path with auth and return the decoded response body."""
    resp = _authed_get(admin_token, path)
    return resp.data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# TestDiagnosticsAuthGate
# ---------------------------------------------------------------------------


class TestDiagnosticsAuthGate:
    """The /admin/diagnostics endpoint must be token-gated."""

    def test_diagnostics_with_token_returns_200(self) -> None:
        """GET /admin/diagnostics with a valid token must return 200."""
        resp = _authed_get(_TOKEN, "/admin/diagnostics")
        assert resp.status_code == 200, (
            f"Expected 200 with valid token, got {resp.status_code}"
        )

    def test_diagnostics_without_token_not_200(self) -> None:
        """GET /admin/diagnostics without any credentials must not return 200.

        The auth hook redirects browsers (302) and returns 401 for API callers.
        """
        with _client(_TOKEN) as c:
            resp = c.get("/admin/diagnostics", follow_redirects=False)
        assert resp.status_code != 200, (
            "Must not return 200 without valid admin token"
        )
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 without token, got {resp.status_code}"
        )

    def test_diagnostics_without_configured_token_returns_503(self) -> None:
        """When ADMIN_TOKEN is unset, /admin/diagnostics returns 503."""
        with _client(None) as c:
            resp = c.get(
                "/admin/diagnostics",
                headers={"X-Admin-Token": "anything"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )

    def test_diagnostics_with_wrong_token_is_rejected(self) -> None:
        """Wrong token in X-Admin-Token header must not return 200."""
        with _client(_TOKEN) as c:
            resp = c.get(
                "/admin/diagnostics",
                headers={"X-Admin-Token": "WRONG-TOKEN"},
            )
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 for wrong token, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# TestDiagnosticsRoute — page content
# ---------------------------------------------------------------------------


class TestDiagnosticsRoute:
    """GET /admin/diagnostics content checks with a valid token."""

    def test_page_title_contains_diagnostics(self) -> None:
        """Response body should include 'Diagnostics' in the title/heading."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert "Diagnostics" in body, "Expected 'Diagnostics' in page body"

    def test_time_range_selector_present(self) -> None:
        """The time-range <select> element must be present."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert 'id="diag-range"' in body, "Expected time-range selector"

    def test_chart_canvases_present(self) -> None:
        """All 8 stat chart canvases must be in the page."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        canvas_ids = [
            "chart-card-counts",
            "chart-retention",
            "chart-interval",
            "chart-ease",
            "chart-future-due",
            "chart-reviews",
            "chart-added",
            "chart-time",
        ]
        for cid in canvas_ids:
            assert f'id="{cid}"' in body, f"Expected canvas id='{cid}' in page"

    def test_diagnostics_js_loaded(self) -> None:
        """diagnostics.js must be referenced in the page."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert "diagnostics.js" in body, "Expected diagnostics.js script reference"

    def test_vendored_chartjs_referenced(self) -> None:
        """The vendored chart.min.js must be referenced in the page."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert "chart.min.js" in body, (
            "Expected vendored chart.min.js referenced in page"
        )
        # Confirm it is loaded from the local vendor path, not a CDN.
        assert "cdn" not in body.lower() or "vendor/chart.min.js" in body, (
            "chart.min.js should be loaded from vendor/, not a CDN"
        )

    def test_nav_link_diagnostics_is_active(self) -> None:
        """The Diagnostics nav link must carry the 'active' CSS class."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        # The active link wraps the word 'Diagnostics' and has class="nav-link active"
        assert "nav-link active" in body, (
            "Expected 'nav-link active' class on the Diagnostics nav item"
        )
        assert "Diagnostics" in body, "Expected 'Diagnostics' text in nav"

    def test_summary_strip_present(self) -> None:
        """The collection summary strip container must be in the page."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert 'id="diag-summary-strip"' in body, (
            "Expected diag-summary-strip element"
        )

    def test_refresh_button_present(self) -> None:
        """A refresh button must be present for re-triggering all loads."""
        body = _authed_get_text(_TOKEN, "/admin/diagnostics")
        assert 'id="diag-refresh-btn"' in body, "Expected refresh button"

    def test_cookie_auth_grants_access(self) -> None:
        """Login via cookie (POST /admin/login) must also grant access."""
        with _client(_TOKEN) as c:
            # Perform login to set session cookie
            login_resp = c.post(
                "/admin/login",
                data={"token": _TOKEN},
                follow_redirects=False,
            )
            assert login_resp.status_code in (302, 200), (
                f"Login expected 302/200, got {login_resp.status_code}"
            )
            # Now access diagnostics with the session cookie in place
            resp = c.get("/admin/diagnostics")
        assert resp.status_code == 200, (
            f"Expected 200 via cookie auth, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# TestVendoredChartJs — static asset
# ---------------------------------------------------------------------------


class TestVendoredChartJs:
    """The vendored Chart.js file must be served as a static asset."""

    def test_chart_min_js_served_200(self) -> None:
        """GET /static/admin/vendor/chart.min.js must return 200."""
        # Auth is not required for static files; use the authed client for
        # consistency, but the static route has no auth gate.
        resp = _authed_get(_TOKEN, "/static/admin/vendor/chart.min.js")
        assert resp.status_code == 200, (
            f"Expected 200 for vendored chart.min.js, got {resp.status_code}"
        )

    def test_chart_min_js_non_empty(self) -> None:
        """The vendored chart.min.js must be non-empty (real Chart.js content)."""
        resp = _authed_get(_TOKEN, "/static/admin/vendor/chart.min.js")
        assert resp.status_code == 200
        content = resp.data
        assert len(content) > 10000, (
            f"chart.min.js too small ({len(content)} bytes); expected Chart.js 4.4.9 (~200 KB)"
        )

    def test_chart_min_js_content_type(self) -> None:
        """chart.min.js should be served with a JavaScript content type."""
        resp = _authed_get(_TOKEN, "/static/admin/vendor/chart.min.js")
        assert resp.status_code == 200
        ct = resp.content_type or ""
        assert "javascript" in ct or "application" in ct, (
            f"Unexpected content-type for chart.min.js: {ct!r}"
        )

    def test_vendor_path_exists_on_disk(self) -> None:
        """The chart.min.js file must exist in the repo static directory."""
        vendor_path = _REPO_ROOT / "static" / "admin" / "vendor" / "chart.min.js"
        assert vendor_path.exists(), (
            f"chart.min.js not found at {vendor_path}"
        )
        assert vendor_path.stat().st_size > 10000, (
            f"chart.min.js is suspiciously small: {vendor_path.stat().st_size} bytes"
        )
