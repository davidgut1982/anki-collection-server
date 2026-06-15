"""
Integration tests for the P0 scheduling control actions (feat/admin-actions, A3).

Actions under test:
  - getDeckConfig       — resolved preset config for a deck
  - getDeckConfigs      — list all presets (preset-selector UI)
  - updateDeckConfig    — save a modified config preset
  - getFsrsParams       — FSRS weight vector + desiredRetention for a deck
  - setDesiredRetention — clamp-validated retention setter
  - computeOptimalRetention — FSRS simulator (may return error dict on thin data)

All tests work on a fresh per-test copy of the committed fixture placed in /tmp.
The committed fixture is NEVER modified directly.

Confirmed deck-config key structure (anki 25.9.2 empirical inspection):
  Top-level: id, name, mod, usn, dyn, desiredRetention, fsrsParams5,
             fsrsParams6, fsrsWeights, sm2Retention, new, rev, lapse, ...
  new: {bury, delays, initialFactor, ints, order, perDay}
  rev: {bury, ease4, hardFactor, ivlFct, maxIvl, perDay}
  lapse: {delays, leechAction, leechFails, minInt, mult}

Test fixture: tests/fixtures/test_collection.anki2
Override:     ANKI_TEST_BACKUP environment variable.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src import collection as col_mod
from src.actions import ACTIONS

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"
_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def destructive_col() -> Generator[None, None, None]:
    """Open a fresh per-test copy of the fixture; close after.

    Each test gets its own /tmp copy so destructive operations never affect
    others.
    """
    if not BACKUP.exists():
        pytest.fail(f"Test backup not found: {BACKUP}.")
    private_dir = Path(tempfile.mkdtemp(prefix="acs-sched-", dir="/tmp"))
    col_path = private_dir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)

    try:
        col_mod.manager.close()
    except Exception:
        pass
    col_mod.manager.open(col_path)
    yield
    try:
        col_mod.manager.close()
    except Exception:
        pass
    shutil.rmtree(private_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def invoke(action: str, **kwargs: object) -> object:
    """Call an action handler by name with the given keyword params."""
    handler = ACTIONS[action]
    return handler(kwargs)


# ===========================================================================
# getDeckConfig
# ===========================================================================


class TestGetDeckConfig:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        """getDeckConfig must return all documented human-relevant keys."""
        result = invoke("getDeckConfig", deck="Default")
        assert isinstance(result, dict)

        expected_keys = {
            "id",
            "name",
            "new_per_day",
            "rev_per_day",
            "learning_steps",
            "relearn_steps",
            "graduating_interval",
            "easy_interval",
            "max_interval",
            "bury_new",
            "bury_reviews",
            "leech_fails",
            "leech_action",
            "new_interval_mult",
            "min_interval",
            "initial_factor",
            "desired_retention",
            "fsrs_params",
            "config",
        }
        missing = expected_keys - set(result.keys())
        assert not missing, f"getDeckConfig missing keys: {missing}"

    def test_values_have_correct_types(self, destructive_col: None) -> None:
        """All documented fields must have the correct Python type."""
        result = invoke("getDeckConfig", deck="Default")

        assert isinstance(result["id"], int)
        assert isinstance(result["name"], str)
        assert isinstance(result["new_per_day"], int)
        assert isinstance(result["rev_per_day"], int)
        assert isinstance(result["learning_steps"], list)
        assert isinstance(result["relearn_steps"], list)
        assert isinstance(result["graduating_interval"], int)
        assert isinstance(result["easy_interval"], int)
        assert isinstance(result["max_interval"], int)
        assert isinstance(result["bury_new"], bool)
        assert isinstance(result["bury_reviews"], bool)
        assert isinstance(result["leech_fails"], int)
        assert isinstance(result["leech_action"], int)
        assert isinstance(result["new_interval_mult"], float)
        assert isinstance(result["min_interval"], int)
        assert isinstance(result["initial_factor"], int)
        assert isinstance(result["desired_retention"], float)
        assert isinstance(result["fsrs_params"], list)
        assert isinstance(result["config"], dict)

    def test_config_key_contains_raw_dict(self, destructive_col: None) -> None:
        """The 'config' key must be a dict with all raw keys including 'new', 'rev', 'lapse'."""
        result = invoke("getDeckConfig", deck="Default")
        raw = result["config"]
        assert isinstance(raw, dict)
        # Must contain the nested sub-dicts
        assert "new" in raw
        assert "rev" in raw
        assert "lapse" in raw
        # Must contain FSRS keys
        assert "desiredRetention" in raw
        assert "fsrsParams6" in raw or "fsrsParams5" in raw

    def test_resolve_by_deck_id(self, destructive_col: None) -> None:
        """getDeckConfig must accept an integer deck id."""
        # Default deck has id=1
        by_name = invoke("getDeckConfig", deck="Default")
        by_id = invoke("getDeckConfig", deck=1)
        assert by_name["id"] == by_id["id"]
        assert by_name["name"] == by_id["name"]

    def test_desired_retention_within_bounds(self, destructive_col: None) -> None:
        """desired_retention must be a float in (0, 1)."""
        result = invoke("getDeckConfig", deck="Default")
        assert 0.0 < result["desired_retention"] <= 1.0

    def test_nonexistent_deck_raises(self, destructive_col: None) -> None:
        """getDeckConfig on a nonexistent deck name raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            invoke("getDeckConfig", deck="__nonexistent_deck_xzq__")


