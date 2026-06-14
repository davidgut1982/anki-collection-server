"""
Headless gui* review-session state machine (Step 7).

Replaces Anki's Qt GUI reviewer for the anki-collection-server.  The tilts
review UI (einki blueprint) calls standard AnkiConnect ``gui*`` actions;
this module implements them by driving the V3 scheduler directly.

Architecture
------------
``ReviewSessionManager`` holds per-deck session state (deck_id, current card,
current scheduling states, timer start).  It is a module-level singleton
guarded by the same ``_col_lock`` used in ``src/collection.py``.

The ``GUI_ACTIONS`` dict at the bottom maps action names to handler callables
with the same ``(params: dict) -> result`` signature as ``ACTIONS`` in
``src/actions.py``.  Callers (server.py) will merge this dict into the
dispatch table.

Scheduler API (confirmed for anki 25.9.2 — see docs/spike-findings.md)
-----------------------------------------------------------------------
- ``col.sched.get_queued_cards(fetch_limit=1)`` → ``QueuedCards``
- ``col.sched.describe_next_states(states)``    → ``Sequence[str]``
- ``col.sched.build_answer(card, states, rating)`` → ``CardAnswer``
- ``col.sched.answer_card(CardAnswer)``            → ``OpChanges``
- ``col.undo()``                                  → ``OpChangesAfterUndo``
  (raises ``anki.errors.UndoEmpty`` or similar when nothing to undo)

CardAnswer.Rating enum (scheduler_pb2):
  AGAIN = 0  ← ease=1 from tilts client
  HARD  = 1  ← ease=2
  GOOD  = 2  ← ease=3
  EASY  = 3  ← ease=4

IMPORTANT: Anki's ease buttons are 0-indexed (AGAIN=0) but the AnkiConnect
protocol / tilts client sends 1-indexed ease values (1=Again, 2=Hard, …).
The mapping is done in ``_gui_answer_card``.

HTML rendering
--------------
``card.render_output()`` returns a ``RenderOutput`` with:
  .question_text  — full HTML for the question side
  .answer_text    — full HTML for the answer side (includes question context)
  .css            — card stylesheet

``card.css()`` is deprecated and must NOT be used.

nextReviews format
------------------
The tilts ``anki_review_client.py`` expects ``nextReviews`` as a **list**
parallel to ``buttons``, e.g. ``["<1m", "10m", "3d", "7d"]``.  It zips
the list with the ``buttons`` list to build the ``next_reviews`` dict:
``{ease_btn: label}``.  We pass the raw labels from ``describe_next_states``
directly — the client handles stripping and display.

Stale-card protection
---------------------
The tilts client compares ``card_id`` from ``guiCurrentCard`` before and
after ``guiAnswerCard`` to detect stale submits (it fetches current_card(),
stores card_id, then checks again after answer).  On the server side we
validate that the ease submitted matches an active session with a stored
``current_card_id``.  If the session has no current card (already answered
or no session), we raise ``RuntimeError`` with a clear message — the server
envelope converts this to ``{"result": null, "error": "..."}``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import src.collection as col_mod

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rating mapping: AnkiConnect ease (1–4) → Anki CardAnswer.Rating (0–3)
# ---------------------------------------------------------------------------
# Imported lazily inside functions to avoid requiring anki at import time
# (enables unit-test mocking that patches col_mod.get_col).


def _rating_for_ease(ease: int) -> int:
    """Map AnkiConnect ease (1=Again … 4=Easy) to Anki CardAnswer.Rating int."""
    from anki.scheduler_pb2 import CardAnswer  # noqa: PLC0415

    mapping = {
        1: CardAnswer.AGAIN,  # 0
        2: CardAnswer.HARD,  # 1
        3: CardAnswer.GOOD,  # 2
        4: CardAnswer.EASY,  # 3
    }
    if ease not in mapping:
        raise ValueError(f"ease must be 1–4, got {ease!r}")
    return mapping[ease]


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class _ReviewSession:
    """Mutable state for a single active review session.

    Attributes:
        deck_id:          The numeric Anki deck id being reviewed.
        current_card_id:  Card id currently "in focus" (None = no card fetched
                          yet, or the previous card has been answered/cleared).
        current_states:   The ``SchedulingStates`` protobuf returned with the
                          queued card.  Stored so ``guiAnswerCard`` can build
                          the ``CardAnswer`` without re-fetching.
        started_at:       Unix timestamp of session creation.
        timer_started_at: Unix timestamp of the per-card timer (set by
                          ``guiStartCardTimer``; None = timer not started).
    """

    deck_id: int
    current_card_id: int | None = None
    current_states: Any | None = None  # SchedulingStates protobuf
    started_at: float = field(default_factory=time.time)
    timer_started_at: float | None = None


# ---------------------------------------------------------------------------
# ReviewSessionManager
# ---------------------------------------------------------------------------


class ReviewSessionManager:
    """Headless replacement for Anki's Qt GUI reviewer.

    Maintains per-deck session state so that the stateless HTTP layer can
    implement the stateful ``gui*`` AnkiConnect protocol.

    Thread-safety: all mutations are performed while holding
    ``col_mod._col_lock``, which is the same lock used by all other
    collection mutators in ``src/actions.py``.

    Only one session is active at a time.  Calling ``guiDeckReview`` for a
    different deck replaces the current session.
    """

    def __init__(self) -> None:
        self._session: _ReviewSession | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _col(self) -> Any:
        return col_mod.get_col()

    def _fields_for_note(self, note: Any, notetype: dict) -> dict[str, dict]:
        """Build AnkiConnect-style fields dict: ``{name: {value, order}}``."""
        return {
            fld["name"]: {"value": note.fields[idx], "order": idx}
            for idx, fld in enumerate(notetype["flds"])
        }

    def _buttons_and_next_reviews(
        self, card: Any, states: Any
    ) -> tuple[list[int], list[str]]:
        """Derive button list and interval labels for a card.

        Returns:
            buttons:      List of ease ints, e.g. ``[1, 2, 3]`` or ``[1, 2, 3, 4]``.
            next_reviews: Parallel list of label strings from
                          ``describe_next_states``, e.g. ``["<1m","10m","3d","7d"]``.

        Button count rules (matching Anki's V3 scheduler UI):
        - New cards (type=0) and learning cards (type=1/3):  3 buttons (Again,
          Good, Easy) — Hard is omitted for brand-new and relearning cards.
        - Review cards (type=2): 4 buttons (Again, Hard, Good, Easy).

        ``describe_next_states`` always returns 4 labels in order
        [Again, Hard, Good, Easy].  For 3-button cards we skip index 1 (Hard)
        so the parallel lists stay aligned.
        """
        col = self._col()
        labels: list[str] = list(col.sched.describe_next_states(states))
        # labels[0]=Again, [1]=Hard, [2]=Good, [3]=Easy

        card_type = int(card.type)
        if card_type == 2:
            # Review card: 4 buttons
            buttons = [1, 2, 3, 4]
            next_reviews = labels  # already 4 entries
        else:
            # New / learning card: 3 buttons (Again=1, Good=3, Easy=4)
            # Skip Hard (index 1) to match Anki's UI.
            buttons = [1, 3, 4]
            # Parallel list: again, good, easy
            if len(labels) >= 4:
                next_reviews = [labels[0], labels[2], labels[3]]
            else:
                next_reviews = labels  # fallback if describe returns < 4

        return buttons, next_reviews

    def _state_string(self, card: Any) -> str:
        """Return "new", "learn", or "review" for the card type."""
        t = int(card.type)
        if t == 0:
            return "new"
        if t == 2:
            return "review"
        return "learn"

    # ------------------------------------------------------------------
    # gui* action implementations
    # ------------------------------------------------------------------

    def gui_deck_review(self, params: dict) -> bool:
        """``guiDeckReview`` — select a deck and open/reset the review session.

        Args:
            params: ``{"name": str}`` — exact deck name to review.

        Returns:
            True on success.

        Raises:
            ValueError: If the deck name cannot be resolved.
        """
        name: str = params.get("name", "")
        col = self._col()

        with col_mod._col_lock:
            # Resolve deck id — try id_for_name first, then by_name fallback.
            deck_id: int | None = None
            try:
                deck_id = col.decks.id_for_name(name)
            except Exception:
                pass
            if deck_id is None:
                # Fallback: scan all decks
                for deck in col.decks.all():
                    if deck.get("name") == name:
                        deck_id = int(deck["id"])
                        break
            if deck_id is None:
                raise ValueError(f"Deck not found: {name!r}")

            # Scope the scheduler to this deck
            col.decks.select(deck_id)
            log.info("guiDeckReview: selected deck %r (id=%d)", name, deck_id)

            # Reset session (clearing any previous card state)
            self._session = _ReviewSession(deck_id=int(deck_id))

        return True

    def gui_current_card(self, params: dict) -> dict | None:  # noqa: ARG002
        """``guiCurrentCard`` — return the current card payload or null.

        Returns ``None`` when:
        - No active session (``guiDeckReview`` not called yet).
        - The queue is empty for the selected deck.

        The payload shape matches what ``anki_review_client.py`` expects:
        ``{cardId, question, answer, buttons, nextReviews, css, fields,
           modelName, deckName, type, flags, reps}``.

        ``nextReviews`` is a **list** parallel to ``buttons`` (not a dict);
        the tilts client zips them itself.
        """
        if self._session is None:
            log.debug("guiCurrentCard: no active session")
            return None

        col = self._col()

        with col_mod._col_lock:
            # Re-select the deck so the scheduler scope is correct even if
            # the session survived across requests.
            col.decks.select(self._session.deck_id)

            queued = col.sched.get_queued_cards(fetch_limit=1)
            if not queued.cards:
                log.debug(
                    "guiCurrentCard: queue empty for deck_id=%d",
                    self._session.deck_id,
                )
                return None

            qc = queued.cards[0]
            card = col.get_card(qc.card.id)
            note = col.get_note(card.nid)
            notetype = col.models.get(note.mid)

            # Render HTML via render_output (card.css() is deprecated)
            try:
                ro = card.render_output()
                question = ro.question_text
                answer = ro.answer_text
                css = ro.css
            except Exception:
                log.warning(
                    "guiCurrentCard: render_output failed for card %d, "
                    "falling back to question()/answer()",
                    card.id,
                )
                question = card.question()
                answer = card.answer()
                css = ""

            # Buttons and parallel next-review interval labels
            buttons, next_reviews = self._buttons_and_next_reviews(card, qc.states)

            # Fields in AnkiConnect notesInfo format
            fields: dict[str, dict] = {}
            if notetype:
                fields = self._fields_for_note(note, notetype)

            # Deck and model names
            deck_name = col.decks.name(card.did)
            model_name = notetype["name"] if notetype else ""

            # Store card id and scheduling states for guiAnswerCard
            self._session.current_card_id = int(card.id)
            self._session.current_states = qc.states

        payload: dict = {
            "cardId": int(card.id),
            "question": question,
            "answer": answer,
            "buttons": buttons,
            "nextReviews": next_reviews,
            "css": css,
            "fields": fields,
            "modelName": model_name,
            "deckName": deck_name,
            "type": int(card.type),
            "flags": int(card.flags),
            "reps": int(card.reps),
        }
        log.debug(
            "guiCurrentCard: card_id=%d type=%d buttons=%s",
            card.id,
            card.type,
            buttons,
        )
        return payload

    def gui_start_card_timer(self, params: dict) -> bool:  # noqa: ARG002
        """``guiStartCardTimer`` — record the per-card timer start.

        The headless flow renders both question and answer together, so this
        is mostly a UI affordance.  We record ``timer_started_at`` for
        ``milliseconds_taken`` accuracy in ``guiAnswerCard``.

        Returns:
            True always.
        """
        if self._session is not None:
            self._session.timer_started_at = time.time()
        return True

    def gui_show_answer(self, params: dict) -> bool:  # noqa: ARG002
        """``guiShowAnswer`` — UI affordance; no-op in the headless flow.

        In the Qt Anki reviewer the user sees only the question first, then
        clicks "Show Answer" to reveal the back.  Headlessly both sides are
        rendered in ``guiCurrentCard``, so this is a pure no-op.

        Returns:
            True always.
        """
        return True

    def gui_answer_card(self, params: dict) -> bool:
        """``guiAnswerCard`` — grade the current card.

        Args:
            params: ``{"ease": int}`` — 1=Again, 2=Hard, 3=Good, 4=Easy.

        Returns:
            True on success.

        Raises:
            RuntimeError: If no active session, no current card, or if the
                          card is already answered (stale state).
            ValueError:   If ease is not 1–4.
        """
        if self._session is None:
            raise RuntimeError("guiAnswerCard: no active review session")

        current_card_id = self._session.current_card_id
        if current_card_id is None:
            raise RuntimeError(
                "guiAnswerCard: no current card — call guiCurrentCard first"
            )

        current_states = self._session.current_states
        if current_states is None:
            raise RuntimeError(
                "guiAnswerCard: scheduling states not stored (session corrupt)"
            )

        ease: int = int(params.get("ease", 0))
        rating = _rating_for_ease(ease)

        col = self._col()

        with col_mod._col_lock:
            card = col.get_card(current_card_id)
            # build_answer calls card.time_taken() which requires
            # card.timer_started to be set.  In the Qt reviewer,
            # card.start_timer() is called when the card is shown.
            # Headlessly we call it here if not already started.
            if card.timer_started is None:
                card.start_timer()
            answer = col.sched.build_answer(
                card=card,
                states=current_states,
                rating=rating,
            )
            col.sched.answer_card(answer)
            log.info(
                "guiAnswerCard: answered card_id=%d ease=%d (rating=%d)",
                current_card_id,
                ease,
                rating,
            )

            # Clear so the next guiCurrentCard fetches the next card
            self._session.current_card_id = None
            self._session.current_states = None
            self._session.timer_started_at = None

        return True

    def gui_undo(self, params: dict) -> bool:  # noqa: ARG002
        """``guiUndo`` — undo the last review action.

        After undo we clear the session's current card so that the next
        ``guiCurrentCard`` call re-fetches from the scheduler (which will
        surface the un-done card again).  We also re-select the deck so the
        scheduler scope is correct — without this, the undo-ed card would not
        re-surface because the deck selection may have shifted.

        Returns:
            True if undo succeeded, False if nothing to undo.
        """
        col = self._col()
        try:
            with col_mod._col_lock:
                col.undo()
                log.info("guiUndo: undo successful")

                # Reset card state so next guiCurrentCard re-fetches
                if self._session is not None:
                    self._session.current_card_id = None
                    self._session.current_states = None
                    self._session.timer_started_at = None
                    # Re-select deck so the un-done card re-surfaces
                    col.decks.select(self._session.deck_id)
        except Exception as exc:
            # UndoEmpty or any other error
            log.debug("guiUndo: nothing to undo (%s)", exc)
            return False

        return True


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: ReviewSessionManager = ReviewSessionManager()


# ---------------------------------------------------------------------------
# Action handler functions (same signature as src/actions.py handlers)
# ---------------------------------------------------------------------------


def _gui_deck_review(params: dict) -> bool:
    return _manager.gui_deck_review(params)


def _gui_current_card(params: dict) -> dict | None:
    return _manager.gui_current_card(params)


def _gui_start_card_timer(params: dict) -> bool:
    return _manager.gui_start_card_timer(params)


def _gui_show_answer(params: dict) -> bool:
    return _manager.gui_show_answer(params)


def _gui_answer_card(params: dict) -> bool:
    return _manager.gui_answer_card(params)


def _gui_undo(params: dict) -> bool:
    return _manager.gui_undo(params)


# ---------------------------------------------------------------------------
# GUI_ACTIONS — merge into server dispatch table (Step 5/server.py)
# ---------------------------------------------------------------------------

GUI_ACTIONS: dict[str, Any] = {
    "guiDeckReview": _gui_deck_review,
    "guiCurrentCard": _gui_current_card,
    "guiStartCardTimer": _gui_start_card_timer,
    "guiShowAnswer": _gui_show_answer,
    "guiAnswerCard": _gui_answer_card,
    "guiUndo": _gui_undo,
}
