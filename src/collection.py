"""
Anki Collection lifecycle manager — module-level singleton.

Responsibilities (Step 4):
  - Open and hold a single ``anki.Collection`` instance for the process lifetime.
  - Acquire an exclusive ``fcntl`` advisory lock on a sidecar lockfile the moment
    the collection is opened so that a second server process (or a stray Anki
    Desktop instance) fails immediately instead of silently corrupting SQLite.
  - Expose ``CollectionManager.col`` (property) and the module-level ``get_col()``
    convenience function that other modules import.
  - Expose ``_col_lock`` (a ``threading.RLock``) that callers MUST use to serialise
    all collection operations.  The server runs with ``waitress threads=2`` so that
    ``GET /health`` can always be served on the second thread while a long sync or
    write holds the lock on the first thread.
  - Provide ``health()`` for the ``GET /health`` endpoint (Step 5).

Environment variables
---------------------
ANKI_COLLECTION_PATH
    Absolute path to ``collection.anki2``.
    Default: ``/config/.local/share/Anki2/User 1/collection.anki2``
    (the in-container path used in production).

Thread-safety note
------------------
waitress is started with ``threads=2``.  **ALL** collection access (every
dispatched action) is serialised through ``_col_lock`` so only one collection
operation runs at a time — the second thread exists *solely* so that
``GET /health`` can respond while a long-running sync holds the lock on the
first thread.

``health()`` is deliberately lock-free (non-blocking acquire):
  - If the lock is free, it acquires it, runs a lightweight ``SELECT 1`` probe,
    and returns counts.
  - If the lock is held (sync or write in progress), it returns immediately with
    ``{"status": "ok", "syncing": true, ...}`` — no blocking, no timeout risk.

This ensures Docker's ``HEALTHCHECK`` (5-second timeout) is never tripped by a
multi-second sync operation.

Single-writer guarantee is preserved: the extra thread only serves /health;
it never performs collection writes or reads (other than the non-blocking
lock-free check of ``self._collection is not None``).

    import src.collection as col_mod

    with col_mod._col_lock:
        result = col_mod.get_col().find_cards(...)

No Qt imports are present anywhere in this module.

API names used (confirmed against anki 25.9.2 in docs/spike-findings.md):
  - ``Collection(path)`` — opens and returns a collection handle
  - ``col.close()``      — flushes WAL and closes the DB handle
  - ``col.card_count()`` — integer count of all cards
  - ``col.note_count()`` — integer count of all notes
  - ``col.db.scalar("select 1")`` — liveness probe
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default collection path (in-container production location)
# ---------------------------------------------------------------------------

_DEFAULT_COLLECTION_PATH = "/config/.local/share/Anki2/User 1/collection.anki2"

# ---------------------------------------------------------------------------
# Public threading lock
# ---------------------------------------------------------------------------
# Serialise ALL collection access.  With waitress threads=2 this is the only
# thing preventing concurrent collection operations — it MUST be acquired by
# every handler before touching the collection.
#
# RLock (reentrant) rather than Lock: the server dispatch layer acquires this
# lock around every handler call for defense-in-depth; individual handlers
# (in actions.py, review_session.py, etc.) also acquire it for multi-step
# mutation sequences.  An RLock allows the SAME thread to re-acquire it
# without deadlocking — a plain Lock would block forever on the inner
# acquisition.
#
# health() uses acquire(blocking=False) so it never waits on a long sync.
_col_lock: threading.RLock = threading.RLock()


# ---------------------------------------------------------------------------
# CollectionManager
# ---------------------------------------------------------------------------


class CollectionManager:
    """Lifecycle manager for a single ``anki.Collection`` instance.

    Instantiate once at module level (see ``manager`` below).  All public
    entry points (``open``, ``close``, ``col``, ``health``) delegate to that
    singleton.
    """

    def __init__(self) -> None:
        self._collection: Any = None
        self._lock_fd: int | None = None
        self._lock_path: Path | None = None

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open(self, path: str | os.PathLike[str] | None = None) -> None:
        """Open the Anki collection and acquire the process-lifetime fcntl lock.

        Parameters
        ----------
        path:
            Path to ``collection.anki2``.  If *None* (the normal case) the
            value is taken from the ``ANKI_COLLECTION_PATH`` environment
            variable, falling back to the in-container default
            ``/config/.local/share/Anki2/User 1/collection.anki2``.

        Raises
        ------
        RuntimeError
            If called a second time while the collection is already open.
        RuntimeError
            If the fcntl lock cannot be acquired (another process holds it).
        FileNotFoundError
            If the resolved path does not exist.
        """
        if self._collection is not None:
            raise RuntimeError("Collection is already open — call close() first.")

        resolved = Path(
            path
            if path is not None
            else os.environ.get("ANKI_COLLECTION_PATH", _DEFAULT_COLLECTION_PATH)
        )

        if not resolved.exists():
            raise FileNotFoundError(f"Collection file not found: {resolved}")

        # --- fcntl advisory lock ----------------------------------------
        # Use a sidecar file so we never write to the .anki2 file itself.
        lock_path = resolved.parent / (resolved.name + ".server.lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError(
                "Collection already locked by another process — refusing to "
                "start. "
                f"Lockfile: {lock_path}. "
                "SQLite is single-writer; two server instances cannot share "
                "the same collection simultaneously. Stop the other process "
                "before retrying."
            ) from None

        self._lock_fd = fd
        self._lock_path = lock_path
        log.info("fcntl lock acquired: %s", lock_path)

        # --- Open collection --------------------------------------------
        # Import lazily so that module-level import of collection.py does
        # not require anki to be installed (useful for tests that mock it).
        # Both the import and the constructor are wrapped in the same
        # try/except so that ANY exception (ModuleNotFoundError, schema
        # mismatch, corrupt file, …) releases the lock before propagating.
        try:
            from anki.collection import Collection  # noqa: PLC0415

            log.info("Opening collection: %s", resolved)
            self._collection = Collection(str(resolved))
        except Exception:
            # Release the lock before propagating so that the caller can
            # fix the problem and retry open() in the same process.
            log.exception(
                "Collection() raised during open — releasing fcntl lock: %s",
                lock_path,
            )
            self.close()
            raise
        log.info("Collection opened successfully.")

    def close(self) -> None:
        """Close the collection and release the fcntl lock.  Idempotent."""
        if self._collection is not None:
            try:
                self._collection.close()
                log.info("Collection closed.")
            except Exception:  # noqa: BLE001
                log.exception("Error closing collection (ignored).")
            finally:
                self._collection = None

        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                if self._lock_path is not None:
                    self._lock_path.unlink(missing_ok=True)
                log.info("fcntl lock released: %s", self._lock_path)
            except Exception:  # noqa: BLE001
                log.exception("Error releasing fcntl lock (ignored).")
            finally:
                self._lock_fd = None
                self._lock_path = None

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def col(self) -> Any:
        """The open ``anki.Collection`` instance.

        Raises
        ------
        RuntimeError
            If :meth:`open` has not been called yet.
        """
        if self._collection is None:
            raise RuntimeError(
                "Collection is not open. Call CollectionManager.open() "
                "before accessing .col."
            )
        return self._collection

    def save(self) -> None:
        """Flush any pending changes to the collection.

        The Anki collection auto-saves on ``close()``, and the backend
        flushes periodically, so calling this is optional.  It is exposed for
        cases where an explicit checkpoint is desirable (e.g. before a sync
        operation).

        Raises
        ------
        RuntimeError
            If the collection is not open.
        """
        self.col.save()
        log.debug("Collection saved.")

    def health(self) -> dict[str, Any]:
        """Return a health-check dict for the ``GET /health`` endpoint.

        Designed to NEVER block, so it cannot trip Docker's 5-second
        HEALTHCHECK timeout even when a long sync is in progress.

        Strategy
        --------
        1. Check ``self._collection is not None`` WITHOUT the lock.  If the
           collection is not open, raise ``RuntimeError`` immediately (the
           caller maps this to a 503 response).

        2. Attempt a NON-BLOCKING lock acquire
           (``_col_lock.acquire(blocking=False)``).

           - **Lock free** (normal case): acquire succeeds → run a lightweight
             ``SELECT 1`` liveness probe + fetch counts → release the lock →
             return ``{"status": "ok", "card_count": …, "note_count": …, …}``.

           - **Lock held** (sync / write in progress on the other thread):
             return IMMEDIATELY ``{"status": "ok", "syncing": true, …}``
             WITHOUT the counts and WITHOUT blocking.  The collection is alive
             (step 1 confirmed it); we simply cannot safely read counts right
             now.

        Returns
        -------
        dict
            Normal: ``{"status": "ok", "collection_path": str,
            "card_count": int, "note_count": int}``

            Busy:   ``{"status": "ok", "syncing": True, "collection_path": str}``

        Raises
        ------
        RuntimeError
            If the collection is not open (caller returns 503).
        Exception
            If the ``SELECT 1`` probe fails (collection handle is broken).
        """
        # Step 1: lock-free None check.  If the collection is not open there
        # is nothing to probe; raise immediately (server.py maps to 503).
        if self._collection is None:
            raise RuntimeError(
                "Collection is not open. Call CollectionManager.open() "
                "before accessing health()."
            )

        col_path = (
            str(self._lock_path.parent / "collection.anki2")
            if self._lock_path
            else os.environ.get("ANKI_COLLECTION_PATH", _DEFAULT_COLLECTION_PATH)
        )

        # Step 2: non-blocking lock acquire.
        acquired = _col_lock.acquire(blocking=False)
        if not acquired:
            # A sync or write is in progress on the other thread.  Return
            # immediately — do NOT block waiting for the lock.
            log.debug(
                "health(): _col_lock held by another thread, returning syncing=true"
            )
            return {
                "status": "ok",
                "syncing": True,
                "collection_path": col_path,
            }

        try:
            # Lock is ours.  Run the lightweight DB probe and fetch counts.
            c = self._collection
            # Confirms the SQLite file descriptor is still alive.
            c.db.scalar("select 1")
            return {
                "status": "ok",
                "collection_path": col_path,
                "card_count": c.card_count(),
                "note_count": c.note_count(),
            }
        finally:
            _col_lock.release()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

manager: CollectionManager = CollectionManager()

# Convenience re-exports so callers can write:
#   from src.collection import get_col
#   col = get_col()


def get_col() -> Any:
    """Return the open ``anki.Collection`` instance from the module singleton.

    Raises
    ------
    RuntimeError
        If the collection has not been opened via ``manager.open()``.
    """
    return manager.col


# ---------------------------------------------------------------------------
# Self-test (run directly: python -m src.collection)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    BACKUP = Path(
        "/mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2"
    )
    TMP_DIR = Path("/tmp/acs-step4")

    print("=== CollectionManager self-test ===\n")

    # ---- Setup ---------------------------------------------------------
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    test_col = TMP_DIR / "collection.anki2"
    shutil.copy2(BACKUP, test_col)
    print(f"Copied backup to {test_col}")

    # ---- 1. Normal open + health() ------------------------------------
    print("\n[1] manager.open() ...")
    manager.open(test_col)
    h = manager.health()
    print(f"    health() → {h}")
    assert h["status"] == "ok", f"Expected ok, got {h['status']}"
    assert h["card_count"] > 0, "Expected non-zero card count"
    assert h["note_count"] > 0, "Expected non-zero note count"
    print("    PASS: health() OK")

    # ---- 2. Second open must fail immediately (lock guard) ------------
    print("\n[2] Second manager.open() must raise RuntimeError ...")
    try:
        manager.open(test_col)
        print("    FAIL: no exception raised — lock not working!")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"    Got expected RuntimeError: {exc}")
        print("    PASS: already-open guard works")

    # ---- 3. Cross-process lock: spawn a child process that tries to
    #         open the SAME collection while we hold the lock ----------
    print("\n[3] Cross-process lock: child process attempts concurrent open ...")
    import subprocess  # noqa: PLC0415

    child_script = f"""