# ===========================================================================
# getDeckConfigs
# ===========================================================================


class TestGetDeckConfigs:
    def test_returns_list(self, destructive_col: None) -> None:
        """getDeckConfigs must return a list."""
        result = invoke("getDeckConfigs")
        assert isinstance(result, list)

    def test_list_nonempty(self, destructive_col: None) -> None:
        """getDeckConfigs must return at least one preset (every collection has Default)."""
        result = invoke("getDeckConfigs")
        assert len(result) >= 1

    def test_each_entry_is_dict_with_id_and_name(self, destructive_col: None) -> None:
        """Each preset must be a dict with 'id' and 'name'."""
        result = invoke("getDeckConfigs")
        for entry in result:
            assert isinstance(entry, dict)
            assert "id" in entry
            assert "name" in entry

    def test_contains_nested_dicts(self, destructive_col: None) -> None:
        """Each preset must have 'new', 'rev', 'lapse' sub-dicts."""
        result = invoke("getDeckConfigs")
        for entry in result:
            assert "new" in entry
            assert "rev" in entry
            assert "lapse" in entry


# ===========================================================================
# updateDeckConfig (round-trip)
# ===========================================================================


class TestUpdateDeckConfig:
    def test_round_trip_new_per_day(self, destructive_col: None) -> None:
        """Read config → change new_per_day → updateDeckConfig → getDeckConfig → verify persisted."""
        original = invoke("getDeckConfig", deck="Default")
        cfg = dict(original["config"])

        original_new_per_day = original["new_per_day"]
        new_value = 77 if original_new_per_day != 77 else 88

        # Mutate the nested 'new' dict
        cfg["new"] = dict(cfg["new"])
        cfg["new"]["perDay"] = new_value

        invoke("updateDeckConfig", config=cfg)

        # Read back
        after = invoke("getDeckConfig", deck="Default")
        assert after["new_per_day"] == new_value, (
            f"Expected new_per_day={new_value} after update, got {after['new_per_day']}"
        )

    def test_round_trip_desired_retention(self, destructive_col: None) -> None:
        """Read config → change desiredRetention → updateDeckConfig → getDeckConfig → verify."""
        original = invoke("getDeckConfig", deck="Default")
        cfg = dict(original["config"])

        new_retention = 0.88
        cfg["desiredRetention"] = new_retention

        invoke("updateDeckConfig", config=cfg)

        after = invoke("getDeckConfig", deck="Default")
        assert abs(after["desired_retention"] - new_retention) < 1e-6, (
            f"Expected desiredRetention={new_retention}, got {after['desired_retention']}"
        )

    def test_missing_id_raises_value_error(self, destructive_col: None) -> None:
        """updateDeckConfig without an 'id' key raises ValueError."""
        with pytest.raises(ValueError, match="id"):
            invoke("updateDeckConfig", config={"name": "Default", "new": {}})

    def test_returns_none(self, destructive_col: None) -> None:
        """updateDeckConfig must return null (None)."""
        original = invoke("getDeckConfig", deck="Default")
        result = invoke("updateDeckConfig", config=original["config"])
        assert result is None


