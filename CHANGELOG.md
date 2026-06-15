# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — admin CRUD actions + paginated browse (feat/admin-actions)

Five new actions supporting the admin UI layer:

#### `deleteNotes`
- **Params:** `{"notes": [int]}`
- **Result:** null
- Calls `col.remove_notes([NoteId(n) for n in notes])` — deletes both the
  notes and all their associated cards in one operation.
- Empty list is a no-op.
- Wrapped in `col_mod._col_lock` (write operation).

#### `deleteDecks`
- **Params:** `{"decks": [int|str], "cardsToo": bool}` (`cardsToo` optional, default `True`)
- **Result:** null
- String entries are resolved via `col.decks.id_for_name()`.
- Default deck (id=1) is silently skipped — Anki forbids removing it.
- Non-existent deck names are silently skipped (idempotent).
- `cardsToo` is accepted for API compatibility but has no effect:
  `col.decks.remove()` in anki 25.9.2 **always** deletes the deck AND
  all its cards — there is no "keep cards" variant in the pip API.
- Wrapped in `col_mod._col_lock` (write operation).

#### `renameDeck`
- **Params:** `{"deck": int|str, "newName": str}`
- **Result:** null
- `deck` may be an integer id or a string name; raises `ValueError` if not found.
- Calls `col.decks.rename(DeckId(did), new_name)`.
- **Collision handling:** anki 25.9.2 does NOT raise on a sibling name
  collision — it silently appends a `"+"` suffix to the new name.  The
  handler detects this post-rename by comparing `deck["name"]` with the
  requested name and raises `ValueError("Rename collision: ...")` if they
  differ, so callers can surface the conflict to the user cleanly.
- Wrapped in `col_mod._col_lock` (write operation).

#### `modelFieldNames`
- **Params:** `{"modelName": str}`
- **Result:** `list[str]`
- Returns `[f["name"] for f in nt["flds"]]` in insertion order (same order
  that Anki stores and displays fields).
- Raises `ValueError("model not found: <name>")` for unknown models.
- Read-only — no lock.

#### `findCardsPaginated`
- **Params:** `{"query": str, "offset": int = 0, "limit": int = 100}`
- **Result:** `{"cards": [int], "total": int, "offset": int}`
- Runs the full `col.find_cards(query)` search and slices in Python, consistent
  with AnkiConnect's own pagination approach (no SQL LIMIT/OFFSET).
- `limit` is clamped to a maximum of 500 to protect against large payloads.
- Read-only — no lock.

#### Test file: `tests/test_admin_actions.py` (25 tests, all pass, 0.18 s)

- **`TestDeleteNotes`** (3 tests): add-then-delete confirms note absent; empty list
  is no-op; associated cards are also removed.
- **`TestDeleteDecks`** (5 tests): delete by name and by id removes deck from
  `deckNames`; Default deck id=1 is skipped; non-existent name is skipped; cards
  inside deleted deck are also removed (confirmed `col.decks.remove()` always
  deletes cards).
- **`TestRenameDeck`** (4 tests): rename by name and by id reflects in `deckNames`;
  sibling name collision raises `ValueError` matching `"collision"`; non-existent
  deck raises `ValueError`.
- **`TestModelFieldNames`** (5 tests): `Basic` → `["Front", "Back"]`; `Latvian Vocab`
  → correct 4-field order; 2-field model; unknown model raises `ValueError` matching
  `"model not found"`; all values are `str`.
- **`TestFindCardsPaginated`** (8 tests): total matches `findCards`; offset+limit
  slices correctly; offset past end returns empty `cards`; `limit=600` clamped to 500;
  default offset=0; default limit=100 (returns all 9 fixture cards); all ids are `int`;
  impossible query returns `total=0, cards=[]`.

Destructive tests (`deleteNotes`, `deleteDecks`) each open a fresh per-test copy
of the fixture via a `destructive_col` function-scoped fixture — the session-shared
`backup_copy` is never modified.

Total test count: 61 (36 existing + 25 new, all pass).

### Added — modelTemplates action

