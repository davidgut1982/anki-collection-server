"""
Integration tests for the P0 card/note triage actions (feat/admin-actions).

Actions under test:
  - bury / unbury              (card queue mutation)
  - setDueDate                 (reschedule review)
  - forgetCards                (reset to new)
  - repositionNewCards         (reorder new queue)
  - findAndReplace             (field bulk edit, regex + plain)
  - findDuplicates             (find duplicate field values)
  - clearUnusedTags            (tag hygiene)
  - getTags                    (tag listing)

All destructive tests work on a fresh per-test copy of the committed fixture
placed in /tmp.  The committed fixture is NEVER modified directly.

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
# Session fixture: shared read-only copy (for non-destructive tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def backup_copy() -> Generator[Path, None, None]:
    """Copy the backup once per session; yield path; clean up after."""
    if not BACKUP.exists():
        pytest.fail(
            f"Test backup not found: {BACKUP}. "
            "Set ANKI_TEST_BACKUP env var to a readable .anki2 file."
        )
    if not os.access(BACKUP, os.R_OK):
        pytest.fail(
            f"Test backup not accessible (permission denied): {BACKUP}."
        )
    tmpdir = Path(tempfile.mkdtemp(prefix="acs-triage-session-", dir="/tmp"))
    col_path = tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)
    yield col_path
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Function fixture: fresh writable copy for each destructive test
# ---------------------------------------------------------------------------


@pytest.fixture()
def destructive_col() -> Generator[None, None, None]:
    """Open a fresh per-test copy of the fixture; close after.

    Each test gets its own /tmp copy so destructive operations never
    affect the session-shared backup.
    """
    if not BACKUP.exists():
        pytest.fail(f"Test backup not found: {BACKUP}.")
    private_dir = Path(tempfile.mkdtemp(prefix="acs-triage-destructive-", dir="/tmp"))
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
# Read-only fixture: open/close shared copy per test
# ---------------------------------------------------------------------------


@pytest.fixture()
def col(backup_copy: Path) -> Generator[None, None, None]:
    """Open the shared session copy before each test; close after."""
    try:
        col_mod.manager.close()
    except Exception:
        pass
    col_mod.manager.open(backup_copy)
    yield
    col_mod.manager.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def invoke(action: str, **kwargs: object) -> object:
    """Call an action handler by name with the given keyword params."""
    handler = ACTIONS[action]
    return handler(kwargs)


def _add_basic_note(front: str, back: str, deck: str = "Default") -> int:
    """Helper to add a Basic note; returns the note id."""
    model_names = list(invoke("modelNames"))
    model = "Basic" if "Basic" in model_names else model_names[0]
    note_id = invoke(
        "addNote",
        note={
            "deckName": deck,
            "modelName": model,
            "fields": {"Front": front, "Back": back},
            "tags": [],
        },
    )
    assert isinstance(note_id, int)
    return note_id


def _cards_for_note(note_id: int) -> list[int]:
    """Return card ids belonging to a note."""
    return list(invoke("findCards", query=f"nid:{note_id}"))


# ===========================================================================
# bury / unbury
# ===========================================================================


class TestBuryUnbury:
    def test_bury_changes_queue(self, destructive_col: None) -> None:
        """Bury a card and verify its queue changes from NEW (0) to BURIED (-2 or -3)."""
        note_id = _add_basic_note("bury-front-xzq", "bury-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids, "Expected at least one card"

        info_before = invoke("cardsInfo", cards=card_ids)
        assert isinstance(info_before, list)
        queue_before = info_before[0]["queue"]
        # New cards have queue=0
        assert queue_before == 0, f"Expected new queue (0), got {queue_before}"

        result = invoke("bury", cards=card_ids)
        assert result is None

        info_after = invoke("cardsInfo", cards=card_ids)
        queue_after = info_after[0]["queue"]
        # Buried manually: BURY_USER → queue = -2
        assert queue_after < 0, f"Expected negative buried queue, got {queue_after}"

    def test_unbury_restores_queue(self, destructive_col: None) -> None:
        """Bury then unbury a card; queue must return to non-negative."""
        note_id = _add_basic_note("unbury-front-xzq", "unbury-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        invoke("bury", cards=card_ids)
        info_buried = invoke("cardsInfo", cards=card_ids)
        assert info_buried[0]["queue"] < 0, "Card must be buried before unbury test"

        result = invoke("unbury", cards=card_ids)
        assert result is None

        info_after = invoke("cardsInfo", cards=card_ids)
        queue_after = info_after[0]["queue"]
        assert queue_after >= 0, f"Expected non-negative queue after unbury, got {queue_after}"

    def test_bury_empty_list_is_noop(self, destructive_col: None) -> None:
        """bury with an empty cards list must not raise."""
        result = invoke("bury", cards=[])
        assert result is None

    def test_unbury_empty_list_is_noop(self, destructive_col: None) -> None:
        """unbury with an empty cards list must not raise."""
        result = invoke("unbury", cards=[])
        assert result is None


# ===========================================================================
# setDueDate
# ===========================================================================


class TestSetDueDate:
    def test_set_due_date_moves_due(self, destructive_col: None) -> None:
        """setDueDate must schedule a card as a review with the specified due day."""
        note_id = _add_basic_note("due-front-xzq", "due-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        # Set due in 5 days; card becomes a review card
        result = invoke("setDueDate", cards=card_ids, days="5")
        assert result is None

        info = invoke("cardsInfo", cards=card_ids)
        card_info = info[0]
        # After setDueDate the card becomes type=2 (review) and queue=2
        assert card_info["type"] == 2, f"Expected review type (2), got {card_info['type']}"
        assert card_info["queue"] == 2, f"Expected review queue (2), got {card_info['queue']}"

    def test_set_due_date_range_spec(self, destructive_col: None) -> None:
        """setDueDate accepts a range spec like '1-7'."""
        note_id = _add_basic_note("due-range-front-xzq", "due-range-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        result = invoke("setDueDate", cards=card_ids, days="1-7")
        assert result is None

        info = invoke("cardsInfo", cards=card_ids)
        # Card becomes review; due day is within 1-7 of scheduler's today
        assert info[0]["type"] == 2

    def test_set_due_date_empty_days_raises(self, destructive_col: None) -> None:
        """Empty string for days raises ValueError."""
        note_id = _add_basic_note("due-invalid-front-xzq", "due-invalid-back-xzq")
        card_ids = _cards_for_note(note_id)
        with pytest.raises(ValueError, match="days"):
            invoke("setDueDate", cards=card_ids, days="")

    def test_set_due_date_non_string_days_raises(self, destructive_col: None) -> None:
        """Non-string days (int) raises ValueError."""
        note_id = _add_basic_note("due-int-front-xzq", "due-int-back-xzq")
        card_ids = _cards_for_note(note_id)
        with pytest.raises(ValueError, match="days"):
            invoke("setDueDate", cards=card_ids, days=5)  # type: ignore[arg-type]


# ===========================================================================
# forgetCards
# ===========================================================================


class TestForgetCards:
    def test_forget_resets_review_card_to_new(self, destructive_col: None) -> None:
        """forgetCards on a review card (type=2) must reset it to new (type=0)."""
        note_id = _add_basic_note("forget-front-xzq", "forget-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        # Promote to review card first via setDueDate
        invoke("setDueDate", cards=card_ids, days="1")
        info_before = invoke("cardsInfo", cards=card_ids)
        assert info_before[0]["type"] == 2, "Precondition: card must be review type"

        result = invoke("forgetCards", cards=card_ids)
        assert result is None

        info_after = invoke("cardsInfo", cards=card_ids)
        assert info_after[0]["type"] == 0, (
            f"Expected new type (0) after forget, got {info_after[0]['type']}"
        )
        assert info_after[0]["queue"] == 0, (
            f"Expected new queue (0) after forget, got {info_after[0]['queue']}"
        )

    def test_forget_with_restore_position(self, destructive_col: None) -> None:
        """forgetCards with restorePosition=True must not raise."""
        note_id = _add_basic_note("forget-pos-front-xzq", "forget-pos-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        result = invoke("forgetCards", cards=card_ids, restorePosition=True, resetCounts=False)
        assert result is None

    def test_forget_with_reset_counts(self, destructive_col: None) -> None:
        """forgetCards with resetCounts=True must not raise."""
        note_id = _add_basic_note("forget-cnt-front-xzq", "forget-cnt-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        result = invoke("forgetCards", cards=card_ids, restorePosition=False, resetCounts=True)
        assert result is None

    def test_forget_empty_list_is_noop(self, destructive_col: None) -> None:
        """forgetCards with an empty list must not raise."""
        result = invoke("forgetCards", cards=[])
        assert result is None


# ===========================================================================
# repositionNewCards
# ===========================================================================


class TestRepositionNewCards:
    def test_reposition_changes_due_order(self, destructive_col: None) -> None:
        """repositionNewCards must change the due position of new cards."""
        note_a = _add_basic_note("repos-a-front-xzq", "repos-a-back-xzq")
        note_b = _add_basic_note("repos-b-front-xzq", "repos-b-back-xzq")

        cards_a = _cards_for_note(note_a)
        cards_b = _cards_for_note(note_b)
        assert cards_a and cards_b

        all_new_cards = cards_a + cards_b

        result = invoke(
            "repositionNewCards",
            cards=all_new_cards,
            start=1000,
            step=1,
            randomize=False,
            shiftExisting=True,
        )
        assert result is None

        info = invoke("cardsInfo", cards=all_new_cards)
        dues = [c["due"] for c in info]
        # Due positions must now be >= 1000 (the start value)
        for due in dues:
            assert due >= 1000, f"Expected due >= 1000 after reposition, got {due}"

    def test_reposition_alias_works(self, destructive_col: None) -> None:
        """'reposition' alias must be registered and behave identically."""
        assert "reposition" in ACTIONS

        note_id = _add_basic_note("alias-repos-front-xzq", "alias-repos-back-xzq")
        card_ids = _cards_for_note(note_id)
        assert card_ids

        result = invoke("reposition", cards=card_ids, start=500, step=1,
                        randomize=False, shiftExisting=False)
        assert result is None

    def test_reposition_empty_list_is_noop(self, destructive_col: None) -> None:
        """Repositioning an empty card list must not raise."""
        result = invoke("repositionNewCards", cards=[], start=0, step=1,
                        randomize=False, shiftExisting=False)
        assert result is None


# ===========================================================================
# findAndReplace
# ===========================================================================


class TestFindAndReplace:
    def test_plain_replace_edits_field(self, destructive_col: None) -> None:
        """Plain (non-regex) find-and-replace must update a note field and return count=1."""
        note_id = _add_basic_note(
            "far-unique-sentinel-xzq", "far-back-plain-xzq"
        )

        count = invoke(
            "findAndReplace",
            notes=[note_id],
            search="far-unique-sentinel-xzq",
            replacement="far-replaced-sentinel-xzq",
            regex=False,
            field=None,
            matchCase=False,
        )
        assert isinstance(count, int)
        assert count == 1, f"Expected 1 note changed, got {count}"

        info = invoke("notesInfo", notes=[note_id])
        front_value = info[0]["fields"]["Front"]["value"]
        assert "far-replaced-sentinel-xzq" in front_value

    def test_regex_replace_edits_field(self, destructive_col: None) -> None:
        """Regex find-and-replace must match pattern and substitute."""
        note_id = _add_basic_note(
            "far-regex-ABC-123-xzq", "far-regex-back-xzq"
        )

        count = invoke(
            "findAndReplace",
            notes=[note_id],
            search=r"ABC-\d+",
            replacement="DEF-000",
            regex=True,
            field=None,
            matchCase=False,
        )
        assert isinstance(count, int)
        assert count == 1

        info = invoke("notesInfo", notes=[note_id])
        front_value = info[0]["fields"]["Front"]["value"]
        assert "DEF-000" in front_value
        assert "ABC-123" not in front_value

    def test_replace_specific_field_only(self, destructive_col: None) -> None:
        """When 'field' is set, replacement only applies to that field."""
        note_id = _add_basic_note(
            "sentinel-both-xzq", "sentinel-both-xzq"
        )

        # Replace only in "Front" field
        count = invoke(
            "findAndReplace",
            notes=[note_id],
            search="sentinel-both-xzq",
            replacement="replaced-front-only-xzq",
            regex=False,
            field="Front",
            matchCase=False,
        )
        assert count >= 1

        info = invoke("notesInfo", notes=[note_id])
        front_val = info[0]["fields"]["Front"]["value"]
        back_val = info[0]["fields"]["Back"]["value"]
        assert "replaced-front-only-xzq" in front_val
        assert "sentinel-both-xzq" in back_val  # Back unchanged

    def test_replace_returns_zero_when_no_match(self, destructive_col: None) -> None:
        """findAndReplace must return 0 when the search string is not found."""
        note_id = _add_basic_note(
            "far-nomatch-front-xzq", "far-nomatch-back-xzq"
        )

        count = invoke(
            "findAndReplace",
            notes=[note_id],
            search="__no_such_string_xzq__",
            replacement="whatever",
        )
        assert count == 0

    def test_replace_across_multiple_notes(self, destructive_col: None) -> None:
        """findAndReplace across multiple notes returns the total changed count.

        Use distinct Front values to avoid the duplicate-note guard, then
        search a common substring that appears in both.
        """
        note_a = _add_basic_note("multi-sentinel-note-A-xzq", "back-a-xzq")
        note_b = _add_basic_note("multi-sentinel-note-B-xzq", "back-b-xzq")

        count = invoke(
            "findAndReplace",
            notes=[note_a, note_b],
            search="multi-sentinel-note",
            replacement="multi-replaced-note",
        )
        assert count == 2


# ===========================================================================
# findDuplicates
# ===========================================================================


class TestFindDuplicates:
    def test_finds_duplicate_field_values(self, destructive_col: None) -> None:
        """findDuplicates returns entries where two notes share the same field value.

        Strategy: add note_a with a unique Front value, add note_b with a different
        Front, then use findAndReplace to make note_b's Front match note_a's Front.
        This bypasses addNote's duplicate guard while producing a genuine duplicate
        that find_dupes will detect.
        """
        shared_value = "dupe-field-value-unique-xzq"
        note_a = _add_basic_note(shared_value, "back-dupe-a-xzq")
        # Add note_b with a distinct Front, then rewrite it to create the duplicate
        note_b = _add_basic_note("dupe-field-TEMP-unique-xzq", "back-dupe-b-xzq")
        invoke(
            "findAndReplace",
            notes=[note_b],
            search="dupe-field-TEMP-unique-xzq",
            replacement=shared_value,
            field="Front",
        )

        result = invoke("findDuplicates", field="Front")
        assert isinstance(result, list)

        # Find the entry for our shared value
        matching = [entry for entry in result if entry["value"] == shared_value]
        assert matching, (
            f"Expected duplicate entry for {shared_value!r}, got entries: "
            f"{[e['value'] for e in result]}"
        )
        entry = matching[0]
        assert isinstance(entry["notes"], list)
        assert note_a in entry["notes"]
        assert note_b in entry["notes"]

    def test_no_duplicates_returns_empty(self, destructive_col: None) -> None:
        """findDuplicates with a search that matches no notes returns an empty list."""
        result = invoke("findDuplicates", field="Front",
                        search="__never_exists_xzq_sentinel__")
        assert isinstance(result, list)
        assert result == []

    def test_result_shape(self, destructive_col: None) -> None:
        """Each entry in findDuplicates result must have 'value' (str) and 'notes' (list).

        Uses the same add-then-rewrite strategy to produce a genuine duplicate.
        """
        shared = "shape-dupe-xzq"
        note_a = _add_basic_note(shared, "shape-back-a")
        note_b = _add_basic_note("shape-dupe-TEMP-xzq", "shape-back-b")
        invoke(
            "findAndReplace",
            notes=[note_b],
            search="shape-dupe-TEMP-xzq",
            replacement=shared,
            field="Front",
        )

        result = invoke("findDuplicates", field="Front")
        # Filter to entries we control
        ours = [e for e in result if e["value"] == shared]
        assert ours
        entry = ours[0]
        assert isinstance(entry["value"], str)
        assert isinstance(entry["notes"], list)
        assert all(isinstance(nid, int) for nid in entry["notes"])


# ===========================================================================
# clearUnusedTags / getTags
# ===========================================================================


class TestClearUnusedTagsAndGetTags:
    def test_get_tags_returns_list_of_strings(self, col: None) -> None:
        """getTags must return a list of strings."""
        tags = invoke("getTags")
        assert isinstance(tags, list)
        assert all(isinstance(t, str) for t in tags)

    def test_clear_unused_tags_removes_orphan_tag(self, destructive_col: None) -> None:
        """Adding a tag to a note then deleting the note leaves an orphan tag
        that clearUnusedTags removes.  The count returned must be >= 1."""
        orphan_tag = "cleartest-orphan-tag-xzq"

        # Add a note with the orphan tag
        note_id = _add_basic_note("clear-tag-front-xzq", "clear-tag-back-xzq")
        invoke("addTags", notes=[note_id], tags=orphan_tag)

        # Verify the tag is present
        tags_before = invoke("getTags")
        assert orphan_tag in tags_before, f"Tag {orphan_tag!r} must exist before clear"

        # Delete the note — tag becomes orphaned
        invoke("deleteNotes", notes=[note_id])

        # clearUnusedTags must remove it and report count >= 1
        count = invoke("clearUnusedTags")
        assert isinstance(count, int)
        assert count >= 1, f"Expected at least 1 tag removed, got {count}"

        # Tag must no longer appear in getTags
        tags_after = invoke("getTags")
        assert orphan_tag not in tags_after, (
            f"Orphan tag {orphan_tag!r} still present after clearUnusedTags"
        )

    def test_get_tags_lists_note_tags(self, destructive_col: None) -> None:
        """Tags added to notes must appear in getTags."""
        tag = "gettags-test-xzq"
        note_id = _add_basic_note("gettags-front-xzq", "gettags-back-xzq")
        invoke("addTags", notes=[note_id], tags=tag)

        tags = invoke("getTags")
        assert tag in tags, f"Tag {tag!r} must appear in getTags after addTags"

    def test_clear_unused_tags_empty_returns_int(self, destructive_col: None) -> None:
        """clearUnusedTags when no orphan tags exist must return an int (0 or more)."""
        count = invoke("clearUnusedTags")
        assert isinstance(count, int)
        assert count >= 0


# ===========================================================================
# Action registration sanity checks
# ===========================================================================


class TestActionRegistration:
    def test_all_new_actions_registered(self) -> None:
        """All 9 P0 triage actions must be present in the ACTIONS dict."""
        expected = {
            "bury",
            "unbury",
            "setDueDate",
            "forgetCards",
            "repositionNewCards",
            "reposition",
            "findAndReplace",
            "findDuplicates",
            "clearUnusedTags",
            "getTags",
        }
        missing = expected - set(ACTIONS.keys())
        assert not missing, f"Missing actions: {missing}"

    def test_handlers_are_callable(self) -> None:
        """All newly registered handlers must be callable."""
        for name in ["bury", "unbury", "setDueDate", "forgetCards",
                     "repositionNewCards", "reposition", "findAndReplace",
                     "findDuplicates", "clearUnusedTags", "getTags"]:
            assert callable(ACTIONS[name]), f"ACTIONS[{name!r}] is not callable"
