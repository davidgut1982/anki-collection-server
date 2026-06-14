"""
Integration tests for src/fsrs.py.

All tests operate against a COPY of the committed test fixture placed
in /tmp — the live collection is never opened or modified.

Default fixture: tests/fixtures/test_collection.anki2 (committed to repo)
Override: set ANKI_TEST_BACKUP env var to point at a different .anki2 file.

Test plan
---------
1. ``is_fsrs_enabled()`` returns *False* on a fresh fixture.
2. ``enable_fsrs(optimize=True)`` is attempted; if the fixture has too few
   revlog items for the health check, the function returns enabled=False with
   an error (health_check_passed may be False).  When it succeeds (e.g. with
   a larger backup via ANKI_TEST_BACKUP), enabled=True, optimized=True,
   num_params==21, health_check_passed==True.
3. ``is_fsrs_enabled()`` returns *True* after a successful enable_fsrs().
4. Second call to ``enable_fsrs(optimize=True)`` succeeds (idempotent) if
   the first call succeeded.
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
# Fixture path (committed to repo; override via ANKI_TEST_BACKUP for CI)
# ---------------------------------------------------------------------------

_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"
_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))


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
    def test_disabled_on_fresh_fixture(self, col: None) -> None:
        """FSRS should be off on the unmodified fixture collection."""
        result = is_fsrs_enabled()
        print(f"\n[is_fsrs_enabled] fresh fixture → {result}")
        assert result is False, "Expected FSRS to be disabled on fresh fixture"


class TestEnableFsrs:
    """Tests for enable_fsrs().

    The committed fixture has only ~40 revlog entries — not enough for the
    FSRS optimizer health check (~1000 items needed).  When running against
    the small fixture the optimizer returns empty params and enable_fsrs rolls
    back to enabled=False.

    These tests therefore have two modes:
    - Large collection (ANKI_TEST_BACKUP override): full optimizer path tested.
    - Small fixture (default): response SHAPE is tested; tests that require
      enabled=True skip gracefully.
    """

    def test_enable_with_optimize_response_shape(self, col: None) -> None:
        """enable_fsrs(optimize=True) always returns a dict with the expected keys."""
        result = enable_fsrs(optimize=True)

        print(
            f"\n[enable_fsrs] result: "
            f"enabled={result['enabled']} "
            f"optimized={result['optimized']} "
            f"num_params={result['num_params']} "
            f"fsrs_items={result['fsrs_items']} "
            f"health_check_passed={result['health_check_passed']}"
        )

        # Shape is always present regardless of optimizer success/failure
        for key in (
            "enabled",
            "optimized",
            "num_params",
            "fsrs_items",
            "health_check_passed",
        ):
            assert key in result, f"Missing key {key!r} in enable_fsrs() result"
        assert isinstance(result["enabled"], bool)
        assert isinstance(result["optimized"], bool)
        assert isinstance(result["num_params"], int)
        assert isinstance(result["fsrs_items"], int)
        assert isinstance(result["health_check_passed"], bool)

    def test_enable_with_optimize_full_path(self, col: None) -> None:
        """When optimizer succeeds, result has enabled=True, 21 params.

        Skips when the collection has too few revlog items for the optimizer
        health check (e.g. the committed test fixture with ~40 entries).
        Run with ANKI_TEST_BACKUP pointing at a large collection to cover this.
        """
        result = enable_fsrs(optimize=True)
        if not result["enabled"]:
            pytest.skip(
                f"Optimizer did not succeed (enabled=False): {result.get('error', 'unknown')}. "
                "This is expected with the small committed fixture. "
                "Set ANKI_TEST_BACKUP to a large collection to test the full optimizer path."
            )

        assert result["optimized"] is True, "optimized should be True when enabled=True"
        assert result["num_params"] == 21, (
            f"Expected 21 FSRS-6 params, got {result['num_params']}"
        )
        assert result["fsrs_items"] > 0, (
            f"Expected >0 fsrs_items, got {result['fsrs_items']}"
        )
        assert result["health_check_passed"] is True, (
            "health_check_passed should be True for a large collection"
        )

    def test_is_enabled_after_successful_enable(self, col: None) -> None:
        """is_fsrs_enabled() must return True after a successful enable_fsrs()."""
        result = enable_fsrs(optimize=True)
        if not result["enabled"]:
            pytest.skip(
                "Optimizer did not succeed; skipping is_fsrs_enabled check. "
                "Set ANKI_TEST_BACKUP to a large collection to test this path."
            )
        after = is_fsrs_enabled()
        print(f"\n[is_fsrs_enabled] after enable → {after}")
        assert after is True, "Expected FSRS to be enabled after enable_fsrs()"

    def test_idempotent_second_call(self, col: None) -> None:
        """Second call to enable_fsrs(optimize=True) must succeed (same shape)."""
        result1 = enable_fsrs(optimize=True)
        if not result1["enabled"]:
            pytest.skip(
                "First call did not enable FSRS (small fixture); "
                "idempotency test requires a successful first call."
            )
        result2 = enable_fsrs(optimize=True)
        print(
            f"\n[enable_fsrs idempotent] "
            f"enabled={result2['enabled']} "
            f"optimized={result2['optimized']} "
            f"num_params={result2['num_params']} "
            f"fsrs_items={result2['fsrs_items']}"
        )
        assert result2["enabled"] is True
        assert result2["optimized"] is True
        assert result2["num_params"] == 21

    def test_enable_without_optimize(self, col: None) -> None:
        """enable_fsrs(optimize=False) must enable FSRS without running optimizer."""
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
        """FSRS stays enabled after optimize=False call."""
        enable_fsrs(optimize=False)  # Ensure it is enabled first
        assert is_fsrs_enabled() is True


class TestFsrsOptimizerRollback:
    """FSRS must be rolled back to disabled when the optimizer raises.

    Contract: never leave FSRS on without a valid weight vector.
    """

    def test_optimizer_failure_rolls_back_fsrs(self, col: None) -> None:
        """When compute_fsrs_params raises, enable_fsrs must:
        - return enabled=False
        - roll FSRS config back to False (not leave it enabled-without-weights)
        """
        boom = RuntimeError("simulated optimizer failure — not enough data")

        # Patch _backend.compute_fsrs_params on the live collection object.
        live_col = col_mod.get_col()
        original_fn = live_col._backend.compute_fsrs_params

        try:
            live_col._backend.compute_fsrs_params = lambda **_kw: (_ for _ in ()).throw(
                boom
            )  # type: ignore[method-assign]

            result = enable_fsrs(optimize=True)

            print(
                f"\n[optimizer rollback] "
                f"enabled={result['enabled']} "
                f"optimized={result['optimized']} "
                f"error={result.get('error')!r}"
            )

            assert result["enabled"] is False, (
                "enabled must be False after optimizer failure — "
                "FSRS must not be left on without valid weights"
            )
            assert result["optimized"] is False
            assert "error" in result, "result must include an 'error' key on failure"

            # Confirm the config was actually rolled back in the collection.
            assert is_fsrs_enabled() is False, (
                "is_fsrs_enabled() must return False after optimizer rollback — "
                "FSRS config must not be left as True"
            )

        finally:
            live_col._backend.compute_fsrs_params = original_fn  # type: ignore[method-assign]