- **`src/actions.py` — `_model_templates(params)`**: implements the
  `modelTemplates` AnkiConnect action.

  **Params:** `{"modelName": "<name>"}`

  **Result:** `{"<template name>": {"Front": "<qfmt>", "Back": "<afmt>"}, ...}`
  one entry per template in insertion order (preserving `card.ord` → index mapping).

  Uses `col.models.by_name(modelName)` and iterates `nt["tmpls"]`; raises
  `ValueError("model was not found: <name>")` (AnkiConnect-style error, converted
  to `{"result": null, "error": "..."}` by the server envelope) when the model
  does not exist.

  **Why:** Tilts' `_card_payload` (in `anki_review_bp.py`) calls this on every
  card load to build template-aware per-side sound lists.  The call is
  `client._invoke("modelTemplates", {"modelName": model_name})` and the result
  is consumed as:
  ```python
  template_list = list(templates.values())   # ordered by card.ord
  tmpl = template_list[card.ord]
  qfmt = tmpl.get("Front", "") or ""
  afmt = tmpl.get("Back", "") or ""
  ```
  Without this action, every card load logged
  `WARNING unsupported action: 'modelTemplates'` and fell back to the
  all-fields-order audio mapping.  With it, audio markers resolve in
  template field order rather than Anki's stored field order — which matters
  for multi-field card types where the front and back reference different
  fields in different orders.

- **`src/actions.py` — `"modelTemplates"` registered in `ACTIONS` dict.**

- **`tests/test_actions.py` — `TestModelTemplates`** (6 tests, all pass):
  - `test_known_model_returns_dict_keyed_by_template_name` — `"Basic"` → dict with ≥ 1 entry.
  - `test_each_entry_has_front_and_back_strings` — every value has `"Front"` and `"Back"` as strings.
  - `test_multi_template_model_returns_multiple_entries_in_order` — `"Basic (and reversed card)"` returns 2 entries in insertion order; Card 1 qfmt references `"Front"`, Card 2 qfmt references `"Back"`.
  - `test_template_content_is_nonempty_for_real_model` — qfmt and afmt are non-empty.
  - `test_latvian_vocab_model_in_fixture` — custom Latvian Vocab model round-trips.
  - `test_unknown_model_raises_value_error` — non-existent model name raises `ValueError` matching `"model was not found"`.

  `modelFieldNames` was NOT added: Tilts does not call it; skipping avoids dead-code scope creep.

### Fixed (QA-confirmed bug: getNumCardsReviewedToday returned 0)

- **`src/actions.py` — `_get_num_cards_reviewed_today`**: `col.sched.day_cutoff` is the
  Unix timestamp at which the current scheduler day *ends* (the next rollover, in the
  future), not when it started.  The previous query used `id >= day_cutoff * 1000` as the
  lower bound, which is a future timestamp — matching zero rows until the day rolled over.
  Fixed to `id >= (day_cutoff - 86400) * 1000` (start of today = end-of-today minus one
  full day in seconds).  Also corrected the misleading docstring that claimed `day_cutoff`
  marks the day's start.

- **`tests/test_actions.py` — `TestGetNumCardsReviewedToday::test_counts_synthetic_today_reviews_nonzero`**:
  New regression test that inserts two synthetic revlog rows dated one and two hours before
  end-of-day (well within today) into a private writable copy of the fixture, then asserts
  `getNumCardsReviewedToday` returns ≥ 2 (non-zero) and agrees with today's bucket from
  `getNumCardsReviewedByDay`.  The test failed against the old code (returned 0) and passes
  against the fix — the bug QA reported is now covered by the test suite.

### Added (Step 9 — self-contained test fixture + wire-shape parity)

- **`tests/fixtures/test_collection.anki2`** (committed, ~136 KB): a programmatically
  generated Anki collection that makes the full test suite runnable with zero external
  dependencies.  Contents:
  - 4 decks: `Default`, `Latvian`, `Latvian::Vocabulary`, `Latvian::Grammar`
  - 7 note types: `Basic`, `Basic (and reversed card)`, `Basic (optional reversed card)`,
    `Basic (type in the answer)`, `Cloze`, `Image Occlusion`, plus a custom
    `Latvian Vocab` type with fields `latvian`, `english`, `pronunciation`, `example`.
  - 8 notes / 9 cards (8 Latvian Vocab + 1 Basic).
  - 4 review cards (`type=2, queue=2, due=0`) — due on scheduler day 0 so
    review-session tests can run against them immediately.
  - 40 revlog entries spanning all 8 cards for stats/FSRS tests.
  - 1 tiny media file (`maja.mp3`, 36-byte WAV header + silence) registered in
    the media database.
  - Generated by `tests/fixtures/generate_fixture.py` (committed alongside it).

