"""
Diagnostics & statistics action handlers — P0 stats endpoints.

All actions are READ-ONLY: they query col.db (SQLite) on the ``cards`` and
``revlog`` tables.  Even though these are reads, every query is run under
``col_mod._col_lock`` for consistency with concurrent writes on the other
thread.

Anki schema reminders (anki 25.9.2)
-------------------------------------
cards:
  id       — epoch_ms (card creation timestamp)
  nid      — note id
  did      — deck id
  ord      — card ordinal (template index)
  queue    — scheduling queue:
               0  = new
               1  = learning (intraday)
               2  = review
               3  = day-learning (interday learning)
              -1  = suspended
              -2  = buried (user)
              -3  = buried (scheduler)
  type     — card type (same values as queue; reflects state before suspend/bury)
  due      — for review cards (queue=2): day number relative to col.crt
             for new cards: position in new queue
             for learning cards: next due timestamp (epoch seconds)
  ivl      — interval in days (positive) or seconds (negative, for learning steps)
  factor   — SM-2 ease factor × 10 (e.g. 2500 = 250%)
  reps     — total reviews
  lapses   — total lapses

revlog:
  id       — epoch_ms (review timestamp, used as primary key)
  cid      — card id
  ease     — button pressed: 1=Again, 2=Hard, 3=Good, 4=Easy
  ivl      — new interval (days if positive, seconds if negative)
  lastIvl  — previous interval (days if positive, seconds if negative)
  type     — review type: 0=learning, 1=review, 2=relearn, 3=cram/filtered
  time     — time taken (ms)

Day cutoff convention (matches ``_get_num_cards_reviewed_today`` in actions.py)
--------------------------------------------------------------------------------
``col.sched.day_cutoff``  — Unix timestamp (seconds) at end of the current
                             Anki scheduler day (i.e. the NEXT rollover).
``col.sched.today``       — integer day number for the current day.  For
                             review cards: ``due - today`` gives the offset
                             in days from now (negative = overdue).

Start-of-window for ``days`` lookback:
  start_ms = (col.sched.day_cutoff - days * 86400) * 1000
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import src.collection as col_mod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col() -> Any:
    """Return the open ``anki.Collection`` instance."""
    return col_mod.get_col()


# ---------------------------------------------------------------------------
# statCardCounts
# ---------------------------------------------------------------------------

# Anki queue values
_QUEUE_NEW = 0
_QUEUE_LRN = 1        # intraday learning
_QUEUE_REV = 2        # review
_QUEUE_DAY_LRN = 3    # interday learning (day-learning)
_QUEUE_SUSPENDED = -1
_QUEUE_BURIED_USER = -2
_QUEUE_BURIED_SCHED = -3


def stat_card_counts(params: dict) -> dict:  # noqa: ARG001
    """Return card counts by scheduling category.

    Output:
        ``{"new": int, "learning": int, "review": int,
           "suspended": int, "buried": int, "total": int}``

    Categories match Anki's pie-chart view:
      - new       : queue=0
      - learning  : queue=1 (intraday) or queue=3 (interday/day-learning)
      - review    : queue=2
      - suspended : queue=-1
      - buried    : queue=-2 or queue=-3
    """
    col = _col()
    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT queue, COUNT(*) FROM cards GROUP BY queue"
            )
        except Exception as exc:
            raise ValueError(f"statCardCounts: query failed: {exc}") from exc

    counts: dict[int, int] = {q: c for q, c in rows}

    new_count = counts.get(_QUEUE_NEW, 0)
    learning_count = counts.get(_QUEUE_LRN, 0) + counts.get(_QUEUE_DAY_LRN, 0)
    review_count = counts.get(_QUEUE_REV, 0)
    suspended_count = counts.get(_QUEUE_SUSPENDED, 0)
    buried_count = counts.get(_QUEUE_BURIED_USER, 0) + counts.get(_QUEUE_BURIED_SCHED, 0)
    total = sum(counts.values())

    return {
        "new": new_count,
        "learning": learning_count,
        "review": review_count,
        "suspended": suspended_count,
        "buried": buried_count,
        "total": total,
    }


# ---------------------------------------------------------------------------
# statTrueRetention
# ---------------------------------------------------------------------------


def stat_true_retention(params: dict) -> dict:
    """Return true retention broken down by card maturity for a time window.

    Params:
        ``{"days": int = 30}``

    Output:
        ``{"young":   {"pass": int, "total": int, "retention": float|None},
           "mature":  {"pass": int, "total": int, "retention": float|None},
           "overall": {"pass": int, "total": int, "retention": float|None}}``

    Methodology:
      - Filter revlog to entries with type=1 (review of a review card) within
        the last ``days`` scheduler days.
      - Success = ease >= 2 (ease=1/Again counts as failure).
      - Maturity based on ``lastIvl``: young < 21 days, mature >= 21 days.
        (Positive lastIvl is days; negative lastIvl is seconds — treat negative
        as young since it indicates a learning-step predecessor.)
      - Retention = pass / total × 100, or None when total=0.

    Window:
        start_ms = (day_cutoff - days × 86400) × 1000
        end_ms   = day_cutoff × 1000  (exclusive upper bound)
    """
    col = _col()
    days: int = max(1, int(params.get("days", 30)))

    day_cutoff = col.sched.day_cutoff
    start_ms = (day_cutoff - days * 86400) * 1000
    end_ms = day_cutoff * 1000

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT ease, lastIvl FROM revlog "
                "WHERE type = 1 AND id >= ? AND id < ?",
                start_ms, end_ms,
            )
        except Exception as exc:
            raise ValueError(f"statTrueRetention: query failed: {exc}") from exc

    young_pass = young_total = 0
    mature_pass = mature_total = 0

    for ease, last_ivl in rows:
        # lastIvl is days (positive) or seconds (negative for learning steps).
        # Negative lastIvl means the card was in a learning step previously;
        # treat those as young.
        is_mature = isinstance(last_ivl, (int, float)) and last_ivl >= 21
        passed = (ease >= 2)

        if is_mature:
            mature_total += 1
            if passed:
                mature_pass += 1
        else:
            young_total += 1
            if passed:
                young_pass += 1

    overall_pass = young_pass + mature_pass
    overall_total = young_total + mature_total

    def _pct(p: int, t: int) -> float | None:
        return round(p / t * 100, 2) if t > 0 else None

    return {
        "young": {
            "pass": young_pass,
            "total": young_total,
            "retention": _pct(young_pass, young_total),
        },
        "mature": {
            "pass": mature_pass,
            "total": mature_total,
            "retention": _pct(mature_pass, mature_total),
        },
        "overall": {
            "pass": overall_pass,
            "total": overall_total,
            "retention": _pct(overall_pass, overall_total),
        },
    }


# ---------------------------------------------------------------------------
# statIntervalDistribution
# ---------------------------------------------------------------------------

# Histogram bucket edges (inclusive lower, exclusive upper; last bucket is open)
_IVL_BUCKETS: list[tuple[str, int, int]] = [
    ("1",       1,   2),
    ("2-7",     2,   8),
    ("8-30",    8,  31),
    ("31-90",  31,  91),
    ("91-180", 91, 181),
    ("181-365", 181, 366),
    (">365",   366, 10 ** 9),
]


def stat_interval_distribution(params: dict) -> dict:  # noqa: ARG001
    """Return a histogram of review-card (queue=2) intervals.

    Output:
        ``{"buckets": [{"label": str, "count": int}, ...]}``

    Buckets cover: 1 day, 2-7 days, 8-30, 31-90, 91-180, 181-365, >365.
    Only cards in queue=2 (active review) are counted; ivl is always positive
    (days) for queue=2 cards.
    """
    col = _col()
    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT ivl FROM cards WHERE queue = 2 AND ivl > 0"
            )
        except Exception as exc:
            raise ValueError(f"statIntervalDistribution: query failed: {exc}") from exc

    bin_counts: dict[str, int] = {label: 0 for label, _, _ in _IVL_BUCKETS}

    for (ivl,) in rows:
        for label, lo, hi in _IVL_BUCKETS:
            if lo <= ivl < hi:
                bin_counts[label] += 1
                break

    return {
        "buckets": [
            {"label": label, "count": bin_counts[label]}
            for label, _, _ in _IVL_BUCKETS
        ]
    }


# ---------------------------------------------------------------------------
# statEaseDistribution
# ---------------------------------------------------------------------------

# SM-2 ease factor histogram edges (factor × 10 units, i.e. raw card.factor)
# card.factor=2500 → ease=250%.  Anki default initialFactor=2500 (250%).
_EASE_BUCKETS: list[tuple[str, int, int]] = [
    ("<130%",  0,   1300),
    ("130-149%", 1300, 1500),
    ("150-169%", 1500, 1700),
    ("170-189%", 1700, 1900),
    ("190-209%", 1900, 2100),
    ("210-229%", 2100, 2300),
    ("230-249%", 2300, 2500),
    ("250-269%", 2500, 2700),
    ("270-289%", 2700, 2900),
    ("290%+",  2900, 10 ** 7),
]


def stat_ease_distribution(params: dict) -> dict:  # noqa: ARG001
    """Return the SM-2 ease factor (card.factor) histogram.

    Output:
        ``{"sm2": [{"label": str, "count": int}, ...],
           "fsrs_note": str}``

    Only cards with factor > 0 (i.e. review cards that have been reviewed at
    least once under SM-2) are included.  If FSRS is enabled, the weight
    vector replaces ease factors, so factor fields may all be 0 or the
    default; the ``fsrs_note`` field documents this.

    FSRS difficulty:
      FSRS stores difficulty in card.data (a JSON blob field in newer Anki
      versions) rather than in card.factor.  Extracting it reliably requires
      parsing per-card JSON, which is expensive and fragile across Anki
      versions.  We note FSRS status and return the factor histogram from
      cards.factor (which will be mostly-default or zero under FSRS); a
      separate FSRS-optimised difficulty histogram is out of scope for this
      action.
    """
    col = _col()

    # Detect FSRS: any deck config with non-empty fsrsParams6 or fsrsParams5
    fsrs_enabled = False
    try:
        for cfg in col.decks.all_config():
            if cfg.get("fsrsParams6") or cfg.get("fsrsParams5"):
                fsrs_enabled = True
                break
    except Exception:
        pass  # best-effort detection

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT factor FROM cards WHERE queue = 2 AND factor > 0"
            )
        except Exception as exc:
            raise ValueError(f"statEaseDistribution: query failed: {exc}") from exc

    bin_counts: dict[str, int] = {label: 0 for label, _, _ in _EASE_BUCKETS}

    for (factor,) in rows:
        for label, lo, hi in _EASE_BUCKETS:
            if lo <= factor < hi:
                bin_counts[label] += 1
                break

    fsrs_note: str
    if fsrs_enabled:
        fsrs_note = (
            "FSRS is enabled for one or more deck configs. "
            "FSRS replaces SM-2 ease with a learned difficulty parameter stored "
            "in card.data (JSON). The histogram above shows card.factor values "
            "which may be default (2500) or zero under FSRS and does not reflect "
            "FSRS difficulty. Per-card FSRS difficulty extraction is not supported "
            "by this action."
        )
    else:
        fsrs_note = "SM-2 scheduler active. card.factor reflects the ease factor (× 10)."

    return {
        "sm2": [
            {"label": label, "count": bin_counts[label]}
            for label, _, _ in _EASE_BUCKETS
        ],
        "fsrs_note": fsrs_note,
    }


# ---------------------------------------------------------------------------
# statFutureDue
# ---------------------------------------------------------------------------


def stat_future_due(params: dict) -> list[dict]:
    """Return daily review-card due counts for the next N days.

    Params:
        ``{"days": int = 30}``

    Output:
        ``[{"day_offset": int, "count": int}, ...]``

    ``day_offset`` is the number of days from today (0 = today, 1 = tomorrow,
    …).  Only cards with queue=2 are counted; their ``due`` field is an
    integer day number relative to ``col.crt``.  We compute:
        day_offset = due - col.sched.today

    Only offsets in [0, days] are returned (overdue cards where
    ``day_offset < 0`` are excluded — they are due now, not in the future).
    Days with zero due cards are included so the caller can render a complete
    histogram.

    Due semantics confirmation (empirically verified on fixture):
        - col.crt = 1781409600 (2026-06-14 04:00 UTC)
        - col.sched.today = 1 (second day of collection lifetime)
        - Review cards with due=0 have day_offset = -1 (overdue by 1 day)
        - A card with due=today+5 would appear at day_offset=5
    """
    col = _col()
    days: int = max(1, int(params.get("days", 30)))
    today = col.sched.today

    # due range: [today, today+days] inclusive
    due_min = today
    due_max = today + days

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT due, COUNT(*) FROM cards "
                "WHERE queue = 2 AND due >= ? AND due <= ? "
                "GROUP BY due",
                due_min, due_max,
            )
        except Exception as exc:
            raise ValueError(f"statFutureDue: query failed: {exc}") from exc

    # Build a complete list for every day offset 0..days
    due_counts: dict[int, int] = {due: cnt for due, cnt in rows}
    result: list[dict] = []
    for offset in range(days + 1):
        due_day = today + offset
        result.append({
            "day_offset": offset,
            "count": due_counts.get(due_day, 0),
        })

    return result


# ---------------------------------------------------------------------------
# statReviewsByDay
# ---------------------------------------------------------------------------


def stat_reviews_by_day(params: dict) -> list[dict]:
    """Return daily review counts and time from revlog.

    Params:
        ``{"days": int = 365}``

    Output:
        ``[{"date": "YYYY-MM-DD", "count": int, "timeMs": int}, ...]``

    Window: last ``days`` scheduler days, using the same convention as
    ``_get_num_cards_reviewed_by_day`` (which groups all revlog by UTC date).
    Dates are in UTC to match the existing action.
    """
    col = _col()
    days: int = max(1, int(params.get("days", 365)))

    day_cutoff = col.sched.day_cutoff
    start_ms = (day_cutoff - days * 86400) * 1000

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT id, time FROM revlog WHERE id >= ? ORDER BY id",
                start_ms,
            )
        except Exception as exc:
            raise ValueError(f"statReviewsByDay: query failed: {exc}") from exc

    # Aggregate by UTC date (same convention as getNumCardsReviewedByDay)
    day_counts: dict[str, int] = {}
    day_time: dict[str, int] = {}

    for ts_ms, time_ms in rows:
        day_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        day_counts[day_str] = day_counts.get(day_str, 0) + 1
        day_time[day_str] = day_time.get(day_str, 0) + (time_ms or 0)

    return [
        {"date": day, "count": day_counts[day], "timeMs": day_time[day]}
        for day in sorted(day_counts)
    ]


# ---------------------------------------------------------------------------
# statAddedByDay
# ---------------------------------------------------------------------------


def stat_added_by_day(params: dict) -> list[dict]:
    """Return daily card-addition counts from cards.id (epoch_ms).

    Params:
        ``{"days": int = 365}``

    Output:
        ``[{"date": "YYYY-MM-DD", "count": int}, ...]``

    ``cards.id`` is the epoch_ms timestamp of card creation.  We use the same
    ``day_cutoff``-based window as the other stats actions.
    """
    col = _col()
    days: int = max(1, int(params.get("days", 365)))

    day_cutoff = col.sched.day_cutoff
    start_ms = (day_cutoff - days * 86400) * 1000

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT id FROM cards WHERE id >= ? ORDER BY id",
                start_ms,
            )
        except Exception as exc:
            raise ValueError(f"statAddedByDay: query failed: {exc}") from exc

    day_counts: dict[str, int] = {}
    for (card_id,) in rows:
        day_str = datetime.fromtimestamp(card_id / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        day_counts[day_str] = day_counts.get(day_str, 0) + 1

    return [
        {"date": day, "count": day_counts[day]}
        for day in sorted(day_counts)
    ]


# ---------------------------------------------------------------------------
# statTimeSpent
# ---------------------------------------------------------------------------


def stat_time_spent(params: dict) -> dict:
    """Return time-spent statistics from revlog.time (ms).

    Params:
        ``{"days": int = 30}``

    Output:
        ``{"totalMs": int,
           "perDayMs": [{"date": "YYYY-MM-DD", "ms": int}, ...],
           "avgMsPerReview": float|None}``

    Window uses the same ``day_cutoff`` convention.
    """
    col = _col()
    days: int = max(1, int(params.get("days", 30)))

    day_cutoff = col.sched.day_cutoff
    start_ms = (day_cutoff - days * 86400) * 1000

    with col_mod._col_lock:
        try:
            rows = col.db.all(
                "SELECT id, time FROM revlog WHERE id >= ? ORDER BY id",
                start_ms,
            )
        except Exception as exc:
            raise ValueError(f"statTimeSpent: query failed: {exc}") from exc

    day_time: dict[str, int] = {}
    total_ms = 0
    total_reviews = 0

    for ts_ms, time_ms in rows:
        t = time_ms or 0
        day_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        day_time[day_str] = day_time.get(day_str, 0) + t
        total_ms += t
        total_reviews += 1

    avg_ms: float | None = (
        round(total_ms / total_reviews, 2) if total_reviews > 0 else None
    )

    return {
        "totalMs": total_ms,
        "perDayMs": [
            {"date": day, "ms": day_time[day]}
            for day in sorted(day_time)
        ],
        "avgMsPerReview": avg_ms,
    }


# ---------------------------------------------------------------------------
# Public action map (imported by actions.py)
# ---------------------------------------------------------------------------

STATS_ACTIONS: dict[str, Any] = {
    "statCardCounts": stat_card_counts,
    "statTrueRetention": stat_true_retention,
    "statIntervalDistribution": stat_interval_distribution,
    "statEaseDistribution": stat_ease_distribution,
    "statFutureDue": stat_future_due,
    "statReviewsByDay": stat_reviews_by_day,
    "statAddedByDay": stat_added_by_day,
    "statTimeSpent": stat_time_spent,
}