# ===========================================================================
# getFsrsParams
# ===========================================================================


class TestGetFsrsParams:
    def test_returns_params_and_desired_retention(self, destructive_col: None) -> None:
        """getFsrsParams must return 'params' (list) and 'desiredRetention' (float)."""
        result = invoke("getFsrsParams", deck="Default")
        assert isinstance(result, dict)
        assert "params" in result
        assert "desiredRetention" in result
        assert isinstance(result["params"], list)
        assert isinstance(result["desiredRetention"], float)

    def test_desired_retention_within_range(self, destructive_col: None) -> None:
        """desiredRetention must be a float in (0.0, 1.0]."""
        result = invoke("getFsrsParams", deck="Default")
        assert 0.0 < result["desiredRetention"] <= 1.0

    def test_params_is_list_of_floats_or_empty(self, destructive_col: None) -> None:
        """params must be a list of floats (may be empty when FSRS not yet trained)."""
        result = invoke("getFsrsParams", deck="Default")
        params = result["params"]
        assert isinstance(params, list)
        if params:
            assert all(isinstance(p, float) for p in params), (
                f"Expected all floats, got types: {[type(p).__name__ for p in params]}"
            )

    def test_nonexistent_deck_raises(self, destructive_col: None) -> None:
        """getFsrsParams on a nonexistent deck raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            invoke("getFsrsParams", deck="__nonexistent_xzq__")


# ===========================================================================
# setDesiredRetention
# ===========================================================================


class TestSetDesiredRetention:
    def test_persists_valid_retention(self, destructive_col: None) -> None:
        """setDesiredRetention(0.85) must persist and be readable via getFsrsParams."""
        result = invoke("setDesiredRetention", deck="Default", retention=0.85)
        assert result is None

        after = invoke("getFsrsParams", deck="Default")
        assert abs(after["desiredRetention"] - 0.85) < 1e-6, (
            f"Expected desiredRetention=0.85 after set, got {after['desiredRetention']}"
        )

    def test_persists_via_get_deck_config(self, destructive_col: None) -> None:
        """setDesiredRetention change must also be visible via getDeckConfig."""
        invoke("setDesiredRetention", deck="Default", retention=0.92)
        after = invoke("getDeckConfig", deck="Default")
        assert abs(after["desired_retention"] - 0.92) < 1e-6, (
            f"Expected desired_retention=0.92, got {after['desired_retention']}"
        )

    def test_rejects_below_minimum(self, destructive_col: None) -> None:
        """retention=0.5 (below 0.70) must raise ValueError."""
        with pytest.raises(ValueError, match="out of range"):
            invoke("setDesiredRetention", deck="Default", retention=0.5)

    def test_rejects_above_maximum(self, destructive_col: None) -> None:
        """retention=0.99 (above 0.97) must raise ValueError."""
        with pytest.raises(ValueError, match="out of range"):
            invoke("setDesiredRetention", deck="Default", retention=0.99)

    def test_rejects_exactly_zero(self, destructive_col: None) -> None:
        """retention=0.0 must raise ValueError (below 0.70)."""
        with pytest.raises(ValueError):
            invoke("setDesiredRetention", deck="Default", retention=0.0)

    def test_rejects_exactly_one(self, destructive_col: None) -> None:
        """retention=1.0 must raise ValueError (above 0.97)."""
        with pytest.raises(ValueError):
            invoke("setDesiredRetention", deck="Default", retention=1.0)

    def test_accepts_boundary_minimum(self, destructive_col: None) -> None:
        """retention=0.70 (minimum) must be accepted."""
        result = invoke("setDesiredRetention", deck="Default", retention=0.70)
        assert result is None
        after = invoke("getFsrsParams", deck="Default")
        assert abs(after["desiredRetention"] - 0.70) < 1e-6

    def test_accepts_boundary_maximum(self, destructive_col: None) -> None:
        """retention=0.97 (maximum) must be accepted."""
        result = invoke("setDesiredRetention", deck="Default", retention=0.97)
        assert result is None
        after = invoke("getFsrsParams", deck="Default")
        assert abs(after["desiredRetention"] - 0.97) < 1e-6

    def test_nonexistent_deck_raises(self, destructive_col: None) -> None:
        """setDesiredRetention on a nonexistent deck raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            invoke("setDesiredRetention", deck="__nonexistent_xzq__", retention=0.85)


