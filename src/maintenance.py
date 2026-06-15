"""
Shared maintenance utilities for anki-collection-server.

Pre-backup helper
-----------------
``pre_backup(reason)`` creates a point-in-time copy of the live collection file
before any destructive operation.  The collection file is an open SQLite WAL
database, so a naive file-copy can race with a WAL flush.  We use:

    col.db.execute("pragma wal_checkpoint(TRUNCATE)")

to flush and truncate the WAL into the main file first, then copy with
``shutil.copy2``.  This is safe while the collection is held open under
``_col_lock`` (no concurrent writers) and avoids closing the collection handle.

The sqlite3 backup API is an alternative but requires opening a second
connection to the same file, which conflicts with anki's existing lock; the
checkpoint + copy approach is simpler and equally correct here.

NOTE: ``col.close()`` is intentionally NOT called before backup; doing so would
release our fcntl lock, invalidate the collection handle, and require re-opening
— far too heavy for a pre-operation checkpoint.

Confirmed anki 25.9.2 APIs (empirically verified on /tmp copy):
  - col.db.execute("pragma wal_checkpoint(TRUNCATE)") → list[[int, int, int]]
  - col.path → str  (absolute path to collection.anki2)
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import src.collection as col_mod

log = logging.getLogger(__name__)


def pre_backup(reason: str) -> str:
    """Copy the live collection file to a timestamped backup before a destructive op.

    Approach: checkpoint WAL → truncate → shutil.copy2.  The collection stays
    open; no handle is invalidated.

    Parameters
    ----------
    reason:
        Short label embedded in the backup filename (e.g. "fixIntegrity").
        Alphanumeric + hyphens only; characters outside ``[A-Za-z0-9-]`` are
        silently replaced with ``-`` for filesystem safety.

    Returns
    -------
    str
        Absolute path of the backup file that was created.

    Raises
    ------
    ValueError
        If the backup directory cannot be created or the copy fails.

    Notes
    -----
    The caller MUST already hold ``col_mod._col_lock`` (or be inside a
    ``with col_mod._col_lock`` block) before calling this function.  We do not
    re-acquire it here to avoid deadlocking with the outer handler lock.
    """
    import re  # noqa: PLC0415

    # Sanitise reason for filesystem use
    safe_reason = re.sub(r"[^A-Za-z0-9-]", "-", reason)

    col: Any = col_mod.get_col()
    col_path = Path(col.path)

    # Backup directory sits alongside the collection file
    backups_dir = col_path.parent / "backups"
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"pre_backup: cannot create backup dir {backups_dir}: {exc}") from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups_dir / f"admin-{safe_reason}-{stamp}.anki2"

    # Flush WAL so the .anki2 file is consistent before copy.
    # Result is [[wal_log, frames_checkpointed, frames_moved]] — ignore value.
    try:
        col.db.execute("pragma wal_checkpoint(TRUNCATE)")
        log.debug("pre_backup: WAL checkpoint completed for %s", col_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("pre_backup: WAL checkpoint failed (proceeding with copy): %s", exc)

    try:
        shutil.copy2(str(col_path), str(backup_path))
    except OSError as exc:
        raise ValueError(f"pre_backup: copy failed {col_path} → {backup_path}: {exc}") from exc

    log.info("pre_backup: %s → %s (%d bytes)", reason, backup_path, os.path.getsize(str(backup_path)))
    return str(backup_path)