- **`tests/parity_check.py`**: standalone script (not in default pytest run) that
  proves our AnkiConnect wire-response shapes are compatible with a live
  `anki-headless:8765` container.

  - Mocks `waitress` in `sys.modules` before importing `src.server`, then starts
    our Flask app on port 18765 via `werkzeug.serving.make_server` on a background
    thread — no external process or install required.
  - Fetches the same read-only actions from BOTH endpoints in parallel and compares
    response SHAPES (key sets + value types), not data values (the two collections
    differ).  Actions checked: `version`, `deckNames`, `modelNames`, `findNotes`,
    `findCards`, `notesInfo`, `cardsInfo`.
  - For `notesInfo` / `cardsInfo`: queries each server's OWN note/card IDs, so each
    server returns a real populated object rather than an empty list.
  - Special-cases the `fields` dict (where field names are collection-specific, e.g.
    `latvian`/`english` vs `Front`/`Back`) by detecting the inner `{value, order}`
    structure and comparing only that.
  - Exits 0 (PASS or SKIPPED if `anki-headless` is not reachable), 1 on failure.
  - Result from first run: 7 / 7 PASS after fixing `notesInfo` and `cardsInfo` gaps.

- **`src/actions.py` — `notesInfo` and `cardsInfo` enriched** to match the live
  AnkiConnect response shape (discovered by the parity check):

  `notesInfo` now returns:
  - `"mod"` — note modification timestamp (int)
  - `"cards"` — list of card IDs belonging to the note
  - `"profile"` — profile name (parent directory of the collection path)

  `cardsInfo` now returns:
  - `"mod"` — card modification timestamp (int)
  - `"left"` — remaining learning steps counter (int)
  - `"nextReviews"` — list of human-readable scheduling strings for each ease
    button (e.g. `["<10m", "1d", "4d", "10d"]`), computed via
    `col.sched.nextIvlStr(card, ease)` for each valid ease.

- **All 5 integration test files updated** to use the committed fixture as the
  default collection source (no more dependency on a production backup):
  ```
  tests/test_actions.py
  tests/test_fsrs.py
  tests/test_review_session.py
  tests/test_sync.py
  tests/test_collection_lock_release.py
  ```
  Each file sets `_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)` where
  `_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"`.
  The `ANKI_TEST_BACKUP` env-var override continues to work for running against a
  real production collection.

- **FSRS tests restructured for small-fixture compatibility**: the committed fixture
  has only 40 revlog entries — the optimizer health check requires ~1000+.  Tests
  that require a successful optimizer run now `pytest.skip()` with a clear message
  pointing at `ANKI_TEST_BACKUP`.  Shape-only and `optimize=False` tests always run.

- **`test_review_session.py` — deck names generalised**: `REVIEW_DECK` now defaults
  to `"Default"` (override via `ANKI_TEST_REVIEW_DECK`) and the parent-deck test
  defaults to `"Latvian"` (override via `ANKI_TEST_PARENT_DECK`) so the tests
  use the committed fixture's actual deck names.

  Final counts: 74 passed, 3 skipped (FSRS optimizer — expected, documented).
  Parity: 7 / 7 PASS.

### Fixed (Critic HIGH — lock-free /health so sync cannot trip Docker healthcheck)

