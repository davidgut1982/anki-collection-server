# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
