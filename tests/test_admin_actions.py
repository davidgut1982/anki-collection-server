"""
Integration tests for the admin CRUD actions added in feat/admin-actions.

Actions under test:
  - deleteNotes         (destructive write)
  - deleteDecks         (destructive write)
  - renameDeck          (write)
  - modelFieldNames     (read-only)
  - findCardsPaginated  (read-only)

All destructive tests operate on a fresh per-test copy of the fixture placed
in /tmp — the committed fixture is NEVER modified directly.

Pattern mirrors tests/test_actions.py exactly:
  - _COMMITTED_FIXTURE / BACKUP path resolution
  - backup_copy session fixture (read-only tests only need the shared copy)
  - destructive_col function fixture (copies fixture fresh for each destructive test)
  - invoke() helper
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
            f"Set ANKI_TEST_BACKUP env var to a readable .anki2 file."
        )
    if not os.access(BACKUP, os.R_OK):
        pytest.fail(
            f"Test backup not accessible (permission denied): {BACKUP}."
        )
    tmpdir = Path(tempfile.mkdtemp(prefix="acs-admin-test-", dir="/tmp"))
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

    Destructive tests (deleteNotes, deleteDecks) use this fixture so they
    never touch the session-shared copy.
    """
    if not BACKUP.exists():
        pytest.fail(f"Test backup not found: {BACKUP}.")
    private_dir = Path(tempfile.mkdtemp(prefix="acs-admin-destructive-", dir="/tmp"))
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


# ===========================================================================
# deleteNotes
# ===========================================================================


class TestDeleteNotes:
    def test_delete_removes_note(self, destructive_col: None) -> None:
        """Add a note, delete it via deleteNotes, verify findNotes no longer finds it."""
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {
                    "Front": "admin-delete-test-front-xzq",
                    "Back": "admin-delete-test-back-xzq",
                },
                "tags": ["admin-delete-test"],
            },
        )
        assert isinstance(note_id, int)

        # Verify it exists before deletion
        found_before = invoke("findNotes", query='tag:admin-delete-test')
        assert note_id in found_before

        invoke("deleteNotes", notes=[note_id])

        # Must no longer be found
        found_after = invoke("findNotes", query='tag:admin-delete-test')
        assert note_id not in found_after

    def test_delete_empty_list_is_noop(self, destructive_col: None) -> None:
        """deleteNotes with an empty list must not raise."""
        result = invoke("deleteNotes", notes=[])
        assert result is None

    def test_delete_removes_associated_cards(self, destructive_col: None) -> None:
        """Cards belonging to deleted notes must also disappear."""
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "Default",
                "modelName": model,
                "fields": {
                    "Front": "admin-delete-cards-front-xzq",
                    "Back": "admin-delete-cards-back-xzq",
                },
                "tags": [],
            },
        )
        # "Basic (and reversed card)" creates 2 cards
        card_ids = list(invoke("findCards", query=f"nid:{note_id}"))
        assert len(card_ids) >= 1

        invoke("deleteNotes", notes=[note_id])

        # Cards must be gone too
        cards_after = list(invoke("findCards", query=f"nid:{note_id}"))
        assert cards_after == []


# ===========================================================================
# deleteDecks
# ===========================================================================


class TestDeleteDecks:
    def test_delete_deck_removes_it_from_deck_names(self, destructive_col: None) -> None:
        """Create a deck with a card, delete it, verify it is absent from deckNames."""
        invoke("createDeck", deck="AdminTestDeleteDeck-xzq")
        model = "Basic (and reversed card)"
        invoke(
            "addNote",
            note={
                "deckName": "AdminTestDeleteDeck-xzq",
                "modelName": model,
                "fields": {
                    "Front": "admin-deck-delete-front-xzq",
                    "Back": "admin-deck-delete-back-xzq",
                },
                "tags": [],
            },
        )
        names_before = invoke("deckNames")
        assert "AdminTestDeleteDeck-xzq" in names_before

        invoke("deleteDecks", decks=["AdminTestDeleteDeck-xzq"])

        names_after = invoke("deckNames")
        assert "AdminTestDeleteDeck-xzq" not in names_after

    def test_delete_deck_by_integer_id(self, destructive_col: None) -> None:
        """deleteDecks accepts integer deck ids as well as string names."""
        did = invoke("createDeck", deck="AdminTestDeleteDeckById-xzq")
        assert isinstance(did, int)

        invoke("deleteDecks", decks=[did])

        names_after = invoke("deckNames")
        assert "AdminTestDeleteDeckById-xzq" not in names_after

    def test_delete_default_deck_is_skipped(self, destructive_col: None) -> None:
        """Attempting to delete the Default deck (id=1) must silently skip it.

        The Default deck is Anki-internal and cannot be removed.  The handler
        skips id=1 rather than raising so callers do not need to guard.
        """
        names_before = invoke("deckNames")
        assert "Default" in names_before

        # Should not raise, should not remove Default
        result = invoke("deleteDecks", decks=[1])
        assert result is None

        names_after = invoke("deckNames")
        assert "Default" in names_after

    def test_delete_nonexistent_deck_name_is_skipped(self, destructive_col: None) -> None:
        """Non-existent deck name is skipped without raising."""
        result = invoke("deleteDecks", decks=["NonExistentDeckXYZ-admin-xzq"])
        assert result is None

    def test_delete_deck_removes_cards(self, destructive_col: None) -> None:
        """Cards inside a deleted deck must also be removed."""
        invoke("createDeck", deck="AdminCardDeleteDeck-xzq")
        model = "Basic (and reversed card)"
        note_id = invoke(
            "addNote",
            note={
                "deckName": "AdminCardDeleteDeck-xzq",
                "modelName": model,
                "fields": {
                    "Front": "admin-card-delete-front-xzq",
                    "Back": "admin-card-delete-back-xzq",
                },
                "tags": [],
            },
        )
        card_ids = list(invoke("findCards", query=f"nid:{note_id}"))
        assert len(card_ids) >= 1

        invoke("deleteDecks", decks=["AdminCardDeleteDeck-xzq"])

        # Cards must be gone (col.decks.remove deletes cards unconditionally)
        cards_after = list(invoke("findCards", query=f"nid:{note_id}"))
        assert cards_after == []


