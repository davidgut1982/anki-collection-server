#!/usr/bin/env python3.11
"""Wire-shape parity check: our server vs live anki-headless:8765.

Usage
-----
  python3.11 tests/parity_check.py

Exits 0 if every tested action's response SHAPE matches anki-headless.
Exits 1 on parity failure (key present in live server that our server omits).

What this script does
---------------------
1. Starts OUR server on a fresh copy of the committed fixture on a test port
   (default 18765) using Flask's built-in dev server (waitress is not needed
   in the test environment — only needed in production Docker).
2. Checks that anki-headless is reachable at localhost:8765 (read-only target).
3. For READ-ONLY actions only (never calls mutating actions against live server):
   - version, deckNames, modelNames, findNotes, findCards, notesInfo, cardsInfo
4. Compares RESPONSE SHAPE (key sets + types) — NOT data values.
   Data values differ between the collections; only structure matters.
5. Reports per-action: SHAPE MATCH or exact key diff.

Safety
------
- NEVER calls mutating actions (addNote, sync, storeMediaFile, gui*, etc.)
  against anki-headless.
- anki-headless is queried READ-ONLY for parity purposes.
- Our server uses a /tmp copy of the committed fixture (not the live collection).

pytest integration
------------------
This script is NOT part of the default pytest run.  To run it:
  python3.11 tests/parity_check.py

If anki-headless:8765 is unreachable the script reports SKIPPED and exits 0.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

# Inject a mock waitress module BEFORE importing src.server so the module
# loads cleanly in the test environment where waitress is not installed.
# (waitress is only needed in the production Docker image, not in dev/test.)
import types as _types

if "waitress" not in sys.modules:
    _mock_waitress = _types.ModuleType("waitress")
    _mock_waitress.serve = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["waitress"] = _mock_waitress

# ---------------------------------------------------------------------------
# Ensure our package is importable
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIVE_HOST = "localhost"
LIVE_PORT = 8765
LIVE_URL = f"http://{LIVE_HOST}:{LIVE_PORT}"

OUR_PORT = 18765
OUR_URL = f"http://localhost:{OUR_PORT}"

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "test_collection.anki2"

# ---------------------------------------------------------------------------
# AnkiConnect wire helpers
# ---------------------------------------------------------------------------


def _post(url: str, action: str, params: dict | None = None) -> dict:
    """POST an AnkiConnect request and return the decoded response dict."""
    body = json.dumps({"action": action, "version": 6, "params": params or {}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"_error": str(exc), "result": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Shape comparison utilities
# ---------------------------------------------------------------------------


def _type_name(v: Any) -> str:
    """Return a concise type label for a value."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return f"list[{_type_name(v[0]) if v else '?'}]"
    if isinstance(v, dict):
        return "dict"
    if v is None:
        return "null"
    return type(v).__name__


def _shape_of(value: Any) -> Any:
    """Recursively extract the shape (keys/types) of a value."""
    if isinstance(value, dict):
        return {k: _shape_of(v) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape_of(value[0])]
    return _type_name(value)


def _key_diff(
    live_shape: Any, our_shape: Any, path: str = ""
) -> tuple[list[str], list[str]]:
    """Return (keys_in_live_not_ours, keys_in_ours_not_live) as flat path lists.

    Special handling for the ``fields`` key in notesInfo/cardsInfo:
    - The live collection and our fixture have *different* note types with
      *different* field names (e.g. "latvian"/"english" vs "Front"/"Back").
    - This is expected and NOT a structural incompatibility.
    - We detect the ``fields`` level by checking if both sides are dicts
      whose values are dicts with ``value`` and ``order`` keys.
    - At the ``fields`` level we compare the INNER structure of any field value
      rather than the specific field names.
    """
    missing_in_ours: list[str] = []
    extra_in_ours: list[str] = []

    if isinstance(live_shape, dict) and isinstance(our_shape, dict):
        live_keys = set(live_shape.keys())
        our_keys = set(our_shape.keys())

        # Check if this dict looks like AnkiConnect's ``fields`` dict:
        # {field_name: {value: str, order: int}}.  Field names vary per
        # collection — don't compare them, only compare the inner structure.
        def _looks_like_fields_dict(d: Any) -> bool:
            if not isinstance(d, dict) or not d:
                return False
            sample = next(iter(d.values()))
            return isinstance(sample, dict) and set(sample.keys()) <= {"value", "order"}

        if _looks_like_fields_dict(live_shape) and _looks_like_fields_dict(our_shape):
            # Fields dicts: compare the INNER structure of one sample value only
            live_sample = next(iter(live_shape.values()))
            our_sample = next(iter(our_shape.values()))
            inner_path = f"{path}.<field_name>"
            m, e = _key_diff(live_sample, our_sample, inner_path)
            missing_in_ours.extend(m)
            extra_in_ours.extend(e)
            return missing_in_ours, extra_in_ours

        # Standard dict comparison: check which keys are missing/extra
        for k in live_keys - our_keys:
            missing_in_ours.append(f"{path}.{k}" if path else k)
        for k in our_keys - live_keys:
            extra_in_ours.append(f"{path}.{k}" if path else k)
        for k in live_keys & our_keys:
            sub_path = f"{path}.{k}" if path else k
            m, e = _key_diff(live_shape[k], our_shape[k], sub_path)
            missing_in_ours.extend(m)
            extra_in_ours.extend(e)
    elif isinstance(live_shape, list) and isinstance(our_shape, list):
        if live_shape and our_shape:
            m, e = _key_diff(live_shape[0], our_shape[0], f"{path}[*]")
            missing_in_ours.extend(m)
            extra_in_ours.extend(e)

    return missing_in_ours, extra_in_ours


