"""
Tests for lock-free GET /health behaviour (Critic HIGH fix).

Scenario
--------
With waitress threads=2, the second thread serves /health while the first
thread holds _col_lock for a long sync or write operation.

CollectionManager.health() must:
  1. Return immediately with {"status": "ok", "syncing": true, ...} when
     _col_lock is held — never block.
  2. Return {"status": "ok", "card_count": ..., "note_count": ..., ...}
     with a SELECT 1 liveness probe when _col_lock is free (normal path).
  3. Raise RuntimeError (→ 503) when _collection is None (collection not open).

The tests below use unittest.mock to avoid needing a live Anki collection,
so they run in any environment.  The threading test simulates lock contention
by holding _col_lock on a background thread while calling health() from the
main thread.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import src.collection as col_mod
from src.collection import CollectionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_collection() -> MagicMock:
    """Return a MagicMock that looks like an open anki.Collection."""
    mock_col = MagicMock()
    mock_col.db.scalar.return_value = 1
    mock_col.card_count.return_value = 42
    mock_col.note_count.return_value = 17
    return mock_col


# ---------------------------------------------------------------------------
# 1. health() raises RuntimeError when _collection is None
# ---------------------------------------------------------------------------


def test_health_raises_when_collection_not_open() -> None:
    """health() raises RuntimeError immediately when the collection is not open."""
    mgr = CollectionManager()
    assert mgr._collection is None

    with pytest.raises(RuntimeError, match="not open"):
        mgr.health()


# ---------------------------------------------------------------------------
# 2. health() normal path: lock free, returns counts
# ---------------------------------------------------------------------------


def test_health_returns_counts_when_lock_free() -> None:
    """health() acquires _col_lock, runs SELECT 1, returns card/note counts."""
    mgr = CollectionManager()
    mock_col = _make_mock_collection()

    # Inject a fake open collection and a fake lock path
    mgr._collection = mock_col
    from pathlib import Path

    mgr._lock_path = Path("/tmp/test_health/collection.anki2.server.lock")

    result = mgr.health()

    assert result["status"] == "ok"
    assert result["card_count"] == 42
    assert result["note_count"] == 17
    assert "collection_path" in result
    assert "syncing" not in result
    mock_col.db.scalar.assert_called_once_with("select 1")


# ---------------------------------------------------------------------------
# 3. health() busy path: lock held → immediate syncing:true, no blocking
# ---------------------------------------------------------------------------


def test_health_returns_syncing_when_lock_held() -> None:
    """health() returns immediately with syncing=true when _col_lock is held.

    This is the Critic HIGH fix: even under lock contention, /health must
    respond within milliseconds — never block waiting for a long sync.
    """
    mgr = CollectionManager()
    mock_col = _make_mock_collection()
    mgr._collection = mock_col
    from pathlib import Path

    mgr._lock_path = Path("/tmp/test_health/collection.anki2.server.lock")

    # Acquire the module-level _col_lock from this thread, simulating
    # a sync or write running on the first waitress worker thread.
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock() -> None:
        with col_mod._col_lock:
            lock_acquired.set()
            # Hold the lock until the main thread signals us to release.
            release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()

    # Wait until the background thread has the lock.
    lock_acquired.wait(timeout=2.0)
    assert lock_acquired.is_set(), "Background thread failed to acquire _col_lock"

    try:
        start = time.monotonic()
        result = mgr.health()
        elapsed = time.monotonic() - start
    finally:
        release_lock.set()
        holder.join(timeout=2.0)

    # Must have returned immediately — well under 1 second.
    assert elapsed < 1.0, (
        f"health() blocked for {elapsed:.3f}s — should have returned immediately"
    )

    assert result["status"] == "ok"
    assert result["syncing"] is True
    assert "collection_path" in result
    # Counts must NOT be present (we couldn't safely read them).
    assert "card_count" not in result
    assert "note_count" not in result
    # The DB probe must NOT have been called (we never held the lock).
    mock_col.db.scalar.assert_not_called()


# ---------------------------------------------------------------------------
# 4. health() busy path timing: must respond in << 1s
# ---------------------------------------------------------------------------


def test_health_responds_under_1s_while_lock_held_for_3s() -> None:
    """Simulates a 3-second sync; /health must respond in << 1 second."""
    mgr = CollectionManager()
    mock_col = _make_mock_collection()
    mgr._collection = mock_col
    from pathlib import Path

    mgr._lock_path = Path("/tmp/test_health/collection.anki2.server.lock")

    health_results: list[dict[str, Any]] = []
    health_elapsed: list[float] = []
    health_done = threading.Event()

    def run_health_from_second_thread() -> None:
        start = time.monotonic()
        result = mgr.health()
        elapsed = time.monotonic() - start
        health_results.append(result)
        health_elapsed.append(elapsed)
        health_done.set()

    lock_started = threading.Event()

    def hold_lock_3s() -> None:
        with col_mod._col_lock:
            lock_started.set()
            # Simulate a 3-second sync network round-trip.
            time.sleep(3.0)

    # Start the simulated sync (holds lock for 3 seconds).
    sync_thread = threading.Thread(target=hold_lock_3s, daemon=True)
    sync_thread.start()

    # Wait for the lock to be held before calling health.
    lock_started.wait(timeout=2.0)
    assert lock_started.is_set()

    # Call health from a second thread (simulates the second waitress worker).
    health_thread = threading.Thread(target=run_health_from_second_thread, daemon=True)
    health_thread.start()

    # health must finish well before the 3-second sync completes.
    health_done.wait(timeout=2.0)

    sync_thread.join(timeout=5.0)
    health_thread.join(timeout=2.0)

    assert health_done.is_set(), "health() never returned — it blocked!"
    assert len(health_results) == 1
    assert health_elapsed[0] < 1.0, (
        f"health() took {health_elapsed[0]:.3f}s — must be << 1s even during a 3s sync"
    )
    result = health_results[0]
    assert result["status"] == "ok"
    assert result.get("syncing") is True


# ---------------------------------------------------------------------------
# 5. Writes serialised: only one collection op at a time
# ---------------------------------------------------------------------------


def test_concurrent_writes_serialised_by_col_lock() -> None:
    """Confirm that _col_lock serialises concurrent dispatch calls.

    Two threads each try to acquire _col_lock simultaneously.  Only one
    should hold it at a time — the critical section must never overlap.
    """
    concurrent_overlap = False
    inside_count = 0
    lock = threading.Lock()  # Guards inside_count

    def _op() -> None:
        nonlocal concurrent_overlap, inside_count
        with col_mod._col_lock:
            with lock:
                inside_count += 1
                if inside_count > 1:
                    concurrent_overlap = True
            time.sleep(0.05)  # Simulate brief collection work
            with lock:
                inside_count -= 1

    threads = [threading.Thread(target=_op) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not concurrent_overlap, (
        "_col_lock failed to serialise: two threads overlapped inside the critical section"
    )


# ---------------------------------------------------------------------------
# 6. Flask /health endpoint integration: 200 OK on both paths
# ---------------------------------------------------------------------------
#
# waitress is not installed in the test environment (it runs inside Docker in
# prod).  We patch the import at sys.modules level before importing src.server
# so Flask routes are importable without waitress being present.
# ---------------------------------------------------------------------------


def _get_flask_app() -> Any:
    """Import src.server.app, mocking waitress so it can be imported in tests."""
    import sys
    from unittest.mock import MagicMock

    if "waitress" not in sys.modules:
        sys.modules["waitress"] = MagicMock()
    if "src.server" not in sys.modules:
        import importlib

        import src.server  # noqa: F401

        importlib.invalidate_caches()

    from src.server import app  # noqa: PLC0415

    return app


def test_flask_health_endpoint_ok_with_counts() -> None:
    """GET /health returns 200 + card/note counts when collection is healthy."""
    from pathlib import Path

    app = _get_flask_app()
    mock_col = _make_mock_collection()

    with patch.object(col_mod.manager, "_collection", mock_col):
        with patch.object(
            col_mod.manager,
            "_lock_path",
            Path("/tmp/fake/collection.anki2.server.lock"),
        ):
            client = app.test_client()
            resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["card_count"] == 42
    assert data["note_count"] == 17
    assert "syncing" not in data


def test_flask_health_endpoint_200_syncing_when_lock_held() -> None:
    """GET /health returns 200 syncing=true when lock is held by another thread."""
    from pathlib import Path

    app = _get_flask_app()
    mock_col = _make_mock_collection()

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock() -> None:
        with col_mod._col_lock:
            lock_acquired.set()
            release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    lock_acquired.wait(timeout=2.0)
    assert lock_acquired.is_set()

    try:
        with patch.object(col_mod.manager, "_collection", mock_col):
            with patch.object(
                col_mod.manager,
                "_lock_path",
                Path("/tmp/fake/collection.anki2.server.lock"),
            ):
                client = app.test_client()
                start = time.monotonic()
                resp = client.get("/health")
                elapsed = time.monotonic() - start
    finally:
        release_lock.set()
        holder.join(timeout=2.0)

    assert resp.status_code == 200
    assert elapsed < 1.0, f"Flask /health blocked for {elapsed:.3f}s"
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["syncing"] is True


def test_flask_health_endpoint_503_when_collection_not_open() -> None:
    """GET /health returns 503 when collection is not open."""
    app = _get_flask_app()

    with patch.object(col_mod.manager, "_collection", None):
        client = app.test_client()
        resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "unavailable"