- **`CollectionManager.health()` blocks while sync holds `_col_lock`** (Critic HIGH):
  With `waitress threads=1`, the single worker thread is occupied during a
  multi-second sync and cannot serve `GET /health` at all — Docker's 5-second
  `HEALTHCHECK` times out and marks the container unhealthy mid-sync.  The
  cutover path performs a sync, so this was a latent production failure.

  **Fix chosen: option (a) — `threads=2` + `_col_lock`-serialised collection
  access + non-blocking `health()`.**

  - `waitress` bumped from `threads=1` to `threads=2` in `server.py`.  One
    thread handles collection operations (`POST /`); the second thread handles
    `GET /health`.  This is the only change that can make `/health` responsive
    during a sync — no amount of lock restructuring helps when there is only
    one thread to serve requests.

  - **Single-writer safety preserved:** ALL collection access (every dispatched
    action in `POST /`) remains serialised through `_col_lock` (RLock).  The
    second thread never acquires the lock for writes; it uses
    `acquire(blocking=False)` in `health()` only.

  - **`CollectionManager.health()` redesigned** (non-blocking):
    1. Lock-free `self._collection is not None` check — if `None`, raise
       `RuntimeError` immediately (server maps to 503).
    2. `_col_lock.acquire(blocking=False)`:
       - **Lock free** (normal path): acquire succeeds → `SELECT 1` liveness
         probe + `card_count` + `note_count` → release →
         `{"status":"ok","collection_path":…,"card_count":…,"note_count":…}`.
       - **Lock held** (sync / write in progress): return IMMEDIATELY
         `{"status":"ok","syncing":true,"collection_path":…}` — no blocking,
         no DB probe, no counts.  The healthcheck never waits.

  Confirmed: `GET /health` returns in < 1 s (measured ~0 ms) even while
  `_col_lock` is held for a simulated 3-second sync on the other thread.
  Normal `/health` still returns `card_count` and `note_count`.

- **Misleading comment on `waitress.serve()` return** (Critic LOW):
  The previous comment stated "waitress.serve() returns when the server is
  stopped externally" without explaining the mechanism.  Updated to clarify:
  `_shutdown()` calls `sys.exit(0)` → waitress catches `SystemExit` →
  `serve()` returns.  The `close()` call after `serve()` is an idempotent
  no-op safety net for any other exit path.

### Added (lock-free health tests)

- `tests/test_health_lock_free.py` — 8 new tests (all pass, no Anki collection
  required; uses `unittest.mock`):
  - `test_health_raises_when_collection_not_open` — 503 path when `_collection is None`.
  - `test_health_returns_counts_when_lock_free` — normal path: SELECT 1 + counts returned.
  - `test_health_returns_syncing_when_lock_held` — lock held on background
    thread; health returns `syncing=True` in < 1 s, no `card_count`/`note_count`.
  - `test_health_responds_under_1s_while_lock_held_for_3s` — 3-second simulated
    sync; health measured at << 1 s from a second thread.
  - `test_concurrent_writes_serialised_by_col_lock` — 4 concurrent threads each
    acquire `_col_lock`; asserts no overlap inside the critical section.
  - `test_flask_health_endpoint_ok_with_counts` — Flask test client: 200 + counts.
  - `test_flask_health_endpoint_200_syncing_when_lock_held` — Flask test client:
    200 + `syncing=True` while lock held; elapsed < 1 s.
  - `test_flask_health_endpoint_503_when_collection_not_open` — Flask test client: 503.

  Total test count: 68 → 76 (all pass).

### Added — Step 5: server.py full wiring (collection startup + dispatch + graceful shutdown)

- `src/server.py`: upgraded from the Step-2 stub to the production AnkiConnect server.

  **Startup:**
  `_open_collection_or_exit()` is called before waitress starts accepting
  requests.  The collection path comes from `ANKI_COLLECTION_PATH` env var
  (default: `/config/.local/share/Anki2/User 1/collection.anki2`).
  On any failure (`FileNotFoundError`, `RuntimeError` for lock contention or
  corrupt file, any other exception) the error is logged at CRITICAL level and
  the process exits with code 1.  This prevents Docker from entering a silent
  restart-loop — fail fast, log clearly.

  **Graceful shutdown:**
  `SIGTERM` and `SIGINT` are caught by `_shutdown()`, which calls
  `CollectionManager.close()` (flushes WAL, releases SQLite handle, removes
  the fcntl advisory lockfile) then `sys.exit(0)`.  Confirmed from logs:
  `Received SIGTERM — closing collection and shutting down.` →
  `Collection closed.` → `fcntl lock released:` → `Collection closed cleanly.`
  A subsequent container run on the same collection copy starts without
  "already locked" errors.

  **Dispatch table:**
  `DISPATCH = {**ACTIONS, **GUI_ACTIONS, **SYNC_ACTIONS, **FSRS_ACTIONS}` —
  all four action dicts merged at import time.  `POST /` parses the
  AnkiConnect envelope `{action, params, version, apiKey}`;  `version` and
  `apiKey` are accepted and silently ignored (wire-compatible with real
  AnkiConnect clients).  Every dispatch call is wrapped in
  `col_mod._col_lock` for defense-in-depth.

  **Wire-protocol error handling:**
  - Unknown action → `{"result": null, "error": "unsupported action: <name>"}` (HTTP 200, logged at WARNING).
  - Handler exception → `{"result": null, "error": "<str(exc)>"}` (HTTP 200, full traceback logged at ERROR).
  - Success → `{"result": <value>, "error": null}` (HTTP 200).

  **`GET /health`:**
  Delegates to `CollectionManager.health()` (card_count, note_count, status,
  collection_path).  Returns HTTP 200 when healthy; HTTP 503 when the
  collection is not open (RuntimeError) or when the SQLite probe fails.

