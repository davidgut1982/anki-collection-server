"""
FSRS (Free Spaced Repetition Scheduler) helpers.

The `anki` package bundles FSRS-5. This module exposes thin wrappers that
translate between the AnkiConnect parameter convention and the anki.scheduler
API so that actions.py can call FSRS operations without knowing the details.

Responsibilities (Step 8):
  - Provide get_optimal_retention(), reschedule_cards(), and fsrs_stats()
    helpers used by the corresponding AnkiConnect actions.
  - Expose computeOptimalRetention and setOptimalRetention action handlers.

TODO (Step 8): implement FSRS helpers once collection.py is stable.
"""
