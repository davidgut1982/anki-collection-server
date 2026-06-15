"""
AnkiConnect action handlers — Step 6.

Each entry in ``ACTIONS`` maps an AnkiConnect action name to a handler
``(params: dict) -> result``.  The result is the raw value placed in the
``{"result": ..., "error": null}`` envelope by server.py.  Handlers raise
exceptions on error; server.py catches them and turns them into
``{"result": null, "error": "<message>"}``.

Handler code is ported from FooSoft/anki-connect (AGPL-3.0), adapted to
call the ``anki`` pip package directly (anki 25.9.2) rather than the
Qt/add-on runtime.  See docs/spike-findings.md for confirmed API signatures.

Excluded from this module (implemented in separate steps):
  - All ``gui*`` / review-session actions  (Step 7 — review_session.py)
  - ``sync``                              (Step 8 — sync.py)
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import src.collection as col_mod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col() -> Any:
    """Return the open ``anki.Collection`` instance."""
    return col_mod.get_col()


def _deck_id(name: str, create: bool = True) -> int:
    """Resolve a deck name to its integer id, optionally creating it."""
    col = _col()
    did = col.decks.id(name, create=create)
    if did is None:
        raise ValueError(f"Deck not found: {name!r}")
    return int(did)


def _notetype_by_name(name: str) -> dict:
    """Return notetype dict by name, raising ValueError if absent."""
    col = _col()
    nt = col.models.by_name(name)
    if nt is None:
        raise ValueError(f"Note type not found: {name!r}")
    return nt


def _fields_for_note(note: Any, notetype: dict) -> dict[str, dict]:
    """Return AnkiConnect-style fields dict for a note.

    Format: ``{fieldName: {"value": str, "order": int}}``
    """
    return {
        fld["name"]: {"value": note.fields[idx], "order": idx}
        for idx, fld in enumerate(notetype["flds"])
    }


def _deck_name_for_did(did: int) -> str:
    """Return human-readable deck name for a deck id."""
    col = _col()
    return col.decks.name(did)


def _notetype_name_for_mid(mid: int) -> str:
    """Return notetype name for a model id."""
    col = _col()
    nt = col.models.get(mid)
    return nt["name"] if nt else str(mid)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def _version(params: dict) -> int:
    return 6


# ---------------------------------------------------------------------------
# Notes — CRUD
# ---------------------------------------------------------------------------


def _find_notes(params: dict) -> list[int]:
    query: str = params.get("query", "")
    return list(_col().find_notes(query))


def _profile_name() -> str:
    """Return the Anki profile name derived from the collection file path.

    AnkiConnect includes ``profile`` in ``notesInfo`` responses.  It is the
    name of the directory containing the collection file, which corresponds to
    the Anki profile name (e.g. ``"User 1"``).
    """
    col = _col()
    return Path(col.path).parent.name


def _notes_info(params: dict) -> list[dict]:
    col = _col()
    note_ids: list[int] = params.get("notes", [])
    profile = _profile_name()
    result = []
    for nid in note_ids:
        note = col.get_note(nid)
        notetype = col.models.get(note.mid)
        # card_ids_of_note() returns the card IDs belonging to this note
        card_ids = list(col.card_ids_of_note(note.id))
        result.append(
            {
                "noteId": note.id,
                "profile": profile,
                "modelName": notetype["name"] if notetype else "",
                "tags": note.tags,
                "fields": _fields_for_note(note, notetype) if notetype else {},
                "mod": note.mod,
                "cards": card_ids,
            }
        )
    return result


def _add_note(params: dict) -> int | None:
    """Add a note and return its id.

    Returns ``None`` (not an integer) when the note is a duplicate AND the
    caller has set ``options.duplicateScope`` or ``options.allowDuplicate``.
    By default AnkiConnect raises an error on duplicate; the tilts client
    catches the exception and treats ``None`` as the sentinel.  We raise a
    ValueError here on duplicate so that the server envelope converts it to
    ``{"result": null, "error": "..."}``, which the tilts client handles
    correctly (it inspects ``data.get("result")`` being ``None``).
    """
    col = _col()
    note_data: dict = params.get("note", {})
    deck_name: str = note_data.get("deckName", "Default")
    model_name: str = note_data.get("modelName", "")
    fields: dict[str, str] = note_data.get("fields", {})
    tags: list[str] = note_data.get("tags", [])

    notetype = _notetype_by_name(model_name)
    did = _deck_id(deck_name, create=True)

    note = col.new_note(notetype)

    # Map fields by name
    fld_names = [f["name"] for f in notetype["flds"]]
    for idx, fname in enumerate(fld_names):
        if fname in fields:
            note.fields[idx] = fields[fname]

    note.tags = list(tags)

    # duplicate_or_empty() returns: 0 = ok, 1 = empty, 2 = duplicate.
    # col.add_note() does NOT raise on duplicate in anki 25.9.2 — it silently
    # creates the note.  We guard explicitly so the server envelope returns
    # {"result": null, "error": "..."}, which the tilts client handles via its
    # `result is None` guard.
    if note.duplicate_or_empty() == 2:
        raise ValueError("cannot create note because it is a duplicate")

    with col_mod._col_lock:
        col.add_note(note, did)

    # add_note returns OpChangesWithCount; the note id is assigned to note.id
    return int(note.id)


def _update_note_fields(params: dict) -> None:
    col = _col()
    note_data: dict = params.get("note", {})
    note_id: int = note_data.get("id")
    fields: dict[str, str] = note_data.get("fields", {})

    with col_mod._col_lock:
        note = col.get_note(note_id)
        notetype = col.models.get(note.mid)
        fld_names = [f["name"] for f in notetype["flds"]]
        for idx, fname in enumerate(fld_names):
            if fname in fields:
                note.fields[idx] = fields[fname]
        col.update_note(note)
    return None


def _add_tags(params: dict) -> None:
    col = _col()
    note_ids: list[int] = params.get("notes", [])
    tags: str = params.get("tags", "")
    with col_mod._col_lock:
        col.tags.bulk_add(note_ids, tags)
    return None


def _remove_tags(params: dict) -> None:
    col = _col()
    note_ids: list[int] = params.get("notes", [])
    tags: str = params.get("tags", "")
    with col_mod._col_lock:
        col.tags.bulk_remove(note_ids, tags)
    return None


# ---------------------------------------------------------------------------
# Cards — CRUD
# ---------------------------------------------------------------------------


def _find_cards(params: dict) -> list[int]:
    query: str = params.get("query", "")
    return list(_col().find_cards(query))


def _cards_info(params: dict) -> list[dict]:
    """Return per-card info matching AnkiConnect cardsInfo shape.

    The tilts client reads: cardId, note, interval, factor, lapses, reps,
    type, flags, css, fields, deckName, modelName, queue, ord, due.

    Additional keys for wire-compatibility with real AnkiConnect:
      mod          — card modification timestamp (Unix seconds)
      left         — reps remaining in current learning step (0 for review cards)
      nextReviews  — list of interval label strings parallel to buttons
                     [Again, Hard, Good, Easy] for review cards (type=2),
                     [Again, Good, Easy] for new/learn cards (types 0,1,3)
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    result = []
    for cid in card_ids:
        card = col.get_card(cid)
        note = card.note()
        notetype = col.models.get(note.mid)
        flds = _fields_for_note(note, notetype) if notetype else {}

        # Render question/answer via render_output (avoids deprecated css())
        try:
            ro = card.render_output()
            question = ro.question_text
            answer = ro.answer_text
            css = ro.css
        except Exception:
            question = ""
            answer = ""
            css = ""

        # nextReviews: interval label strings per ease button.
        # For review cards (type=2) there are 4 buttons [Again, Hard, Good, Easy].
        # For new/learn cards there are 3 buttons [Again, Good, Easy].
        # nextIvlStr returns the same label strings as AnkiConnect's nextReviews.
        try:
            card_type = int(card.type)
            if card_type == 2:
                next_reviews = [
                    col.sched.nextIvlStr(card, ease) for ease in (1, 2, 3, 4)
                ]
            else:
                # New/learn: 3 buttons (Again, Good, Easy) — skip Hard
                next_reviews = [col.sched.nextIvlStr(card, ease) for ease in (1, 3, 4)]
        except Exception:
            next_reviews = []

        result.append(
            {
                "cardId": card.id,
                "note": card.nid,
                "deckName": _deck_name_for_did(card.did),
                "modelName": notetype["name"] if notetype else "",
                "fields": flds,
                "fieldOrder": card.ord,
                "question": question,
                "answer": answer,
                "css": css,
                "ord": card.ord,
                "type": int(card.type),
                "queue": int(card.queue),
                "due": card.due,
                "interval": card.ivl,
                "factor": card.factor,
                "reps": card.reps,
                "lapses": card.lapses,
                "left": card.left,
                "mod": card.mod,
                "flags": card.flags,
                "nextReviews": next_reviews,
            }
        )
    return result


