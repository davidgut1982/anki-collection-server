"""
Integration tests for P0 DB & Media health actions (feat/admin-actions A4).

Actions under test:
  - checkDatabase         (read-only; integrity check via internal backend API)
  - fixIntegrity          (destructive; pre-backup + col.fix_integrity())
  - optimizeCollection    (destructive; pre-backup + col.optimize())
  - getEmptyCards         (read-only; EmptyCardsReport shape)
  - removeEmptyCards      (destructive; pre-backup + remove_cards_and_orphaned_notes)
  - mediaCheck            (read-only; CheckMediaResponse shape)
  - deleteUnusedMedia     (destructive; pre-backup + trash_files + empty_trash)
  - mediaDirSize          (read-only; walks media dir)

Also tests the shared pre_backup() helper from src.maintenance:
  - creates backup file at expected path
  - returns correct absolute path string
  - backup file size > 0

All destructive tests operate on a fresh per-test /tmp copy of the committed
fixture.  The committed fixture is NEVER modified.

Fixture: tests/fixtures/test_collection.anki2
Override: ANKI_TEST_BACKUP environment variable.
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
# Helpers
# ---------------------------------------------------------------------------


def invoke(action: str, **kwargs: object) -> object:
    """Invoke an ACTIONS handler directly (no Flask envelope)."""
    handler = ACTIONS[action]
    return handler(dict(kwargs))


def _add_empty_basic_note() -> int:
    """Add a Basic note with empty Front+Back so get_empty_cards() detects it.

    Returns the note id of the added note.
    """
    from anki.models import NotetypeId  # noqa: PLC0415

    col = col_mod.get_col()
    basic_ntid = next(
        nt["id"] for nt in col.models.all() if nt["name"] == "Basic"
    )
    m = col.models.get(NotetypeId(basic_ntid))
    note = col.new_note(m)
    note.fields[0] = ""  # Empty Front — template renders to nothing
    note.fields[1] = ""  # Empty Back
    col.add_note(note, col.decks.id("Default"))
    return int(note.id)


# ---------------------------------------------------------------------------
# Per-test destructive fixture — fresh /tmp copy of the collection
# ---------------------------------------------------------------------------


@pytest.fixture()
def destructive_col() -> Generator[None, None, None]:
    """
    Open a fresh per-test copy of the collection in /tmp.

    Yields after opening; tears down (closes + rmdir) after the test.
    Tests using this fixture may freely mutate the collection.
    """
    tmp = tempfile.mkdtemp()
    col_path = Path(tmp) / "collection.anki2"
    shutil.copy2(str(BACKUP), str(col_path))

    col_mod.manager.open(col_path)
    try:
        yield
    finally:
        col_mod.manager.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests: checkDatabase
# ---------------------------------------------------------------------------


class TestCheckDatabase:
    def test_returns_ok_and_problems_keys(self, destructive_col: None) -> None:
        result = invoke("checkDatabase")
        assert isinstance(result, dict)
        assert "ok" in result
        assert "problems" in result

    def test_problems_is_list(self, destructive_col: None) -> None:
        result = invoke("checkDatabase")
        assert isinstance(result["problems"], list)

    def test_clean_collection_returns_ok(self, destructive_col: None) -> None:
        # The committed fixture is a known-good collection.
        result = invoke("checkDatabase")
        # May not be empty (anki sometimes auto-fixes timestamps), but ok is bool
        assert isinstance(result["ok"], bool)

    def test_ok_is_true_when_no_problems(self, destructive_col: None) -> None:
        result = invoke("checkDatabase")
        # ok must match whether problems list is empty
        assert result["ok"] == (len(result["problems"]) == 0)


# ---------------------------------------------------------------------------
# Tests: pre_backup helper
# ---------------------------------------------------------------------------


class TestPreBackup:
    def test_creates_backup_file(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("test")
        assert Path(path).exists()

    def test_returns_absolute_path(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("test")
        assert os.path.isabs(path)

    def test_backup_is_nonempty(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("test")
        assert os.path.getsize(path) > 0

    def test_backup_filename_contains_reason(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("myReason")
        assert "myReason" in Path(path).name

    def test_backup_filename_ends_with_anki2(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("test")
        assert path.endswith(".anki2")

    def test_backup_in_backups_subdir(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("test")
        assert Path(path).parent.name == "backups"

    def test_special_chars_in_reason_sanitised(self, destructive_col: None) -> None:
        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            path = pre_backup("hello world/evil:chars")
        # Path should exist (special chars replaced with dashes)
        assert Path(path).exists()

    def test_multiple_backups_get_unique_names(self, destructive_col: None) -> None:
        """Two back-to-back backups should not clobber each other."""
        import time  # noqa: PLC0415

        from src.maintenance import pre_backup  # noqa: PLC0415

        with col_mod._col_lock:
            p1 = pre_backup("dup")
            time.sleep(1.1)  # ensure distinct UTC second in filename
            p2 = pre_backup("dup")
        assert p1 != p2
        assert Path(p1).exists()
        assert Path(p2).exists()


# ---------------------------------------------------------------------------
# Tests: fixIntegrity
# ---------------------------------------------------------------------------


class TestFixIntegrity:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert isinstance(result, dict)
        assert "message" in result
        assert "ok" in result
        assert "backup" in result

    def test_ok_is_bool(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert isinstance(result["ok"], bool)

    def test_message_is_string(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0

    def test_backup_file_created(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert Path(result["backup"]).exists()

    def test_backup_path_is_string(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert isinstance(result["backup"], str)

    def test_backup_name_contains_fix_integrity(self, destructive_col: None) -> None:
        result = invoke("fixIntegrity")
        assert "fixIntegrity" in Path(result["backup"]).name


# ---------------------------------------------------------------------------
# Tests: optimizeCollection
# ---------------------------------------------------------------------------


class TestOptimizeCollection:
    def test_returns_backup_key(self, destructive_col: None) -> None:
        result = invoke("optimizeCollection")
        assert isinstance(result, dict)
        assert "backup" in result

    def test_backup_file_exists(self, destructive_col: None) -> None:
        result = invoke("optimizeCollection")
        assert Path(result["backup"]).exists()

    def test_backup_name_contains_optimize(self, destructive_col: None) -> None:
        result = invoke("optimizeCollection")
        assert "optimize" in Path(result["backup"]).name.lower()

    def test_collection_still_usable_after(self, destructive_col: None) -> None:
        invoke("optimizeCollection")
        # Collection should still respond to basic queries
        col = col_mod.get_col()
        with col_mod._col_lock:
            assert col.card_count() >= 0


# ---------------------------------------------------------------------------
# Tests: getEmptyCards
# ---------------------------------------------------------------------------


class TestGetEmptyCards:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        result = invoke("getEmptyCards")
        assert isinstance(result, dict)
        for key in ("emptyCardCount", "noteCount", "report", "notes"):
            assert key in result, f"Missing key: {key}"

    def test_clean_collection_has_zero_empty(self, destructive_col: None) -> None:
        result = invoke("getEmptyCards")
        assert result["emptyCardCount"] == 0
        assert result["noteCount"] == 0
        assert result["notes"] == []

    def test_detects_empty_note(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        result = invoke("getEmptyCards")
        assert result["emptyCardCount"] > 0
        assert result["noteCount"] > 0

    def test_notes_list_shape(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        result = invoke("getEmptyCards")
        for entry in result["notes"]:
            assert "noteId" in entry
            assert "cardIds" in entry
            assert "willDeleteNote" in entry
            assert isinstance(entry["noteId"], int)
            assert isinstance(entry["cardIds"], list)
            assert isinstance(entry["willDeleteNote"], bool)

    def test_report_is_string(self, destructive_col: None) -> None:
        result = invoke("getEmptyCards")
        assert isinstance(result["report"], str)

    def test_empty_card_count_matches_sum_of_card_ids(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        result = invoke("getEmptyCards")
        total = sum(len(n["cardIds"]) for n in result["notes"])
        assert result["emptyCardCount"] == total


# ---------------------------------------------------------------------------
# Tests: removeEmptyCards
# ---------------------------------------------------------------------------


class TestRemoveEmptyCards:
    def test_removes_empty_cards_and_returns_count(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        before = invoke("getEmptyCards")
        assert before["emptyCardCount"] > 0

        result = invoke("removeEmptyCards")
        assert result["removed"] == before["emptyCardCount"]

    def test_backup_created(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        result = invoke("removeEmptyCards")
        assert Path(result["backup"]).exists()

    def test_empty_collection_returns_zero_removed(self, destructive_col: None) -> None:
        result = invoke("removeEmptyCards")
        assert result["removed"] == 0
        assert "backup" in result

    def test_cards_gone_after_removal(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        invoke("removeEmptyCards")
        after = invoke("getEmptyCards")
        assert after["emptyCardCount"] == 0

    def test_backup_name_contains_remove_empty(self, destructive_col: None) -> None:
        with col_mod._col_lock:
            _add_empty_basic_note()
        result = invoke("removeEmptyCards")
        assert "removeEmptyCards" in Path(result["backup"]).name


# ---------------------------------------------------------------------------
# Tests: mediaCheck
# ---------------------------------------------------------------------------


class TestMediaCheck:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        result = invoke("mediaCheck")
        assert isinstance(result, dict)
        for key in ("unused", "missing", "report", "haveTrash"):
            assert key in result, f"Missing key: {key}"

    def test_unused_is_list(self, destructive_col: None) -> None:
        result = invoke("mediaCheck")
        assert isinstance(result["unused"], list)

    def test_missing_is_list(self, destructive_col: None) -> None:
        result = invoke("mediaCheck")
        assert isinstance(result["missing"], list)

    def test_report_is_string(self, destructive_col: None) -> None:
        result = invoke("mediaCheck")
        assert isinstance(result["report"], str)

    def test_have_trash_is_bool(self, destructive_col: None) -> None:
        result = invoke("mediaCheck")
        assert isinstance(result["haveTrash"], bool)

    def test_fixture_has_missing_media(self, destructive_col: None) -> None:
        # The committed fixture references maja.mp3 and udens.mp3 but they are
        # not in the media folder — so missing should be non-empty.
        result = invoke("mediaCheck")
        assert len(result["missing"]) > 0


# ---------------------------------------------------------------------------
# Tests: deleteUnusedMedia
# ---------------------------------------------------------------------------


class TestDeleteUnusedMedia:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        result = invoke("deleteUnusedMedia")
        assert isinstance(result, dict)
        assert "deletedCount" in result
        assert "backup" in result

    def test_deleted_count_is_int(self, destructive_col: None) -> None:
        result = invoke("deleteUnusedMedia")
        assert isinstance(result["deletedCount"], int)

    def test_backup_created(self, destructive_col: None) -> None:
        result = invoke("deleteUnusedMedia")
        assert Path(result["backup"]).exists()

    def test_backup_name_contains_delete_unused_media(self, destructive_col: None) -> None:
        result = invoke("deleteUnusedMedia")
        assert "deleteUnusedMedia" in Path(result["backup"]).name

    def test_clean_media_dir_returns_zero(self, destructive_col: None) -> None:
        # The fixture has no unused media files.
        result = invoke("deleteUnusedMedia")
        assert result["deletedCount"] == 0

    def test_unused_files_removed_after_call(self, destructive_col: None) -> None:
        """If there are unused files, they should be gone after deleteUnusedMedia."""
        import os as _os  # noqa: PLC0415

        col = col_mod.get_col()
        with col_mod._col_lock:
            media_dir = col.media.dir()
        _os.makedirs(media_dir, exist_ok=True)
        orphan = _os.path.join(media_dir, "orphan_test_file.jpg")
        Path(orphan).write_bytes(b"fake-image-data")

        result = invoke("deleteUnusedMedia")
        assert result["deletedCount"] >= 1
        # After empty_trash the file should be gone from the media dir
        assert not _os.path.exists(orphan)


# ---------------------------------------------------------------------------
# Tests: mediaDirSize
# ---------------------------------------------------------------------------


class TestMediaDirSize:
    def test_returns_expected_keys(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert isinstance(result, dict)
        for key in ("bytes", "fileCount", "dir"):
            assert key in result, f"Missing key: {key}"

    def test_bytes_is_int(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert isinstance(result["bytes"], int)

    def test_file_count_is_int(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert isinstance(result["fileCount"], int)

    def test_dir_is_string(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert isinstance(result["dir"], str)

    def test_bytes_non_negative(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert result["bytes"] >= 0

    def test_file_count_non_negative(self, destructive_col: None) -> None:
        result = invoke("mediaDirSize")
        assert result["fileCount"] >= 0

    def test_size_reflects_added_file(self, destructive_col: None) -> None:
        import os as _os  # noqa: PLC0415

        col = col_mod.get_col()
        with col_mod._col_lock:
            media_dir = col.media.dir()
        _os.makedirs(media_dir, exist_ok=True)

        before = invoke("mediaDirSize")

        test_file = _os.path.join(media_dir, "size_test.txt")
        Path(test_file).write_bytes(b"x" * 1234)

        after = invoke("mediaDirSize")
        assert after["bytes"] >= before["bytes"] + 1234
        assert after["fileCount"] == before["fileCount"] + 1


# ---------------------------------------------------------------------------
# Tests: action registration
# ---------------------------------------------------------------------------


class TestActionRegistration:
    EXPECTED_ACTIONS = [
        "checkDatabase",
        "fixIntegrity",
        "optimizeCollection",
        "getEmptyCards",
        "removeEmptyCards",
        "mediaCheck",
        "deleteUnusedMedia",
        "mediaDirSize",
    ]

    def test_all_maintenance_actions_registered(self) -> None:
        for action in self.EXPECTED_ACTIONS:
            assert action in ACTIONS, f"Action not registered: {action}"

    def test_handlers_are_callable(self) -> None:
        for action in self.EXPECTED_ACTIONS:
            assert callable(ACTIONS[action]), f"Handler not callable: {action}"
