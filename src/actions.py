"""
AnkiConnect action handlers.

Each public function in this module corresponds to one AnkiConnect action
(e.g. `deck_names`, `add_note`, `find_cards`) and is registered in the
dispatch table in server.py.

Responsibilities (Step 6):
  - Implement the full set of AnkiConnect v6 actions required by Tilts.
  - Handler code derives from FooSoft/anki-connect (AGPL-3.0) and is adapted
    to use `anki` (pip) rather than the Qt/add-on runtime.
  - All handlers receive `params: dict` and return a plain Python value;
    server.py wraps the result in the {"result": ..., "error": ...} envelope.

TODO (Step 6): implement all required action handlers.
"""
