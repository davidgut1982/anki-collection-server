"""
Tests for A6: /admin/api/invoke proxy + GET /admin/browse (A6).

Coverage
--------
- POST /admin/api/invoke without a token -> 401 (or 302 redirect).
- POST /admin/api/invoke with a valid token + known action (deckNames) -> 200,
  returns the AnkiConnect envelope {result: [...], error: null}.
- POST /admin/api/invoke with a valid token + unknown action -> 200, envelope
  has {result: null, error: "unsupported action: <name>"}.
- GET /admin/browse with a valid token -> 200.
- GET /admin/browse without a token -> 302 to /admin/login (or 401).

Design notes
------------
- All tests use _build_test_app() (inherited pattern from test_admin_auth.py)
  which patches ADMIN_TOKEN state at function level, so tests are isolated.
- We stub the collection (col_mod.get_col()) so action handlers that call
  _col() return a MagicMock rather than trying to open a real .anki2 file.
- The invoke route uses `from src.server import DISPATCH` at request time,
  so we can patch DISPATCH in place for the "unknown action" test.
- waitress is not installed in the test env; it is stubbed before any src.*
  imports.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub waitress before any src.* imports (matches test_admin_auth.py pattern)
# ---------------------------------------------------------------------------
if "waitress" not in sys.modules:
    sys.modules["waitress"] = MagicMock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TOKEN = "test-browse-token"


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
        __name__ + f"_browse_test_{id(admin_token)}",
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
# POST /admin/api/invoke -- auth gate
# ---------------------------------------------------------------------------


class TestInvokeAuthGate:
    """The /admin/api/invoke endpoint must be token-gated."""

    def test_invoke_without_token_is_rejected(self) -> None:
        """POST /admin/api/invoke without any credentials must not return 200.

        Expect 401 (API path, not a browser GET) or 302 (redirect to login).
        This is the primary security property: the raw action proxy must never
        be reachable without a valid admin token.
        """
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                # No credentials: no cookie, no header, no basic auth
            )
        assert resp.status_code in (401, 302, 503), (
            f"Expected 401/302/503 without token, got {resp.status_code}"
        )
        assert resp.status_code != 200, (
            "Must not return 200 without valid admin token"
        )

    def test_invoke_with_wrong_token_is_rejected(self) -> None:
        """Wrong token in X-Admin-Token header -> not 200."""
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                headers={"X-Admin-Token": "WRONG-TOKEN"},
            )
        assert resp.status_code in (401, 302, 503), (
            f"Expected 401/302 for wrong token, got {resp.status_code}"
        )

    def test_invoke_without_configured_token_returns_503(self) -> None:
        """When ADMIN_TOKEN is unset, all /admin routes return 503."""
        with _client(None) as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "deckNames", "params": {}},
                headers={"X-Admin-Token": "anything"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# POST /admin/api/invoke -- dispatch (with valid token)
# ---------------------------------------------------------------------------


class TestInvokeDispatch:
    """With a valid token, /admin/api/invoke dispatches to the DISPATCH table."""

    def test_invoke_known_action_deckNames(self) -> None:
        """Invoke deckNames via the proxy; expect the AnkiConnect envelope.

        deckNames calls col.decks.all_names_and_ids() -- we mock the
        collection so no real .anki2 file is needed.
        """
        with _client(_TOKEN) as c:
            # Mock the collection so _col() returns a controllable object.
            mock_deck = MagicMock()
            mock_deck.name = "Default"
            mock_col = MagicMock()
            mock_col.decks.all_names_and_ids.return_value = [mock_deck]

            with patch("src.collection.get_col", return_value=mock_col):
                resp = c.post(
                    "/admin/api/invoke",
                    json={"action": "deckNames", "params": {}},
                    headers={"X-Admin-Token": _TOKEN},
                )

        assert resp.status_code == 200, (
            f"Expected 200 from invoke with valid token, got {resp.status_code}"
        )
        data = resp.get_json()
        assert data is not None, "Response must be JSON"
        assert "result" in data, f"Missing 'result' key: {data}"
        assert "error" in data, f"Missing 'error' key: {data}"
        assert data["error"] is None, f"Expected no error, got: {data['error']}"
        assert isinstance(data["result"], list), (
            f"deckNames result must be a list, got: {type(data['result'])}"
        )

    def test_invoke_returns_anki_envelope_on_success(self) -> None:
        """Successful invocation must return {result: <value>, error: null}."""
        with _client(_TOKEN) as c:
            with patch("src.collection.get_col") as mock_get_col:
                mock_col = MagicMock()
                # _version doesn't call the collection at all
                mock_get_col.return_value = mock_col

                resp = c.post(
                    "/admin/api/invoke",
                    json={"action": "version", "params": {}},
                    headers={"X-Admin-Token": _TOKEN},
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["error"] is None
        assert data["result"] == 6, f"version action should return 6, got {data['result']}"

    def test_invoke_unknown_action_returns_envelope_error(self) -> None:
        """Unknown action must return the AnkiConnect error envelope (not HTTP 4xx).

        This mirrors the behaviour of the raw POST / endpoint: HTTP 200 with
        {result: null, error: "unsupported action: <name>"}.
        """
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"action": "nonExistentAction_xyz", "params": {}},
                headers={"X-Admin-Token": _TOKEN},
            )

        assert resp.status_code == 200, (
            f"Unknown action must still return HTTP 200, got {resp.status_code}"
        )
        data = resp.get_json()
        assert data["result"] is None, (
            f"Unknown action result must be null, got {data['result']}"
        )
        assert data["error"] is not None, (
            "Unknown action must have a non-null error message"
        )
        assert "nonExistentAction_xyz" in data["error"], (
            f"Error message should name the action, got: {data['error']!r}"
        )

    def test_invoke_missing_action_field_returns_400(self) -> None:
        """Invoke with no 'action' field returns 400 (client error, not 200)."""
        with _client(_TOKEN) as c:
            resp = c.post(
                "/admin/api/invoke",
                json={"params": {}},  # no 'action' key
                headers={"X-Admin-Token": _TOKEN},
            )
        # Our route returns 400 for missing action field
        assert resp.status_code == 400, (
            f"Expected 400 for missing action, got {resp.status_code}"
        )

    def test_invoke_handler_exception_returns_envelope_error(self) -> None:
        """If the handler raises, invoke returns HTTP 200 with {error: <msg>}."""
        with _client(_TOKEN) as c:
            # cardsInfo with no valid collection will raise; we confirm the
            # envelope shape is correct (not a 500).
            with patch("src.collection.get_col", side_effect=RuntimeError("col not open")):
                resp = c.post(
                    "/admin/api/invoke",
                    json={"action": "deckNames", "params": {}},
                    headers={"X-Admin-Token": _TOKEN},
                )

        assert resp.status_code == 200, (
            f"Handler exception should still give HTTP 200, got {resp.status_code}"
        )
        data = resp.get_json()
        assert data["result"] is None
        assert data["error"] is not None
        assert "col not open" in data["error"], (
            f"Expected error message to contain 'col not open', got {data['error']!r}"
        )

    def test_invoke_with_cookie_auth(self) -> None:
        """The token cookie (set by /admin/login) also grants access to invoke."""
        with _client(_TOKEN) as c:
            with patch("src.collection.get_col") as _:
                # Set the cookie as if the user had logged in
                c.set_cookie("localhost", "token", _TOKEN)
                resp = c.post(
                    "/admin/api/invoke",
                    json={"action": "version", "params": {}},
                )

        assert resp.status_code == 200, (
            f"Cookie auth should grant access to invoke, got {resp.status_code}"
        )
        data = resp.get_json()
        assert data["result"] == 6


# ---------------------------------------------------------------------------
# GET /admin/browse -- route auth
# ---------------------------------------------------------------------------


class TestBrowseRoute:
    """GET /admin/browse auth and rendering."""

    def test_browse_without_token_redirects(self) -> None:
        """GET /admin/browse without a token redirects to /admin/login."""
        with _client(_TOKEN) as c:
            resp = c.get("/admin/browse", follow_redirects=False)
        assert resp.status_code in (302, 401, 503), (
            f"Expected 302/401/503 without token, got {resp.status_code}"
        )

    def test_browse_with_valid_token_returns_200(self) -> None:
        """GET /admin/browse with a valid token returns 200 and renders the page."""
        with _client(_TOKEN) as c:
            # Stub the collection so browse() doesn't fail on get_col()
            mock_deck = MagicMock()
            mock_deck.name = "Default"
            mock_col = MagicMock()
            mock_col.decks.all_names_and_ids.return_value = [mock_deck]
            mock_col.tags.all.return_value = ["latvian", "verb"]
            mock_col.models.all.return_value = []

            with patch("src.collection.get_col", return_value=mock_col):
                resp = c.get(
                    "/admin/browse",
                    headers={"X-Admin-Token": _TOKEN},
                )

        assert resp.status_code == 200, (
            f"Expected 200 for /admin/browse with valid token, got {resp.status_code}"
        )
        body = resp.data.decode()
        # Basic content checks
        assert "Browse" in body, "Page should contain 'Browse'"
        assert "search" in body.lower(), "Page should contain search UI"

    def test_browse_without_configured_token_returns_503(self) -> None:
        """When ADMIN_TOKEN is unset, /admin/browse returns 503."""
        with _client(None) as c:
            resp = c.get(
                "/admin/browse",
                headers={"X-Admin-Token": "anything"},
            )
        assert resp.status_code == 503, (
            f"Expected 503 when ADMIN_TOKEN unset, got {resp.status_code}"
        )

    def test_browse_page_contains_expected_ui_elements(self) -> None:
        """Browse page must include the search form and results table skeleton."""
        with _client(_TOKEN) as c:
            mock_col = MagicMock()
            mock_col.decks.all_names_and_ids.return_value = []
            mock_col.tags.all.return_value = []
            mock_col.models.all.return_value = []

            with patch("src.collection.get_col", return_value=mock_col):
                resp = c.get(
                    "/admin/browse",
                    headers={"X-Admin-Token": _TOKEN},
                )

        assert resp.status_code == 200
        body = resp.data.decode()
        # Search form
        assert 'id="search-form"' in body, "Must have search-form element"
        assert 'id="search-input"' in body, "Must have search-input element"
        # Results table
        assert 'id="results-table"' in body, "Must have results-table element"
        # Bulk toolbar
        assert 'id="bulk-toolbar"' in body, "Must have bulk-toolbar element"
        # Note editor panel
        assert 'id="editor-overlay"' in body, "Must have editor-overlay element"
        # Find & Replace
        assert "Find" in body and "Replace" in body, "Must have Find & Replace section"

    def test_browse_nav_link_is_wired(self) -> None:
        """The base template nav must contain an active Browser link on /admin/browse."""
        with _client(_TOKEN) as c:
            mock_col = MagicMock()
            mock_col.decks.all_names_and_ids.return_value = []
            mock_col.tags.all.return_value = []
            mock_col.models.all.return_value = []

            with patch("src.collection.get_col", return_value=mock_col):
                resp = c.get(
                    "/admin/browse",
                    headers={"X-Admin-Token": _TOKEN},
                )

        body = resp.data.decode()
        # The nav link for Browse should be present and active
        assert "/admin/browse" in body, (
            "Nav must contain /admin/browse link"
        )
        # Active class is set when request.endpoint == 'admin.browse'
        assert "active" in body, (
            "Browse nav link should have 'active' class when on the browse page"
        )
