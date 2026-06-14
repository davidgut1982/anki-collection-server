# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (Step 7 — review_session.py)

- `src/review_session.py`: Headless gui* review-session state machine — replaces
  Anki's Qt GUI reviewer.  `ReviewSessionManager` holds per-deck session state and
  implements the six ``gui*`` AnkiConnect actions required by the tilts einki client:

  - **`guiDeckReview` {name}** — resolves deck via `col.decks.id_for_name`, calls
    `col.decks.select(deck_id)` to scope the scheduler, creates/resets the session.
    Returns `True`.

  - **`guiCurrentCard` {}** — calls `col.sched.get_queued_cards(fetch_limit=1)`;
    renders HTML via `card.render_output()` (`.question_text`, `.answer_text`, `.css`);
    derives buttons (`[1,2,3,4]` for review cards, `[1,3,4]` for new/learning) and
    `nextReviews` (parallel list from `col.sched.describe_next_states(qc.states)`);
    builds `fields: {name: {value, order}}` matching `notesInfo` format; stores
    `current_card_id` + `current_states` (SchedulingStates protobuf) in the session for
    the answer step.  Returns the full payload or `None` when queue is empty.

  - **`guiStartCardTimer` {}** — records `timer_started_at`; no-op in headless flow.
    Returns `True`.

  - **`guiShowAnswer` {}** — pure no-op (headless renders both sides in guiCurrentCard).
    Returns `True`.

  - **`guiAnswerCard` {ease}** — maps ease 1–4 to `CardAnswer.Rating` (AGAIN=0,
    HARD=1, GOOD=2, EASY=3); calls `card.start_timer()` if `timer_started` is `None`
    (required by `build_answer` → `card.time_taken()`); calls
    `col.sched.build_answer(card, states, rating)` → `col.sched.answer_card(answer)`;
    clears `current_card_id` so next `guiCurrentCard` fetches the next card.
    Raises `RuntimeError` on stale submit (no active card in session).

  - **`guiUndo` {}** — calls `col.undo()`; resets `current_card_id` and re-selects
    deck so the un-done card re-surfaces on the next `guiCurrentCard`.
    Returns `True` on success, `False` if nothing to undo.

  Exports `GUI_ACTIONS: dict[str, Callable]` for merging into the server dispatch table.

  **CardAnswer.Rating enum confirmed for anki 25.9.2** (`anki.scheduler_pb2`):
  `AGAIN=0`, `HARD=1`, `GOOD=2`, `EASY=3` (0-indexed, not 1–4).

  **`card.timer_started` behaviour**: `build_answer` calls `card.time_taken()` which
  requires `card.timer_started` to be set via `card.start_timer()`.  Without this call
  the scheduler raises `TypeError: unsupported operand type(s) for -: 'float' and
  'NoneType'`.  Fixed by calling `card.start_timer()` in `gui_answer_card` if
  `timer_started is None`.

- `tests/test_review_session.py`: 27 integration tests against a `/tmp` copy of the
  static backup (SM-2 collection). Coverage:
  - `guiDeckReview` selects a real deck and rejects unknown decks.
  - `guiCurrentCard` payload: all 12 required keys, buttons length matches card type,
    `nextReviews` parallel to `buttons`, non-empty HTML question/answer, idempotency.
  - Full flip→grade loop (`guiCurrentCard` → `guiShowAnswer` → `guiAnswerCard(3)` →
    next `guiCurrentCard` returns different card or None).
  - `guiUndo` after a grade restores the answered card.
  - FSRS mode: enable `fsrs=True` + compute params; `guiCurrentCard` still returns
    valid `nextReviews` under FSRS scheduler.
  - Stale protection: `guiAnswerCard` without prior `guiCurrentCard` raises
    `RuntimeError`; double-answer raises; ease=0 raises `ValueError`.

  All 27 pass (0.61 s); 55 total tests pass across all test files (1 skipped —
  unrelated cross-process lock test).

### Fixed (critic HIGH/MEDIUM — actions.py)