def _cards_to_notes(params: dict) -> list[int]:
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    note_ids: list[int] = []
    seen: set[int] = set()
    for cid in card_ids:
        card = col.get_card(cid)
        nid = int(card.nid)
        if nid not in seen:
            seen.add(nid)
            note_ids.append(nid)
    return note_ids


def _change_deck(params: dict) -> None:
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    deck_name: str = params.get("deck", "Default")
    did = _deck_id(deck_name, create=True)
    with col_mod._col_lock:
        col.set_deck(card_ids, did)
    return None


def _create_deck(params: dict) -> int:
    deck_name: str = params.get("deck", "")
    return _deck_id(deck_name, create=True)


def _deck_names(params: dict) -> list[str]:
    col = _col()
    return [nid.name for nid in col.decks.all_names_and_ids()]


def _build_deck_tree_index(node: Any, index: dict | None = None) -> dict:
    """Recursively index DeckTreeNode objects by their full deck name.

    ``deck_due_tree()`` returns a virtual root node (name="") whose children
    are the top-level decks.  Each node exposes:
      - node.name          full deck name (e.g. "Latvian (ChatGPT)")
      - node.deck_id       integer deck id
      - node.new_count     new cards due today (respects daily limit, rolls up subdecks)
      - node.learn_count   learning cards due today (rolls up subdecks)
      - node.review_count  review cards due today (rolls up subdecks)
      - node.children      list of child DeckTreeNode objects

    Returns:
        {deck_name: DeckTreeNode} for every node in the tree (excluding root).
    """
    if index is None:
        index = {}
    # Skip the virtual root (name == "")
    if node.name:
        index[node.name] = node
    for child in node.children:
        _build_deck_tree_index(child, index)
    return index


def _get_deck_stats(params: dict) -> dict[str, dict]:
    """Return deck stats keyed by deck id (as string).

    AnkiConnect shape consumed by tilts client:
      {str(deck_id): {deck_id, name, new_count, learn_count, review_count, total_in_deck}}

    FIX (v25.9.2-3): uses ``col.sched.deck_due_tree()`` — the same source
    Anki's own UI reads — instead of the stale ``newToday/lrnToday/revToday``
    cached fields.  The tree nodes already:
      (a) respect the daily new/review limits for the current scheduler day, and
      (b) roll up counts from all child decks into the parent node.

    This fixes two bugs in the previous implementation:
      - Day-gating bug: ``[day_idx, count]`` zeroed counts on a fresh scheduler
        day because day_idx didn't match ``col.sched.today`` until study began.
      - Parent-only total: ``SELECT count(*) … WHERE did = ?`` counted only the
        parent deck's own cards, not subdecks.  Anki search ``deck:"name"``
        (without a wildcard) includes subdecks, so ``find_cards`` is used instead.
    """
    col = _col()
    deck_names_param: list[str] = params.get("decks", [])

    # Build tree index: name -> DeckTreeNode (counts already rolled up + day-limited)
    tree = col.sched.deck_due_tree()
    tree_index = _build_deck_tree_index(tree)

    result: dict[str, dict] = {}
    for name in deck_names_param:
        node = tree_index.get(name)
        if node is None:
            # Deck doesn't exist; return zeros under a DISTINCT key so that N
            # missing decks produce N entries rather than all collapsing to the
            # single key "0" (did=0 is used for every absent deck, causing
            # later entries to overwrite earlier ones in the result dict).
            result[f"missing:{name}"] = {
                "deck_id": 0,
                "name": name,
                "new_count": 0,
                "learn_count": 0,
                "review_count": 0,
                "total_in_deck": 0,
            }
            continue

        did = int(node.deck_id)

        # total_in_deck: bare deck:"name" search includes subdecks in Anki's
        # search engine (no wildcard needed).  This gives the correct total
        # across the full subtree, matching what Anki shows in the deck browser.
        total = len(col.find_cards(f'deck:"{name}"'))

        result[str(did)] = {
            "deck_id": did,
            "name": name,
            "new_count": int(node.new_count),
            "learn_count": int(node.learn_count),
            "review_count": int(node.review_count),
            "total_in_deck": total,
        }

    return result


# ---------------------------------------------------------------------------
# Scheduler — suspend / unsuspend
# ---------------------------------------------------------------------------


def _suspend(params: dict) -> None:
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    with col_mod._col_lock:
        col.sched.suspend_cards(card_ids)
    return None


def _unsuspend(params: dict) -> None:
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    with col_mod._col_lock:
        col.sched.unsuspend_cards(card_ids)
    return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _model_names(params: dict) -> list[str]:
    # all_names() is deprecated in anki 25.9.2; use all_names_and_ids() instead.
    # Each item is a protobuf NotetypeNameId with .name and .id attributes.
    return [nt.name for nt in _col().models.all_names_and_ids()]


