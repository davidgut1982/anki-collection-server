"""
Anki Collection lifecycle manager.

Responsibilities (Step 4):
  - Open and close the anki.Collection instance pointed at by ANKI_COLLECTION_PATH.
  - Hold an fcntl advisory lock so a second server instance cannot open the same
    collection simultaneously (guard against accidental concurrent writers).
  - Expose a module-level `col` handle that server.py and actions.py import.
  - Detect and refuse to open a collection already locked by Anki Desktop.

TODO (Step 4): implement open_collection(), close_collection(), and the
               get_collection() accessor used throughout actions.py.
"""