- **`addNote` duplicate detection** (`_add_note`): `col.add_note()` in anki 25.9.2
  does NOT raise on duplicate — it silently creates the note.  Added an explicit
  `note.duplicate_or_empty() == 2` guard before `col.add_note()` that raises
  `ValueError("cannot create note because it is a duplicate")`.  The server envelope
  converts this to `{"result": null, "error": "..."}` which the tilts client handles
  via its `result is None` guard.  (Critic HIGH.)

- **`getDeckStats` missing-deck key collision** (`_get_deck_stats`): previously all
  unknown decks used `did=0` as the result key, so N missing decks collapsed to one
  `"0"` entry (each silently overwrote the last).  Now each missing deck gets the
  distinct key `f"missing:{name}"`, ensuring N missing → N result entries with the
  correct `name` field per entry.  (Critic HIGH.)

- **`storeMediaFile` filename contract** (`_store_media_file`): now always returns the
  actual stored filename from `col.media.write_data()` (which may be sanitized, e.g.
  `"a/b.mp3"` → `"ab.mp3"`).  Added docstring noting the sanitization behaviour and
  that callers must use the returned value.  For clean filenames the returned value
  equals the input, so tilts-client assertions remain valid.  (Critic HIGH.)

- **`modelNames` deprecation** (`_model_names`): replaced the deprecated
  `col.models.all_names()` call with
  `[nt.name for nt in col.models.all_names_and_ids()]`.  Each item returned by
  `all_names_and_ids()` is a protobuf `NotetypeNameId` with `.name` and `.id`
  attributes.  Silences the anki 25.9.2 deprecation warning.  (Critic MEDIUM.)

### Added (tests — actions.py critic fixes)

- `TestAddNoteDuplicateDetection::test_add_duplicate_raises_value_error`: adds a note
  then adds the same note again; asserts the second call raises `ValueError` matching
  `"duplicate"`.
- `TestGetDeckStats::test_deck_stats_two_missing_decks_produce_two_entries`: calls
  `getDeckStats` with two non-existent deck names; asserts the result dict has exactly
  2 distinct entries with the correct `name` fields.
- `TestMediaRoundTrip::test_store_clean_filename_returns_exact_name_and_retrieves`:
  stores a file with a clean (no-slash) filename; asserts the returned stored name
  equals the input name AND `retrieveMediaFile` round-trips the bytes correctly.

  Total test count: 25 → 28 (all pass, 0.32 s).

### Added
- `src/actions.py`: Complete `ACTIONS` dispatch dict implementing all CRUD/media/stats
  AnkiConnect handlers required by the Tilts client (Step 6).  Handlers ported from
  FooSoft/anki-connect (AGPL-3.0), adapted to the `anki` pip package (25.9.2).

  **Notes CRUD**: `version` (→6), `findNotes`, `notesInfo`, `addNote`, `updateNoteFields`,
  `addTags`, `removeTags`.

  **Cards CRUD**: `findCards`, `cardsInfo` (renders via `card.render_output()`),
  `cardsToNotes`, `changeDeck`, `createDeck`, `deckNames`, `getDeckStats`,
  `suspend`, `unsuspend`.

  **Models**: `modelNames`, `createModel`.

  **Card mutation**: `setSpecificValueOfCard` (flags key implemented; others return error).

  **Media**: `storeMediaFile` (base64 → `col.media.write_data`), `retrieveMediaFile`
  (read + base64 encode; returns `false` if missing per AnkiConnect), `deleteMediaFile`
  (`col.media.trash_files`).

  **Stats**: `getNumCardsReviewedToday` (revlog count since `col.sched.day_cutoff * 1000`),
  `getNumCardsReviewedByDay` (aggregate revlog by date), `getReviewsOfCards` (full revlog
  per card; returns `ease`, `time`, `type` consumed by tilts client), `getCollectionStatsHTML`
  (minimal HTML ping — tilts only uses it to verify connection availability).

  Response shapes verified against `tilts-system/agent/modules/anki_connect_client.py`.

