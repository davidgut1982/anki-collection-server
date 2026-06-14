"""
Integration tests for src/sync.py.

All tests operate against a COPY of the static backup collection placed
in /tmp and a DISPOSABLE anki-sync container on a throwaway port (27798).
The production anki-sync container (port 27701, /mnt/data/apps/anki-sync)
is NEVER touched.

Sync server image: ghcr.io/luckyturtledev/anki:latest
    - Listens on port 8080 inside the container.
    - Credentials configured via ``SYNC_USER1=username:password`` env var.
    - Data stored in ``/data`` (mapped to a throwaway /tmp volume).

Test plan
---------
1. Start a disposable sync container on port 27798 (→ container port 8080).
2. Open a /tmp copy of the backup collection.
3. First ``do_sync()`` call:
       required == FULL_UPLOAD (4) because the server is empty.
       do_sync resolves it by uploading → uploaded == True.
4. Second ``do_sync()`` call:
       required == NO_CHANGES (0) or NORMAL_SYNC (1) — already in sync.
       uploaded may be False (NO_CHANGES) or True (NORMAL_SYNC exchange).
5. ``do_sync(force_upload=True)``:
       Skips sync_collection(); required == -1; uploaded == True.
6. Stop and remove the container and /tmp data dir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

import src.collection as col_mod
from src.sync import do_sync

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"
_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))

# Disposable sync server — completely separate from production.
_SYNC_IMAGE = "ghcr.io/luckyturtledev/anki:latest"
_SYNC_CONTAINER = "anki-sync-test-disposable"
_HOST_PORT = 27798  # throwaway; never clashes with prod (27701)
_CONTAINER_PORT = 8080  # luckyturtledev image listens on 8080
_TEST_USER = "testuser"
_TEST_PASS = "testpassword"
_SYNC_ENDPOINT = f"http://localhost:{_HOST_PORT}"


# ---------------------------------------------------------------------------
# Module-scoped fixture: disposable sync server + collection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sync_env(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Start a disposable sync container and open a /tmp collection copy.

    Yields after everything is ready.  Tears down the container and all
    /tmp artefacts on exit.
    """
    if not BACKUP.exists():
        pytest.fail(
            f"Backup collection not found: {BACKUP}\n"
            f"Set ANKI_TEST_BACKUP=/path/to/collection.anki2 to override."
        )

    # ------------------------------------------------------------------ #
    # 1. Prepare /tmp directories                                          #
    # ------------------------------------------------------------------ #
    col_tmpdir = Path(tempfile.mkdtemp(prefix="acs-sync-col-", dir="/tmp"))
    sync_datadir = Path(tempfile.mkdtemp(prefix="acs-sync-data-", dir="/tmp"))

    col_path = col_tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)

    # ------------------------------------------------------------------ #
    # 2. Start disposable sync container                                   #
    # ------------------------------------------------------------------ #
    # Stop/remove any stale container with the same name.
    subprocess.run(
        ["docker", "rm", "-f", _SYNC_CONTAINER],
        capture_output=True,
    )

    proc = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            _SYNC_CONTAINER,
            "-p",
            f"{_HOST_PORT}:{_CONTAINER_PORT}",
            "-e",
            f"SYNC_USER1={_TEST_USER}:{_TEST_PASS}",
            "-v",
            f"{sync_datadir}:/data",
            _SYNC_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = proc.stdout.strip()
    print(f"\n[sync_env] Started container {container_id[:12]} on port {_HOST_PORT}")

    # Wait for the server to be ready (it starts quickly but give it 5 s).
    _wait_for_sync_server(_SYNC_ENDPOINT, timeout=10)

    # ------------------------------------------------------------------ #
    # 3. Open collection and inject credentials into env                   #
    # ------------------------------------------------------------------ #
    mgr = col_mod.CollectionManager()
    mgr.open(col_path)

    _orig_manager = col_mod.manager
    col_mod.manager = mgr  # type: ignore[assignment]

    # Inject credentials as env vars (do_sync reads these).
    os.environ["ANKI_SYNC_USERNAME"] = _TEST_USER
    os.environ["ANKI_SYNC_PASSWORD"] = _TEST_PASS
    os.environ["ANKI_SYNC_ENDPOINT"] = _SYNC_ENDPOINT

    yield

    # ------------------------------------------------------------------ #
    # 4. Teardown                                                          #
    # ------------------------------------------------------------------ #
    col_mod.manager = _orig_manager  # type: ignore[assignment]
    mgr.close()

    # Remove env vars we set.
    for key in ("ANKI_SYNC_USERNAME", "ANKI_SYNC_PASSWORD", "ANKI_SYNC_ENDPOINT"):
        os.environ.pop(key, None)

    # Stop and remove the disposable container.
    subprocess.run(["docker", "rm", "-f", _SYNC_CONTAINER], capture_output=True)
    print(f"\n[sync_env] Container {_SYNC_CONTAINER} removed.")

    # Remove /tmp directories.
    shutil.rmtree(col_tmpdir, ignore_errors=True)
    shutil.rmtree(sync_datadir, ignore_errors=True)
    print("[sync_env] Temp directories cleaned up.")