- `src/collection.py` — `_col_lock` changed from `threading.Lock` to
  `threading.RLock` (reentrant lock).

  **Why:** the server dispatch layer acquires `_col_lock` around every handler
  call for defense-in-depth; individual handlers (in `actions.py`,
  `review_session.py`, etc.) also acquire it for multi-step mutation sequences.
  A plain `Lock` blocks forever when the same thread tries to re-acquire it
  (deadlock on any `gui*` or write action).  `RLock` allows the same thread to
  re-enter without blocking — no behaviour change when threads=1 and no
  cross-thread contention.

- `tests/test_actions.py` — `BACKUP` path now reads `ANKI_TEST_BACKUP` env
  var (with the canonical path as fallback), matching the pattern already used
  by `test_fsrs.py`, `test_review_session.py`, and `test_sync.py`.  `pytest.skip`
  replaced with `pytest.fail` (loud failure rather than silent green) and an
  explicit `os.access(BACKUP, os.R_OK)` check added, consistent with
  `test_review_session.py`.  This allows running the full suite as any UID with
  a readable copy at `/tmp/anki-test-backup.anki2`.

### Fixed — Step 5

- **Deadlock on gui* and write actions (CRITICAL):** The dispatch layer wrapped
  every handler call in `col_mod._col_lock` (a `threading.Lock`).  Handlers
  that internally re-acquire the same lock (all `gui*` actions in
  `review_session.py`, all write actions in `actions.py`) would block forever
  on the inner acquisition — the server appeared to hang with no log output and
  waitress logged "Task queue depth is 1".  Fix: `threading.Lock` →
  `threading.RLock` in `collection.py`.  RLock tracks the owning thread and
  allows the same thread to re-enter; other threads still block as expected.

- **Duplicate "Opening collection" log line:** `_open_collection_or_exit()` no
  longer logs the path itself; `CollectionManager.open()` already logs it.
  Removes the double `INFO Opening collection: …` line from startup output.

### Fixed (critic hardening — sync.py + fsrs.py)

- **fsrs.py — assert stripped under -O (HIGH):** Replaced `assert col.get_config("fsrs") is True`
  with an explicit `if … is not True: raise RuntimeError(…)`.  `assert` statements are silently
  discarded under `python -O` / `PYTHONOPTIMIZE`, making the guard a no-op in optimised builds.

- **fsrs.py — FSRS left enabled-without-weights on optimizer failure (HIGH):** The previous
  `except Exception` handler returned `{"enabled": True, "optimized": False}`, leaving the FSRS
  config flag set even though no valid weight vector had been applied — a silent scheduling
  degradation on every synced device.  Fix: on any optimizer exception, `set_config("fsrs", False)`
  is called before returning, and the result now carries `"enabled": False` and `"error": str(exc)`.
  Same rollback applied to the empty-params and short-params paths.

- **fsrs.py — truncated param vector applied to presets (MEDIUM):** When the optimizer returned
  fewer than 21 params, the code previously logged a warning and still applied the vector (writing
  a 17-element `fsrsParams5` truncation to all presets).  Now: if `len(params) < 21`, FSRS is
  rolled back to disabled and an early-return signals `optimized=False`.  Params are only written
  when exactly 21 are present.