def _create_model(params: dict) -> dict:
    """Create a new note type and return its dict.

    Param shape (same as AnkiConnect createModel):
      {modelName, inOrderFields, css, cardTemplates: [{Name, Front, Back}]}
    """
    col = _col()
    model_name: str = params.get("modelName", "")
    in_order_fields: list[str] = params.get("inOrderFields", [])
    css: str = params.get("css", "")
    card_templates: list[dict] = params.get("cardTemplates", [])

    # Build a new notetype dict that Anki expects
    mm = col.models
    notetype: dict = mm.new(model_name)
    notetype["css"] = css

    for fname in in_order_fields:
        fld = mm.new_field(fname)
        mm.add_field(notetype, fld)

    for tpl in card_templates:
        template = mm.new_template(tpl.get("Name", "Card 1"))
        template["qfmt"] = tpl.get("Front", "")
        template["afmt"] = tpl.get("Back", "")
        mm.add_template(notetype, template)

    with col_mod._col_lock:
        mm.add_dict(notetype)

    # Return the now-persisted notetype (it will have its id assigned)
    persisted = mm.by_name(model_name)
    return persisted if persisted is not None else notetype


def _model_templates(params: dict) -> dict[str, dict[str, str]]:
    """Return the templates for a note type.

    AnkiConnect contract:
      params:  {"modelName": "<name>"}
      result:  {"<template name>": {"Front": "<qfmt>", "Back": "<afmt>"}, ...}
               in template insertion order (preserving card.ord mapping).

    The Tilts ``_card_payload`` helper calls this on every card load to build
    template-aware per-side sound lists.  It selects the right template by
    ``card.ord`` (0-based index into the ordered template list) and reads the
    ``"Front"`` and ``"Back"`` keys to extract field names via regex.

    Raises:
        ValueError: if the model name does not exist (AnkiConnect-style error;
                    the server converts this to ``{"result": null, "error": "..."}``)
    """
    col = _col()
    model_name: str = params.get("modelName", "")
    nt = col.models.by_name(model_name)
    if nt is None:
        raise ValueError(f"model was not found: {model_name}")
    return {t["name"]: {"Front": t["qfmt"], "Back": t["afmt"]} for t in nt["tmpls"]}


def _set_specific_value_of_card(params: dict) -> list:
    """Set specific card fields.

    AnkiConnect signature:
      {card: int, keys: ["flags"], newValues: [int]}

    Returns ``[[True, "successfully updated"]]`` per key (or [[False, msg]] on error).
    """
    col = _col()
    card_id: int = params.get("card", 0)
    keys: list[str] = params.get("keys", [])
    new_values: list[Any] = params.get("newValues", [])

    result = []
    for key, value in zip(keys, new_values):
        if key == "flags":
            try:
                with col_mod._col_lock:
                    card = col.get_card(card_id)
                    card.flags = int(value)
                    col.update_card(card)
                result.append([True, "successfully updated"])
            except Exception as exc:
                result.append([False, str(exc)])
        else:
            result.append([False, f"unsupported key: {key!r}"])

    return result


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def _store_media_file(params: dict) -> str:
    """Write base64-encoded data to the media folder and return the stored filename.

    Returns the ACTUAL filename used by Anki, which may differ from the
    requested ``filename`` when Anki sanitizes it (e.g. forward slashes are
    stripped: ``"a/b.mp3"`` → ``"ab.mp3"``).  Callers must use the returned
    value — not the requested name — to look up the file afterwards.  For
    clean filenames (no path separators or special characters) the returned
    name equals the input name, so existing tilts-client assertions of the
    form ``assert result == filename`` continue to hold.
    """
    col = _col()
    filename: str = params.get("filename", "")
    data_b64: str = params.get("data", "")
    raw: bytes = base64.b64decode(data_b64)
    stored: str = col.media.write_data(filename, raw)
    return stored


def _retrieve_media_file(params: dict) -> str | bool:
    """Return base64-encoded file contents, or ``False`` if file is missing."""
    col = _col()
    filename: str = params.get("filename", "")
    media_path = Path(col.media.dir()) / filename
    if not media_path.exists():
        return False
    raw = media_path.read_bytes()
    return base64.b64encode(raw).decode("ascii")


def _delete_media_file(params: dict) -> None:
    col = _col()
    filename: str = params.get("filename", "")
    col.media.trash_files([filename])
    return None


# ---------------------------------------------------------------------------
# Admin — Notes / Decks (destructive write operations)
# ---------------------------------------------------------------------------


def _delete_notes(params: dict) -> None:
    """Delete notes (and their cards) by note id.

    Input: ``{"notes": [int]}``
    Output: null

    Empty list is tolerated as a no-op.  Uses ``col.remove_notes()`` which
    deletes all cards belonging to each note in the same operation.

    Wrap in lock: this is a write operation.
    """
    col = _col()
    note_ids: list[int] = params.get("notes") or []
    if not note_ids:
        return None
    from anki.notes import NoteId  # noqa: PLC0415

    with col_mod._col_lock:
        col.remove_notes([NoteId(n) for n in note_ids])
    return None


def _delete_decks(params: dict) -> None:
    """Delete decks (and their cards) by id or name.

    Input: ``{"decks": [int|str], "cardsToo": bool}``
    Output: null

    .. warning::
        **DATA LOSS — permanent, unrecoverable.**

        ``col.decks.remove()`` deletes:

        1. Every named deck.
        2. **ALL SUBDECKS** of each named deck (the entire subtree).
        3. **ALL CARDS** (and their notes, if those notes have no remaining
           cards) that live in any of the above decks.

        There is no "keep cards" or "keep subdecks" variant in the
        ``anki`` pip API (Anki 25.9.2).  The ``cardsToo`` parameter is
        accepted for AnkiConnect wire-compatibility but is ignored — cards
        are ALWAYS deleted.

        Callers / UI layers MUST present an explicit confirmation step
        (listing affected decks and an approximate card count) before
        invoking this action.

    - String entries are resolved via ``col.decks.id_for_name()``.
    - Integer 1 (the built-in Default deck) is silently skipped — Anki does
      not allow the Default deck to be removed.
    - Non-existent deck names are silently skipped (idempotent).
    - Wrap in lock: this is a write operation.
    """
    col = _col()
    decks_param: list[int | str] = params.get("decks") or []
    # cardsToo accepted but not used — remove() always deletes cards
    _ = params.get("cardsToo", True)

    from anki.decks import DeckId  # noqa: PLC0415

    deck_ids: list[DeckId] = []
    for d in decks_param:
        if isinstance(d, str):
            did = col.decks.id_for_name(d)
            if did is None:
                # Deck name not found — skip silently (idempotent)
                log.warning("deleteDecks: deck not found by name %r — skipping", d)
                continue
            did_int = int(did)
        else:
            did_int = int(d)

        if did_int == 1:
            # Default deck (id=1) cannot be deleted; Anki forbids it.
            log.warning(
                "deleteDecks: refusing to delete Default deck (id=1) — skipping"
            )
            continue

        deck_ids.append(DeckId(did_int))

    if not deck_ids:
        return None

    with col_mod._col_lock:
        col.decks.remove(deck_ids)
    return None


