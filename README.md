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

## Version pinning

`anki==25.9.2` (matches Anki Desktop 25.09.2 collection schema). Do not upgrade
without verifying that the collection schema version is compatible — an
unexpected schema migration will prevent the collection from being opened by
Anki Desktop again without a forced upgrade.