- **sync.py — NORMAL_SYNC semantics misrepresented (HIGH):** The `NORMAL_SYNC (1)` branch
  previously returned `{"uploaded": True}`, implying a pure upload when the incremental protocol
  is actually a bidirectional merge.  Now returns `{"synced": True}` and emits a `log.warning`
  noting that server changes may have been applied locally and that upload-wins is only guaranteed
  if no other client writes to the sync server.  Tests updated to assert on `"synced"` for the
  `NORMAL_SYNC` path.

- **sync.py — missing explicit FULL_DOWNLOAD branch (MEDIUM):** Added `_FULL_DOWNLOAD = 3`
  constant and an explicit branch: when `required == 3`, `full_upload_or_download(upload=True)`
  is called with a warning log asserting local authority.  Previously this case silently fell
  through to the unknown-value error path.

- **sync.py — ANKI_SYNC_USER1 credential strip (MEDIUM):** After `partition(":")`, both the
  username and password parts are now `.strip()`-ped to remove accidental whitespace.  The
  `partition` (first-colon-only split) was already correct; only the strip was missing.

- **sync.py — lock-hold comment (LOW):** Added a comment before the `with col_mod._col_lock:`
  block noting that the lock is held for the full network round-trip, and the implications if
  waitress thread count is ever raised above 1.

- **tests/test_fsrs.py — FSRS rollback test added:** New `TestFsrsOptimizerRollback` class with
  `test_optimizer_failure_rolls_back_fsrs`: monkeypatches `col._backend.compute_fsrs_params` to
  raise, then asserts `result["enabled"] is False` and `is_fsrs_enabled() is False` — confirming
  the rollback reaches the collection config and is not just a return-value claim.

- **tests/test_sync.py — renamed-field assertions:** Updated `TestSyncSecondRun` to assert on
  `result["synced"]` (not `result["uploaded"]`) when `required == 1` (NORMAL_SYNC), and to assert
  `result["uploaded"] is False` when `required == 0` (NO_CHANGES).

### Added (Step 8 — sync.py + fsrs.py)

- `src/sync.py`: Upload-wins sync integration against an AnkiWeb-compatible
  sync server (e.g. `ghcr.io/luckyturtledev/anki`).

  - `do_sync(force_upload, sync_media)` — full sync state machine:
    - `FULL_UPLOAD (4)`: server empty → upload.
    - `FULL_SYNC (2)`: conflict → upload (Tilts wins, logged loudly).
    - `NORMAL_SYNC (1)`: incremental exchange via `sync_collection`.
    - `NO_CHANGES (0)`: already in sync.
    - `force_upload=True`: skips `sync_collection` probe entirely, calls
      `full_upload_or_download(upload=True)` unconditionally; returns
      `required=-1`.  Used for the cutover path.
  - Credentials from `ANKI_SYNC_USERNAME` / `ANKI_SYNC_PASSWORD`, or
    `ANKI_SYNC_USER1=user:pass` (luckyturtledev convention).
  - Endpoint from `ANKI_SYNC_ENDPOINT` (default `http://anki-sync:8080`).
  - `SYNC_ACTIONS` dict (`{"sync": handler}`) for server.py dispatch.
  - Media sync: `sync_collection(sync_media=True)` covers media in one
    round-trip; separate `col.sync_media(auth)` is only needed if
    `sync_media=False` is passed (callers can opt out).

  Confirmed API signatures (anki 25.9.2):
    - `col.sync_login(username, password, endpoint) -> SyncAuth`
    - `col.sync_collection(auth, sync_media) -> SyncOutput`
    - `col.full_upload_or_download(*, auth, server_usn, upload) -> None`
    - `col.sync_media(auth) -> None`   (separate; exists but not called
      when `sync_media=True` in `sync_collection`)
    - `SyncCollectionResponse.required` enum: 0/1/2/4

