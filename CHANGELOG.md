# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — configurable admin console URL prefix (feat/admin-basepath)

- **`ADMIN_BASE_PATH` environment variable**: Controls the URL prefix under which
  the entire admin console (pages, API, static assets) is served.  Default is
  `/admin` for full backward-compatibility with existing deployments.

  With `ADMIN_BASE_PATH=/anki-admin` a 1:1 reverse proxy can map
  `/anki-admin/* → sidecar /anki-admin/*` without URL rewriting, because every
  admin URL the server emits (HTML links, CSS/JS asset refs, login redirect
  `Location` header, JS fetch targets, cookie `Path`) is now under the configured
  prefix.

- **Blueprint url_prefix applied centrally** (`src/server.py`): The admin
  blueprint is registered via `app.register_blueprint(admin_bp, url_prefix=ADMIN_BASE_PATH)`
  rather than hard-coding `/admin` inside the blueprint definition.

- **Blueprint static assets** (`src/admin/routes.py`): The admin blueprint now
  owns a `static_folder` mounted at `<prefix>/static/`.  All templates reference
  admin assets via `url_for('admin.static', filename=...)` (was `url_for('static', ...)`),
  so asset URLs automatically reflect the configured prefix.  Blueprint static
  files are exempt from authentication (`admin.static` added to `_EXEMPT_ENDPOINTS`)
  so the login page can load its stylesheet before the user has a session cookie.

- **Context processor** (`src/admin/routes.py`): Every blueprint template receives
  `admin_base` (the configured prefix without trailing slash).  `base.html` and
  `login.html` inject `<script>window.ADMIN_BASE = "{{ admin_base }}";</script>`
  so client-side JS can resolve fetch targets and redirects without hardcoded paths.

- **JS fetch targets updated** (`static/admin/admin.js`, `static/admin/maintenance.js`):
  All fetch calls and client-side redirects previously hardcoded to `/admin/api/invoke`
  and `/admin/login` now use `window.ADMIN_BASE` (with `/admin` as the JS-side
  fallback so scripts remain functional in test environments where the global is
  absent).

- **Cookie path scoped to prefix** (`src/admin/routes.py` `login_post`): The
  `token` session cookie `Path` attribute is set to `ADMIN_BASE_PATH` (was
  unset / global `/`), so the cookie is only sent to URLs under the admin prefix.

- **`_normalize_base_path` helper** (`src/server.py`): Adds leading slash if
  missing, strips trailing slashes, defaults to `/admin` for empty input.

- **Tests** (`tests/test_admin_basepath.py`): 31 new tests covering:
  - `_normalize_base_path` edge cases
  - Default prefix (`/admin`) — all existing behaviour preserved
  - Custom prefix (`/anki-admin`) — login 200, old `/admin/*` 404, API gated,
    static served, HTML emits correct prefix, `window.ADMIN_BASE` injected,
    login redirect under custom prefix, cookie path set, logout under prefix,
    admin disabled returns 503 under custom prefix

### Fixed — scheduling panel FSRS preset-name bug (feat/admin-actions)

- **BUG — scheduling.js FSRS panel**: Removed `getFsrsParams({deck: presetName})`
  call that raised "Deck not found: '<preset name>'" on every preset selection
  when the preset name did not match any deck name (e.g. "Latvian Basic").
  The FSRS panel now reads `desiredRetention` and `fsrsParams6`/`fsrsParams5`/
  `fsrsWeights` directly from the already-loaded `currentConfig` dict (populated
  by `getDeckConfigs`) — no extra round-trip needed and no spurious server ERROR.

- **actions.py `setDesiredRetention`**: Added `configId` (preset id) parameter
  so the UI can write to the correct preset without resolving through a deck name.
  Legacy `deck` parameter still accepted for backward compatibility.  New helper
  `_resolve_config_id()` shared between `setDesiredRetention` and `getFsrsParams`.

- **actions.py `getFsrsParams`**: Added optional `configId` parameter so callers
  can look up FSRS params for a specific preset without needing a deck name.
  Falls back to legacy `deck` parameter when `configId` is absent.

- **tests/test_admin_scheduling_actions.py**: Added `TestSetDesiredRetention`
  cases for `configId` path (persists, visible via getDeckConfig, rejects bad id,
  still enforces retention bounds) and new `TestGetFsrsParamsByConfigId` class
  (matches deck-path result, rejects non-existent configId). Test count: 32 → 42.

### Fixed — Code Critic remediation (feat/admin-actions)

- **HIGH XSS — diagnostics.js summary strip**: Added `escHtml()` helper (mirrors
  `maintenance.js`). `status` from unauthenticated `/health` is now escaped in
  both the `class="status-…"` suffix and the visible `<strong>` text. `note_count`
  is coerced via `Number(…).toLocaleString()` (was already guarded but tightened
  to match the `total` pattern). All other summary-strip slots use numeric
  `.toLocaleString()` or static strings — no further escaping needed.

- **MEDIUM in-flight guards — maintenance.js** (`removeEmptyBtn`,
  `deleteUnusedBtn`): Module-level boolean flags (`_removeEmptyInFlight`,
  `_deleteUnusedInFlight`) prevent a second destructive dispatch while a
  pre-fetch or confirm dialog is pending. Flags are set before `startOp` and
  cleared in `finally`; fast double-clicks bail immediately.

- **MEDIUM empty-count UX — maintenance.js**: `removeEmptyBtn` now shows "No
  empty cards found." and returns without a confirm dialog when
  `emptyCardCount === 0`. `deleteUnusedBtn` shows "No unused media found." when
  the unused list is empty.

- **LOW preset-switch race — scheduling.js**: `onPresetChange()` snapshots
  `currentConfig = { ...allPresets[idx] }` (shallow copy) before calling
  `loadFsrsPanel()`, preventing a fast preset-switch from showing mismatched FSRS
  data from a stale async fetch.

- **CHORE — tests/test_admin_triage_actions.py:569**: Removed unused `note_a`
  variable assignment (ruff F841) — call result is intentionally side-effect-only.

### Added — A9: Diagnostics dashboard (feat/admin-actions) (CHECKPOINT)

#### GET /admin/diagnostics — Diagnostics Dashboard

New page at `/admin/diagnostics` (token-gated, added to base nav as active
link replacing placeholder span; dashboard panel marked live with link).
Renders a static shell; all stat data is fetched client-side via `acsInvoke`.

**Chart.js vendored locally**

Chart.js **4.4.9** (UMD minified) is downloaded into
`static/admin/vendor/chart.min.js` (206 944 bytes).  The page loads it via:

```html
<script src="{{ url_for('static', filename='admin/vendor/chart.min.js') }}"></script>
```

No CDN is used; the sidecar works fully offline/internal.

**Header strip — collection summary**

A summary bar at the top of the page shows (via parallel `acsInvoke` calls):
total cards, note count (from `/health`), new/learning/review breakdown
(from `statCardCounts`), and collection status.  Loaded independently from the
charts; best-effort (missing fields gracefully omitted).

**Time-range selector**

A dropdown (30 / 90 / 365 days) controls the `days` parameter sent to all
time-range-aware stat actions.  Changing the selection or clicking Refresh
re-loads all charts in parallel.

**8 charts wired to A5 stat actions**