def _rename_deck(params: dict) -> None:
    """Rename a deck.

    Input: ``{"deck": int|str, "newName": str}``
    Output: null

    - ``deck`` may be an integer id or a string name.
    - Raises ``ValueError`` if the deck is not found.
    - Raises ``ValueError`` before calling rename if ``newName`` is already
      in use by a different deck.  This avoids relying on the brittle
      post-rename ``"+"``-suffix detection: anki 25.9.2's ``decks.rename()``
      silently appends ``"+"`` on collision, but that heuristic false-positives
      when the caller *legitimately* requests a name that ends in ``"+"``.
      A pre-check against ``col.decks.id_for_name(new_name)`` is authoritative
      and races only in the window between the check and the rename (acceptable
      for a single-writer collection).
    - Anki's ``decks.rename()`` automatically moves all child decks when a
      parent is renamed (e.g. renaming "A" to "B" also renames "A::Sub" to
      "B::Sub").  This behaviour is preserved here — callers do not need to
      enumerate subdecks separately.
    - Wrap in lock: this is a write operation.
    """
    col = _col()
    deck_param: int | str = params.get("deck", "")
    new_name: str = params.get("newName", "")

    from anki.decks import DeckId  # noqa: PLC0415

    if isinstance(deck_param, str):
        did = col.decks.id_for_name(deck_param)
        if did is None:
            raise ValueError(f"Deck not found: {deck_param!r}")
        did_int = int(did)
    else:
        did_int = int(deck_param)
        # Verify it exists
        existing = col.decks.get(DeckId(did_int))
        if existing is None:
            raise ValueError(f"Deck not found: id={did_int}")

    # Pre-check: if a deck already exists under new_name and it is NOT the
    # deck being renamed, refuse — this is a collision.
    existing_target_id = col.decks.id_for_name(new_name)
    if existing_target_id is not None and int(existing_target_id) != did_int:
        raise ValueError(
            f"Rename collision: a deck named {new_name!r} already exists. "
            f"Rename or remove it first."
        )

    with col_mod._col_lock:
        col.decks.rename(DeckId(did_int), new_name)

    return None


# ---------------------------------------------------------------------------
# Admin — Models (read-only)
# ---------------------------------------------------------------------------


def _model_field_names(params: dict) -> list[str]:
    """Return ordered field names for a note type.

    Input: ``{"modelName": str}``
    Output: ``list[str]``

    Read-only — no lock needed.

    Raises ``ValueError`` if the model is not found.
    """
    col = _col()
    name: str = params.get("modelName", "")
    nt = col.models.by_name(name)
    if nt is None:
        raise ValueError(f"model not found: {name!r}")
    return [f["name"] for f in nt["flds"]]


# ---------------------------------------------------------------------------
# Admin — Cards (paginated browse, read-only)
# ---------------------------------------------------------------------------


