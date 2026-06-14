"""Step 4b regression: flock must be released when Collection() fails to open.

Scenario
--------
1. `manager.open(corrupt_path)` → Collection() raises (corrupt/empty file).
   - The exception must propagate to the caller.
   - The fcntl lock must be released and self._lock_fd reset to None.

2. Immediately afterwards, `manager.open(valid_path)` → must succeed in the
   SAME process (proves the lock was actually freed).

The valid collection is a pre-copied readable version of the production backup
at /tmp/acs-valid-test.anki2 (created via ``sudo cp`` before running the test
because the original backup is owned by a different uid).
The corrupt file is a deliberately empty file (zero bytes) that anki will
refuse to open.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Pre-copied readable backup (sudo cp + chmod 644 done before test run)
READABLE_BACKUP = Path("/tmp/acs-valid-test.anki2")

TMP_DIR = Path("/tmp/acs-step4b")
VALID_COL = TMP_DIR / "valid" / "collection.anki2"
CORRUPT_COL = TMP_DIR / "corrupt" / "collection.anki2"


# ---------------------------------------------------------------------------
# Session-scoped setup / teardown
# ---------------------------------------------------------------------------


def _setup_tmp() -> None:
    """Prepare /tmp/acs-step4b with a valid copy and an empty (corrupt) file."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    valid_dir = TMP_DIR / "valid"
    valid_dir.mkdir(exist_ok=True)
    shutil.copy2(READABLE_BACKUP, VALID_COL)

    corrupt_dir = TMP_DIR / "corrupt"
    corrupt_dir.mkdir(exist_ok=True)
    # Write garbage bytes — anki raises DBError("file is not a database")
    # because the SQLite magic header check fails.
    # (An empty file is treated as a new collection and opens successfully.)
    CORRUPT_COL.write_bytes(b"THIS IS NOT A VALID SQLITE DATABASE FILE\x00\xff")


def _teardown_tmp() -> None:
    shutil.rmtree(TMP_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def tmp_collections():
    """Create test files before the module runs; clean up afterwards."""
    if not READABLE_BACKUP.exists():
        pytest.skip(
            f"Readable backup not found: {READABLE_BACKUP}. "
            "Run: sudo cp /mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2 "
            "/tmp/acs-valid-test.anki2 && sudo chmod 644 /tmp/acs-valid-test.anki2"
        )
    _setup_tmp()
    yield
    _teardown_tmp()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_lock_released_after_failed_open_then_valid_open_succeeds() -> None:
    """Opening a corrupt file releases the lock; the next open() must succeed."""
    # Re-create a fresh CollectionManager (independent of the module singleton)
    # so this test is self-contained and does not disturb other tests.
    from src.collection import CollectionManager

    mgr = CollectionManager()

    # --- Step 1: open() on corrupt file must raise -----------------------
    print(f"\n[step 1] Opening corrupt file: {CORRUPT_COL}", flush=True)
    with pytest.raises(Exception) as exc_info:
        mgr.open(CORRUPT_COL)

    print(
        f"[step 1] Got expected exception: {type(exc_info.value).__name__}: {exc_info.value}",
        flush=True,
    )

    # --- Step 2: verify internal state is fully reset --------------------
    assert mgr._collection is None, "mgr._collection must be None after failed open"
    assert mgr._lock_fd is None, (
        "mgr._lock_fd must be None — flock should have been released"
    )
    assert mgr._lock_path is None, "mgr._lock_path must be None after failed open"
    print(
        "[step 2] Internal state reset confirmed (lock_fd=None, lock_path=None)",
        flush=True,
    )

    # --- Step 3: lockfile must not exist (or not be locked) --------------
    # The sidecar lockfile for the corrupt collection.
    lock_file = CORRUPT_COL.parent / (CORRUPT_COL.name + ".server.lock")
    # Either the file is gone or we can acquire an exclusive lock on it
    # ourselves (proving no one holds it).
    if lock_file.exists():
        import fcntl as _fcntl

        probe_fd = os.open(str(lock_file), os.O_RDWR)
        try:
            # This would raise BlockingIOError if the lock were still held.
            _fcntl.flock(probe_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _fcntl.flock(probe_fd, _fcntl.LOCK_UN)
            print(
                f"[step 3] Lockfile exists but is NOT held (flock probe passed): {lock_file}",
                flush=True,
            )
        finally:
            os.close(probe_fd)
    else:
        print(f"[step 3] Lockfile absent (already cleaned up): {lock_file}", flush=True)

    # --- Step 4: open() against a VALID copy must succeed ----------------
    print(f"\n[step 4] Opening valid collection: {VALID_COL}", flush=True)
    mgr.open(VALID_COL)
    try:
        h = mgr.health()
        print(f"[step 4] health() -> {h}", flush=True)
        assert h["status"] == "ok"
        assert h["card_count"] > 0, "Expected non-zero card count from valid backup"
        assert h["note_count"] > 0, "Expected non-zero note count from valid backup"
        print("[step 4] PASS: valid open succeeded after prior failed open", flush=True)
    finally:
        mgr.close()

    print("\nAll assertions passed — lock-release-on-failed-open verified.", flush=True)