| Chart | Action | Chart type | Notes |
|-------|--------|------------|-------|
| Card Counts | `statCardCounts` | Doughnut | new/learning/review/suspended/buried; total label |
| True Retention | `statTrueRetention` | Bar + table | young/mature/overall pass/total/%; time-range |
| Interval Distribution | `statIntervalDistribution` | Bar histogram | 7 bucket labels; no time-range param |
| Ease/Difficulty | `statEaseDistribution` | Bar | SM-2 factor buckets; `fsrs_note` shown if present |
| Future Due | `statFutureDue` | Bar forecast | day_offset → count; time-range |
| Reviews by Day | `statReviewsByDay` | Grouped bar | both `reps` (all) and `reviews` (type=1); time-range |
| Cards Added by Day | `statAddedByDay` | Bar | daily card-creation count; time-range |
| Time Spent | `statTimeSpent` | Bar (minutes) | per-day bars; totalMs + avgMsPerRep summary strip |

**Per-chart error isolation**

Each loader is independently wrapped in `try/catch` around its `acsInvoke`
call and run via `Promise.allSettled`.  A failure in one stat action renders
that chart's `.diag-error` banner and does not prevent any other chart from
loading.  Loading states use the canvas placeholder while data is in-flight.
Empty-data states (all-zero results) show a `.diag-empty` banner with a
"no data" message per chart.

**New files**

- `templates/admin/diagnostics.html` — Jinja2 template extending
  `admin/base.html`.  Contains 8 `<canvas>` elements, the summary strip, and
  the time-range selector.
- `static/admin/diagnostics.js` — ~390 LOC dependency-free vanilla ES5/ES2020.
  Defines independent loader functions for each chart; `loadAll()` runs all via
  `Promise.allSettled` for full concurrency.
- `static/admin/vendor/chart.min.js` — Chart.js 4.4.9 UMD bundle (vendored;
  206 944 bytes; downloaded from cdn.jsdelivr.net/npm/chart.js@4.4.9).

**Modified files**

- `src/admin/routes.py`: added `GET /admin/diagnostics` route; docstring
  updated to include A9.
- `templates/admin/base.html`: "Diagnostics" nav replaced from placeholder
  `<span class="nav-placeholder">` to active `<a>` link with active-class logic.
- `templates/admin/index.html`: Database & Media and Diagnostics dashboard
  panels both marked live with links.
- `static/admin/admin.css`: A9 diagnostics styles appended (~170 LOC) —
  responsive 2-column grid, chart cards, summary strip, empty/error states,
  retention table, FSRS note, time summary.
- `README.md`: `/admin/diagnostics` added to admin pages table.

**Tests: `tests/test_admin_diagnostics_ui.py` — 17 tests, all pass, 0.28 s**

- `TestDiagnosticsAuthGate` (4): valid token → 200; no token → 302/401; no
  ADMIN_TOKEN → 503; wrong token → 302/401.
- `TestDiagnosticsRoute` (9): page title "Diagnostics"; time-range selector
  present; all 8 chart canvas IDs present; diagnostics.js loaded; vendored
  chart.min.js referenced; nav link active; summary strip present; refresh
  button present; cookie auth grants access.
- `TestVendoredChartJs` (4): GET static path → 200; non-empty (>10 KB);
  JavaScript content-type; file exists on disk at expected path.

Total test count: 353 → 370 (all pass, 3 skipped — FSRS optimizer, expected).
`ruff check src/ tests/test_admin_diagnostics_ui.py`: clean.

---

### Added — A8: Database & Media health panel (feat/admin-actions)

#### GET /admin/maintenance — Database & Media Health

New page at `/admin/maintenance` (token-gated, added to base nav).  Renders a
static shell; all actions are called client-side via `acsInvoke`.

**Database section**

Five operation buttons with inline spinner + per-section result display:

| Button | Action | Confirm? | Destructive? |
|--------|--------|----------|--------------|
| Check Database | `checkDatabase` | No | No |
| Find Empty Cards | `getEmptyCards` | No | No |
| Optimize (VACUUM) | `optimizeCollection` | ⚠ single | Yes — backup shown |
| Fix Integrity | `fixIntegrity` | ⚠⚠ double | Yes — backup shown prominently |
| Remove Empty Cards | `removeEmptyCards` | ⚠⚠ double + count | Yes — backup shown |

Remove Empty Cards pre-fetches the empty card count via `getEmptyCards` and
interpolates it into the confirm text before calling `removeEmptyCards`.

**Media section**

| Button | Action | Confirm? | Destructive? |
|--------|--------|----------|--------------|
| Media Check | `mediaCheck` | No | No |
| Media Dir Size | `mediaDirSize` | No | No |
| Delete Unused Media | `deleteUnusedMedia` | ⚠⚠ double + count | Yes — NOT backup-recoverable |

Delete Unused Media pre-fetches the unused file count via `mediaCheck` and
interpolates it into the confirm text.

**UX details**

- Long-running operations (Optimize, Fix Integrity, Media Check) use a 5-minute
  AbortController timeout so the browser does not give up early.
- Every in-flight operation disables its button and shows an inline spinner.
- Destructive results display the returned backup path in a highlighted
  monospace box.
- Unused / missing media file lists are collapsible `<details>` elements,
  capped at 50 items with a "…and N more" overflow indicator.

### Added — A7: Scheduling admin page (feat/admin-actions)

#### GET /admin/scheduling — Deck Options + FSRS Panel

New page at `/admin/scheduling` (token-gated, added to base nav, dashboard
panel marked live).  Renders a static shell; all data is fetched client-side
via `acsInvoke` through the existing `POST /admin/api/invoke` proxy — no
collection access at render time.

**Preset selector**

- `getDeckConfigs` is called on page load to populate a dropdown of all
  configuration presets (name + id).
- Selecting a preset populates the Deck Options form and the FSRS panel.
- Auto-selects the only preset when the collection has exactly one (common case).

**Deck Options form (read-modify-write)**

Fields grouped into New Cards / Reviews / Lapses fieldsets, bound to the
confirmed camelCase keys from the deck config dict:

| Field | Config key |
|-------|-----------|
| New cards per day | `new.perDay` |
| Learning steps | `new.delays` (space-separated min input → list) |
| Graduating interval | `new.ints[1]` |
| Easy interval | `new.ints[0]` |
| Bury new siblings | `new.bury` |
| Reviews per day | `rev.perDay` |
| Max interval | `rev.maxIvl` |
| Interval modifier | `rev.ivlFct` (stored as fraction; displayed as %) |
| Bury review siblings | `rev.bury` |
| Relearn steps | `lapse.delays` |
| Leech threshold | `lapse.leechFails` |
| Leech action | `lapse.leechAction` (0=suspend/1=tag dropdown) |
| Min interval after lapse | `lapse.minInt` |
| New interval after lapse | `lapse.mult` (stored as fraction; displayed as %) |

Read-modify-write: the full raw config dict from `getDeckConfigs` is stored
in memory. On save, a shallow copy of the dict + shallow copies of only the
three mutated sub-dicts (`new`, `rev`, `lapse`) are built; all other top-level
keys (FSRS params, `id`, `name`, `mod`, `usn`, metadata) are preserved verbatim.
The mutated dict is sent to `updateDeckConfig`. Client-side validation runs
before the confirm dialog and the server re-validates on receipt.

Save shows a confirm dialog: "This changes scheduling for ALL decks using preset
`<name>`."  After a successful save, the in-memory reference is updated so
subsequent saves stay consistent.

**FSRS panel**

- `isFsrsEnabled` → enabled/disabled badge; Enable FSRS button or "active" note.
- **Enable FSRS** (`enableFsrs`, optimize=true): confirm dialog with warning
  about optimizer affecting all devices; re-loads the FSRS panel on success.