- `src/fsrs.py`: FSRS enable + optimize helpers for the cutover action.

  - `is_fsrs_enabled() -> bool` — reads `col.get_config("fsrs", False)`.
  - `enable_fsrs(optimize=True) -> dict` — idempotent enable + optional
    weight optimization:
    - `col.set_config("fsrs", True)`
    - `col._backend.compute_fsrs_params(search="", current_params=[],
      ignore_revlogs_before_ms=0, num_of_relearning_steps=1,
      health_check=True)` → 21-float FSRS-6 vector
    - Applies params to all deck presets: `fsrsWeights`, `fsrsParams5[:17]`,
      `fsrsParams6` via `col.decks.all_config()` + `update_config()`.
    - Graceful degradation: optimizer failure → enabled=True, optimized=False
      (collection keeps default weights rather than crashing).
  - `FSRS_ACTIONS` dict (`enableFsrs`, `isFsrsEnabled`) for server.py.

  Observed from 3741 revlog entries: `num_params=21`, `fsrs_items=1107`,
  `health_check_passed=True`.

- `tests/test_fsrs.py`: 6 tests (all pass, 0.71 s):
  - `is_fsrs_enabled` returns False on fresh backup.
  - `enable_fsrs(optimize=True)` → `enabled=True optimized=True
    num_params=21 fsrs_items=1107 health_check_passed=True`.
  - `is_fsrs_enabled` returns True after enable.
  - Idempotent second call succeeds.
  - `optimize=False` path works.
  - FSRS stays enabled after no-optimize call.

- `tests/test_sync.py`: 3 tests (all pass, 1.04 s) against a disposable
  `ghcr.io/luckyturtledev/anki` container on port 27798 (production
  anki-sync on port 27701 is never touched):
  - First sync: `required=4` (FULL_UPLOAD) → upload succeeds.
  - Second sync: `required=0` (NO_CHANGES) — already in sync.
  - Force upload: `required=-1`, `uploaded=True`.

### Fixed (critic HIGH/LOW — review_session.py)

- **Review-duration timer records ~0 ms (Critic HIGH)** — `card.start_timer()` was
  previously called inside `gui_answer_card` immediately before `build_answer`, so
  `card.time_taken()` always returned ~0 ms and every revlog entry recorded zero
  duration.  Real Anki starts the timer when the card is *shown*.

  Fix: `gui_current_card` now calls `card.start_timer()` when the card is served and
  saves the resulting `card.timer_started` float as `session.card_timer_started`.  In
  `gui_answer_card` the saved value is restored onto the freshly-fetched card object
  (`card.timer_started = saved_timer`) *before* `build_answer` is called, so
  `card.time_taken()` returns the actual elapsed wall-clock seconds.

  Measured: 1.1 s think-time → `revlog.time = 1100 ms` (previously 0 ms).
  `guiStartCardTimer` still works as a UI affordance (it can refresh the start time).

- **No ease-in-buttons guard (Critic HIGH)** — a malformed ease value not in the
  current card's available buttons (e.g. ease=2 on a new card with buttons `[1,3,4]`)
  silently applied wrong scheduling.  Fix: `current_buttons` is now stored in the
  session at `guiCurrentCard` time; `gui_answer_card` raises
  `ValueError("ease N not available; valid buttons=[...]")` if the submitted ease is
  not in the stored button list.

- **Test-fixture silent skip in CI (Critic LOW)** — the backup path is mode 0600/UID
  1005, so running tests as another UID caused `pytest.skip` on the backup fixture,
  making CI green with 0 assertions executed.  Fix:
  - Read backup path from `ANKI_TEST_BACKUP` env var (fallback to default path).
  - Replace `pytest.skip` with `pytest.fail` (loud failure) when the resolved path
    does not exist or is not readable.
  - Tests run green (29 passed) with `ANKI_TEST_BACKUP=/tmp/anki-test-backup.anki2`.

- **Two new tests added**:
  - `TestStaleProtection::test_ease_not_in_buttons_raises_value_error` — submitting an
    ease absent from the card's button set raises `ValueError` (tests Critic HIGH fix).
  - `TestReviewDurationTimer::test_revlog_time_is_nonzero_after_sleep` — serves a
    card, sleeps 1.1 s, answers it, and asserts `revlog.time >= 500 ms`
    (tests Critic HIGH timer fix; measured ~1100 ms in practice).

  Total test count: 27 → 29 (all pass, 1.63 s).

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
