"""
Integration tests for src/fsrs.py.

All tests operate against a COPY of the static backup collection placed
in /tmp — the live collection is never opened or modified.

Backup:
    /mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2
    (read-only; copied to /tmp for the test session).

Test plan
---------
1. ``is_fsrs_enabled()`` returns *False* on a fresh backup.
2. ``enable_fsrs(optimize=True)`` returns enabled=True, optimized=True,
   num_params==21, health_check_passed==True.
3. ``is_fsrs_enabled()`` returns *True* after step 2.
4. Second call to ``enable_fsrs(optimize=True)`` succeeds (idempotent).
5. ``enable_fsrs(optimize=False)`` works (no optimizer run).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest

import src.collection as col_mod
from src.fsrs import enable_fsrs, is_fsrs_enabled

# ---------------------------------------------------------------------------
# Backup path (read-only source — never modified)
# ---------------------------------------------------------------------------

_DEFAULT_BACKUP = Path(
    "/mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2"
)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", str(_DEFAULT_BACKUP)))


# ---------------------------------------------------------------------------
# Session-scoped fixture: copy backup once, open collection, yield, close
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def col(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Open a fresh copy of the backup collection for the FSRS test module.

    Uses a module-scoped tmp dir so the collection is opened once for all
    tests in this file.  The CollectionManager singleton is reset at teardown.
    """
    if not BACKUP.exists():
        pytest.fail(
            f"Backup collection not found: {BACKUP}\n"
            f"Set ANKI_TEST_BACKUP=/path/to/collection.anki2 to override."
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="acs-fsrs-test-", dir="/tmp"))
    col_path = tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)

    # Use a fresh manager so we don't interfere with other test modules.
    mgr = col_mod.CollectionManager()
    mgr.open(col_path)

    # Monkey-patch the module singleton so src.fsrs.get_col() sees our copy.
    _orig_manager = col_mod.manager
    col_mod.manager = mgr  # type: ignore[assignment]

    yield

    # Teardown
    col_mod.manager = _orig_manager  # type: ignore[assignment]
    mgr.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIsFsrsEnabled:
    def test_disabled_on_fresh_backup(self, col: None) -> None:
        """FSRS should be off on the unmodified backup collection."""
        result = is_fsrs_enabled()
        print(f"\n[is_fsrs_enabled] fresh backup → {result}")
        assert result is False, "Expected FSRS to be disabled on fresh backup"


class TestEnableFsrs:
    def test_enable_with_optimize(self, col: None) -> None:
        """enable_fsrs(optimize=True) should enable FSRS and return 21 params."""
        result = enable_fsrs(optimize=True)

        print(
            f"\n[enable_fsrs] result: "
            f"enabled={result['enabled']} "
            f"optimized={result['optimized']} "
            f"num_params={result['num_params']} "
            f"fsrs_items={result['fsrs_items']} "
            f"health_check_passed={result['health_check_passed']}"
        )

        assert result["enabled"] is True, "enabled should be True"
        assert result["optimized"] is True, "optimized should be True"
        assert result["num_params"] == 21, (
            f"Expected 21 FSRS-6 params, got {result['num_params']}"
        )
        assert result["fsrs_items"] > 0, (
            f"Expected >0 fsrs_items, got {result['fsrs_items']}"
        )
        assert result["health_check_passed"] is True, (
            "health_check_passed should be True for a collection with 3741 revlog entries"
        )

    def test_is_enabled_after_enable(self, col: None) -> None:
        """is_fsrs_enabled() must return True after enable_fsrs()."""
        result = is_fsrs_enabled()
        print(f"\n[is_fsrs_enabled] after enable → {result}")
        assert result is True, "Expected FSRS to be enabled after enable_fsrs()"

    def test_idempotent_second_call(self, col: None) -> None:
        """Second call to enable_fsrs(optimize=True) must succeed."""
        result = enable_fsrs(optimize=True)

        print(
            f"\n[enable_fsrs idempotent] "
            f"enabled={result['enabled']} "
            f"optimized={result['optimized']} "
            f"num_params={result['num_params']} "
            f"fsrs_items={result['fsrs_items']}"
        )

        assert result["enabled"] is True
        assert result["optimized"] is True
        assert result["num_params"] == 21

    def test_enable_without_optimize(self, col: None) -> None:
        """enable_fsrs(optimize=False) should enable FSRS without running optimizer."""
        result = enable_fsrs(optimize=False)

        print(
            f"\n[enable_fsrs no-optimize] "
            f"enabled={result['enabled']} "
            f"optimized={result['optimized']} "
            f"num_params={result['num_params']}"
        )

        assert result["enabled"] is True
        assert result["optimized"] is False
        assert result["num_params"] == 0

    def test_still_enabled_after_no_optimize_call(self, col: None) -> None:
        """FSRS stays enabled regardless of optimize=False."""
        assert is_fsrs_enabled() is True