- **Desired retention**: current value from `getFsrsParams`; editable (0.70–0.97);
  Save button → confirm → `setDesiredRetention`.
- **FSRS parameters**: read-only grid of the 21-float weight vector (or empty note
  if no params stored).
- **Compute Optimal Retention**: `computeOptimalRetention` → displays suggested
  value or structured error note; best-effort.
- **Prominent caveat banner**: "this headless server cannot auto-reschedule existing
  cards when FSRS/params change (desktop-only operation). New scheduling applies as
  cards are reviewed." (A0-documented Qt-only limitation; no fake reschedule button).

**New files**

- `templates/admin/scheduling.html` — Jinja2 template extending `admin/base.html`.
- `static/admin/scheduling.js` — ~380 LOC dependency-free vanilla ES2020.

**Modified files**

- `src/admin/routes.py`: added `GET /admin/scheduling` route + docstring update.
- `templates/admin/base.html`: "Scheduling" nav replaced from placeholder `<span>`
  to active `<a>` link.
- `templates/admin/index.html`: Scheduling panel marked live with link.
- `static/admin/admin.css`: A7 scheduling styles appended (~130 LOC).
- `README.md`: `/admin/scheduling` added to admin pages table + section.

**Tests: `tests/test_admin_scheduling_ui.py` — 15 tests, all pass, 0.28 s**

- `TestSchedulingAuthGate` (3): without token -> 302/401; no ADMIN_TOKEN -> 503;
  wrong token -> 302/401.
- `TestSchedulingRoute` (12): valid token -> 200; page title contains
  "Scheduling"; preset selector present; deck-options form present; FSRS card
  present; caveat banner present + mentions headless/desktop; Enable FSRS button
  present; desired-retention input present; compute-retention button present;
  scheduling.js loaded; nav link active; cookie auth grants access.

Total test count: 323 → 338 (all pass, 3 skipped — FSRS optimizer, expected).
`ruff check src/ tests/test_admin_scheduling_ui.py`: clean.

### Fixed — Code Critic remediation: browse/invoke (feat/admin-actions)

#### HIGH — `test_invoke_with_cookie_auth` test bug (`tests/test_admin_browse.py`)

- The test called `c.set_cookie("localhost", "token", _TOKEN)` using the old
  Werkzeug positional-domain API.  Flask 3.1 / Werkzeug 3.x removes the
  positional `server_name` argument; calling `set_cookie(key, value)` on that
  version silently stores the cookie against the wrong domain, so the assertion
  was never reached.
- Fix: replaced `set_cookie` with the real login flow — `c.post("/admin/login",
  data={"token": _TOKEN})` — which works on all Flask versions and authentically
  exercises the path a browser takes.  The subsequent `POST /admin/api/invoke`
  now reaches the auth check and passes with the session cookie.
- Confirmed: `TestInvokeDispatch::test_invoke_with_cookie_auth` now executes and
  passes (`PASSED [ 64%]`).

#### MEDIUM — XSS hardening on `confirm()` dialog (`static/admin/browse.js`)

- `confirmBody.innerHTML = body` in the `confirm()` helper permitted HTML
  injection if any future caller passed unescaped user data as the message body.
- Fix: `confirm()` now sets `confirmBody.textContent = body`, which HTML-encodes
  all characters and prevents any markup from being interpreted.  The `opts.bodyNode`
  escape hatch is provided for callers that need pre-built DOM nodes (must be
  constructed from safe data by the caller).
- All 11 `confirm()` call sites converted from `innerHTML`-style template
  literals with `<strong>` / `<br>` tags to plain-text messages; user-supplied
  values (tags, deck names, day specs) appear directly via `textContent` — no
  `esc()` call needed in the message string because `textContent` never
  interprets HTML.
- The now-unused `esc()` calls in the `changeDeck`, `setDueDate`, `addTags`,
  `removeTags`, `reposition`, and `fnrApply` callers were removed.

#### LOW — double-encoding on `data-query` attribute (`static/admin/browse.js` ~line 1010)

- `data-query="dupe:${esc(field)}"` HTML-encoded the field name, which was then
  double-decoded when `a.dataset.query` was written into `searchInput.value`.
  Field names are server-controlled identifiers (never user-supplied text in this
  path), so no encoding is needed.
- Fix: changed to `data-query="dupe:${field}"` — the raw field string is stored
  in the attribute and read back without double-decode artefacts.

323 passed, 3 skipped.  `ruff check src/ tests/test_admin_browse.py`: clean.

### Added — A6: Card/Note Browser + Triage admin page (feat/admin-actions)

#### POST /admin/api/invoke -- token-gated AnkiConnect proxy
- New route `POST /admin/api/invoke` on the admin blueprint.
- Accepts `{"action": str, "params": dict}` and dispatches via the same
  merged `DISPATCH` table (`ACTIONS + GUI_ACTIONS + SYNC_ACTIONS + FSRS_ACTIONS`)
  used by the raw `POST /` AnkiConnect endpoint.
- Enforced by the existing `before_request` auth hook -- requires the same
  token (cookie / X-Admin-Token header / Basic auth) as all `/admin/*` routes.
- Returns the standard AnkiConnect envelope `{"result": ..., "error": ...}`.
- The browser admin UI never hits the unauthenticated `POST /` directly.

#### `acsInvoke(action, params)` JS helper (`static/admin/admin.js`)
- Thin `fetch`-based wrapper around `POST /admin/api/invoke`.
- Sends `credentials: 'same-origin'` so the session cookie is forwarded.
- Returns the `result` value on success; throws `Error(envelope.error)` on
  action-level failure; auto-redirects to `/admin/login` on 401.

#### GET /admin/browse -- Card/Note Browser
- Full-featured card and note browser at `/admin/browse`.
- **Search bar**: free-text Anki query with `deck:`, `tag:`, `is:due/new/suspended/buried`,
  `prop:ivl`, `added:`, `flag:`, `dupe:`, `re:` support; search-help hint panel.
- **Results table**: columns -- first field, deck, note type, due, interval,
  reps, lapses, flags (color swatch), state (new/learn/review/suspended/buried).
  Client-side sortable; row click opens note editor panel.
- **Pagination**: prev/next, configurable page size (25/50/100/200), live
  `findCardsPaginated` + `cardsInfo` calls via `acsInvoke`.
- **Multi-select**: per-row checkbox + select-all; selection count displayed
  in status bar; bulk toolbar appears when cards are selected.
- **Bulk actions** (all confirm before executing):
  - suspend / unsuspend, bury / unbury
  - set flag (0-7 with color picker)
  - change deck (dropdown populated from `deckNames`)
  - set due date (day-spec string, e.g. "3-7")
  - forget cards (double-confirm, resets to new)
  - reposition new cards (start + step)
  - add tags / remove tags
  - delete notes (double-confirm, permanent)
- **Note editor panel** (slide-in overlay): editable fields (textarea per
  field), tag editor (space-separated), Save -> `updateNoteFields` + tag diff
  (`addTags`/`removeTags`), Delete-note button (double-confirm).
- **Find & Replace** mini-tool: field selector, search/replacement text,
  regex + match-case toggles; scoped to current page's notes; shows count
  changed.
- **Find Duplicates** mini-tool: field name selector -> `findDuplicates`;
  lists duplicate groups with filter links.
- Nav link wired in `templates/admin/base.html` (replaces "Browser" placeholder).
- Dashboard `index.html` updated: Browser panel marked live with link.
- `static/admin/admin.css`: all browse-specific styles added (buttons, table,
  state badges, flag dots, editor overlay, modal dialogs, spinner).