# ===========================================================================
# renameDeck
# ===========================================================================


class TestRenameDeck:
    def test_rename_updates_deck_name(self, destructive_col: None) -> None:
        """Create a deck, rename it, verify the new name appears in deckNames."""
        invoke("createDeck", deck="AdminRenameOld-xzq")

        invoke("renameDeck", deck="AdminRenameOld-xzq", newName="AdminRenameNew-xzq")

        names = invoke("deckNames")
        assert "AdminRenameNew-xzq" in names
        assert "AdminRenameOld-xzq" not in names

    def test_rename_by_integer_id(self, destructive_col: None) -> None:
        """renameDeck accepts an integer deck id."""
        did = invoke("createDeck", deck="AdminRenameById-xzq")
        assert isinstance(did, int)

        invoke("renameDeck", deck=did, newName="AdminRenamedById-xzq")

        names = invoke("deckNames")
        assert "AdminRenamedById-xzq" in names

    def test_rename_collision_raises_value_error(self, destructive_col: None) -> None:
        """Renaming to an existing sibling name raises ValueError.

        Anki silently appends '+' on collision; our handler detects this
        and raises a clear ValueError so callers can surface it to the user.
        """
        invoke("createDeck", deck="AdminCollisionA-xzq")
        invoke("createDeck", deck="AdminCollisionB-xzq")

        with pytest.raises(ValueError, match="collision"):
            invoke("renameDeck", deck="AdminCollisionA-xzq", newName="AdminCollisionB-xzq")

    def test_rename_nonexistent_deck_raises_value_error(self, destructive_col: None) -> None:
        """Renaming a non-existent deck raises ValueError."""
        with pytest.raises(ValueError):
            invoke("renameDeck", deck="DoesNotExistXYZ-admin-xzq", newName="Whatever")


# ===========================================================================
# modelFieldNames
# ===========================================================================


class TestModelFieldNames:
    def test_basic_model_field_names(self, col: None) -> None:
        """'Basic' model must return at least ['Front', 'Back'] in that order.

        Guard with skip if the model is absent (backup collections may differ).
        """
        model_names = invoke("modelNames")
        if "Basic" not in model_names:
            pytest.skip("Basic model not present in this collection")
        fields = invoke("modelFieldNames", modelName="Basic")
        assert isinstance(fields, list)
        assert len(fields) >= 2
        assert fields[0] == "Front"
        assert fields[1] == "Back"

    def test_latvian_vocab_field_names_in_order(self, col: None) -> None:
        """'Latvian Vocab' fixture model must return its fields in insertion order."""
        model_names = invoke("modelNames")
        if "Latvian Vocab" not in model_names:
            pytest.skip("Latvian Vocab model not present in this collection")

        fields = invoke("modelFieldNames", modelName="Latvian Vocab")
        assert isinstance(fields, list)
        assert len(fields) >= 1
        # When present in the committed fixture the order must be as inserted
        if len(fields) == 4:
            assert fields == ["latvian", "english", "audio", "image"]

    def test_multi_field_model_returns_all_fields(self, col: None) -> None:
        """'Basic (and reversed card)' has at least 2 fields including Front and Back."""
        model_names = invoke("modelNames")
        if "Basic (and reversed card)" not in model_names:
            pytest.skip("Basic (and reversed card) model not present in this collection")
        fields = invoke("modelFieldNames", modelName="Basic (and reversed card)")
        assert isinstance(fields, list)
        assert len(fields) >= 2
        assert "Front" in fields
        assert "Back" in fields

    def test_unknown_model_raises_value_error(self, col: None) -> None:
        """Requesting fields for a non-existent model raises ValueError."""
        with pytest.raises(ValueError, match="model not found"):
            invoke("modelFieldNames", modelName="NonExistentModelXYZ-admin-xzq")

    def test_returns_list_of_strings(self, col: None) -> None:
        """All returned field names must be strings (uses first available model)."""
        model_names = list(invoke("modelNames"))
        assert model_names, "Collection must have at least one model"
        fields = invoke("modelFieldNames", modelName=model_names[0])
        assert isinstance(fields, list)
        assert len(fields) >= 1
        assert all(isinstance(f, str) for f in fields)


