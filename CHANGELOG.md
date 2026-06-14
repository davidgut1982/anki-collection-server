# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
