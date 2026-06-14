"""
AnkiWeb sync integration.

Wraps the anki.sync module to provide push/pull synchronisation with AnkiWeb
(or a self-hosted anki-sync-server).

Responsibilities (Step 8):
  - Read ANKI_SYNC_ENDPOINT, ANKI_SYNC_USER, ANKI_SYNC_PASSWORD from env.
  - Implement sync_collection() which closes the collection, syncs, and
    reopens it (sync must happen with the collection closed).
  - Expose the `sync` and `fullSync` AnkiConnect actions.

TODO (Step 8): implement sync_collection() and supporting helpers.
"""