# ---------------------------------------------------------------------------
# Server management — uses Flask dev server on a background thread
# ---------------------------------------------------------------------------


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if the TCP port is open."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    """Poll until the AnkiConnect server responds to a version ping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = _post(url, "version")
            if resp.get("result") == 6:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


class OurServer:
    """Context manager that starts our Flask server on a background thread."""

    def __init__(self, fixture_path: Path, port: int) -> None:
        self.fixture_path = fixture_path
        self.port = port
        self._tmpdir: Path | None = None
        self._col_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None  # werkzeug.serving.BaseWSGIServer

    def __enter__(self) -> "OurServer":
        import src.collection as col_mod

        # Copy fixture to /tmp (server writes to it)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="acs-parity-"))
        self._col_path = self._tmpdir / "collection.anki2"
        shutil.copy2(self.fixture_path, self._col_path)
        self._col_path.chmod(0o600)

        # Open the collection in the global manager
        col_mod.manager.open(self._col_path)

        # Import the Flask app (waitress mock is already in sys.modules)
        from src.server import app

        # Start the Flask dev server on a background daemon thread
        import werkzeug.serving

        self._server = werkzeug.serving.make_server(
            "127.0.0.1", self.port, app, threaded=True
        )

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        # Close the collection
        try:
            import src.collection as col_mod

            col_mod.manager.close()
        except Exception:
            pass
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Parity report
# ---------------------------------------------------------------------------


