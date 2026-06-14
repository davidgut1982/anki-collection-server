"""
FSRS (Free Spaced Repetition Scheduler) enable + optimize helpers.

This module implements *Step 8* of the anki-collection-server build plan.
It wraps the anki backend's FSRS parameter computation and applies the
optimised weights to all deck configuration presets.

Design decisions
----------------
* **Cutover action.**  Calling ``enable_fsrs(optimize=True)`` changes the
  scheduling algorithm for ALL clients that sync against this collection on
  their next sync.  This is intentional — it is a one-way migration that
  should only be triggered at cutover time.

* **Idempotent.**  ``enable_fsrs()`` can safely be called multiple times.
  If FSRS is already enabled the flag is set again (no-op) and, if
  ``optimize=True``, the optimizer re-runs to refresh weights from the
  latest revlog history.

* **Graceful degradation.**  The FSRS optimizer requires a minimum number
  of review items (~30–50).  If the optimizer raises (e.g. not enough
  data) *or* returns an empty / unusably-short param list, we log a warning
  and continue without applying params.  The ``optimized`` key in the return
  dict will be *False* in that case.

API names used (confirmed against anki 25.9.2 — see docs/spike-findings.md)
----------------------------------------------------------------------------
- ``col.get_config("fsrs") -> bool``
- ``col.set_config("fsrs", True)``
- ``col._backend.compute_fsrs_params(
      *, search, current_params, ignore_revlogs_before_ms,
      num_of_relearning_steps, health_check
  ) -> ComputeFsrsParamsResponse``
  Response fields:
    .params               — repeated float (21 values, FSRS-6)
    .fsrs_items           — int (review items used)
    .health_check_passed  — bool
- ``col.decks.all_config() -> list[dict]``
- ``col.decks.update_config(cfg: dict) -> None``
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Expected number of FSRS-6 parameters.  Used for validation only.
_FSRS6_PARAM_COUNT = 21


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_fsrs_enabled() -> bool:
    """Return *True* if FSRS is currently enabled on the collection.

    Reads the ``"fsrs"`` config key via ``col.get_config``.

    Returns
    -------
    bool
        ``True`` when FSRS is enabled, ``False`` when the SM-2 legacy
        scheduler is in use.

    Raises
    ------
    RuntimeError
        If the collection is not open.
    """
    import src.collection as col_mod  # noqa: PLC0415

    with col_mod._col_lock:
        return bool(col_mod.get_col().get_config("fsrs", False))


def enable_fsrs(optimize: bool = True) -> dict[str, Any]:
    """Enable FSRS scheduling and optionally optimise weights from revlog.

    .. warning::
        Enabling FSRS changes the scheduling algorithm for **all synced
        clients** on their next sync.  This is a deliberate cutover action
        and should not be called in normal operation.

    Parameters
    ----------
    optimize:
        When *True* (default), run ``backend.compute_fsrs_params`` against
        the full revlog history and apply the resulting 21-float vector to
        all deck configuration presets (``fsrsWeights``, ``fsrsParams5``,
        ``fsrsParams6``).

        When *False*, FSRS is enabled with whatever weights are currently
        stored in the deck presets (Anki ships reasonable defaults).

    Returns
    -------
    dict
        Keys:

        ``enabled`` (bool)
            Always *True* on success.

        ``optimized`` (bool)
            *True* if optimizer ran and params were applied successfully.

        ``num_params`` (int)
            Length of the parameter vector returned by the optimizer
            (21 for FSRS-6, 0 if optimization was skipped or failed).

        ``fsrs_items`` (int)
            Number of review items the optimizer consumed from the revlog.
            0 if optimization was skipped or failed.

        ``health_check_passed`` (bool)
            Whether the optimizer's data-quality health check passed.
            *False* if optimization was skipped or failed.

    Raises
    ------
    RuntimeError
        If the collection is not open.
    """
    import src.collection as col_mod  # noqa: PLC0415

    with col_mod._col_lock:
        col = col_mod.get_col()

        # ------------------------------------------------------------------
        # Step 1: Enable FSRS (idempotent)
        # ------------------------------------------------------------------
        col.set_config("fsrs", True)
        # Use an explicit guard instead of assert: assert statements are
        # silently removed under python -O / PYTHONOPTIMIZE.
        if col.get_config("fsrs") is not True:
            raise RuntimeError("set_config('fsrs', True) did not persist")
        log.info("FSRS enabled (set_config 'fsrs' = True).")

        if not optimize:
            log.info("FSRS enabled without optimization (optimize=False).")
            return {
                "enabled": True,
                "optimized": False,
                "num_params": 0,
                "fsrs_items": 0,
                "health_check_passed": False,
            }

        # ------------------------------------------------------------------
        # Step 2: Compute optimised parameters from full revlog history
        # ------------------------------------------------------------------
        log.info("Running FSRS parameter optimizer (search='', full revlog)…")
        try:
            result = col._backend.compute_fsrs_params(
                search="",  # empty = use all cards
                current_params=[],  # empty = start from defaults
                ignore_revlogs_before_ms=0,  # 0 = use entire revlog history
                num_of_relearning_steps=1,  # match standard relearning step count
                health_check=True,  # validate data quality
            )
        except Exception as exc:
            # Roll back FSRS so we never leave it enabled-without-valid-weights.
            # Leaving FSRS on with no params would cause silent scheduling
            # degradation for every synced device.
            log.exception(
                "FSRS optimizer raised — collection may have too few review "
                "items.  Rolling back FSRS to disabled."
            )
            try:
                col.set_config("fsrs", False)
                log.warning("FSRS rolled back to disabled after optimizer failure.")
            except Exception:
                log.exception(
                    "Failed to roll back FSRS config — manual intervention required."
                )
            return {
                "enabled": False,
                "optimized": False,
                "num_params": 0,
                "fsrs_items": 0,
                "health_check_passed": False,
                "error": str(exc),
            }

        params: list[float] = list(result.params)
        fsrs_items: int = int(result.fsrs_items)
        health_check_passed: bool = bool(result.health_check_passed)

        log.info(
            "Optimizer complete: %d params, %d fsrs_items, health_check_passed=%s",
            len(params),
            fsrs_items,
            health_check_passed,
        )

        if not params:
            # Empty param list — treat the same as short params: roll back so
            # we never leave FSRS enabled without a valid weight vector.
            log.error(
                "Optimizer returned empty params — rolling back FSRS to disabled."
            )
            try:
                col.set_config("fsrs", False)
            except Exception:
                log.exception("Failed to roll back FSRS config after empty params.")
            return {
                "enabled": False,
                "optimized": False,
                "num_params": 0,
                "fsrs_items": fsrs_items,
                "health_check_passed": health_check_passed,
                "error": "Optimizer returned empty params",
            }

        if len(params) < _FSRS6_PARAM_COUNT:
            # Applying a truncated/short param vector would silently corrupt
            # scheduling — treat this the same as optimizer failure.
            log.error(
                "Optimizer returned only %d params (expected %d) — "
                "refusing to apply truncated vector.  "
                "FSRS NOT enabled.",
                len(params),
                _FSRS6_PARAM_COUNT,
            )
            try:
                col.set_config("fsrs", False)
            except Exception:
                log.exception("Failed to roll back FSRS config after short params.")
            return {
                "enabled": False,
                "optimized": False,
                "num_params": len(params),
                "fsrs_items": fsrs_items,
                "health_check_passed": health_check_passed,
                "error": f"Optimizer returned {len(params)} params, expected {_FSRS6_PARAM_COUNT}",
            }

        # ------------------------------------------------------------------
        # Step 3: Apply params to all deck configuration presets
        # (only reached when exactly 21 params are present)
        # ------------------------------------------------------------------
        configs: list[dict[str, Any]] = col.decks.all_config()
        log.info("Applying FSRS params to %d deck preset(s).", len(configs))

        for cfg in configs:
            cfg["fsrsWeights"] = params  # legacy field (still read)
            cfg["fsrsParams5"] = params[:17]  # FSRS-5 subset (first 17)
            cfg["fsrsParams6"] = params  # FSRS-6 (all 21)
            col.decks.update_config(cfg)
            log.debug(
                "Updated deck preset id=%s name=%s", cfg.get("id"), cfg.get("name")
            )

        log.info(
            "FSRS params applied to %d preset(s). num_params=%d fsrs_items=%d",
            len(configs),
            len(params),
            fsrs_items,
        )

        return {
            "enabled": True,
            "optimized": True,
            "num_params": len(params),
            "fsrs_items": fsrs_items,
            "health_check_passed": health_check_passed,
        }


# ---------------------------------------------------------------------------
# AnkiConnect action handler (for future wiring in server.py Step 5)
# ---------------------------------------------------------------------------


def _action_enable_fsrs(params: dict[str, Any]) -> dict[str, Any]:
    """AnkiConnect ``enableFsrs`` action handler.

    Parameters
    ----------
    params:
        Optional dict with key ``"optimize"`` (bool, default *True*).

    Returns
    -------
    dict
        Same dict as :func:`enable_fsrs`.
    """
    optimize: bool = bool(params.get("optimize", True))
    return enable_fsrs(optimize=optimize)


def _action_is_fsrs_enabled(params: dict[str, Any]) -> bool:  # noqa: ARG001
    """AnkiConnect ``isFsrsEnabled`` action handler."""
    return is_fsrs_enabled()


# ---------------------------------------------------------------------------
# Action dispatch table (merged into server.py dispatch in Step 5)
# ---------------------------------------------------------------------------

FSRS_ACTIONS: dict[str, Any] = {
    "enableFsrs": _action_enable_fsrs,
    "isFsrsEnabled": _action_is_fsrs_enabled,
}