def _wait_for_sync_server(endpoint: str, timeout: float = 10.0) -> None:
    """Poll the sync server until it responds or the timeout expires."""
    import urllib.request  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # The sync server returns a non-200 for a bare GET / but at least
            # it responds without a connection-refused error.
            urllib.request.urlopen(endpoint, timeout=1)
            return
        except urllib.error.HTTPError:
            # Any HTTP response (even 404) means the server is up.
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.3)

    raise RuntimeError(
        f"Sync server at {endpoint} did not respond within {timeout}s: {last_err}"
    )


# ---------------------------------------------------------------------------
# Tests (ordered — each builds on the state of the previous)
# ---------------------------------------------------------------------------


class TestSyncFirstRun:
    def test_first_sync_requires_full_upload(self, sync_env: None) -> None:
        """First sync against an empty server should require FULL_UPLOAD (4)
        and do_sync should resolve it by uploading.
        """
        result = do_sync(force_upload=False, sync_media=False)

        print(
            f"\n[first sync] required={result['required']} "
            f"uploaded={result.get('uploaded', result.get('synced'))} "
            f"message={result['message']!r}"
        )

        # Server was empty → FULL_UPLOAD (4) or already resolved to upload.
        assert result["required"] == 4, (
            f"Expected required=4 (FULL_UPLOAD) on first sync, got {result['required']}"
        )
        assert result["uploaded"] is True, "Expected uploaded=True on first sync"


class TestSyncSecondRun:
    def test_second_sync_no_changes_or_normal(self, sync_env: None) -> None:
        """Second sync should find no new changes — server is already up to date.

        NORMAL_SYNC (1) returns ``synced=True`` (bidirectional exchange).
        NO_CHANGES (0) returns ``uploaded=False``.
        """
        result = do_sync(force_upload=False, sync_media=False)

        print(
            f"\n[second sync] required={result['required']} "
            f"synced={result.get('synced')} uploaded={result.get('uploaded')} "
            f"message={result['message']!r}"
        )

        # After a full upload the server is in sync.  Anki may report
        # NO_CHANGES (0) or NORMAL_SYNC (1) depending on whether it detects
        # a trivial empty delta.  Both are acceptable.
        assert result["required"] in (0, 1), (
            f"Expected required in (0=NO_CHANGES, 1=NORMAL_SYNC) after full upload, "
            f"got {result['required']}"
        )
        # For NORMAL_SYNC the return key is 'synced'; for NO_CHANGES it's 'uploaded'.
        if result["required"] == 1:
            assert result.get("synced") is True, (
                "NORMAL_SYNC path must set synced=True (bidirectional merge)"
            )
        else:
            assert result.get("uploaded") is False, (
                "NO_CHANGES path must set uploaded=False"
            )


class TestForceUpload:
    def test_force_upload_skips_probe(self, sync_env: None) -> None:
        """force_upload=True should skip sync_collection and set required=-1."""
        result = do_sync(force_upload=True, sync_media=False)

        print(
            f"\n[force_upload] required={result['required']} "
            f"uploaded={result['uploaded']} "
            f"message={result['message']!r}"
        )

        assert result["required"] == -1, (
            f"Expected required=-1 (force-upload skips probe), got {result['required']}"
        )
        assert result["uploaded"] is True, "Expected uploaded=True on force upload"