class ParityReport:
    """Accumulate per-action parity results and print a final report."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def record(self, action: str, passed: bool, detail: str = "") -> None:
        self.results.append((action, passed, detail))
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {action}: {detail}")

    def summary(self) -> bool:
        """Print summary; return True if all passed."""
        failures = [(a, d) for a, ok, d in self.results if not ok]
        passes = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print()
        print("=" * 60)
        print(f"PARITY REPORT: {passes}/{total} actions matched live shape")
        if failures:
            print()
            print("FAILURES (keys present in live anki-headless but MISSING in ours):")
            for action, detail in failures:
                print(f"  {action}: {detail}")
            print()
            print("VERDICT: FAIL — wire incompatibilities found (see above)")
        else:
            print("VERDICT: PASS — all action shapes match anki-headless")
        print("=" * 60)
        return not failures


# ---------------------------------------------------------------------------
# Individual action checks
# ---------------------------------------------------------------------------


def _check_list_action(
    report: ParityReport,
    action: str,
    live_url: str,
    our_url: str,
    params: dict | None = None,
    expected_elem_type: type = str,
) -> tuple[list, list]:
    """Check that both servers return list[expected_elem_type]. Return (live_result, our_result)."""
    live_resp = _post(live_url, action, params)
    our_resp = _post(our_url, action, params)

    live_result = live_resp.get("result") or []
    our_result = our_resp.get("result") or []

    if live_resp.get("_error") or our_resp.get("_error"):
        report.record(
            action,
            False,
            f"Request error: live={live_resp.get('_error')} our={our_resp.get('_error')}",
        )
        return live_result, our_result

    live_ok = isinstance(live_result, list) and (
        not live_result or isinstance(live_result[0], expected_elem_type)
    )
    our_ok = isinstance(our_result, list) and (
        not our_result or isinstance(our_result[0], expected_elem_type)
    )

    if live_ok and our_ok:
        report.record(
            action,
            True,
            f"both return list[{expected_elem_type.__name__}] "
            f"(live: {len(live_result)}, ours: {len(our_result)})",
        )
    elif not live_ok:
        report.record(
            action, False, f"live returned unexpected type: {type(live_result)}"
        )
    else:
        report.record(
            action, False, f"our server returned unexpected type: {type(our_result)}"
        )

    return live_result, our_result


def _check_info_shape(
    report: ParityReport,
    action: str,
    live_url: str,
    our_url: str,
    id_action: str,
    id_param: str,
    id_query_params: dict | None = None,
) -> None:
    """Check notesInfo/cardsInfo shape by sampling one id from each server."""
    # Fetch IDs from LIVE server
    live_ids_resp = _post(live_url, id_action, id_query_params or {"query": "*"})
    live_ids = live_ids_resp.get("result") or []
    live_sample_id = live_ids[0] if live_ids else None

    # Fetch IDs from OUR server
    our_ids_resp = _post(our_url, id_action, id_query_params or {"query": "*"})
    our_ids = our_ids_resp.get("result") or []
    our_sample_id = our_ids[0] if our_ids else None

    if live_sample_id is None:
        report.record(action, False, "live server has no notes/cards")
        return
    if our_sample_id is None:
        report.record(action, False, "our server has no notes/cards")
        return

    # Fetch info using EACH server's OWN id (data values differ — shape is what matters)
    live_resp = _post(live_url, action, {id_param: [live_sample_id]})
    our_resp = _post(our_url, action, {id_param: [our_sample_id]})

    live_result = live_resp.get("result") or []
    our_result = our_resp.get("result") or []

    if not live_result:
        report.record(action, False, f"live returned empty for {action}")
        return
    if not our_result:
        report.record(action, False, f"our server returned empty for {action}")
        return

    # Compare shape of first item
    live_shape = _shape_of(live_result[0])
    our_shape = _shape_of(our_result[0])
    missing_in_ours, extra_in_ours = _key_diff(live_shape, our_shape)

    if missing_in_ours:
        report.record(
            action,
            False,
            f"MISSING in our server (keys live has but we omit): {missing_in_ours}",
        )
        print(f"    live {action} item shape:  {json.dumps(live_shape, indent=6)}")
        print(f"    our  {action} item shape:  {json.dumps(our_shape, indent=6)}")
    elif extra_in_ours:
        report.record(
            action,
            True,
            f"shape matches (our server has {len(extra_in_ours)} extra key(s) not in live: {extra_in_ours})",
        )
    else:
        report.record(action, True, "shape matches")


# ---------------------------------------------------------------------------
# Main parity runner
# ---------------------------------------------------------------------------


def run_parity() -> bool:
    """Run all parity checks. Return True if all passed."""
    print()
    print("=" * 60)
    print("WIRE SHAPE PARITY CHECK: our server vs anki-headless")
    print("=" * 60)

    # --- Check live server reachability ---
    print(f"\nChecking live anki-headless at {LIVE_URL} ...")
    if not _is_port_open(LIVE_HOST, LIVE_PORT):
        print(f"  SKIPPED: anki-headless not reachable at {LIVE_URL}")
        print("  (This is acceptable — live server is optional for parity.)")
        return True

    live_version = _post(LIVE_URL, "version").get("result")
    print(f"  live anki-headless version: {live_version}")

    # --- Start our server ---
    if not FIXTURE_PATH.exists():
        print(f"  ERROR: fixture not found: {FIXTURE_PATH}")
        return False

    print(
        f"\nStarting our server on port {OUR_PORT} (fixture: {FIXTURE_PATH.name}) ..."
    )

    with OurServer(FIXTURE_PATH, OUR_PORT) as _srv:
        if not _wait_for_server(OUR_URL, timeout=20):
            print(f"  ERROR: our server did not start on port {OUR_PORT}")
            return False
        our_version = _post(OUR_URL, "version").get("result")
        print(f"  our server version: {our_version}")

        report = ParityReport()
        print("\nRunning read-only parity checks (never mutates anki-headless) ...")

        # 1. version
        live_v = _post(LIVE_URL, "version").get("result")
        our_v = _post(OUR_URL, "version").get("result")
        if live_v == our_v == 6:
            report.record("version", True, "both return int 6")
        elif live_v == 6 and our_v == 6:
            report.record("version", True, "both return int 6")
        else:
            report.record("version", False, f"live={live_v!r} ours={our_v!r}")

        # 2. deckNames → list[str]
        _check_list_action(
            report, "deckNames", LIVE_URL, OUR_URL, expected_elem_type=str
        )

        # 3. modelNames → list[str]
        _check_list_action(
            report, "modelNames", LIVE_URL, OUR_URL, expected_elem_type=str
        )

        # 4. findNotes → list[int]
        _check_list_action(
            report,
            "findNotes",
            LIVE_URL,
            OUR_URL,
            params={"query": "*"},
            expected_elem_type=int,
        )

        # 5. findCards → list[int]
        _check_list_action(
            report,
            "findCards",
            LIVE_URL,
            OUR_URL,
            params={"query": "*"},
            expected_elem_type=int,
        )

        # 6. notesInfo — key structure of one item from each server's own collection
        _check_info_shape(
            report,
            "notesInfo",
            LIVE_URL,
            OUR_URL,
            id_action="findNotes",
            id_param="notes",
            id_query_params={"query": "*"},
        )

        # 7. cardsInfo — key structure of one item from each server's own collection
        _check_info_shape(
            report,
            "cardsInfo",
            LIVE_URL,
            OUR_URL,
            id_action="findCards",
            id_param="cards",
            id_query_params={"query": "*"},
        )

        return report.summary()


# ---------------------------------------------------------------------------
# pytest marker helper
# ---------------------------------------------------------------------------


def parity_available() -> bool:
    """Return True if anki-headless is reachable (for conditional pytest use)."""
    return _is_port_open(LIVE_HOST, LIVE_PORT)


if __name__ == "__main__":
    success = run_parity()
    sys.exit(0 if success else 1)