def _find_cards_paginated(params: dict) -> dict:
    """Return a paginated slice of card ids matching a search query.

    Input:  ``{"query": str, "offset": int = 0, "limit": int = 100}``
    Output: ``{"cards": [int], "total": int, "offset": int}``

    - Runs the full ``col.find_cards(query)`` search and slices in Python;
      this is consistent with how AnkiConnect implements pagination (no SQL
      LIMIT/OFFSET, since Anki's search engine does not expose cursor-based
      pagination).
    - ``limit`` is clamped to a maximum of 500 to protect against very large
      response payloads.
    - Read-only — no lock needed.
    """
    col = _col()
    query: str = params.get("query", "")
    offset: int = max(0, int(params.get("offset", 0)))
    limit: int = max(0, min(int(params.get("limit", 100)), 500))

    all_ids = list(col.find_cards(query))
    page = all_ids[offset : offset + limit]
    return {
        "cards": [int(cid) for cid in page],
        "total": len(all_ids),
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def _get_num_cards_reviewed_today(params: dict) -> int:
    """Count revlog entries that fall within today's scheduler day.

    ``col.sched.day_cutoff`` is the Unix timestamp (seconds) at which the
    current Anki scheduler day *ends* (the next rollover, in the future).
    The start of today is therefore ``day_cutoff - 86400``.  revlog ids are
    timestamps in *milliseconds* since epoch.
    """
    col = _col()
    day_start_ms = (col.sched.day_cutoff - 86400) * 1000
    return int(
        col.db.scalar("select count() from revlog where id >= ?", day_start_ms) or 0
    )


def _get_num_cards_reviewed_by_day(params: dict) -> list[list]:
    """Return ``[[date_str, count], ...]`` aggregated from the full revlog.

    Revlog ids are millisecond timestamps; we convert to date strings using
    UTC (matching Anki's convention for historical display).
    """
    col = _col()
    rows = col.db.all("select id from revlog order by id")
    counts: dict[str, int] = {}
    for (ts_ms,) in rows:
        day_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        counts[day_str] = counts.get(day_str, 0) + 1
    return [[d, c] for d, c in sorted(counts.items())]


def _get_reviews_of_cards(params: dict) -> dict[str, list[dict]]:
    """Return review log entries for the given card ids.

    Shape consumed by tilts client:
      {str(card_id): [{"id": int, "ease": int, "type": int, "time": int, ...}]}

    Tilts client specifically reads ``ease`` (1-4), ``time`` (ms), ``type``.
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    if not card_ids:
        return {}

    # Placeholders for the IN clause
    placeholders = ",".join("?" * len(card_ids))
    rows = col.db.all(
        f"select id, cid, usn, ease, ivl, lastIvl, factor, time, type "
        f"from revlog where cid in ({placeholders}) order by id",
        *card_ids,
    )

    result: dict[str, list[dict]] = {str(cid): [] for cid in card_ids}
    for row in rows:
        rid, cid, usn, ease, ivl, last_ivl, factor, time_ms, rtype = row
        key = str(cid)
        if key in result:
            result[key].append(
                {
                    "id": rid,
                    "usn": usn,
                    "ease": ease,
                    "ivl": ivl,
                    "lastIvl": last_ivl,
                    "factor": factor,
                    "time": time_ms,
                    "type": rtype,
                }
            )
    return result


def _get_collection_stats_html(params: dict) -> str:
    """Return a minimal HTML string.

    The tilts client calls this only as a connection ping
    (``get_collection_stats`` — inspects ``available=True``).
    Returning a non-empty HTML string satisfies all callers.
    """
    col = _col()
    try:
        card_count = col.card_count()
        note_count = col.note_count()
        reviewed_today = _get_num_cards_reviewed_today({})
    except Exception:
        card_count = 0
        note_count = 0
        reviewed_today = 0
    return (
        "<html><body>"
        f"<p>Cards: {card_count}</p>"
        f"<p>Notes: {note_count}</p>"
        f"<p>Reviewed today: {reviewed_today}</p>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Card / Note triage — P0 admin actions
# ---------------------------------------------------------------------------


def _bury(params: dict) -> None:
    """Bury cards so they do not appear until tomorrow.

    Input:  ``{"cards": [int]}``
    Output: null

    Uses ``col.sched.bury_cards(ids, manual=True)`` — the ``manual=True``
    flag sets ``BURY_USER`` mode (same as a manual bury from the reviewer),
    distinct from ``BURY_SCHED`` (automatic sibling bury).

    Confirmed signature (anki 25.9.2 scheduler/base.py:163):
        bury_cards(self, ids: Sequence[CardId], manual: bool = True) -> OpChangesWithCount
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    with col_mod._col_lock:
        col.sched.bury_cards(card_ids, manual=True)
    return None


def _unbury(params: dict) -> None:
    """Unbury cards so they are available again today.

    Input:  ``{"cards": [int]}``
    Output: null

    Uses ``col.sched.unbury_cards(ids)``.

    Confirmed signature (anki 25.9.2 scheduler/base.py:143):
        unbury_cards(self, ids: Sequence[CardId]) -> OpChanges
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    with col_mod._col_lock:
        col.sched.unbury_cards(card_ids)
    return None


def _set_due_date(params: dict) -> None:
    """Set the due date for cards using a day-spec string.

    Input:  ``{"cards": [int], "days": str}``
    Output: null

    ``days`` is a non-empty string spec accepted by the Anki backend, e.g.
    ``"1"``, ``"3"``, or ``"1-7"`` (random between 1 and 7 days).  Cards are
    converted to review cards if they were new/learning.

    Confirmed signature (anki 25.9.2 scheduler/base.py:205):
        set_due_date(self, card_ids: Sequence[CardId], days: str,
                     config_key: Config.String.V | None = None) -> OpChanges

    Raises:
        ValueError: if ``days`` is empty or not a string.
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    days: str = params.get("days", "")
    if not isinstance(days, str) or not days.strip():
        raise ValueError(
            "setDueDate: 'days' must be a non-empty string (e.g. '1', '3-7')"
        )
    with col_mod._col_lock:
        col.sched.set_due_date(card_ids, days)
    return None


def _forget_cards(params: dict) -> None:
    """Reset cards to the new queue (forget review history).

    Input:  ``{"cards": [int], "restorePosition": bool = False, "resetCounts": bool = False}``
    Output: null

    Equivalent to AnkiConnect's ``forgetCards`` action.

    Confirmed signature (anki 25.9.2 scheduler/base.py:182):
        schedule_cards_as_new(self, card_ids: Sequence[CardId], *,
                               restore_position: bool = False,
                               reset_counts: bool = False,
                               context: ... | None = None) -> OpChanges
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    restore_position: bool = bool(params.get("restorePosition", False))
    reset_counts: bool = bool(params.get("resetCounts", False))
    with col_mod._col_lock:
        col.sched.schedule_cards_as_new(
            card_ids,
            restore_position=restore_position,
            reset_counts=reset_counts,
        )
    return None


def _reposition_new_cards(params: dict) -> None:
    """Reorder new cards within the new queue.

    Input:  ``{"cards": [int], "start": int, "step": int = 1,
               "randomize": bool = False, "shiftExisting": bool = True}``
    Output: null

    Also registered as ``"repositionNewCards"`` (AnkiConnect name) and
    ``"reposition"`` (short alias).

    Confirmed signature (anki 25.9.2 scheduler/base.py:247):
        reposition_new_cards(self, card_ids: Sequence[CardId],
                              starting_from: int, step_size: int,
                              randomize: bool, shift_existing: bool) -> OpChangesWithCount

    Note: all positional keyword args are required (no defaults in the backend).
    """
    col = _col()
    card_ids: list[int] = params.get("cards", [])
    start: int = int(params.get("start", 1))
    step: int = int(params.get("step", 1))
    randomize: bool = bool(params.get("randomize", False))
    shift_existing: bool = bool(params.get("shiftExisting", False))
    with col_mod._col_lock:
        col.sched.reposition_new_cards(
            card_ids,
            starting_from=start,
            step_size=step,
            randomize=randomize,
            shift_existing=shift_existing,
        )
    return None


def _find_and_replace(params: dict) -> int:
    """Find and replace text across note fields.

    Input:  ``{"notes": [int], "search": str, "replacement": str,
               "regex": bool = False, "field": str | None = None,
               "matchCase": bool = False}``
    Output: count of notes modified (int).

    Confirmed signature (anki 25.9.2 collection.py:714):
        find_and_replace(self, *, note_ids: Sequence[NoteId], search: str,
                          replacement: str, regex: bool = False,
                          field_name: str | None = None,
                          match_case: bool = False) -> OpChangesWithCount

    Returns the ``count`` field from the ``OpChangesWithCount`` result.
    """
    col = _col()
    note_ids: list[int] = params.get("notes", [])
    search: str = params.get("search", "")
    replacement: str = params.get("replacement", "")
    regex: bool = bool(params.get("regex", False))
    field: str | None = params.get("field", None)
    match_case: bool = bool(params.get("matchCase", False))

    from anki.notes import NoteId  # noqa: PLC0415

    with col_mod._col_lock:
        result = col.find_and_replace(
            note_ids=[NoteId(n) for n in note_ids],
            search=search,
            replacement=replacement,
            regex=regex,
            field_name=field if field else None,
            match_case=match_case,
        )
    return int(result.count)


def _find_duplicates(params: dict) -> list[dict]:
    """Find notes with duplicate values in a given field.

    Input:  ``{"field": str, "search": str = ""}``
    Output: ``[{"value": str, "notes": [int]}, ...]``

    Confirmed signature (anki 25.9.2 collection.py:738):
        find_dupes(self, field_name: str, search: str = "") -> list[tuple[str, list]]

    The native return is a list of ``(value_str, [note_ids])`` tuples, where
    only values that appear in 2+ notes are included.  We convert to the
    AnkiConnect-compatible JSON-friendly list of dicts.

    Implementation note — anki.lang.current_i18n:
        ``col.find_dupes()`` calls ``anki.utils.strip_html_media()`` which
        delegates to ``anki.lang.current_i18n.strip_html()``.  Anki desktop
        sets ``current_i18n`` via ``anki.lang.set_lang(lang)`` at startup; our
        headless server never does this.  We initialise it lazily here using
        the open collection's own ``_backend`` (a ``RustBackend`` instance),
        which implements the same ``strip_html`` method.  This is safe to call
        more than once — subsequent calls are no-ops once the reference is set.
    """
    import anki.lang  # noqa: PLC0415

    col = _col()

    # Lazy i18n init: required by find_dupes → strip_html_media → current_i18n
    if anki.lang.current_i18n is None:
        anki.lang.current_i18n = col._backend  # type: ignore[assignment]

    field: str = params.get("field", "")
    search: str = params.get("search", "")
    # Read-only — no lock needed; find_dupes only reads the collection.
    dupes = col.find_dupes(field, search)
    return [{"value": val, "notes": [int(nid) for nid in nids]} for val, nids in dupes]


def _clear_unused_tags(params: dict) -> int:
    """Remove tags that are not used by any note.

    Input:  ``{}``
    Output: count of tags removed (int).

    Confirmed signature (anki 25.9.2 tags.py):
        clear_unused_tags(self) -> OpChangesWithCount
    """
    col = _col()
    with col_mod._col_lock:
        result = col.tags.clear_unused_tags()
    return int(result.count)


def _get_tags(params: dict) -> list[str]:
    """Return all tags currently registered in the collection.

    Input:  ``{}``
    Output: ``list[str]``

    Confirmed signature (anki 25.9.2 tags.py):
        all(self) -> list[str]

    Read-only — no lock needed.
    """
    col = _col()
    return col.tags.all()


# ---------------------------------------------------------------------------
# Scheduling control — deck config read/write + FSRS helpers  (P0 admin A3)
# ---------------------------------------------------------------------------

#
# Confirmed deck-config key structure (anki 25.9.2, empirically inspected):
#
# Top-level:
#   id, name, mod, usn, dyn
#   new: {bury, delays, initialFactor, ints, order, perDay}
#   rev: {bury, ease4, hardFactor, ivlFct, maxIvl, perDay}
#   lapse: {delays, leechAction, leechFails, minInt, mult}
#   desiredRetention (float)   — FSRS target retention
#   fsrsParams5 (list[float])  — FSRS-5 weight vector (first 17 params)
#   fsrsParams6 (list[float])  — FSRS-6 weight vector (21 params, preferred)
#   fsrsWeights (list[float])  — legacy alias; same as fsrsParams6 when set
#   sm2Retention (float)       — SM-2 retention target
#   buryInterdayLearning, interdayLearningMix, maxTaken, newGatherPriority,
#   newMix, newPerDayMinimum, newSortOrder, reviewOrder, autoplay, replayq,
#   timer, weightSearch, ignoreRevlogsBeforeDate, easyDaysPercentages,
#   answerAction, questionAction, secondsToShowAnswer, secondsToShowQuestion,
#   stopTimerOnAnswer, waitForAudio
#
# NOTE: there is NO "desired_retention" (snake_case) key.  The camelCase
# "desiredRetention" is the real key.  Similarly "fsrsParams6" not "fsrs_params_6".


def _resolve_deck_id(deck: int | str) -> int:
    """Resolve a deck name or id to an integer deck id.

    Parameters
    ----------
    deck:
        Either an integer deck id or a string deck name.

    Returns
    -------
    int
        The resolved deck id.

    Raises
    ------
    ValueError
        If the deck name is not found.
    """
    col = _col()
    if isinstance(deck, str):
        did = col.decks.id_for_name(deck)
        if did is None:
            raise ValueError(f"Deck not found: {deck!r}")
        return int(did)
    return int(deck)


def _get_deck_config(params: dict) -> dict:
    """Return the resolved deck configuration preset for a deck.

    Input:  ``{"deck": int|str}``
    Output: human-relevant fields + full raw config under ``"config"``

    Resolved field names (empirically confirmed against anki 25.9.2):
      - new_per_day          ← cfg["new"]["perDay"]
      - rev_per_day          ← cfg["rev"]["perDay"]
      - learning_steps       ← cfg["new"]["delays"]
      - relearn_steps        ← cfg["lapse"]["delays"]
      - graduating_interval  ← cfg["new"]["ints"][1]  (good interval, days)
      - easy_interval        ← cfg["new"]["ints"][0]  (immediate easy exit, days)
      - max_interval         ← cfg["rev"]["maxIvl"]
      - bury_new             ← cfg["new"]["bury"]
      - bury_reviews         ← cfg["rev"]["bury"]
      - leech_fails          ← cfg["lapse"]["leechFails"]
      - leech_action         ← cfg["lapse"]["leechAction"]
      - new_interval_mult    ← cfg["lapse"]["mult"]   (interval mult after lapse)
      - min_interval         ← cfg["lapse"]["minInt"]  (min interval after lapse, days)
      - initial_factor       ← cfg["new"]["initialFactor"]  (SM-2 ease factor * 10)
      - desired_retention    ← cfg["desiredRetention"]
      - fsrs_params          ← cfg["fsrsParams6"] (falls back to fsrsParams5 if empty)

    Also returns the preset ``id`` and ``name``, plus the full raw dict under
    the ``"config"`` key for callers that need less-common fields.

    Read-only — no lock needed.
    """
    col = _col()
    deck_param = params.get("deck", "Default")
    did = _resolve_deck_id(deck_param)

    from anki.decks import DeckId  # noqa: PLC0415

    cfg = col.decks.config_dict_for_deck_id(DeckId(did))

    new = cfg.get("new", {})
    rev = cfg.get("rev", {})
    lapse = cfg.get("lapse", {})

    # ints: [immediate_easy_days, graduating_days, 0_unused] in the fixture
    ints = new.get("ints", [1, 4, 0])
    easy_interval = ints[0] if len(ints) > 0 else 1
    graduating_interval = ints[1] if len(ints) > 1 else 4

    # FSRS params: prefer fsrsParams6 (21 params), fall back to fsrsParams5 (17 params)
    fsrs_params = cfg.get("fsrsParams6") or cfg.get("fsrsParams5") or []

    return {
        "id": cfg.get("id"),
        "name": cfg.get("name"),
        # New card options
        "new_per_day": new.get("perDay"),
        "learning_steps": new.get("delays"),
        "graduating_interval": graduating_interval,
        "easy_interval": easy_interval,
        "initial_factor": new.get("initialFactor"),
        # Review options
        "rev_per_day": rev.get("perDay"),
        "max_interval": rev.get("maxIvl"),
        # Bury flags
        "bury_new": new.get("bury"),
        "bury_reviews": rev.get("bury"),
        # Lapse options
        "relearn_steps": lapse.get("delays"),
        "leech_fails": lapse.get("leechFails"),
        "leech_action": lapse.get("leechAction"),
        "new_interval_mult": lapse.get("mult"),
        "min_interval": lapse.get("minInt"),
        # FSRS
        "desired_retention": cfg.get("desiredRetention"),
        "fsrs_params": fsrs_params,
        # Full raw dict for callers needing extended fields
        "config": dict(cfg),
    }


def _get_deck_configs(params: dict) -> list[dict]:  # noqa: ARG001
    """Return all deck configuration presets (for a preset-selector UI).

    Input:  ``{}``
    Output: list of raw DeckConfigDict dicts

    Each entry has the full key set documented above.  Callers can use this
    to populate a preset selector and then pass a modified dict to
    ``updateDeckConfig``.

    Read-only — no lock needed.
    """
    col = _col()
    return list(col.decks.all_config())


def _update_deck_config(params: dict) -> None:
    """Update (save) a deck configuration preset.

    Input:  ``{"config": <full DeckConfigDict>}``
    Output: null

    The caller must supply the full config dict (including ``"id"`` and all
    fields), typically obtained from ``getDeckConfig`` or ``getDeckConfigs``
    and then modified.  The ``"id"`` field is required — if missing or
    non-integer a ``ValueError`` is raised.  The id must match an existing
    preset; unknown ids are rejected to prevent ghost-preset creation.

    When scheduling sub-dict keys are present they are validated for type
    before the write:

    * ``new.perDay``, ``rev.perDay``, ``rev.maxIvl``, ``lapse.leechFails``,
      ``lapse.leechAction``, ``lapse.minInt`` → int
    * ``lapse.mult``, ``rev.ivlFct``, ``rev.ease4``, ``rev.hardFactor``
      → float (bare int is coerced)
    * ``new.delays``, ``lapse.delays`` → list

    Only keys that are present in the submitted dict are validated; absent
    keys are left untouched (Anki merges sparse updates).

    Wrap in lock: this is a write operation.

    Raises
    ------
    ValueError
        If the ``"id"`` field is absent, non-integer, or does not match any
        existing preset; or if a scheduling field has the wrong type.
    """
    cfg: dict = params.get("config", {})

    # --- id presence and type ------------------------------------------------
    if "id" not in cfg:
        raise ValueError(
            "updateDeckConfig: config dict must contain an 'id' field. "
            "Obtain the config via getDeckConfig or getDeckConfigs, modify it, "
            "and pass the full dict back."
        )
    if not isinstance(cfg["id"], int):
        raise ValueError(
            f"updateDeckConfig: 'id' must be an int, got {type(cfg['id']).__name__!r}."
        )

    col = _col()

    # --- reject unknown preset id (prevents ghost-preset creation) -----------
    if not any(c["id"] == cfg["id"] for c in col.decks.all_config()):
        raise ValueError(
            f"updateDeckConfig: no preset with id={cfg['id']} — "
            "use getDeckConfigs for valid ids."
        )

    # --- scheduling field type validation ------------------------------------
    _INT_FIELDS: list[tuple[str, str]] = [
        ("new", "perDay"),
        ("rev", "perDay"),
        ("rev", "maxIvl"),
        ("lapse", "leechFails"),
        ("lapse", "leechAction"),
        ("lapse", "minInt"),
    ]
    _FLOAT_FIELDS: list[tuple[str, str]] = [
        ("lapse", "mult"),
        ("rev", "ivlFct"),
        ("rev", "ease4"),
        ("rev", "hardFactor"),
    ]
    _LIST_FIELDS: list[tuple[str, str]] = [
        ("new", "delays"),
        ("lapse", "delays"),
    ]

    for sub, key in _INT_FIELDS:
        sub_dict = cfg.get(sub)
        if isinstance(sub_dict, dict) and key in sub_dict:
            if not isinstance(sub_dict[key], int):
                raise ValueError(
                    f"updateDeckConfig: {sub}.{key} must be int, "
                    f"got {type(sub_dict[key]).__name__!r}."
                )

    for sub, key in _FLOAT_FIELDS:
        sub_dict = cfg.get(sub)
        if isinstance(sub_dict, dict) and key in sub_dict:
            val = sub_dict[key]
            if isinstance(val, int):
                # Coerce bare int to float; mutate in the cfg copy the caller
                # already holds so Anki sees a float.
                cfg[sub] = dict(cfg[sub])
                cfg[sub][key] = float(val)
            elif not isinstance(val, float):
                raise ValueError(
                    f"updateDeckConfig: {sub}.{key} must be float, "
                    f"got {type(val).__name__!r}."
                )

    for sub, key in _LIST_FIELDS:
        sub_dict = cfg.get(sub)
        if isinstance(sub_dict, dict) and key in sub_dict:
            if not isinstance(sub_dict[key], list):
                raise ValueError(
                    f"updateDeckConfig: {sub}.{key} must be list, "
                    f"got {type(sub_dict[key]).__name__!r}."
                )

    with col_mod._col_lock:
        col.decks.update_config(cfg, preserve_usn=False)
    return None


def _get_fsrs_params(params: dict) -> dict:
    """Return the FSRS parameters and desired retention for a deck.

    Input:  ``{"deck": int|str}``
    Output: ``{"params": list[float], "desiredRetention": float}``

    ``params`` is the FSRS weight vector (21 floats for FSRS-6, or 17 for
    FSRS-5 if FSRS-6 weights are absent).  An empty list means no weights
    have been set yet (Anki will use built-in defaults).

    ``desiredRetention`` is the target retention probability (0.0–1.0).

    Read-only — no lock needed.
    """
    col = _col()
    deck_param = params.get("deck", "Default")
    did = _resolve_deck_id(deck_param)

    from anki.decks import DeckId  # noqa: PLC0415

    cfg = col.decks.config_dict_for_deck_id(DeckId(did))

    # Prefer FSRS-6 (21 params); fall back to FSRS-5 (17 params)
    fsrs_params = cfg.get("fsrsParams6") or cfg.get("fsrsParams5") or []
    desired_retention: float = float(cfg.get("desiredRetention", 0.9))

    return {
        "params": list(fsrs_params),
        "desiredRetention": desired_retention,
    }


# Allowed retention range — same bounds enforced by Anki's desktop UI
_RETENTION_MIN = 0.70
_RETENTION_MAX = 0.97


def _set_desired_retention(params: dict) -> None:
    """Set the desired retention for a deck's configuration preset.

    Input:  ``{"deck": int|str, "retention": float}``
    Output: null

    ``retention`` must be in [0.70, 0.97].  Values outside this range raise
    ``ValueError`` — Anki's own UI enforces the same bounds.

    Wrap in lock: this is a write operation.

    Raises
    ------
    ValueError
        If ``retention`` is outside [0.70, 0.97].
    """
    col = _col()
    deck_param = params.get("deck", "Default")
    retention: float = float(params.get("retention", 0.9))

    if retention < _RETENTION_MIN or retention > _RETENTION_MAX:
        raise ValueError(
            f"setDesiredRetention: retention {retention!r} is out of range "
            f"[{_RETENTION_MIN}, {_RETENTION_MAX}]. "
            "Anki's scheduler requires this bound."
        )

    did = _resolve_deck_id(deck_param)

    from anki.decks import DeckId  # noqa: PLC0415

    cfg = col.decks.config_dict_for_deck_id(DeckId(did))
    cfg["desiredRetention"] = retention

    with col_mod._col_lock:
        col.decks.update_config(cfg, preserve_usn=False)

    return None


def _compute_optimal_retention(params: dict) -> dict:
    """Compute the optimal retention for a deck using the FSRS simulator.

    Input:  ``{"deck": int|str, optional simulation params}``
    Output: ``{"optimalRetention": float}`` on success,
            ``{"error": str, "note": str}`` on failure.

    Optional simulation parameters (all have reasonable defaults):
      - ``daysToSimulate`` (int, default 365)  — simulation horizon in days
      - ``deckSize``       (int, default 0)    — 0 = infer from collection
      - ``newLimit``       (int, default 0)    — 0 = use deck preset
      - ``reviewLimit``    (int, default 0)    — 0 = use deck preset
      - ``maxInterval``    (int, default 0)    — 0 = use deck preset
      - ``search``         (str, default "")   — card filter (empty = whole deck)

    Uses ``col._backend.compute_optimal_retention(SimulateFsrsReviewRequest)``
    which is an internal backend API (not part of the public Collection API).
    If the import or backend call fails, returns ``{"error": ..., "note": ...}``
    instead of raising — this prevents a 500-style error when the internal API
    changes between anki versions.

    The FSRS params stored in the deck's config preset are used automatically;
    the backend reads them from the collection config.

    Confirmed signature (anki 25.9.2 backend):
        compute_optimal_retention(SimulateFsrsReviewRequest) -> float
    SimulateFsrsReviewRequest fields (anki.scheduler_pb2):
        params, desired_retention, deck_size, days_to_simulate,
        new_limit, review_limit, max_interval, search,
        new_cards_ignore_review_limit, easy_days_percentages,
        review_order, suspend_after_lapse_count, historical_retention,
        learning_step_count, relearning_step_count
    """
    try:
        from anki.scheduler_pb2 import SimulateFsrsReviewRequest  # noqa: PLC0415
    except ImportError as exc:
        return {
            "error": f"SimulateFsrsReviewRequest not available: {exc}",
            "note": "internal API — may break on anki upgrade",
        }

    col = _col()
    deck_param = params.get("deck", "Default")
    # _resolve_deck_id raises ValueError for unknown deck names/ids.  That
    # exception is intentionally *not* caught here — it propagates out of this
    # function and the server envelope converts it to a clean error response.
    # Do not broaden the try/except below to swallow it.
    did = _resolve_deck_id(deck_param)

    from anki.decks import DeckId  # noqa: PLC0415

    cfg = col.decks.config_dict_for_deck_id(DeckId(did))
    fsrs_params = cfg.get("fsrsParams6") or cfg.get("fsrsParams5") or []
    desired_retention = float(cfg.get("desiredRetention", 0.9))

    days_to_simulate: int = int(params.get("daysToSimulate", 365))
    deck_size: int = int(params.get("deckSize", 0))
    new_limit: int = int(params.get("newLimit", 0))
    review_limit: int = int(params.get("reviewLimit", 0))
    max_interval: int = int(params.get("maxInterval", 0))
    search: str = str(params.get("search", ""))

    request = SimulateFsrsReviewRequest(
        params=list(fsrs_params),
        desired_retention=desired_retention,
        deck_size=deck_size,
        days_to_simulate=days_to_simulate,
        new_limit=new_limit,
        review_limit=review_limit,
        max_interval=max_interval,
        search=search,
    )

    try:
        result: float = col._backend.compute_optimal_retention(request)
        return {"optimalRetention": float(result)}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "computeOptimalRetention backend call failed: %s — "
            "collection may have too few review items or FSRS is not configured",
            exc,
        )
        return {
            "error": str(exc),
            "note": (
                "compute_optimal_retention is an internal backend API. "
                "It requires FSRS to be enabled with a trained weight vector "
                "and a minimum number of review items in the collection. "
                "The API may also break on anki version upgrades."
            ),
        }


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

ACTIONS: dict[str, Any] = {
    # Meta
    "version": _version,
    # Notes
    "findNotes": _find_notes,
    "notesInfo": _notes_info,
    "addNote": _add_note,
    "updateNoteFields": _update_note_fields,
    "addTags": _add_tags,
    "removeTags": _remove_tags,
    # Admin — Notes (destructive)
    "deleteNotes": _delete_notes,
    # Cards
    "findCards": _find_cards,
    "cardsInfo": _cards_info,
    "cardsToNotes": _cards_to_notes,
    "changeDeck": _change_deck,
    # Admin — Cards (paginated browse)
    "findCardsPaginated": _find_cards_paginated,
    # Decks
    "createDeck": _create_deck,
    "deckNames": _deck_names,
    "getDeckStats": _get_deck_stats,
    # Admin — Decks (destructive + rename)
    "deleteDecks": _delete_decks,
    "renameDeck": _rename_deck,
    # Scheduler — suspend / unsuspend
    "suspend": _suspend,
    "unsuspend": _unsuspend,
    # Scheduler — triage (bury / unbury / set due / forget / reposition)
    "bury": _bury,
    "unbury": _unbury,
    "setDueDate": _set_due_date,
    "forgetCards": _forget_cards,
    "repositionNewCards": _reposition_new_cards,
    "reposition": _reposition_new_cards,  # short alias
    # Notes — find & replace / duplicates
    "findAndReplace": _find_and_replace,
    "findDuplicates": _find_duplicates,
    # Tags — bulk cleanup + listing
    "clearUnusedTags": _clear_unused_tags,
    "getTags": _get_tags,
    # Models
    "modelNames": _model_names,
    "createModel": _create_model,
    "modelTemplates": _model_templates,
    # Admin — Models (read-only)
    "modelFieldNames": _model_field_names,
    # Card field mutation
    "setSpecificValueOfCard": _set_specific_value_of_card,
    # Media
    "storeMediaFile": _store_media_file,
    "retrieveMediaFile": _retrieve_media_file,
    "deleteMediaFile": _delete_media_file,
    # Stats
    "getNumCardsReviewedToday": _get_num_cards_reviewed_today,
    "getNumCardsReviewedByDay": _get_num_cards_reviewed_by_day,
    "getReviewsOfCards": _get_reviews_of_cards,
    "getCollectionStatsHTML": _get_collection_stats_html,
    # Scheduling control — deck config + FSRS (P0 admin A3)
    "getDeckConfig": _get_deck_config,
    "getDeckConfigs": _get_deck_configs,
    "updateDeckConfig": _update_deck_config,
    "getFsrsParams": _get_fsrs_params,
    "setDesiredRetention": _set_desired_retention,
    "computeOptimalRetention": _compute_optimal_retention,
}
