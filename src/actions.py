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
    # Cards
    "findCards": _find_cards,
    "cardsInfo": _cards_info,
    "cardsToNotes": _cards_to_notes,
    "changeDeck": _change_deck,
    # Decks
    "createDeck": _create_deck,
    "deckNames": _deck_names,
    "getDeckStats": _get_deck_stats,
    # Scheduler
    "suspend": _suspend,
    "unsuspend": _unsuspend,
    # Models
    "modelNames": _model_names,
    "createModel": _create_model,
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
}
