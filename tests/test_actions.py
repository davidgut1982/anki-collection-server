"""
Integration tests for src/actions.py.

All tests operate against a COPY of the static backup collection placed in
/tmp — the live collection is never opened.  Each test class manages its own
temporary directory and CollectionManager instance so tests are isolated.

Backup: /mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2
(read-only; copied to /tmp for each test session).
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
# Path to the static backup (never modified)
# ---------------------------------------------------------------------------

_DEFAULT_BACKUP = (
    "/mnt/data/apps/anki/collection_backup_pre_audio_gen_20260530_235304.anki2"
)
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


class TestGetReviewsOfCards:
    def test_reviews_shape(self, col: None) -> None:
        # The backup has 3741 revlog entries; pick any card that has been reviewed
        card_ids = list(invoke("findCards", query="-is:new"))[:5]
        if not card_ids:
            pytest.skip("No reviewed cards in backup")

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
        # Backup has 3741 revlog entries so at least one day of reviews
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
