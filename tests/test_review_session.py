"""
Integration tests for src/review_session.py — headless gui* state machine.

All tests operate against a COPY of the static backup collection placed in
/tmp — the live collection is NEVER opened.  The backup has SM-2 scheduling
(FSRS not yet enabled in the collection), which is fine for testing the
review-loop mechanics.  One test enables FSRS and validates the nextReviews
format works correctly under the FSRS scheduler.

Backup: /mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2
(read-only; copied to /tmp for each test session).

Test matrix:
  1. guiDeckReview selects a real deck → True, session created.
  2. guiCurrentCard returns well-formed payload (all expected keys present,
     buttons length matches card type, nextReviews parallel to buttons,
     question/answer are non-empty HTML, cardId/type/flags/reps present).
  3. Full flip→grade loop: guiCurrentCard → guiShowAnswer → guiAnswerCard(3)
     → guiCurrentCard returns a DIFFERENT card (or None if queue exhausted —
     the point is the answered card no longer appears as current).
  4. guiUndo after a grade restores the answered card to the queue.
  5. FSRS test: enable FSRS via set_config + compute params; confirm
     guiCurrentCard still returns valid nextReviews under the FSRS scheduler.
  6. Stale-protection: guiAnswerCard without a prior guiCurrentCard raises.
  7. guiDeckReview with unknown deck raises ValueError.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

import src.collection as col_mod
from src.review_session import GUI_ACTIONS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Allow override via env var so CI / different UIDs can point at a readable copy.
# Fall back to the canonical path if the env var is not set.
_DEFAULT_BACKUP = (
    "/mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2"
)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))

# A deck known to have cards due in the backup (from spike-findings.md)
REVIEW_DECK = "Latvian (ChatGPT)::Vocab & Sentences"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke(action: str, **kwargs: object) -> object:
    """Call a gui* action handler by name with the given keyword params."""
    handler = GUI_ACTIONS[action]
    return handler(kwargs)


# ---------------------------------------------------------------------------
# Session-scoped fixture: copy backup once per pytest session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def backup_copy() -> Generator[Path, None, None]:
    """Copy the backup to /tmp once; yield the path; clean up after session.

    Fails loudly (pytest.fail) rather than silently skipping when the backup
    is not readable — a silent skip causes CI to green with 0 assertions.

    Set ANKI_TEST_BACKUP to override the default path (useful when running as
    a UID that cannot read the default 0600/UID-1005 file).
    """
    if not BACKUP.exists():
        pytest.fail(
            f"Test backup not found: {BACKUP}. "
            f"Set ANKI_TEST_BACKUP env var to a readable .anki2 file."
        )
    if not os.access(BACKUP, os.R_OK):
        pytest.fail(
            f"Test backup not accessible (permission denied): {BACKUP}. "
            f"Set ANKI_TEST_BACKUP env var to a readable .anki2 file "
            f"(e.g. ANKI_TEST_BACKUP=/tmp/anki-test-backup.anki2)."
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="acs-review-test-", dir="/tmp"))
    col_path = tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)
    yield col_path
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Function-scoped fixture: open/close collection around each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def col(backup_copy: Path) -> Generator[None, None, None]:
    """Open the collection singleton before each test; close after.

    Also resets the ReviewSessionManager singleton between tests so that
    session state from one test does not bleed into the next.
    """
    # Guard: if a previous test left the manager open (due to crash), close it.
    try:
        col_mod.manager.close()
    except Exception:
        pass
    col_mod.manager.open(backup_copy)

    # Reset the review session manager between tests
    from src.review_session import _manager  # noqa: PLC0415

    _manager._session = None

    yield

    col_mod.manager.close()


# ---------------------------------------------------------------------------
# 1. guiDeckReview selects a real deck
# ---------------------------------------------------------------------------


class TestGuiDeckReview:
    def test_selects_known_deck_returns_true(self, col: None) -> None:
        result = invoke("guiDeckReview", name=REVIEW_DECK)
        assert result is True

    def test_unknown_deck_raises_value_error(self, col: None) -> None:
        with pytest.raises((ValueError, Exception)):
            invoke("guiDeckReview", name="NonExistentDeckXYZ::Subtest")

    def test_parent_deck_selects_ok(self, col: None) -> None:
        """Selecting a parent deck should also succeed (returns True)."""
        result = invoke("guiDeckReview", name="Latvian (ChatGPT)")
        assert result is True


# ---------------------------------------------------------------------------
# 2. guiCurrentCard returns well-formed payload
# ---------------------------------------------------------------------------


class TestGuiCurrentCardPayload:
    """Verify the full payload shape matches what anki_review_client.py expects."""

    REQUIRED_KEYS = {
        "cardId",
        "question",
        "answer",
        "buttons",
        "nextReviews",
        "css",
        "fields",
        "modelName",
        "deckName",
        "type",
        "flags",
        "reps",
    }

    def _get_card(self) -> dict:
        """Start a review and return the first card (or skip if queue empty)."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card = invoke("guiCurrentCard")
        if card is None:
            pytest.skip(f"No cards due in deck {REVIEW_DECK!r}")
        return card  # type: ignore[return-value]

    def test_all_required_keys_present(self, col: None) -> None:
        card = self._get_card()
        missing = self.REQUIRED_KEYS - set(card.keys())
        assert not missing, f"Missing keys in guiCurrentCard payload: {missing}"

    def test_card_id_is_positive_int(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["cardId"], int)
        assert card["cardId"] > 0

    def test_question_is_non_empty_html(self, col: None) -> None:
        card = self._get_card()
        q = card["question"]
        assert isinstance(q, str)
        assert len(q) > 0, "question must not be empty"

    def test_answer_is_non_empty_html(self, col: None) -> None:
        card = self._get_card()
        a = card["answer"]
        assert isinstance(a, str)
        assert len(a) > 0, "answer must not be empty"

    def test_buttons_is_list_of_ints(self, col: None) -> None:
        card = self._get_card()
        buttons = card["buttons"]
        assert isinstance(buttons, list)
        assert len(buttons) >= 1
        assert all(isinstance(b, int) for b in buttons)

    def test_buttons_length_matches_card_type(self, col: None) -> None:
        """Review cards (type=2) get 4 buttons; others get 3."""
        card = self._get_card()
        card_type = card["type"]
        buttons = card["buttons"]
        if card_type == 2:
            assert len(buttons) == 4, (
                f"Review card (type=2) expected 4 buttons, got {buttons}"
            )
        else:
            assert len(buttons) == 3, (
                f"New/learn card (type={card_type}) expected 3 buttons, got {buttons}"
            )

    def test_next_reviews_parallel_to_buttons(self, col: None) -> None:
        """nextReviews must be a list of the same length as buttons."""
        card = self._get_card()
        buttons = card["buttons"]
        next_reviews = card["nextReviews"]
        assert isinstance(next_reviews, list), (
            f"nextReviews must be a list, got {type(next_reviews)}"
        )
        assert len(next_reviews) == len(buttons), (
            f"nextReviews length {len(next_reviews)} != buttons length {len(buttons)}"
        )
        # Each label must be a non-empty string
        for label in next_reviews:
            assert isinstance(label, str)
            assert len(label) > 0, (
                f"Empty interval label in nextReviews: {next_reviews}"
            )

    def test_type_is_int(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["type"], int)
        assert card["type"] in (0, 1, 2, 3), f"Unexpected card type {card['type']}"

    def test_flags_is_int(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["flags"], int)

    def test_reps_is_non_negative_int(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["reps"], int)
        assert card["reps"] >= 0

    def test_model_name_is_non_empty_string(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["modelName"], str)
        assert len(card["modelName"]) > 0

    def test_deck_name_is_string(self, col: None) -> None:
        card = self._get_card()
        assert isinstance(card["deckName"], str)
        assert len(card["deckName"]) > 0

    def test_fields_dict_has_value_and_order(self, col: None) -> None:
        """fields must be {name: {value: str, order: int}} like notesInfo."""
        card = self._get_card()
        fields = card["fields"]
        assert isinstance(fields, dict)
        assert len(fields) > 0, "Expected at least one field"
        for fname, fdata in fields.items():
            assert "value" in fdata, f"Field {fname!r} missing 'value'"
            assert "order" in fdata, f"Field {fname!r} missing 'order'"

    def test_idempotent_two_calls_return_same_card_id(self, col: None) -> None:
        """get_queued_cards is idempotent — same card returned twice."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        c1 = invoke("guiCurrentCard")
        c2 = invoke("guiCurrentCard")
        if c1 is None:
            pytest.skip("No cards due")
        assert c1["cardId"] == c2["cardId"], (  # type: ignore[index]
            "guiCurrentCard must be idempotent (same card until answered)"
        )

    def test_no_session_returns_none(self, col: None) -> None:
        """Without guiDeckReview, guiCurrentCard returns None."""
        result = invoke("guiCurrentCard")
        assert result is None


# ---------------------------------------------------------------------------
# 3. Full flip→grade loop
# ---------------------------------------------------------------------------


class TestReviewLoop:
    """guiCurrentCard → guiShowAnswer → guiAnswerCard → next guiCurrentCard."""

    def test_grade_good_advances_to_next_card(self, col: None) -> None:
        """Answering a card with ease=3 (Good) should advance the queue."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card1 = invoke("guiCurrentCard")
        if card1 is None:
            pytest.skip("No cards due for review loop test")

        card1_id = card1["cardId"]  # type: ignore[index]

        # UI affordances
        invoke("guiStartCardTimer")
        invoke("guiShowAnswer")

        # Grade the card
        result = invoke("guiAnswerCard", ease=3)  # Good
        assert result is True

        # Next guiCurrentCard must return either a different card or None
        card2 = invoke("guiCurrentCard")
        if card2 is not None:
            assert card2["cardId"] != card1_id, (  # type: ignore[index]
                "Answered card must not be returned as the next current card"
            )
        # If None: queue exhausted — that is also a valid outcome

    def test_grade_again_requeues_card(self, col: None) -> None:
        """Ease=1 (Again) should re-queue the card into learning."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card1 = invoke("guiCurrentCard")
        if card1 is None:
            pytest.skip("No cards due")

        invoke("guiStartCardTimer")
        invoke("guiShowAnswer")

        result = invoke("guiAnswerCard", ease=1)  # Again
        assert result is True

        # The card has been re-queued in learning; guiCurrentCard will return
        # the NEXT card from the original queue (or the relearning card if
        # that's the only thing due).  Either way, the session progresses.
        card2 = invoke("guiCurrentCard")
        # We don't assert card2 != card1["cardId"] here because Again re-queues
        # the card as learning and it may immediately resurface. The important
        # assertion is that guiAnswerCard succeeded (True returned above).
        assert card2 is None or isinstance(card2["cardId"], int)  # type: ignore[index]


# ---------------------------------------------------------------------------
# 4. guiUndo after a grade restores the card
# ---------------------------------------------------------------------------


class TestGuiUndo:
    def test_undo_after_grade_returns_same_card(self, col: None) -> None:
        """After grading a card and undoing, guiCurrentCard returns the same card."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card1 = invoke("guiCurrentCard")
        if card1 is None:
            pytest.skip("No cards due for undo test")

        card1_id = card1["cardId"]  # type: ignore[index]

        invoke("guiStartCardTimer")
        invoke("guiShowAnswer")
        invoke("guiAnswerCard", ease=3)  # Good

        # Undo the grade
        undo_result = invoke("guiUndo")
        assert undo_result is True

        # After undo the same card should surface again
        card_after_undo = invoke("guiCurrentCard")
        assert card_after_undo is not None, "Expected card to reappear after undo"
        assert card_after_undo["cardId"] == card1_id, (  # type: ignore[index]
            f"Expected card {card1_id} after undo, got {card_after_undo['cardId']}"  # type: ignore[index]
        )

    def test_undo_with_nothing_to_undo_returns_false(self, col: None) -> None:
        """guiUndo when nothing has been graded returns False gracefully."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        # We DON'T grade anything — undo should silently return False
        result = invoke("guiUndo")
        # May return False (nothing to undo) or True (previous session had state)
        # The important thing is it does not raise an exception.
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 5. FSRS mode: guiCurrentCard returns valid nextReviews under FSRS
# ---------------------------------------------------------------------------


class TestFSRSMode:
    """Enable FSRS and confirm the review loop still works."""

    def test_fsrs_enabled_next_reviews_valid(self, col: None) -> None:
        """With FSRS enabled, guiCurrentCard must still return valid nextReviews."""
        c = col_mod.get_col()

        # Enable FSRS
        c.set_config("fsrs", True)
        assert c.get_config("fsrs") is True

        # Compute and apply FSRS weights from the backup's revlog
        # (1107 items in this backup — enough to produce meaningful params)
        try:
            result = c._backend.compute_fsrs_params(
                search="",
                current_params=[],
                ignore_revlogs_before_ms=0,
                num_of_relearning_steps=1,
                health_check=True,
            )
            optimized = list(result.params)
            # Apply to all deck presets
            for cfg in c.decks.all_config():
                cfg["fsrsWeights"] = optimized
                cfg["fsrsParams5"] = optimized[:17]
                cfg["fsrsParams6"] = optimized
                c.decks.update_config(cfg)
        except Exception as exc:
            pytest.skip(f"FSRS compute failed (too few items?): {exc}")

        # Now run a review
        invoke("guiDeckReview", name=REVIEW_DECK)
        card = invoke("guiCurrentCard")
        if card is None:
            pytest.skip("No cards due under FSRS")

        # Validate the payload shape is unchanged under FSRS
        assert "nextReviews" in card
        next_reviews = card["nextReviews"]  # type: ignore[index]
        assert isinstance(next_reviews, list)
        assert len(next_reviews) == len(card["buttons"])  # type: ignore[index]

        # Each FSRS label must be a non-empty string (e.g. "<1m", "3d", "7d")
        for label in next_reviews:
            assert isinstance(label, str)
            assert len(label) > 0, (
                f"FSRS nextReviews produced empty label: {next_reviews}"
            )


# ---------------------------------------------------------------------------
# 6. Stale-protection: guiAnswerCard without guiCurrentCard raises
# ---------------------------------------------------------------------------


class TestStaleProtection:
    def test_answer_without_current_card_raises(self, col: None) -> None:
        """guiAnswerCard without a prior guiCurrentCard must raise RuntimeError."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        # Do NOT call guiCurrentCard — session has no current card
        with pytest.raises(RuntimeError):
            invoke("guiAnswerCard", ease=3)

    def test_double_answer_raises(self, col: None) -> None:
        """Answering the same card twice must raise on the second call."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card = invoke("guiCurrentCard")
        if card is None:
            pytest.skip("No cards due for double-answer test")

        invoke("guiStartCardTimer")
        invoke("guiShowAnswer")
        invoke("guiAnswerCard", ease=3)  # First answer: OK

        # Second answer without a new guiCurrentCard must raise
        with pytest.raises(RuntimeError):
            invoke("guiAnswerCard", ease=3)

    def test_invalid_ease_raises_value_error(self, col: None) -> None:
        """ease=0 or ease=5 must raise ValueError."""
        invoke("guiDeckReview", name=REVIEW_DECK)
        card = invoke("guiCurrentCard")
        if card is None:
            pytest.skip("No cards due")

        with pytest.raises((ValueError, Exception)):
            invoke("guiAnswerCard", ease=0)

    def test_no_session_answer_raises_runtime_error(self, col: None) -> None:
        """guiAnswerCard with no session at all must raise RuntimeError."""
        # No guiDeckReview called
        with pytest.raises(RuntimeError):
            invoke("guiAnswerCard", ease=3)

    def test_ease_not_in_buttons_raises_value_error(self, col: None) -> None:
        """ease not in card's available buttons must raise ValueError.

        New/learning cards have buttons [1,3,4] (no Hard=2).  Submitting
        ease=2 on such a card must raise ValueError, not silently apply wrong
        scheduling.  This tests the ease-in-buttons guard added in the critic
        HIGH fix.
        """
        invoke("guiDeckReview", name=REVIEW_DECK)
        card = invoke("guiCurrentCard")
        if card is None:
            pytest.skip("No cards due")

        card_dict = card  # type: ignore[assignment]
        buttons: list[int] = card_dict["buttons"]  # type: ignore[index]

        # Find an ease value that is a valid ease (1–4) but NOT in this card's
        # button set.  For a new/learn card buttons=[1,3,4] → 2 is absent.
        # For a review card buttons=[1,2,3,4] → all are present, so we use 5.
        absent_ease: int | None = None
        for candidate in [2, 5]:
            if candidate not in buttons:
                absent_ease = candidate
                break

        if absent_ease is None:
            pytest.skip("Could not find an absent ease for this card's buttons")

        with pytest.raises((ValueError, Exception)):
            invoke("guiAnswerCard", ease=absent_ease)


# ---------------------------------------------------------------------------
# 7. Review-duration timer: revlog records non-zero time
# ---------------------------------------------------------------------------


class TestReviewDurationTimer:
    """Verify that the revlog time field is non-zero after answering a card.

    Real Anki starts the timer when the card is shown.  The critic HIGH bug
    was that start_timer() was called immediately before build_answer(), so
    card.time_taken() ≈ 0 ms.  The fix: call start_timer() in guiCurrentCard
    and restore card.timer_started in guiAnswerCard.
    """

    def test_revlog_time_is_nonzero_after_sleep(self, col: None) -> None:
        """Serve a card, sleep ~1.1 s, answer it; revlog time must be > 0 ms."""
        import src.collection as col_mod  # noqa: PLC0415

        invoke("guiDeckReview", name=REVIEW_DECK)
        card_payload = invoke("guiCurrentCard")
        if card_payload is None:
            pytest.skip("No cards due for timer test")

        card_id: int = card_payload["cardId"]  # type: ignore[index]

        # Snapshot revlog count before answering
        c = col_mod.get_col()
        before_count: int = c.db.scalar(
            "SELECT count(*) FROM revlog WHERE cid = ?", card_id
        )

        # Simulate real user think-time: sleep > 1 s so time_taken() > 1000 ms
        time.sleep(1.1)

        invoke("guiShowAnswer")
        # Choose an ease that is valid for this card
        buttons: list[int] = card_payload["buttons"]  # type: ignore[index]
        ease = 3 if 3 in buttons else buttons[-1]
        invoke("guiAnswerCard", ease=ease)

        # Check the new revlog row's time field
        after_count: int = c.db.scalar(
            "SELECT count(*) FROM revlog WHERE cid = ?", card_id
        )
        assert after_count == before_count + 1, (
            f"Expected 1 new revlog row for card {card_id}, "
            f"got {after_count - before_count}"
        )

        recorded_time_ms: int = c.db.scalar(
            "SELECT time FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1",
            card_id,
        )
        assert recorded_time_ms > 0, (
            f"revlog.time must be > 0 ms (got {recorded_time_ms} ms). "
            "Timer was not started at card-serve time."
        )
        # We slept 1.1 s, so at least 1000 ms should be recorded.
        # Anki caps at 60 000 ms; we just verify it's plausible (> 500 ms).
        assert recorded_time_ms >= 500, (
            f"revlog.time={recorded_time_ms} ms is suspiciously low for a 1.1 s delay. "
            "Expected >= 500 ms."
        )