- `static/admin/browse.js`: 600 LOC dependency-free vanilla JS.

#### Tests (`tests/test_admin_browse.py` -- 14 new tests)
- `TestInvokeAuthGate`: invoke without token -> 401/302; wrong token -> 401;
  no ADMIN_TOKEN -> 503.
- `TestInvokeDispatch`: `deckNames` via proxy returns correct envelope;
  `version` action returns 6; unknown action -> `{result:null,error:"..."}`;
  missing `action` field -> 400; handler exception -> envelope error;
  cookie auth grants access.
- `TestBrowseRoute`: browse without token redirects; browse with token -> 200;
  no ADMIN_TOKEN -> 503; page contains expected UI elements; nav link active.

### Fixed — Code Critic WARN remediation: honest naming + atomicity (feat/admin-actions A5)

Addresses WARN findings from the Code Critic review of `src/stats.py`.
Counting semantics are UNCHANGED — all fixes are naming, documentation, and
a thread-safety improvement.

**`statReviewsByDay` — clarified naming + dual rep-type fields:**

- The action has always counted ALL `revlog` entries regardless of
  `revlog.type` (learning=0, review=1, relearning=2, filtered=3), matching
  Anki's own "Reviews" graph and `getNumCardsReviewedByDay`.  This is correct
  and intentional; the Critic WARN was that the name "reviews" implied
  review-type-only.
- Fix: docstring now explicitly states "counts ALL repetitions (learning +
  review + relearning + filtered), matching Anki's Reviews graph — NOT
  review-type-only."
- The SQL query now fetches `revlog.type` alongside `id` and `time`.
- Two new output fields added per day (no breaking change — existing
  `count` and `timeMs` are preserved):
  - `reps` (int): all rep types, equal to `count`; name makes the
    all-type semantics explicit.
  - `reviews` (int): `revlog.type = 1` only (graduated card review reps),
    so callers who need pure-review throughput do not need a second query.
- Fixture has 40 total reps (28 type=1 + 12 type=0); both values now
  independently tested.

**`statTimeSpent` — rename `avgMsPerReview` → `avgMsPerRep`:**

