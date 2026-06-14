"""
Stateful review session manager for gui* AnkiConnect actions.

The AnkiConnect `gui*` family of actions (guiCurrentCard, guiAnswerCard,
guiDeckReview, etc.) model the Anki reviewer as a state machine. Because
the server is stateless between HTTP requests but Anki's reviewer is not,
this module maintains an in-process ReviewSession object that tracks:
  - which deck is being reviewed
  - the current card (if any)
  - whether the session is active

Responsibilities (Step 7):
  - Implement ReviewSession class with start(), current_card(), answer(),
    and end() methods.
  - Wire the session into actions.py gui* handlers.
  - Handle edge cases: empty deck, session already active, etc.

TODO (Step 7): implement ReviewSession and integrate with actions.py.
"""