import sys
sys.path.insert(0, '/home/david/anki-collection-server')
from src.collection import CollectionManager
m2 = CollectionManager()
try:
    m2.open('{test_col}')
    print('FAIL: no exception — lock not working!')
    sys.exit(1)
except RuntimeError as e:
    print(f'Got expected RuntimeError: {{e}}')
    print('PASS: cross-process fcntl lock works')
    sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
    )
    print(f"    child stdout: {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"    child stderr: {result.stderr.strip()}")
        print("    FAIL: child process did not exit cleanly")
        sys.exit(1)
    print("    PASS: cross-process lock guard confirmed")

    # ---- 4. Close and re-open must succeed ---------------------------
    print("\n[4] manager.close() then re-open ...")
    manager.close()
    manager.open(test_col)
    h2 = manager.health()
    print(f"    health() after re-open → {h2}")
    assert h2["status"] == "ok", f"Expected ok after re-open, got {h2['status']}"
    print("    PASS: re-open after close succeeds")

    # ---- 5. Idempotent close -----------------------------------------
    print("\n[5] Idempotent close (call twice) ...")
    manager.close()
    manager.close()
    print("    PASS: double close is idempotent")

    # ---- 6. col accessor raises when closed --------------------------
    print("\n[6] manager.col raises when closed ...")
    try:
        _ = manager.col
        print("    FAIL: no exception raised")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"    Got expected RuntimeError: {exc}")
        print("    PASS: col accessor raises when not open")

    # ---- Cleanup -------------------------------------------------------
    shutil.rmtree(TMP_DIR)
    print(f"\nCleaned up {TMP_DIR}")

    print("\n=== All self-test assertions passed ===")