- The time total has always covered ALL rep types (matches Anki's "Time
  Spent" graph), making the key name `avgMsPerReview` misleading.
- **BREAKING rename**: output key `avgMsPerReview` → `avgMsPerRep`.
  Any callers reading this field must update their key lookup.
- Docstring updated to state "ALL revlog entries regardless of type".
- `avgMsPerRep` is `None` when the window contains zero repetitions
  (div-by-zero guard was already present; now explicitly documented).

**`statEaseDistribution` — atomic FSRS detection + SQL under lock:**

- The `col.decks.all_config()` FSRS-detection loop was previously executed
  BEFORE entering `col_mod._col_lock`, meaning a concurrent write that
  enabled FSRS between detection and the SQL query could produce a stale
  `fsrs_note` label.
- Fix: moved the detection loop INSIDE the `with col_mod._col_lock:` block
  so detection and SQL fetch are atomic under `threads=2`.  No behaviour
  change in single-threaded usage.

**Tests (`tests/test_admin_stats.py`):**

- `TestStatReviewsByDay.test_shape`: now asserts `reps` and `reviews` keys.
- `TestStatReviewsByDay.test_reps_equals_count`: new — verifies `reps == count`.
- `TestStatReviewsByDay.test_reviews_lte_reps`: new — verifies `reviews <= reps`.
- `TestStatReviewsByDay.test_fixture_total_reps_all_types`: renamed from
  `test_fixture_total_reviews`; comment clarified as "all rep types, 40 total".
- `TestStatReviewsByDay.test_fixture_total_reviews_type1_only`: new — asserts
  `sum(reviews) == 28` (fixture has 28 type=1 entries).
- `TestStatTimeSpent.*`: all `avgMsPerReview` references renamed to
  `avgMsPerRep`; `test_avg_ms_per_review_range` renamed
  `test_avg_ms_per_rep_range`.
- `TestStatTimeSpent.test_fixture_known_time`: docstring clarified to state
  "ALL rep types counted (Anki-consistent): 28×10000ms + 12×5000ms = 340000ms".

53 tests, all passing.  `ruff check` clean.

### Added — P0 diagnostics & stats actions (feat/admin-actions A5)

New module `src/stats.py` adds 8 read-only diagnostic action handlers registered
under `STATS_ACTIONS` and merged into the global `ACTIONS` dispatch table.  All
queries run under `col_mod._col_lock` for consistency.  All errors propagate as
`ValueError` so the server envelope converts them to `{"result": null, "error": ...}`.

**Actions added:**

| Action | Params | Returns |
|--------|--------|---------|
| `statCardCounts` | (none) | `{new, learning, review, suspended, buried, total}` |
| `statTrueRetention` | `{days=30}` | retention by young/mature/overall |
| `statIntervalDistribution` | (none) | 7-bucket interval histogram (queue=2 cards) |
| `statEaseDistribution` | (none) | SM-2 ease factor histogram + FSRS note |
| `statFutureDue` | `{days=30}` | per-day due counts for next N days |
| `statReviewsByDay` | `{days=365}` | daily `{count, reps, reviews, timeMs}` from revlog (all types) |
| `statAddedByDay` | `{days=365}` | daily card-creation counts |
| `statTimeSpent` | `{days=30}` | `{totalMs, perDayMs, avgMsPerRep}` from revlog (all types) |

**Due semantics confirmed (empirically):**
`cards.due` for queue=2 (review) cards is an integer day number since `col.crt`.
`col.sched.today` is the current day number.  `due - today` gives the offset
from now in days (negative = overdue).  The `statFutureDue` query uses
`due >= today AND due <= today + days` to capture only cards due today or in
the future; overdue cards (due < today) are excluded.

**FSRS difficulty caveat:**
FSRS stores per-card difficulty in `card.data` (a JSON blob), not in
`card.factor`.  `statEaseDistribution` reports `card.factor` buckets (which
are mostly the default 2500 / 250% under FSRS) and includes a `fsrs_note`
string explaining the limitation.  Per-card FSRS difficulty extraction would
require parsing the JSON blob for every review card — expensive and fragile
across Anki versions — so it is not implemented.

**Tests:** `tests/test_admin_stats.py` — 53 tests, all passing.  Covers shape
validation, math sanity (pass ≤ total, 0 ≤ retention ≤ 100), cross-action
consistency (interval sum == review count; `statReviewsByDay` matches
`getNumCardsReviewedByDay`), and fixture-specific known-value assertions.
(Test count reflects Code Critic remediation additions above.)

### Fixed — Code Critic HIGH remediation: maintenance/backup (feat/admin-actions)

**`src/maintenance.py` — WAL checkpoint abort on failure**

- Changed the `except` around `pragma wal_checkpoint(TRUNCATE)` from
  log-and-continue to `raise ValueError(...)` — a failed checkpoint means the
  WAL has not been flushed into the main `.anki2` file; proceeding with a
  `shutil.copy2` would produce a structurally incomplete backup.  The
  `ValueError` propagates through the action handler (which converts it to
  `{"result":null,"error":"..."}` HTTP 200) and prevents the destructive
  operation from running.

**`tests/test_admin_maintenance_actions.py` — regression test for partial-empty note**

- Added `TestRemoveEmptyCards.test_partial_empty_multicard_note_preserves_note_and_nonempty_card`:
  creates a 2-template notetype, adds a note with both fields filled (2 cards
  generated), clears Field2 so Card2 becomes empty, asserts `getEmptyCards`
  reports `willDeleteNote=False`, calls `removeEmptyCards`, then asserts the
  note survives with exactly 1 card and `getEmptyCards` returns 0.  Guards
  against a future regression if the remove logic is ever reimplemented manually.
  (Note: Anki does not generate a card when the template field is empty at insert
  time — the "empty card" state is reached only by clearing a field on a
  previously-created note; the test reproduces that realistic scenario.)

### Added — P0 DB & Media health actions + pre_backup helper (feat/admin-actions, A4)

Eight new actions for database integrity, collection optimisation, empty-card
management, and media hygiene.  All implemented in `src/actions.py` and
registered in `ACTIONS`.  All mutations hold `col_mod._col_lock`; errors raise
`ValueError` → server converts to `{"result":null,"error":"..."}` HTTP 200.

**New module: `src/maintenance.py`**

- `pre_backup(reason: str) -> str`: copies the live collection `.anki2` file to
  `<collection_dir>/backups/admin-<reason>-<UTC-stamp>.anki2`.  Uses
  `col.db.execute("pragma wal_checkpoint(TRUNCATE)")` to flush the WAL into the
  main file before copying with `shutil.copy2` — safe while the collection is
  open and held under `_col_lock`.  Called by every destructive action below.
  Special characters in `reason` are replaced with `-`.

**New actions (all run under `col_mod._col_lock`):**

| Action | Type | Output |
|---|---|---|
| `checkDatabase` | read-only | `{problems:[str], ok:bool}` — uses `col._backend.check_database()` (internal API, may break on anki upgrade) |
| `fixIntegrity` | DESTRUCTIVE + backup | `{message:str, ok:bool, backup:str}` — `col.fix_integrity()` |
| `optimizeCollection` | DESTRUCTIVE + backup | `{backup:str}` — `col.optimize()` (VACUUM) |
| `getEmptyCards` | read-only | `{emptyCardCount:int, noteCount:int, report:str, notes:[{noteId,cardIds,willDeleteNote}]}` |
| `removeEmptyCards` | DESTRUCTIVE + backup | `{removed:int, backup:str}` — `col.remove_cards_and_orphaned_notes()` |
| `mediaCheck` | read-only | `{unused:[str], missing:[str], report:str, haveTrash:bool}` — `col.media.check()` (rebuilds media DB as side effect) |
| `deleteUnusedMedia` | DESTRUCTIVE + backup | `{deletedCount:int, backup:str}` — `col.media.trash_files()` + `col.media.empty_trash()`.  NOTE: .anki2 backup does NOT contain media files; restore media from filesystem backup. |
| `mediaDirSize` | read-only | `{bytes:int, fileCount:int, dir:str}` — walks `col.media.dir()` |

**Confirmed API shapes (empirically verified on anki 25.9.2 /tmp copy):**

- `col._backend.check_database()` → `RepeatedScalarContainer` (iterable of `str`)
- `col.fix_integrity()` → `tuple[str, bool]` (message, ok)
- `col.optimize()` → `None`
- `col.get_empty_cards()` → `EmptyCardsReport` with `.notes` (list of
  `NoteWithEmptyCards` with `.note_id`, `.card_ids`, `.will_delete_note`) and
  `.report` (str HTML)
- `col.media.check()` → `CheckMediaResponse` with `.unused`, `.missing`,
  `.missing_media_notes`, `.report`, `.have_trash`
- `col.media.trash_files(fnames: list[str])` → `None`
- `col.media.empty_trash()` → `None`
- `col.media.dir()` → `str`
- Media rebuild: no dedicated rebuild method in anki 25.9.2; `col.media.check()`
  rebuilds the media hash DB as a side effect.  `col.media.force_resync()`
  schedules an AnkiWeb re-sync but does NOT rebuild the local hash table.

**Tests: `tests/test_admin_maintenance_actions.py`** — 54 tests; all pass.

### Security — updateDeckConfig input validation (feat/admin-actions, Code Critic HIGH)

- **Reject unknown preset id**: `updateDeckConfig` now verifies that `cfg["id"]`
  matches an existing preset via `col.decks.all_config()` before writing.
  Unknown ids raise `ValueError("no preset with id=... — use getDeckConfigs for
  valid ids")`.  This prevents ghost-preset creation in Anki's deck config store.
- **Reject missing or non-integer id**: A missing `"id"` key or a non-int value
  now raises `ValueError` with a distinct message before the preset lookup.
- **Scheduling field type validation**: If any of the following keys are present
  in the submitted config dict, they are now type-checked before the write:
  - `new.perDay`, `rev.perDay`, `rev.maxIvl`, `lapse.leechFails`,
    `lapse.leechAction`, `lapse.minInt` → must be `int`
  - `lapse.mult`, `rev.ivlFct`, `rev.ease4`, `rev.hardFactor` → must be `float`
    (bare `int` is coerced silently)
  - `new.delays`, `lapse.delays` → must be `list`
  Absent keys are not validated (sparse updates remain supported).

### Fixed — _compute_optimal_retention exception propagation clarity

- Added inline comment in `_compute_optimal_retention` explaining that the
  `ValueError` from `_resolve_deck_id` is intentionally propagated (not caught)
  so unknown deck names surface as clean envelope errors.  This prevents a
  future edit from accidentally broadening the `try/except` to swallow it.

### Tests — test_admin_scheduling_actions.py (Code Critic guards)

- `test_unknown_preset_id_raises`: verifies `id=99999` raises `ValueError` and
  that `all_config()` length is unchanged (no ghost preset created).
- `test_bad_type_new_per_day_raises`: verifies `new.perDay="bad"` raises
  `ValueError` matching `"new.perDay"` and that the real preset's `perDay` is
  unchanged.
- `test_non_int_id_raises`: verifies a string `"id"` raises `ValueError`.
- `test_valid_update_still_works_after_guards`: regression guard confirming the
  happy-path round-trip still succeeds after the new guard logic.

### Added — P0 scheduling control actions (feat/admin-actions, A3)

Six new actions for the admin scheduling UI, implemented in `src/actions.py`
and registered in `ACTIONS`.  All mutations run under `col_mod._col_lock`.
Errors raise `ValueError` → server converts to `{"result":null,"error":"..."}` HTTP 200.

#### Confirmed deck-config key structure (empirically inspected, anki 25.9.2)

Real key names differ from snake_case guesses; use these:

| Path | Actual key |
|------|-----------|
| Top-level retention | `desiredRetention` (float, NOT `desired_retention`) |
| Top-level FSRS-6 params | `fsrsParams6` (list[float], NOT `fsrs_params_6`) |
| Top-level FSRS-5 params | `fsrsParams5` (list[float]) |
| `new` sub-dict | `perDay`, `delays`, `initialFactor`, `ints`, `order`, `bury` |
| `rev` sub-dict | `perDay`, `maxIvl`, `ivlFct`, `ease4`, `hardFactor`, `bury` |
| `lapse` sub-dict | `delays`, `leechFails`, `leechAction`, `minInt`, `mult` |

API confirmed:
- `col.decks.config_dict_for_deck_id(DeckId(did)) -> DeckConfigDict`
- `col.decks.all_config() -> list[DeckConfigDict]`
- `col.decks.update_config(cfg, preserve_usn=False) -> None`
- `col._backend.compute_optimal_retention(SimulateFsrsReviewRequest) -> float`
- `SimulateFsrsReviewRequest` lives in `anki.scheduler_pb2` (NOT `anki.backend_pb2`)

#### Actions

**`getDeckConfig`**
- Params: `{"deck": int|str}`
- Result: human-relevant fields + raw config under `"config"` key
- Fields returned: `id`, `name`, `new_per_day`, `rev_per_day`, `learning_steps`,
  `relearn_steps`, `graduating_interval`, `easy_interval`, `max_interval`,
  `bury_new`, `bury_reviews`, `leech_fails`, `leech_action`, `new_interval_mult`,
  `min_interval`, `initial_factor`, `desired_retention`, `fsrs_params`, `config`
- `fsrs_params` prefers `fsrsParams6` (21 params), falls back to `fsrsParams5` (17)

**`getDeckConfigs`**
- Params: `{}`
- Result: `list[DeckConfigDict]` — all presets (for preset-selector UI)

**`updateDeckConfig`**
- Params: `{"config": <full DeckConfigDict>}`
- Result: null
- Requires `"id"` field in the config dict; raises `ValueError` if absent.
- Calls `col.decks.update_config(cfg, preserve_usn=False)` under `_col_lock`.

**`getFsrsParams`**
- Params: `{"deck": int|str}`
- Result: `{"params": list[float], "desiredRetention": float}`
- Prefers `fsrsParams6`; falls back to `fsrsParams5`; returns empty list if neither set.

**`setDesiredRetention`**
- Params: `{"deck": int|str, "retention": float}`
- Result: null
- Clamp: raises `ValueError` if `retention` is outside `[0.70, 0.97]`.
- Reads config via `config_dict_for_deck_id`, sets `desiredRetention`, calls `update_config`.

**`computeOptimalRetention`**
- Params: `{"deck": int|str, optional simulation params}`
- Result: `{"optimalRetention": float}` on success; `{"error": str, "note": str}` on failure.
- Optional params: `daysToSimulate` (default 365), `deckSize`, `newLimit`, `reviewLimit`,
  `maxInterval`, `search`.
- Calls `col._backend.compute_optimal_retention(SimulateFsrsReviewRequest(...))`
  (internal API, `anki.scheduler_pb2`).
- **Never raises** — on import failure or backend error, returns `{"error": ..., "note": ...}`
  with an informative message explaining the caveat.
- Requires FSRS enabled + trained weights + sufficient revlog data to succeed.

#### Tests — `tests/test_admin_scheduling_actions.py` (32 tests, all pass, 0.26 s)

- **`TestGetDeckConfig`** (6): all 19 expected keys present; correct types; `config` key
  has raw dict with nested sub-dicts; accepts int deck id; `desired_retention` in (0,1];
  nonexistent deck raises `ValueError`.
- **`TestGetDeckConfigs`** (4): returns list; non-empty; each entry is dict with id/name;
  each entry has `new`, `rev`, `lapse` sub-dicts.
- **`TestUpdateDeckConfig`** (4): round-trip `new_per_day`; round-trip `desiredRetention`;
  missing `id` raises `ValueError`; returns `None`.
- **`TestGetFsrsParams`** (4): returns `params` (list) + `desiredRetention` (float);
  retention in (0,1]; params is list of floats or empty; nonexistent deck raises.
- **`TestSetDesiredRetention`** (9): persists 0.85 (verified via `getFsrsParams`);
  persists 0.92 (verified via `getDeckConfig`); rejects 0.5 (below min); rejects 0.99
  (above max); rejects 0.0; rejects 1.0; accepts boundary 0.70; accepts boundary 0.97;
  nonexistent deck raises.
- **`TestComputeOptimalRetention`** (3): returns float in (0,1) OR error dict — never raises;
  does not raise on thin fixture data; accepts optional simulation params.
- **`TestActionRegistration`** (2): all 6 action keys in ACTIONS; all handlers callable.

Total test count: 165 → 197 (all pass, 3 skipped — FSRS optimizer tests, expected).

### Fixed — `_reposition_new_cards` defaults (Code Critic remediation)

- **HIGH** `shiftExisting` default changed from `True` → `False`.  The previous
  default silently mass-renumbered every new card in the collection whenever a
  caller omitted the parameter.  `False` matches Anki's `reposition_defaults`
  (shift=False) and AnkiConnect behaviour.
- **LOW** `start` default changed from `0` → `1`.  Position 0 is not a valid
  new-card due slot in Anki; `1` matches AnkiConnect/Anki convention.
- Regression test added:
  `TestRepositionNewCards::test_reposition_no_shift_default_leaves_bystanders_unchanged`
  — verifies that omitting `shiftExisting` does **not** renumber bystander cards.

### Added — P0 card/note triage actions (feat/admin-actions)

Nine new actions for the admin triage UI, implemented in `src/actions.py`
and registered in `ACTIONS`.  All mutations run under `col_mod._col_lock`.
Errors raise `ValueError` → server converts to `{"result":null,"error":"..."}` HTTP 200.

#### Scheduler actions

**`bury`**
- Params: `{"cards": [int]}`
- Result: null
- Calls `col.sched.bury_cards(card_ids, manual=True)` → `BURY_USER` mode (manual bury, distinct from sibling auto-bury).
- Confirmed sig: `bury_cards(self, ids: Sequence[CardId], manual: bool = True) -> OpChangesWithCount`

**`unbury`**
- Params: `{"cards": [int]}`
- Result: null
- Calls `col.sched.unbury_cards(card_ids)`.
- Confirmed sig: `unbury_cards(self, ids: Sequence[CardId]) -> OpChanges`

**`setDueDate`**
- Params: `{"cards": [int], "days": str}`
- Result: null
- `days` is a non-empty string spec: `"1"`, `"3"`, `"1-7"` (random range).  Converts cards to review type.
- Raises `ValueError` if `days` is empty or not a string.
- Confirmed sig: `set_due_date(self, card_ids: Sequence[CardId], days: str, config_key: ... | None = None) -> OpChanges`

**`forgetCards`**
- Params: `{"cards": [int], "restorePosition": bool = False, "resetCounts": bool = False}`
- Result: null
- Resets cards to the new queue (AnkiConnect name for `schedule_cards_as_new`).
- Confirmed sig: `schedule_cards_as_new(self, card_ids: Sequence[CardId], *, restore_position: bool = False, reset_counts: bool = False, context: ... | None = None) -> OpChanges`

**`repositionNewCards`** / **`reposition`** (alias)
- Params: `{"cards": [int], "start": int, "step": int = 1, "randomize": bool = False, "shiftExisting": bool = True}`
- Result: null
- Confirmed sig: `reposition_new_cards(self, card_ids: Sequence[CardId], starting_from: int, step_size: int, randomize: bool, shift_existing: bool) -> OpChangesWithCount`
- Note: all 4 positional kwargs are required (no defaults in the backend).

#### Note actions

**`findAndReplace`**
- Params: `{"notes": [int], "search": str, "replacement": str, "regex": bool = False, "field": str | None = None, "matchCase": bool = False}`
- Result: count (int) of notes modified.
- Confirmed sig: `find_and_replace(self, *, note_ids: Sequence[NoteId], search: str, replacement: str, regex: bool = False, field_name: str | None = None, match_case: bool = False) -> OpChangesWithCount`

**`findDuplicates`**
- Params: `{"field": str, "search": str = ""}`
- Result: `[{"value": str, "notes": [int]}, ...]`
- Confirmed sig: `find_dupes(self, field_name: str, search: str = "") -> list[tuple[str, list]]`
- Implementation note: `find_dupes` calls `strip_html_media` which requires `anki.lang.current_i18n`.
  Our headless server never calls `set_lang()`; the handler lazily sets
  `anki.lang.current_i18n = col._backend` (the open collection's `RustBackend`) on first
  call.  This is safe and idempotent.

#### Tag actions

**`clearUnusedTags`**
- Params: `{}`
- Result: count (int) of tags removed.
- Confirmed sig: `clear_unused_tags(self) -> OpChangesWithCount`

**`getTags`**
- Params: `{}`
- Result: `list[str]`
- Confirmed sig: `all(self) -> list[str]`
- Read-only, no lock.

#### Signature deviations from the prompt spec

| Action | Spec said | Actual anki 25.9.2 | Notes |
|--------|-----------|---------------------|-------|
| `bury` | `bury_cards(card_ids, manual=True)` | `bury_cards(ids, manual=True)` | param named `ids`, not `card_ids`; called positionally |
| `unbury` | `unbury_cards(card_ids)` | `unbury_cards(ids)` | same — positional call unaffected |
| `reposition` | `reposition_new_cards(card_ids, starting_from=, step_size=, randomize=, shift_existing=)` | confirmed exact | all kwargs required (no defaults) |

Confirmed by reading `/home/david/.local/lib/python3.11/site-packages/anki/scheduler/base.py`.

#### Tests — `tests/test_admin_triage_actions.py` (29 tests, all pass, 0.29 s)

- **`TestBuryUnbury`** (4): bury changes queue to negative; unbury restores to ≥0; empty lists are no-ops.
- **`TestSetDueDate`** (4): `"5"` → card becomes type=2/queue=2; `"1-7"` range spec accepted; empty string raises `ValueError`; non-string raises `ValueError`.
- **`TestForgetCards`** (4): review card (type=2) reset to new (type=0/queue=0); `restorePosition=True` and `resetCounts=True` variants; empty list is no-op.
- **`TestRepositionNewCards`** (3): due positions ≥ start value; `"reposition"` alias works; empty list is no-op.
- **`TestFindAndReplace`** (5): plain replace edits field, returns count=1; regex replace; field-scoped replace; zero count when no match; multi-note count=2.
- **`TestFindDuplicates`** (3): finds two notes sharing a field value; empty search returns `[]`; result shape has `value` (str) + `notes` (list[int]).
  Note: `findDuplicates` tests use add-then-rewrite strategy (add note A normally; add note B with distinct front; use `findAndReplace` to make B's front match A's) to bypass `addNote`'s duplicate guard while producing a genuine duplicate for `find_dupes` to detect.
- **`TestClearUnusedTagsAndGetTags`** (4): `getTags` returns list of strings; `clearUnusedTags` removes orphan after note deletion; tag added via `addTags` appears in `getTags`; `clearUnusedTags` with no orphans returns int ≥ 0.
- **`TestActionRegistration`** (2): all 10 new keys present in `ACTIONS`; all handlers are callable.

Total test count: 138 → 167 (164 pass, 3 skipped — FSRS optimizer tests, expected).

### Security — Code Critic hardening: /admin auth boundary (feat/admin-actions)

#### HIGH — cookie `Secure` flag + ProxyFix (`src/admin/routes.py`, `src/server.py`)

- `response.set_cookie(...)` now passes `secure=True` so the `token` cookie is
  only transmitted over HTTPS.  The previous code omitted the flag "because the
  sidecar runs behind an internal Docker network" — but the admin UI is always
  accessed via pfSense TLS termination, so the Secure flag must be set.
- `werkzeug.middleware.proxy_fix.ProxyFix(app.wsgi_app, x_proto=1, x_host=1)`
  added in `server.py` so Flask sees `https://` from `X-Forwarded-Proto` and
  the flag takes effect without further changes in production.

#### MED — IP-keyed login rate-limit (`src/admin/auth.py`, `src/admin/routes.py`)

- New module-level in-memory throttle in `auth.py`:
  - Max 10 failed `POST /admin/login` attempts per IP in any rolling 5-minute
    window (using `time.monotonic()`).
  - On limit exceeded: 429 with `Retry-After` header, no token comparison made.
  - Counter reset to zero on successful login (`_ratelimit_reset`).
  - Thread-safe via a `threading.Lock`.  Appropriate for the single-process
    waitress deployment; no external dependency (no flask-limiter).
- Public API: `_ratelimit_check(ip)`, `_ratelimit_record_failure(ip)`,
  `_ratelimit_reset(ip)` (prefixed `_` — internal to admin package).

#### MED — decouple `secret_key` from `ADMIN_TOKEN` (`src/server.py`)

- `app.secret_key` was previously set to the raw `ADMIN_TOKEN` value, meaning
  leaking the session key was equivalent to leaking the admin token.
- New derivation (in priority order):
  1. `FLASK_SECRET_KEY` env var if set (operator override).
  2. `hashlib.sha256(b"acs-flask-session:" + ADMIN_TOKEN.encode()).hexdigest()`
     — stable across restarts, domain-separated, distinct from the token.
  3. `os.urandom(32)` — only when `ADMIN_TOKEN` is also unset (admin UI
     disabled anyway).
- Comment added: `# INVARIANT: secret_key is derived, not the raw ADMIN_TOKEN`.

#### LOW — `login_post` returns 503 (not 200) when `ADMIN_TOKEN` unset

- The render of `admin/login.html` with the "Admin UI is disabled" error
  previously returned the default HTTP 200.  Now correctly returns 503, making
  the disabled state machine-readable and consistent with `GET /admin/`.

#### LOW — rename `_token_valid` → `token_valid`; clean up stale comment

- `_token_valid` renamed to `token_valid` in `auth.py` (public function — all
  auth paths should go through the same auditable comparison point; a private
  name invited future bypass).  `routes.py` import updated.
- Removed the stale "Static files under the admin blueprint also need no auth"
  comment from `check_admin_auth()` — the blueprint no longer serves static
  files, and the comment was misleading.  Replaced with a clear note that
  exemptions are the caller's responsibility via `_EXEMPT_ENDPOINTS`.

#### Tests — 8 new tests in `tests/test_admin_auth.py`

- **`TestSecureCookieFlags`** (1): `Set-Cookie` on valid login has `Secure`,
  `HttpOnly`, and `SameSite=Strict` flags.
- **`TestLoginPostTokenUnset`** (1): `POST /admin/login` returns 503 (not 200)
  when `ADMIN_TOKEN` is unset.
- **`TestRateLimit`** (3): 429 returned after `_RATELIMIT_MAX_FAILURES` failed
  attempts; `Retry-After` header present; counter resets on direct call to
  `_ratelimit_reset`; counter resets on successful login (subsequent fail = 401
  not 429).
- **`TestSecretKeyDecoupled`** (3): `secret_key != ADMIN_TOKEN`; deterministic
  across two app builds with the same token; different tokens produce different
  keys.

Total: 18 → 26 tests (all pass, 0.33 s).  `ruff check` clean.

### Added — A1: /admin scaffold + token auth gate (feat/admin-actions)

**New files**

- `src/admin/__init__.py` — package; exports `admin_bp`.
- `src/admin/auth.py` — token authentication helpers.
  - `ADMIN_TOKEN` read once from env at startup; `ADMIN_TOKEN_CONFIGURED` bool.
  - If unset, every `/admin*` request returns **HTTP 503** with a clear
    "set ADMIN_TOKEN" message — the UI is disabled, not open.
  - Token accepted via (priority order):
    1. `X-Admin-Token` request header (API / curl).
    2. HTTP Basic Auth password field (any username) — `curl -u :TOKEN ...`.
    3. `token` HttpOnly session cookie (set by POST /admin/login).
  - All comparisons use `hmac.compare_digest` (constant-time).
- `src/admin/routes.py` — Flask `admin_bp` blueprint (url_prefix `/admin`).
  - `GET  /admin/login`   — login form (exempt from auth gate).
  - `POST /admin/login`   — validates token; sets `token` cookie; redirects to `/admin`.
  - `GET  /admin/`        — dashboard: collection health summary + nav placeholders.
  - `GET  /admin/logout`  — clears cookie; redirects to `/admin/login`.
  - `before_request` hook gates all other routes (`admin.login` and
    `admin.login_post` are exempt).
- `templates/admin/base.html` — nav header with placeholder links for future
  panels (Browser, Scheduling, DB & Media, Diagnostics).
- `templates/admin/login.html` — standalone login page (password input, error display).
- `templates/admin/index.html` — dashboard: health grid (status, card count,
  note count, collection path) + panels-pending list for A6–A9.
- `static/admin/admin.css` — clean, dependency-free CSS (no framework).
- `static/admin/admin.js` — placeholder; no-op on load.

**Modified files**

- `src/server.py`:
  - Imports `Path` and `admin_bp`.
  - `Flask(...)` now explicit `template_folder` and `static_folder` pointing
    to `<repo>/templates/` and `<repo>/static/` (resolved from `__file__`).
  - `app.secret_key` derived from `ADMIN_TOKEN` (stable across restarts).
  - `app.register_blueprint(admin_bp)` — registers `/admin/*` routes.
  - **POST / and GET /health are completely unaffected.**

**Tests**

- `tests/test_admin_auth.py` — 18 tests, all pass, 0.25 s (no Anki collection needed).
  - `TestAdminDisabled` (2): 503 with "ADMIN_TOKEN" text; login page still 200.
  - `TestNoCredentials` (2): GET /admin → 302 to login; POST with empty token → 401.
  - `TestXAdminTokenHeader` (2): valid header → 200; wrong → 302/401.
  - `TestBasicAuth` (3): valid → 200; any username accepted; wrong password → 302/401.
  - `TestCookieAuth` (2): valid cookie → 200; wrong cookie → 302/401.
  - `TestLoginFlow` (4): login page 200; valid POST → 302 + cookie; wrong POST → 401 + no cookie; logout → 302 + clears cookie.
  - `TestExistingRoutesUnaffected` (3): /health → 200 with no admin credentials; /health may 503 (collection) never 401; POST / version → 200 with result=6.

**Deployment note (A10)**:
Add `ADMIN_TOKEN` to the sidecar's `docker-compose` env section before deploy.
The token must be a high-entropy secret (32+ chars recommended).

### Fixed — Code Critic remediation (feat/admin-actions)

#### `_find_cards_paginated` — clamp negative offset/limit (HIGH)

- `offset` and `limit` parsed from caller-supplied params were not validated for
  sign.  A negative `offset` produced a nonsensical Python slice (`all_ids[-5:]`)
  that silently returned up to 5 cards from the end of the result list instead of
  the intended page.  A negative `limit` produced an empty slice but via `min(-1,
  500) == -1` rather than the intended zero-card response.
- Fix: `offset = max(0, int(params.get("offset", 0)))` and
  `limit = max(0, min(int(params.get("limit", 100)), 500))`.  Negative values
  are now clamped: negative offset → 0 (start of results), negative limit → 0
  (empty page, `total` still returned correctly).

#### `_rename_deck` — pre-check collision instead of post-rename "+" heuristic (HIGH)

- The previous implementation called `col.decks.rename()` and then detected
  collision by comparing the resulting deck name to `new_name`: if Anki had
  silently appended `"+"` it raised `ValueError`.  This heuristic false-positives
  when the caller legitimately requests a name that ends in `"+"`.
- Fix: before calling rename, `col.decks.id_for_name(new_name)` is checked.
  If it returns a non-`None` id that is not the same deck being renamed, a
  `ValueError("Rename collision: a deck named '<new_name>' already exists.")` is
  raised immediately — no rename attempt is made.  The post-rename `"+"` comparison
  is removed entirely.
- Docstring updated to note: Anki's `decks.rename()` also automatically renames
  all child decks when a parent is renamed (e.g. "A" → "B" also moves "A::Sub" →
  "B::Sub"); callers do not need to enumerate subdecks.

#### `_delete_decks` — loud docstring warning for subdeck + data-loss surface (HIGH)

- `col.decks.remove()` silently deletes not only the named decks but also every
  subdeck and every card (and orphaned note) in the entire subtree.  The previous
  docstring mentioned this only as an incidental note about `cardsToo`.
- Fix: added a prominent `.. warning::` block listing the three data-loss surfaces:
  (1) named decks, (2) ALL subdecks, (3) ALL cards/orphaned notes.  Notes that the
  `cardsToo` param is always ignored.  Instructs callers/UI to present an explicit
  confirmation step listing affected decks and approximate card count before calling.
- Behaviour unchanged.

#### `_delete_decks` / `_delete_notes` — normalize `None` param to `[]` (LOW)

- `params.get("decks", [])` returns `None` (not `[]`) when the key is present but
  explicitly set to `null` in the JSON payload, causing `TypeError: 'NoneType'
  object is not iterable` on the `for d in decks_param` loop.
- Fix: `params.get("decks") or []` (and same for `"notes"` in `_delete_notes`).
  A `null` param is now a safe no-op, consistent with an empty list.

#### `tests/test_admin_actions.py` — fixture-agnostic test suite (HIGH)

Four tests previously hardcoded committed-fixture values and failed when run
against a real backup collection via `ANKI_TEST_BACKUP`:

- **`TestModelFieldNames.test_basic_model_field_names`**: now guards with
  `pytest.skip` if `"Basic"` model is absent; asserts positional fields
  (`fields[0] == "Front"`, `fields[1] == "Back"`) rather than exact length.
- **`TestModelFieldNames.test_latvian_vocab_field_names_in_order`**: the 4-field
  exact-order assertion is now conditional on `len(fields) == 4`; minimum
  `len >= 1` always asserted.
- **`TestModelFieldNames.test_multi_field_model_returns_all_fields`**: guards
  with `pytest.skip` if model absent; asserts `len >= 2` rather than `== 2`.
- **`TestModelFieldNames.test_returns_list_of_strings`**: now uses the first
  model returned by `modelNames` instead of hardcoding `"Basic"`.
- **`TestFindCardsPaginated.test_default_limit_is_100`**: removed the
  hardcoded comment "Fixture has 9 cards"; now asserts `len(cards) <= 100`,
  `total == len(findCards("*"))`, and (if collection size ≤ 100) all cards
  returned in one page.

Two new tests added for the negative-param clamp fix:

- **`test_negative_offset_clamped_to_zero`**: passes `offset=-5`; asserts
  `result["offset"] == 0` and cards match the `offset=0` result.
- **`test_negative_limit_returns_empty_cards`**: passes `limit=-1`; asserts
  `result["cards"] == []` and `total` is still correct.

Results: 27 passed (committed fixture) / 26 passed + 1 skipped (backup
collection, skip on absent `Basic` model — expected and correct).
Both modes green.  `ruff check` passes with no findings.

---

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