# ===========================================================================
# computeOptimalRetention
# ===========================================================================


class TestComputeOptimalRetention:
    def test_returns_float_or_clean_error(self, destructive_col: None) -> None:
        """computeOptimalRetention must return {"optimalRetention": float} OR
        {"error": str, "note": str} — never raise an exception.

        The committed fixture has too few review items for the optimizer to
        succeed, so we accept either a valid result or a well-structured error.
        """
        result = invoke("computeOptimalRetention", deck="Default")
        assert isinstance(result, dict)

        if "optimalRetention" in result:
            # Success path: must be a float in (0, 1)
            value = result["optimalRetention"]
            assert isinstance(value, float), (
                f"Expected float, got {type(value).__name__}"
            )
            assert 0.0 < value < 1.0, f"optimalRetention {value!r} out of (0, 1)"
        else:
            # Error path: must have 'error' and 'note' keys (not raise)
            assert "error" in result, (
                f"Expected 'error' key in failure result, got: {sorted(result.keys())}"
            )
            assert isinstance(result["error"], str)
            # 'note' is optional but expected when the backend API call fails
            # (it may be absent on an import error too, but at least error is present)

    def test_does_not_raise_on_thin_data(self, destructive_col: None) -> None:
        """computeOptimalRetention must not raise even when the fixture has too few items."""
        # If this line raises, the action is incorrectly propagating exceptions.
        try:
            result = invoke("computeOptimalRetention", deck="Default")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"computeOptimalRetention raised {type(exc).__name__}: {exc} — "
                "it must return an error dict instead of raising"
            )
        assert isinstance(result, dict)

    def test_accepts_optional_sim_params(self, destructive_col: None) -> None:
        """computeOptimalRetention must accept optional simulation parameters without raising."""
        try:
            result = invoke(
                "computeOptimalRetention",
                deck="Default",
                daysToSimulate=180,
                deckSize=100,
                newLimit=10,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"computeOptimalRetention with optional params raised: {exc}")
        assert isinstance(result, dict)


# ===========================================================================
# Action registration sanity checks
# ===========================================================================


class TestActionRegistration:
    def test_all_scheduling_actions_registered(self) -> None:
        """All 6 P0 scheduling control actions must be present in the ACTIONS dict."""
        expected = {
            "getDeckConfig",
            "getDeckConfigs",
            "updateDeckConfig",
            "getFsrsParams",
            "setDesiredRetention",
            "computeOptimalRetention",
        }
        missing = expected - set(ACTIONS.keys())
        assert not missing, f"Missing scheduling actions: {missing}"

    def test_handlers_are_callable(self) -> None:
        """All newly registered scheduling handlers must be callable."""
        for name in [
            "getDeckConfig",
            "getDeckConfigs",
            "updateDeckConfig",
            "getFsrsParams",
            "setDesiredRetention",
            "computeOptimalRetention",
        ]:
            assert callable(ACTIONS[name]), f"ACTIONS[{name!r}] is not callable"
