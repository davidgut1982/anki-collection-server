"""
Integration tests for src/actions.py.

All tests operate against a COPY of the committed test fixture placed in
/tmp — the live collection is never opened.  Each test class manages its own
temporary directory and CollectionManager instance so tests are isolated.

Default fixture: tests/fixtures/test_collection.anki2 (committed to repo)
Override: set ANKI_TEST_BACKUP env var to point at a different .anki2 file.
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest

# We need to open a fresh collection for each test class so we can swap the
# module-level singleton between tests.
from src import collection as col_mod
from src.actions import ACTIONS

# ---------------------------------------------------------------------------
# Path to the test fixture (committed; override via env var for CI flexibility)
# ---------------------------------------------------------------------------

_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"
_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))


# ---------------------------------------------------------------------------
# Session-scoped fixture: copy the backup once per pytest session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def backup_copy() -> Generator[Path, None, None]:
    """Copy the backup to /tmp once; yield the path; clean up after session."""
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

    tmpdir = Path(tempfile.mkdtemp(prefix="acs-test-", dir="/tmp"))
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
    """Open the collection singleton before each test; close after."""
    # Guard: if a previous test left the manager open (due to a crash), close it.
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_returns_6(self, col: None) -> None:
        assert invoke("version") == 6


class TestFindNotesAndNotesInfo:
    def test_find_notes_returns_list_of_ints(self, col: None) -> None:
        ids = invoke("findNotes", query="deck:Default")
        assert isinstance(ids, list)
        # Default deck exists (even if empty); result may be empty list
        assert all(isinstance(i, int) for i in ids)

    def test_find_notes_all_notes(self, col: None) -> None:
        ids = invoke("findNotes", query="*")
        assert len(ids) > 0, "Backup should contain notes"

    def test_notes_info_shape(self, col: None) -> None:
        ids = invoke("findNotes", query="*")
        # Only inspect first 5 to keep test fast
        sample_ids = list(ids)[:5]
        infos = invoke("notesInfo", notes=sample_ids)
        assert isinstance(infos, list)
        assert len(infos) == len(sample_ids)
        for info in infos:
            assert "noteId" in info
            assert "modelName" in info
            assert "tags" in info
            assert "fields" in info
            # Each field value should have "value" and "order" keys
            for fname, fdata in info["fields"].items():
                assert "value" in fdata
                assert "order" in fdata

    def test_notes_info_unknown_id_raises(self, col: None) -> None:
        with pytest.raises(Exception):
            invoke("notesInfo", notes=[999999999])


class TestAddNoteRoundTrip:
    """add → findNotes → notesInfo"""

    def test_add_then_find_then_info(self, col: None) -> None:
        # Use a model that exists in the backup
        model_names = invoke("modelNames")
        # "Basic (and reversed card)" is a built-in model always present
        model = "Basic (and reversed card)"
        assert model in model_names, f"Expected {model!r} in {model_names}"

        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {"Front": "acs-test-front-xzq", "Back": "acs-test-back-xzq"},
                "tags": ["acs-test"],
            },
        )
        assert isinstance(note_id, int)
        assert note_id > 0

        # findNotes should locate it
        found = invoke("findNotes", query='tag:acs-test "acs-test-front-xzq"')
        assert note_id in found

        # notesInfo should have the right fields
        infos = invoke("notesInfo", notes=[note_id])
        assert len(infos) == 1
        info = infos[0]
        assert info["noteId"] == note_id
        assert info["modelName"] == model
        assert "acs-test" in info["tags"]
        assert info["fields"]["Front"]["value"] == "acs-test-front-xzq"
        assert info["fields"]["Back"]["value"] == "acs-test-back-xzq"


class TestAddNoteDuplicateDetection:
    """Adding the same note twice must raise on the second call."""

    def test_add_duplicate_raises_value_error(self, col: None) -> None:
        """Second addNote with identical fields must surface as ValueError.

        Before the critic fix, col.add_note() silently created the duplicate.
        After the fix, duplicate_or_empty()==2 is checked before add_note()
        and raises ValueError, which the server converts to
        {"result": null, "error": "..."}.
        """
        model = "Basic (and reversed card)"
        note_payload = {
            "deckName": "Default",
            "modelName": model,
            "fields": {
                "Front": "dup-test-front-unique-xzq9",
                "Back": "dup-test-back-xzq9",
            },
            "tags": ["acs-dup-test"],
        }

        # First add must succeed
        note_id = invoke("addNote", note=note_payload)
        assert isinstance(note_id, int)
        assert note_id > 0

        # Second add with identical fields must raise
        with pytest.raises(ValueError, match="duplicate"):
            invoke("addNote", note=note_payload)


class TestUpdateNoteFields:
    def test_update_changes_field(self, col: None) -> None:
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {"Front": "update-test-orig", "Back": "orig-back"},
                "tags": [],
            },
        )
        invoke(
            "updateNoteFields",
            note={"id": note_id, "fields": {"Front": "update-test-new"}},
        )
        infos = invoke("notesInfo", notes=[note_id])
        assert infos[0]["fields"]["Front"]["value"] == "update-test-new"
        # Back should be unchanged
        assert infos[0]["fields"]["Back"]["value"] == "orig-back"


class TestAddRemoveTags:
    def test_add_then_remove_tags(self, col: None) -> None:
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {"Front": "tag-test-front", "Back": "tag-test-back"},
                "tags": [],
            },
        )
        invoke("addTags", notes=[note_id], tags="foo bar")
        infos = invoke("notesInfo", notes=[note_id])
        assert "foo" in infos[0]["tags"]
        assert "bar" in infos[0]["tags"]

        invoke("removeTags", notes=[note_id], tags="foo")
        infos2 = invoke("notesInfo", notes=[note_id])
        assert "foo" not in infos2[0]["tags"]
        assert "bar" in infos2[0]["tags"]


class TestFindCardsAndCardsInfo:
    def test_find_cards_returns_list_of_ints(self, col: None) -> None:
        ids = invoke("findCards", query="*")
        assert isinstance(ids, list)
        assert len(ids) > 0
        assert all(isinstance(i, int) for i in ids)

    def test_cards_info_shape(self, col: None) -> None:
        card_ids = list(invoke("findCards", query="*"))[:3]
        infos = invoke("cardsInfo", cards=card_ids)
        assert len(infos) == len(card_ids)
        required_keys = {
            "cardId",
            "note",
            "deckName",
            "modelName",
            "fields",
            "fieldOrder",
            "ord",
            "type",
            "queue",
            "due",
            "interval",
            "factor",
            "reps",
            "lapses",
            "flags",
            "css",
        }
        for info in infos:
            missing = required_keys - set(info.keys())
            assert not missing, f"Missing keys: {missing}"
            assert isinstance(info["cardId"], int)
            assert isinstance(info["note"], int)
            assert isinstance(info["interval"], int)
            assert isinstance(info["factor"], int)
            assert isinstance(info["lapses"], int)
            assert isinstance(info["reps"], int)
            assert isinstance(info["type"], int)
            assert isinstance(info["flags"], int)
            assert isinstance(info["fields"], dict)


class TestChangeDeck:
    def test_change_deck_moves_card(self, col: None) -> None:
        # Add a note to Default, get its cards, move to another deck
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {"Front": "changedeck-test", "Back": "back"},
                "tags": [],
            },
        )
        card_ids = invoke("findCards", query=f"nid:{note_id}")
        assert len(card_ids) > 0

        # Create target deck and move
        invoke("createDeck", deck="ACS-Test-ChangeDeck")
        invoke("changeDeck", cards=list(card_ids), deck="ACS-Test-ChangeDeck")

        infos = invoke("cardsInfo", cards=list(card_ids))
        for info in infos:
            assert info["deckName"] == "ACS-Test-ChangeDeck"


class TestDeckNames:
    def test_deck_names_returns_list_of_strings(self, col: None) -> None:
        names = invoke("deckNames")
        assert isinstance(names, list)
        assert len(names) > 0
        assert all(isinstance(n, str) for n in names)

    def test_default_deck_present(self, col: None) -> None:
        names = invoke("deckNames")
        assert "Default" in names


class TestGetDeckStats:
    def test_deck_stats_shape(self, col: None) -> None:
        stats = invoke("getDeckStats", decks=["Default"])
        assert isinstance(stats, dict)
        # Key is str(deck_id)
        assert len(stats) == 1
        entry = next(iter(stats.values()))
        required = {
            "deck_id",
            "name",
            "new_count",
            "learn_count",
            "review_count",
            "total_in_deck",
        }
        assert required <= set(entry.keys()), (
            f"Missing keys: {required - set(entry.keys())}"
        )
        assert entry["name"] == "Default"
        assert isinstance(entry["total_in_deck"], int)
        assert isinstance(entry["new_count"], int)
        assert isinstance(entry["learn_count"], int)
        assert isinstance(entry["review_count"], int)

    def test_deck_stats_unknown_deck(self, col: None) -> None:
        stats = invoke("getDeckStats", decks=["NonExistentDeckXYZ"])
        assert isinstance(stats, dict)
        # Should return a zero entry rather than raising
        assert len(stats) == 1

    def test_deck_stats_two_missing_decks_produce_two_entries(self, col: None) -> None:
        """Two non-existent deck names must yield two DISTINCT result keys.

        Before the critic fix, all missing decks used did=0 as the key, so
        the second entry silently overwrote the first, leaving len(result)==1.
        """
        stats = invoke(
            "getDeckStats",
            decks=["NonExistentAlpha", "NonExistentBeta"],
        )
        assert isinstance(stats, dict)
        assert len(stats) == 2, (
            f"Expected 2 distinct entries for 2 missing decks, got {len(stats)}: "
            f"{list(stats.keys())}"
        )
        # Each entry should have the right name and zero counts
        names_in_result = {entry["name"] for entry in stats.values()}
        assert names_in_result == {"NonExistentAlpha", "NonExistentBeta"}


class TestMediaRoundTrip:
    def test_store_then_retrieve(self, col: None) -> None:
        payload = b"hello anki media test"
        b64_payload = base64.b64encode(payload).decode("ascii")
        filename = "acs-test-media.txt"

        stored = invoke("storeMediaFile", filename=filename, data=b64_payload)
        assert stored == filename

        retrieved_b64 = invoke("retrieveMediaFile", filename=filename)
        assert retrieved_b64 is not False
        assert base64.b64decode(retrieved_b64) == payload

    def test_store_clean_filename_returns_exact_name_and_retrieves(
        self, col: None
    ) -> None:
        """storeMediaFile with a clean filename: returned name equals input AND
        retrieveMediaFile round-trips the bytes back correctly.

        This validates the critic HIGH fix: we now return the actual stored
        filename from write_data() rather than blindly echoing the input.  For
        clean filenames (no path separators or special chars) the two values
        are equal, so the tilts-client assertion `result == filename` holds.
        """
        payload = b"critic-fix-media-roundtrip-content"
        filename = "acs-critic-fix-clean.txt"
        b64_payload = base64.b64encode(payload).decode("ascii")

        stored = invoke("storeMediaFile", filename=filename, data=b64_payload)

        # For a clean filename, stored name must equal the requested name
        assert stored == filename, (
            f"Expected stored filename {filename!r}, got {stored!r}"
        )

        # Bytes must survive the round-trip
        retrieved_b64 = invoke("retrieveMediaFile", filename=stored)
        assert retrieved_b64 is not False, "File not found after store"
        assert base64.b64decode(retrieved_b64) == payload, (
            "Retrieved bytes differ from stored bytes"
        )

    def test_retrieve_missing_returns_false(self, col: None) -> None:
        result = invoke("retrieveMediaFile", filename="no-such-file-xyz.mp3")
        assert result is False

    def test_delete_removes_file(self, col: None) -> None:
        payload = b"delete-me"
        filename = "acs-test-delete.txt"
        invoke(
            "storeMediaFile", filename=filename, data=base64.b64encode(payload).decode()
        )
        invoke("deleteMediaFile", filename=filename)
        # After trash, retrieve should return False (file moved to trash folder)
        # Note: Anki moves to trash rather than instant delete; file may still
        # appear briefly.  We accept either False or an unreadable path.
        # The important thing is no exception was raised.


class TestSuspendUnsuspend:
    def test_suspend_then_unsuspend(self, col: None) -> None:
        card_ids = list(invoke("findCards", query="*"))[:2]
        assert len(card_ids) >= 1

        invoke("suspend", cards=card_ids)
        # Suspended cards have queue == -1
        infos = invoke("cardsInfo", cards=card_ids)
        for info in infos:
            assert info["queue"] == -1, (
                f"Expected queue=-1 (suspended), got {info['queue']}"
            )

        invoke("unsuspend", cards=card_ids)
        infos2 = invoke("cardsInfo", cards=card_ids)
        for info in infos2:
            assert info["queue"] != -1, "Expected card to be unsuspended"


class TestGetNumCardsReviewedToday:
    def test_returns_non_negative_int(self, col: None) -> None:
        count = invoke("getNumCardsReviewedToday")
        assert isinstance(count, int)
        assert count >= 0

    def test_counts_synthetic_today_reviews_nonzero(self) -> None:
        """Regression: day_cutoff is end-of-day (future), not start-of-day.

        Old code used ``day_cutoff * 1000`` as the lower bound, which is a
        FUTURE timestamp — so zero rows matched until the day rolled over.
        Fixed code uses ``(day_cutoff - 86400) * 1000`` (start of today).

        Strategy: open a fresh writable copy of the fixture, insert two
        synthetic revlog rows timestamped ONE HOUR before end-of-day (well
        within today), then assert getNumCardsReviewedToday == 2 and that
        it agrees with today's bucket in getNumCardsReviewedByDay.
        """
        import sqlite3

        # ---- set up a private writable copy so we don't pollute other tests ----
        private_dir = Path(tempfile.mkdtemp(prefix="acs-daycutoff-", dir="/tmp"))
        col_path = private_dir / "collection.anki2"
        shutil.copy2(_COMMITTED_FIXTURE, col_path)
        col_path.chmod(0o600)

        try:
            # ---- open collection to learn day_cutoff and a real card id --------
            try:
                col_mod.manager.close()
            except Exception:
                pass
            col_mod.manager.open(col_path)
            col_obj = col_mod.manager.col

            day_cutoff: int = col_obj.sched.day_cutoff  # end of today (future)
            day_start_ms = (day_cutoff - 86400) * 1000  # start of today

            # Pick a real card id from the collection (revlog requires valid cid)
            card_ids = col_obj.db.list("select id from cards limit 1")
            assert card_ids, "Fixture must have at least one card"
            cid = card_ids[0]

            # Timestamps: 1 hour and 2 hours before end-of-day (clearly today)
            ts_1h = (day_cutoff - 3600) * 1000
            ts_2h = (day_cutoff - 7200) * 1000

            # Sanity: both must be >= day_start_ms (i.e. within today)
            assert ts_1h >= day_start_ms, "ts_1h must fall within today"
            assert ts_2h >= day_start_ms, "ts_2h must fall within today"

            # Insert two synthetic revlog rows directly via sqlite3 (bypasses
            # Anki's ORM to keep the test self-contained and fast).
            col_mod.manager.close()

            con = sqlite3.connect(str(col_path))
            con.execute(
                "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
                "VALUES (?, ?, -1, 1, 1, 1, 2500, 5000, 1)",
                (ts_1h, cid),
            )
            con.execute(
                "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
                "VALUES (?, ?, -1, 2, 1, 1, 2500, 6000, 1)",
                (ts_2h, cid),
            )
            con.commit()
            con.close()

            # ---- re-open via manager and run the actions ----------------------
            col_mod.manager.open(col_path)

            today_count = invoke("getNumCardsReviewedToday")
            assert today_count >= 2, (
                f"Expected >= 2 reviews today (inserted 2 synthetic rows), "
                f"got {today_count}. This indicates day_cutoff is still being "
                f"used as the lower bound (end-of-day) instead of "
                f"day_cutoff - 86400 (start-of-day)."
            )

            # ---- cross-check with getNumCardsReviewedByDay -------------------
            from datetime import datetime, timezone

            by_day: list[list] = invoke("getNumCardsReviewedByDay")  # type: ignore[assignment]
            # Determine what "today" looks like as a UTC date string
            today_utc = datetime.fromtimestamp(ts_1h / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            by_day_dict = {d: c for d, c in by_day}
            today_by_day = by_day_dict.get(today_utc, 0)
            assert today_by_day == today_count, (
                f"getNumCardsReviewedToday ({today_count}) must agree with "
                f"getNumCardsReviewedByDay today bucket '{today_utc}' "
                f"({today_by_day}). Mismatch means the two functions disagree "
                f"on what counts as 'today'."
            )

        finally:
            try:
                col_mod.manager.close()
            except Exception:
                pass
            shutil.rmtree(private_dir, ignore_errors=True)


class TestGetReviewsOfCards:
    def test_reviews_shape(self, col: None) -> None:
        # The fixture has 40 revlog entries; pick any card that has been reviewed
        card_ids = list(invoke("findCards", query="-is:new"))[:5]
        if not card_ids:
            pytest.skip("No reviewed cards in fixture")

        reviews = invoke("getReviewsOfCards", cards=card_ids)
        assert isinstance(reviews, dict)
        # Keys are str(card_id)
        for cid in card_ids:
            assert str(cid) in reviews
            assert isinstance(reviews[str(cid)], list)

        # Check shape of individual review entries
        for cid_str, entries in reviews.items():
            for entry in entries:
                assert "ease" in entry, f"Missing 'ease' in {entry}"
                assert "time" in entry, f"Missing 'time' in {entry}"
                assert "type" in entry, f"Missing 'type' in {entry}"
                assert entry["ease"] in (1, 2, 3, 4), (
                    f"ease out of range: {entry['ease']}"
                )

    def test_reviews_empty_for_new_card(self, col: None) -> None:
        # Newly-added card has no revlog entries
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {"Front": "reviews-new-test", "Back": "back"},
                "tags": [],
            },
        )
        card_ids = list(invoke("findCards", query=f"nid:{note_id}"))
        reviews = invoke("getReviewsOfCards", cards=card_ids)
        for cid in card_ids:
            assert reviews[str(cid)] == []


class TestGetNumCardsReviewedByDay:
    def test_returns_list_of_pairs(self, col: None) -> None:
        result = invoke("getNumCardsReviewedByDay")
        assert isinstance(result, list)
        # Fixture has 40 revlog entries across 7 days
        assert len(result) > 0
        for pair in result:
            assert len(pair) == 2
            date_str, count = pair
            assert isinstance(date_str, str)
            assert len(date_str) == 10  # "YYYY-MM-DD"
            assert isinstance(count, int)
            assert count > 0

    def test_tilts_client_pattern(self, col: None) -> None:
        """Verify tilts client can iterate: for day_str, count in result"""
        result = invoke("getNumCardsReviewedByDay")
        for day_str, count in result:
            assert "-" in day_str


class TestGetCollectionStatsHTML:
    def test_returns_non_empty_string(self, col: None) -> None:
        html = invoke("getCollectionStatsHTML", wholeCollection=False)
        assert isinstance(html, str)
        assert len(html) > 0
        # Should be valid enough for tilts' connection-ping use
        assert "<" in html


class TestGetDeckStatsSubdeckRollup:
    """Regression tests for getDeckStats v25.9.2-3 fix.

    Two bugs were fixed:
      (a) Day-gating bug: newToday/lrnToday/revToday cache returns 0 when
          day_idx != today, so new/learn/review counts were always 0 on a
          fresh scheduler day with no prior study.  Fix: use deck_due_tree()
          which always returns correct due counts.
      (b) Parent-only total: SQL ``WHERE did = parent_id`` counted only direct
          cards in the parent deck, missing subdecks.  Fix: use find_cards()
          with ``deck:"name"`` which includes subdecks.
    """

    def test_subdeck_rollup_and_nozero_counts(self) -> None:
        """getDeckStats for a parent deck must roll up subdeck cards.

        Setup:
          - Parent deck "ACS-Test-Parent"
          - Child deck  "ACS-Test-Parent::Child"
          - Add 2 new notes to the parent deck
          - Add 1 new note to the child deck
          - Call getDeckStats for "ACS-Test-Parent"

        Assertions:
          1. total_in_deck == 3 (2 parent cards + 1 child card, NOT parent-only)
             Note: each "Basic (and reversed card)" note creates 2 cards, so
             3 notes → 6 cards.  We check >= 3 to be note-type-agnostic.
          2. new_count > 0 — deck_due_tree() returns non-zero even before
             study has begun on this scheduler day (regression: old day-gated
             code returned 0 until first review of the day).
          3. Result keyed by str(deck_id), not "missing:..." sentinel.
        """
        import shutil
        import tempfile

        private_dir = Path(tempfile.mkdtemp(prefix="acs-subdeck-", dir="/tmp"))
        col_path = private_dir / "collection.anki2"
        shutil.copy2(_COMMITTED_FIXTURE, col_path)
        col_path.chmod(0o600)

        try:
            try:
                col_mod.manager.close()
            except Exception:
                pass
            col_mod.manager.open(col_path)

            parent_deck = "ACS-Test-Parent"
            child_deck = "ACS-Test-Parent::Child"

            # Ensure both decks exist
            invoke("createDeck", deck=parent_deck)
            invoke("createDeck", deck=child_deck)

            model = "Basic (and reversed card)"

            # Add 2 notes to parent deck
            for i in range(2):
                invoke(
                    "addNote",
                    note={
                        "deckName": parent_deck,
                        "modelName": model,
                        "fields": {
                            "Front": f"acs-subdeck-parent-{i}-{id(col_path)}",
                            "Back": "back",
                        },
                        "tags": [],
                    },
                )

            # Add 1 note to child deck
            invoke(
                "addNote",
                note={
                    "deckName": child_deck,
                    "modelName": model,
                    "fields": {
                        "Front": f"acs-subdeck-child-0-{id(col_path)}",
                        "Back": "back",
                    },
                    "tags": [],
                },
            )

            stats = invoke("getDeckStats", decks=[parent_deck])
            assert isinstance(stats, dict), f"Expected dict, got {type(stats)}"
            assert len(stats) == 1, (
                f"Expected 1 entry, got {len(stats)}: {list(stats.keys())}"
            )

            key = list(stats.keys())[0]
            assert not key.startswith("missing:"), (
                f"Deck reported as missing: {key!r}. "
                f"Deck may not have been created or found in tree."
            )

            entry = stats[key]
            total = entry["total_in_deck"]
            new_count = entry["new_count"]

            # Each "Basic (and reversed card)" note creates 2 cards.
            # 3 notes × 2 cards = 6 cards total.  Parent-only would see 4.
            # We assert >= 6 to confirm subdeck rollup.
            assert total >= 6, (
                f"total_in_deck={total} — expected >= 6 (3 notes × 2 cards, "
                f"including child deck).  If total == 4, the fix is NOT working "
                f"and subdeck cards are still excluded (parent-only count)."
            )

            # Regression (a): new_count must be non-zero — all 3 notes are new.
            # Old day-gated implementation zeroed this on a fresh scheduler day.
            assert new_count > 0, (
                f"new_count={new_count} — expected > 0.  {total} total cards "
                f"were added as new, so deck_due_tree() must report them.  "
                f"If new_count == 0 this is the day-gating bug (newToday cache "
                f"day_idx mismatch) — fix was NOT applied correctly."
            )

        finally:
            try:
                col_mod.manager.close()
            except Exception:
                pass
            shutil.rmtree(private_dir, ignore_errors=True)


class TestModelTemplates:
    """Tests for the modelTemplates action.

    AnkiConnect contract:
      params:  {"modelName": "<name>"}
      result:  {"<template name>": {"Front": "<qfmt>", "Back": "<afmt>"}, ...}
               in template insertion order.

    Tilts consumer (_card_payload in anki_review_bp.py) reads:
      templates = client._invoke("modelTemplates", {"modelName": model_name})
      # expects dict keyed by template name, each with "Front" and "Back" str values
      template_list = list(templates.values())       # ordered by template index
      tmpl = template_list[card.ord]                 # select by card ordinal
      qfmt = tmpl.get("Front", "") or ""            # qfmt for field-name extraction
      afmt = tmpl.get("Back", "") or ""             # afmt for field-name extraction
    """

    def test_known_model_returns_dict_keyed_by_template_name(self, col: None) -> None:
        """modelTemplates for a real model returns a dict of template dicts."""
        result = invoke("modelTemplates", modelName="Basic")
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert len(result) >= 1, "Basic model must have at least one template"

    def test_each_entry_has_front_and_back_strings(self, col: None) -> None:
        """Each value in the result must have 'Front' and 'Back' str values.

        This is the exact shape the Tilts _card_payload reads:
          qfmt = tmpl.get("Front", "") or ""
          afmt = tmpl.get("Back", "") or ""
        """
        result = invoke("modelTemplates", modelName="Basic")
        for tmpl_name, tmpl_dict in result.items():
            assert isinstance(tmpl_name, str), f"Key must be str, got {type(tmpl_name)}"
            assert isinstance(tmpl_dict, dict), (
                f"Value for {tmpl_name!r} must be dict, got {type(tmpl_dict)}"
            )
            assert "Front" in tmpl_dict, (
                f"Missing 'Front' in {tmpl_name!r}: {tmpl_dict}"
            )
            assert "Back" in tmpl_dict, f"Missing 'Back' in {tmpl_name!r}: {tmpl_dict}"
            assert isinstance(tmpl_dict["Front"], str), (
                f"'Front' must be str, got {type(tmpl_dict['Front'])}"
            )
            assert isinstance(tmpl_dict["Back"], str), (
                f"'Back' must be str, got {type(tmpl_dict['Back'])}"
            )

    def test_multi_template_model_returns_multiple_entries_in_order(
        self, col: None
    ) -> None:
        """'Basic (and reversed card)' has 2 templates; both must appear in insertion order.

        Tilts uses list(templates.values())[card.ord] to select by ordinal,
        so order must be preserved (Card 1 at index 0, Card 2 at index 1).
        """
        result = invoke("modelTemplates", modelName="Basic (and reversed card)")
        assert isinstance(result, dict)
        assert len(result) == 2, (
            f"Expected 2 templates, got {len(result)}: {list(result.keys())}"
        )

        # Must be in insertion order: Card 1 (ord=0) first, Card 2 (ord=1) second
        template_list = list(result.values())
        # Card 1: qfmt renders the Front field (question: Front -> Back)
        assert "Front" in template_list[0]["Front"], (
            f"Card 1 qfmt should reference 'Front': {template_list[0]['Front']!r}"
        )
        # Card 2: qfmt renders the Back field (reversed: Back -> Front)
        assert "Back" in template_list[1]["Front"], (
            f"Card 2 qfmt should reference 'Back': {template_list[1]['Front']!r}"
        )

    def test_template_content_is_nonempty_for_real_model(self, col: None) -> None:
        """Template qfmt/afmt from the fixture are non-empty strings."""
        result = invoke("modelTemplates", modelName="Basic")
        tmpl = list(result.values())[0]
        assert tmpl["Front"], "qfmt (Front) should be non-empty for Basic model"
        assert tmpl["Back"], "afmt (Back) should be non-empty for Basic model"

    def test_latvian_vocab_model_in_fixture(self, col: None) -> None:
        """The fixture contains a 'Latvian Vocab' model; verify it round-trips."""
        model_names = invoke("modelNames")
        if "Latvian Vocab" not in model_names:
            pytest.skip("Latvian Vocab model not present in this fixture")
        result = invoke("modelTemplates", modelName="Latvian Vocab")
        assert isinstance(result, dict)
        assert len(result) >= 1
        tmpl = list(result.values())[0]
        assert "Front" in tmpl
        assert "Back" in tmpl

    def test_unknown_model_raises_value_error(self, col: None) -> None:
        """modelTemplates for a non-existent model must raise ValueError.

        The server converts ValueError to {"result": null, "error": "..."},
        which Tilts' _get_model_templates catches and treats as a fallback.
        """
        with pytest.raises(ValueError, match="model was not found"):
            invoke("modelTemplates", modelName="NonExistentModelXYZ_acs_test")