# ===========================================================================
# findCardsPaginated
# ===========================================================================


class TestFindCardsPaginated:
    def test_total_matches_find_cards(self, col: None) -> None:
        """total in paginated result must equal len(findCards(query))."""
        all_card_ids = list(invoke("findCards", query="*"))
        result = invoke("findCardsPaginated", query="*")
        assert isinstance(result, dict)
        assert result["total"] == len(all_card_ids)

    def test_offset_and_limit_slice_correctly(self, col: None) -> None:
        """Offset and limit must slice the full result consistently."""
        all_card_ids = list(invoke("findCards", query="*"))
        total = len(all_card_ids)
        # Skip if fixture is too small to slice meaningfully
        if total < 3:
            pytest.skip("Fixture has fewer than 3 cards; cannot test slicing")

        result = invoke("findCardsPaginated", query="*", offset=1, limit=2)
        assert result["offset"] == 1
        assert len(result["cards"]) == 2
        assert result["total"] == total
        # Values must match the manual slice
        assert result["cards"] == [int(c) for c in all_card_ids[1:3]]

    def test_offset_beyond_total_returns_empty_cards(self, col: None) -> None:
        """Offset past the end of results must return an empty cards list."""
        result = invoke("findCardsPaginated", query="*", offset=99999, limit=10)
        assert result["cards"] == []
        assert result["total"] >= 0
        assert result["offset"] == 99999

    def test_limit_clamped_to_500(self, col: None) -> None:
        """limit=600 must be clamped to 500 (no more than 500 cards returned)."""
        result = invoke("findCardsPaginated", query="*", offset=0, limit=600)
        assert len(result["cards"]) <= 500

    def test_default_offset_is_zero(self, col: None) -> None:
        """Omitting offset must default to 0."""
        result = invoke("findCardsPaginated", query="*", limit=3)
        assert result["offset"] == 0

    def test_default_limit_is_100(self, col: None) -> None:
        """Omitting limit defaults to 100; result is consistent with findCards.

        We do not assert a specific card count because different collections
        (fixture vs backup) have different sizes.  Instead we verify:
          - returned card count <= 100 (the default limit)
          - total equals len(findCards(same query))
          - if the collection has <= 100 cards, all are returned in one page
        """
        all_card_ids = list(invoke("findCards", query="*"))
        result = invoke("findCardsPaginated", query="*")
        assert len(result["cards"]) <= 100
        assert result["total"] == len(all_card_ids)
        if len(all_card_ids) <= 100:
            assert len(result["cards"]) == result["total"]

    def test_negative_offset_clamped_to_zero(self, col: None) -> None:
        """Negative offset must be clamped to 0 (no bad slice, no exception)."""
        all_card_ids = list(invoke("findCards", query="*"))
        result = invoke("findCardsPaginated", query="*", offset=-5, limit=10)
        assert result["offset"] == 0
        assert result["total"] == len(all_card_ids)
        # Result must equal what offset=0 produces
        expected = invoke("findCardsPaginated", query="*", offset=0, limit=10)
        assert result["cards"] == expected["cards"]

    def test_negative_limit_returns_empty_cards(self, col: None) -> None:
        """Negative limit must be clamped to 0, returning an empty card list."""
        all_card_ids = list(invoke("findCards", query="*"))
        result = invoke("findCardsPaginated", query="*", offset=0, limit=-1)
        assert result["cards"] == []
        assert result["total"] == len(all_card_ids)
        assert result["offset"] == 0

    def test_cards_are_integers(self, col: None) -> None:
        """All card ids in the result must be plain Python ints."""
        result = invoke("findCardsPaginated", query="*")
        assert all(isinstance(c, int) for c in result["cards"])

    def test_empty_result_for_impossible_query(self, col: None) -> None:
        """A query that matches nothing returns total=0 and cards=[]."""
        result = invoke(
            "findCardsPaginated",
            query="tag:__nonexistent_tag_admin_xzq__",
        )
        assert result["total"] == 0
        assert result["cards"] == []