- `tests/test_actions.py`: 25 integration tests covering all hot paths against a /tmp
  copy of the static collection backup.  Collection singleton is opened/closed per-test
  via the `col` fixture; backup is never modified.  All 25 pass (0.29 s).

### Fixed
- `src/collection.py` `CollectionManager.open()`: the fcntl lock is now
  released (and `_lock_fd`/`_lock_path` reset) if `Collection()` raises for
  any reason (corrupt file, schema mismatch, `ModuleNotFoundError`, etc.).
  Previously the lock remained held until the process exited, blocking all
  subsequent `open()` calls. Fix wraps the lazy `from anki.collection import
  Collection` import AND the `Collection(path)` constructor in a single
  `try/except` that delegates to the already-idempotent `self.close()` before
  re-raising. Regression test added:
  `tests/test_collection_lock_release.py::test_lock_released_after_failed_open_then_valid_open_succeeds`.
  (Critic HIGH bug.)

### Added
- `src/collection.py`: `CollectionManager` class and module-level singleton
  (`manager`).
  - `manager.open(path)` opens `anki.Collection` from `ANKI_COLLECTION_PATH`
    env var (default `/config/.local/share/Anki2/User 1/collection.anki2`).
  - `manager.close()` closes the collection and releases the lock; idempotent.
  - `manager.col` property raises `RuntimeError` if not yet opened.
  - `manager.save()` explicit flush helper (anki auto-saves on close).
  - `manager.health()` returns `{"status","collection_path","card_count","note_count"}`
    with a `SELECT 1` liveness probe; used by `GET /health` (Step 5).
  - `get_col()` module-level convenience function for other modules.
  - `_col_lock` module-level `threading.Lock` for optional serialisation.
  - fcntl advisory lock (`LOCK_EX|LOCK_NB`) on `<collection>.server.lock`
    acquired at open and released on close; second open (same or different
    process) raises immediately with a clear message.
  - Lazy `from anki.collection import Collection` import so module-level import
    does not require anki installed (enables mocking in tests).

### Changed
- **WSGI server**: replaced Flask built-in dev server with `waitress==3.0.2`
  (`threads=1`, mandatory for single-writer SQLite). The Flask `app` object is
  unchanged; only the server entrypoint differs. Addresses Code Critic WARN
  (HIGH): Werkzeug dev server not safe for production.
- **Healthcheck hardening**: Dockerfile `HEALTHCHECK` and `docker-compose.example.yml`
  healthcheck now hit `GET /health` (returns `{"status":"ok"}` with HTTP 200)
  instead of the brittle POST+grep against the AnkiConnect envelope. Addresses
  Code Critic WARN (HIGH): grep-based healthcheck too fragile.
- Added `.dockerignore` to exclude `.git`, `__pycache__`, `*.pyc`, `*.pyo`,
  `tests/`, `*.md`, `.env*`, `.ruff_cache` from the build context.

### Added
- Project scaffolded: directory structure, `src/` package stubs, `tests/` skeleton.
- `anki` pinned to **25.9.2** — matches the Anki Desktop 25.09.2 collection schema
  written by the Tilts production collection. Do not upgrade without verifying schema
  compatibility.
- `flask==3.1.1` pinned as the HTTP layer.
- Minimal bootable `src/server.py`: Flask app exposing the AnkiConnect wire protocol
  on port 8765. Implements the `version` action (returns `6`); all other actions
  return `{"result": null, "error": "not implemented: <action>"}`.
- `GET /health` liveness probe.
- `Dockerfile` (python:3.11-slim, single-worker CMD, HEALTHCHECK via AnkiConnect
  version probe).
- `docker-compose.example.yml` reference showing server + anki-sync-server pairing.
- Placeholder stubs with docstrings and TODO markers for `collection.py` (Step 4),
  `actions.py` (Step 6), `review_session.py` (Step 7), `sync.py` (Step 8),
  `fsrs.py` (Step 8).

### Notes
- AGPL-3.0 license chosen to match FooSoft/anki-connect, whose handler code will
  be adapted near-verbatim in Step 6.
- Single-writer constraint documented throughout: one process, one thread, never
  run alongside Anki Desktop on the same collection.
