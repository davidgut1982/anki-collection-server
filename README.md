# anki-collection-server

A headless, AnkiConnect-compatible HTTP server built on `pip install anki` (the
official Anki Rust backend). No Qt. No Xvfb. No webtop.

## What it is

`anki-collection-server` re-exposes the [AnkiConnect](https://foosoft.net/projects/anki-connect/)
JSON wire protocol on port 8765, backed by the `anki` PyPI package — which
bundles Anki's Rust core and scheduler without requiring a desktop GUI.

Clients that already speak AnkiConnect (e.g. the Tilts platform) work with zero
code changes. The server accepts `POST /` with an `{"action", "params", "version"}`
envelope and returns `{"result", "error"}`.

## Why it exists

The previous approach ran a full Anki Desktop process inside a webtop container
(Qt + Xvfb + VNC). That image is ~2 GB, takes 30–60 s to cold-start, and
occasionally crashes when the virtual display tears. `anki-collection-server`
replaces it with a ~200 MB image that boots in under 5 s.

## License

AGPL-3.0. The action handler code in `src/actions.py` (added in Step 6) derives
from [FooSoft/anki-connect](https://github.com/FooSoft/anki-connect), which is
also AGPL-3.0. We matched the license intentionally so that derivative work
can be distributed under compatible terms.

## Single-writer constraint

**Critical:** only one instance of this server may open a given `.anki2`
collection at a time. Never run the server alongside Anki Desktop (or a second
container) pointed at the same file — SQLite will corrupt the database. The
server enforces this with an `fcntl` advisory lock (implemented in Step 4).

## Quickstart

```bash
# Build
docker build -t anki-collection-server:dev .

# Run (substitute your collection path)
docker run -d \
  -p 8765:8765 \
  -v /path/to/anki/collection:/data/collection:rw \
  -e ANKI_COLLECTION_PATH=/data/collection/collection.anki2 \
  --user 1005:136 \
  anki-collection-server:dev

# Verify — should return {"result":6,"error":null}
curl -s -X POST http://localhost:8765 \
  -H 'Content-Type: application/json' \
  -d '{"action":"version","version":6}'

# Liveness probe
curl -s http://localhost:8765/health
```

See `docker-compose.example.yml` for a full example including an optional
self-hosted sync server.

## Admin console (`/admin`)

An optional web-based admin console is available at `/admin`.

### Enabling the admin UI

Set the `ADMIN_TOKEN` environment variable to a high-entropy secret (32+
characters recommended):

```bash
docker run -d \
  -p 8765:8765 \
  -v /path/to/anki/collection:/data/collection:rw \
  -e ANKI_COLLECTION_PATH=/data/collection/collection.anki2 \
  -e ADMIN_TOKEN=your-secret-token-here \
  --user 1005:136 \
  anki-collection-server:dev
```

If `ADMIN_TOKEN` is **not set**, every request to `/admin/*` returns HTTP 503
with a plain-text message explaining that the admin UI is disabled. The
AnkiConnect API (`POST /`) and health probe (`GET /health`) are **not affected**.

### Authentication

The admin console accepts the token via any of the following (checked in order):

| Method | How to use |
|--------|------------|
| `X-Admin-Token` header | `curl -H "X-Admin-Token: TOKEN" http://host:8765/admin/` |
| HTTP Basic Auth | `curl -u :TOKEN http://host:8765/admin/` (any username) |
| `token` cookie | Set automatically by the login form at `/admin/login` |

All token comparisons use `hmac.compare_digest` (constant-time, safe against
timing attacks).

The login form at `/admin/login` sets an `HttpOnly; SameSite=Strict; Secure`
session cookie after a valid token is submitted, enabling normal browser
navigation without re-entering the token on each page.

### Admin pages

| Route | Description |
|-------|-------------|
| `GET /admin` | Dashboard: collection health summary, links to panels |
| `GET /admin/browse` | Card/Note Browser: search, paginated table, bulk actions, note editor, Find & Replace, Find Duplicates |
| `POST /admin/api/invoke` | Token-gated AnkiConnect proxy used by all admin pages (not for direct browser use) |

#### /admin/browse

Search your collection with the full Anki query syntax (`deck:`, `tag:`,
`is:due/new/suspended/buried`, `prop:ivl`, `added:`, `flag:`, `re:`).
Results appear in a sortable table. Select cards for bulk actions:

- Suspend / unsuspend, bury / unbury
- Set flag (0-7), change deck, set due date
- Forget cards (reset to new), reposition new cards
- Add / remove tags
- Delete notes (double-confirm, permanent)

Click a row to open the **Note Editor** panel: edit field values and tags,
then save without leaving the page.

Use the **Find & Replace** tool to apply regex or plain-text substitutions
across the current search results, or **Find Duplicates** to locate notes
with matching field values.

All mutating actions are confirmed before execution. All calls go through
the token-gated `POST /admin/api/invoke` proxy -- the raw `POST /` endpoint
is never called from the browser admin UI.

**Note:** The `Secure` cookie flag requires HTTPS.  The server trusts
`X-Forwarded-Proto` from the reverse proxy (pfSense / nginx TLS termination)
via Werkzeug's `ProxyFix`, so the flag works correctly behind one hop of TLS
offloading without any additional configuration.

**Rate-limiting:** `POST /admin/login` is rate-limited to 10 failed attempts
per IP per 5 minutes.  Excess attempts receive `429 Too Many Requests` with a
`Retry-After` header.  The counter resets on successful login.

**Optional env vars:**

| Variable | Purpose | Default |
|----------|---------|---------|
| `ADMIN_TOKEN` | Admin UI token (required to enable `/admin`) | — |
| `FLASK_SECRET_KEY` | Override the Flask session signing key | Derived from `ADMIN_TOKEN` via SHA-256 |

`FLASK_SECRET_KEY` is optional.  When unset, the server derives a stable key
from `ADMIN_TOKEN` using a domain-separated SHA-256 hash — the key is never the
raw token value.

## Status

**MVP in progress.** The scaffold and stub server are complete (Step 2 of 13).
Full action dispatch, collection lifecycle, review sessions, and sync are
implemented in subsequent steps.

| Step | Status | Description |
|------|--------|-------------|
| 1 | Done | Repository created |
| 2 | Done | Scaffold + minimal server |
| 3 | Pending | Phase-0 spike (de-risk unknowns) |
| 4 | Pending | `collection.py` lifecycle + lock |
| 5 | Pending | Full action dispatch |
| 6 | Pending | All AnkiConnect action handlers |
| 7 | Pending | `gui*` review session state machine |
| 8 | Pending | Sync + FSRS helpers |
| 9 | Pending | Test suite + parity checkpoint |
| 10 | Pending | GHCR image publish |
| 11 | Pending | Tilts compose wiring |
| 12 | Pending | Cutover checkpoint |
| 13 | Pending | 48 h soak + retire anki-headless |

## Supported AnkiConnect actions

| Category | Actions |
|----------|---------|
| Meta | `version` |
| Notes | `findNotes`, `notesInfo`, `addNote`, `updateNoteFields`, `addTags`, `removeTags` |
| Notes (admin) | `deleteNotes`, `findAndReplace`, `findDuplicates` |
| Cards | `findCards`, `cardsInfo`, `cardsToNotes`, `changeDeck` |
| Cards (admin) | `findCardsPaginated` |
| Decks | `createDeck`, `deckNames`, `getDeckStats` |
| Decks (admin) | `deleteDecks`, `renameDeck` |
| Scheduler | `suspend`, `unsuspend`, `bury`, `unbury`, `setDueDate`, `forgetCards`, `repositionNewCards`, `reposition` |
| Scheduling config | `getDeckConfig`, `getDeckConfigs`, `updateDeckConfig`, `getFsrsParams`, `setDesiredRetention`, `computeOptimalRetention` |
| DB health | `checkDatabase`, `fixIntegrity`, `optimizeCollection` |
| Empty cards | `getEmptyCards`, `removeEmptyCards` |
| Media health | `mediaCheck`, `deleteUnusedMedia`, `mediaDirSize` |
| Models | `modelNames`, `createModel`, `modelTemplates` |
| Models (admin) | `modelFieldNames` |
| Card mutation | `setSpecificValueOfCard` |
| Media | `storeMediaFile`, `retrieveMediaFile`, `deleteMediaFile` |
| Tags | `getTags`, `clearUnusedTags` |
| Stats | `getNumCardsReviewedToday`, `getNumCardsReviewedByDay`, `getReviewsOfCards`, `getCollectionStatsHTML` |
| Diagnostics | `statCardCounts`, `statTrueRetention`, `statIntervalDistribution`, `statEaseDistribution`, `statFutureDue`, `statReviewsByDay`, `statAddedByDay`, `statTimeSpent` |
| Review GUI | `guiDeckReview`, `guiCurrentCard`, `guiStartCardTimer`, `guiShowAnswer`, `guiAnswerCard`, `guiUndo` |
| Sync | `sync` |
| FSRS | `enableFsrs`, `isFsrsEnabled` |

## Version pinning

`anki==25.9.2` (matches Anki Desktop 25.09.2 collection schema). Do not upgrade
without verifying that the collection schema version is compatible — an
unexpected schema migration will prevent the collection from being opened by
Anki Desktop again without a forced upgrade.
