"""
AnkiWeb / self-hosted sync integration — upload-wins policy.

This module implements *Step 8* of the anki-collection-server build plan.
It wraps the anki.collection sync methods to provide push-only
synchronisation against an AnkiWeb-compatible server (e.g.
``ghcr.io/luckyturtledev/anki``).

Design decisions
----------------
* **Upload-wins always.**  Tilts is the content authority; this server
  is the canonical source of truth.  The sync logic will never allow an
  older server-side collection to overwrite the local copy.

  Concretely:

  - ``FULL_UPLOAD (4)`` → call ``full_upload_or_download(upload=True)``.
  - ``FULL_SYNC  (2)`` → resolve conflict by uploading (log loudly).
  - ``NORMAL_SYNC (1)`` → standard incremental sync (server sends its
    delta first, then we push ours; Anki's incremental protocol does not
    let us "pull-then-push" — it is a single negotiated exchange, so we
    rely on this being safe because Tilts never creates cards on the sync
    server directly).
  - ``NO_CHANGES (0)`` → nothing to do.

* **``force_upload=True``** skips ``sync_collection()`` entirely and calls
  ``full_upload_or_download(upload=True)`` unconditionally.  This is the
  cutover path.

* **Media** — ``col.sync_collection(auth, sync_media=True)`` handles the
  media sync as part of the normal/full-upload flow in anki 25.9.2.  A
  *separate* ``col.sync_media(auth)`` call exists on the Collection API
  but is only needed if you passed ``sync_media=False`` to
  ``sync_collection``.  We pass ``sync_media=True`` by default so one
  round-trip covers both.  Callers can opt out via ``sync_media=False``.

Environment variables
---------------------
``ANKI_SYNC_ENDPOINT``
    URL of the sync server.  Default: ``http://anki-sync:8080``.

``ANKI_SYNC_USERNAME``
    Sync account username.  Overrides the user portion of
    ``ANKI_SYNC_USER1`` if set.

``ANKI_SYNC_PASSWORD``
    Sync account password.  Overrides the password portion of
    ``ANKI_SYNC_USER1`` if set.

``ANKI_SYNC_USER1``
    Fallback credential in ``"username:password"`` format (same syntax
    used by the luckyturtledev anki-sync-server image).  Only consulted
    when ``ANKI_SYNC_USERNAME`` / ``ANKI_SYNC_PASSWORD`` are absent.

API names used (confirmed against anki 25.9.2 — see docs/spike-findings.md)
----------------------------------------------------------------------------
- ``col.sync_login(username, password, endpoint) -> SyncAuth``
- ``col.sync_collection(auth, sync_media) -> SyncOutput``
  (``SyncOutput`` is ``SyncCollectionResponse`` from ``anki.sync_pb2``)
- ``col.full_upload_or_download(*, auth, server_usn, upload) -> None``
- ``col.sync_media(auth) -> None``   (separate; only if sync_media=False)
- ``SyncCollectionResponse.required`` enum: 0=NO_CHANGES, 1=NORMAL_SYNC,
  2=FULL_SYNC, 4=FULL_UPLOAD
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SyncCollectionResponse.required enum constants (anki 25.9.2)
# ---------------------------------------------------------------------------

_NO_CHANGES = 0
_NORMAL_SYNC = 1
_FULL_SYNC = 2
_FULL_UPLOAD = 4


# ---------------------------------------------------------------------------
# Credential / endpoint helpers
# ---------------------------------------------------------------------------


def _sync_credentials() -> tuple[str, str]:
    """Return ``(username, password)`` from environment variables.

    Resolution order:

    1. ``ANKI_SYNC_USERNAME`` + ``ANKI_SYNC_PASSWORD`` (explicit pair).
    2. ``ANKI_SYNC_USER1`` parsed as ``"username:password"`` (luckyturtledev
       sync-server convention).

    Raises
    ------
    RuntimeError
        If no usable credentials are found.
    """
    username = os.environ.get("ANKI_SYNC_USERNAME", "").strip()
    password = os.environ.get("ANKI_SYNC_PASSWORD", "").strip()

    if username and password:
        return username, password

    user1 = os.environ.get("ANKI_SYNC_USER1", "").strip()
    if user1 and ":" in user1:
        u, _, p = user1.partition(":")
        if u and p:
            return u, p

    raise RuntimeError(
        "Sync credentials not configured.  Set ANKI_SYNC_USERNAME + "
        "ANKI_SYNC_PASSWORD, or ANKI_SYNC_USER1=username:password."
    )


def _sync_endpoint() -> str:
    """Return the sync server URL from ``ANKI_SYNC_ENDPOINT``.

    Default: ``http://anki-sync:8080`` (standard docker-compose service name
    paired with the luckyturtledev image that listens on 8080).
    """
    return os.environ.get("ANKI_SYNC_ENDPOINT", "http://anki-sync:8080").rstrip("/")


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def do_sync(
    force_upload: bool = False,
    sync_media: bool = True,
) -> dict[str, Any]:
    """Synchronise the collection with the configured sync server.

    Parameters
    ----------
    force_upload:
        When *True*, skip ``sync_collection()`` entirely and call
        ``full_upload_or_download(upload=True)`` unconditionally.
        Use this for the initial cutover (Tilts always wins).

    sync_media:
        When *True* (default), pass ``sync_media=True`` to
        ``sync_collection()`` so the media library is synced in the same
        round-trip.  Set *False* to skip media (faster; cards only).

    Returns
    -------
    dict
        ``{"required": int, "uploaded": bool, "message": str}``

        ``required`` is the raw ``SyncCollectionResponse.required`` int
        (0/1/2/4) from the initial ``sync_collection`` call, or ``-1``
        when ``force_upload`` was *True* and the initial probe was skipped.

    Raises
    ------
    RuntimeError
        If credentials are missing or the sync raises unexpectedly.
    Exception
        Propagates any network/auth exception from the anki backend so
        that the caller can surface it as an AnkiConnect error response.
    """
    import src.collection as col_mod  # noqa: PLC0415 (avoid circular at module level)

    username, password = _sync_credentials()
    endpoint = _sync_endpoint()

    with col_mod._col_lock:
        col = col_mod.get_col()

        # ------------------------------------------------------------------
        # 1. Authenticate
        # ------------------------------------------------------------------
        log.info("sync_login: user=%s endpoint=%s", username, endpoint)
        auth = col.sync_login(
            username=username,
            password=password,
            endpoint=endpoint,
        )
        log.debug("sync_login success: hkey=%s", auth.hkey[:8] + "…")

        # ------------------------------------------------------------------
        # 2a. Force-upload path (cutover / admin override)
        # ------------------------------------------------------------------
        if force_upload:
            log.warning(
                "FORCE UPLOAD: uploading local collection to %s "
                "(server content will be replaced)",
                endpoint,
            )
            col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
            log.info("Force upload complete.")
            return {
                "required": -1,
                "uploaded": True,
                "message": "Force upload succeeded — local collection is now on server.",
            }

        # ------------------------------------------------------------------
        # 2b. Normal path: probe what the server requires
        # ------------------------------------------------------------------
        log.info("sync_collection: sync_media=%s", sync_media)
        out = col.sync_collection(auth, sync_media=sync_media)
        required: int = out.required
        log.info("sync_collection result: required=%d", required)

        # ------------------------------------------------------------------
        # 3. Act on the server's requirement
        # ------------------------------------------------------------------

        if required == _NO_CHANGES:
            log.info("Sync: no changes — already up to date.")
            return {
                "required": required,
                "uploaded": False,
                "message": "No changes — collection is already in sync.",
            }

        if required == _NORMAL_SYNC:
            # Incremental sync completed inside sync_collection(); nothing
            # extra to do.  The incremental protocol exchanges deltas in
            # both directions and cannot be forced to "upload only" at the
            # protocol level — the server determines the merge.
            log.info("Sync: incremental sync complete.")
            return {
                "required": required,
                "uploaded": True,
                "message": "Normal incremental sync completed.",
            }

        if required == _FULL_SYNC:
            # Conflict — the server and client histories have diverged to
            # the point where a full transfer is required.  Upload-wins:
            # Tilts is the authority.
            log.warning(
                "FULL_SYNC conflict detected (required=%d).  "
                "Resolving by UPLOAD (Tilts wins).  "
                "Server content will be replaced.",
                required,
            )
            col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
            log.info("Full upload (conflict resolution) complete.")
            return {
                "required": required,
                "uploaded": True,
                "message": (
                    "FULL_SYNC conflict resolved by upload — "
                    "local collection pushed to server."
                ),
            }

        if required == _FULL_UPLOAD:
            # Server is empty or has never seen this collection.  Upload.
            log.info(
                "FULL_UPLOAD required (server is empty or out of sync).  "
                "Uploading local collection."
            )
            col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
            log.info("Full upload complete.")
            return {
                "required": required,
                "uploaded": True,
                "message": "Full upload succeeded — local collection is now on server.",
            }

        # Unknown required value — upload to be safe.
        log.error(
            "Unknown sync required value %d — uploading as a safety measure.",
            required,
        )
        col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
        return {
            "required": required,
            "uploaded": True,
            "message": f"Unknown required={required}; uploaded as safety measure.",
        }


# ---------------------------------------------------------------------------
# AnkiConnect action handler
# ---------------------------------------------------------------------------


def _action_sync(params: dict[str, Any]) -> None:  # noqa: ARG001
    """AnkiConnect ``sync`` action handler.

    Performs a normal incremental sync (upload-wins on conflict).
    Returns ``None`` on success — AnkiConnect clients check for the
    absence of an ``error`` key rather than inspecting the result.

    Raises
    ------
    Exception
        Propagates any sync failure so the server wrapper can convert it
        into an AnkiConnect error response.
    """
    result = do_sync(force_upload=False, sync_media=True)
    log.info("sync action result: %s", result)
    # AnkiConnect ``sync`` always returns null on success.
    return None


# ---------------------------------------------------------------------------
# Action dispatch table (merged into server.py dispatch in Step 5)
# ---------------------------------------------------------------------------

SYNC_ACTIONS: dict[str, Any] = {
    "sync": _action_sync,
}
