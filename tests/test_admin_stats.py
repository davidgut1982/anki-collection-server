"""
Integration tests for P0 diagnostics & stats actions (feat/admin-actions A5).

Actions under test:
  - statCardCounts          — card counts by scheduling category
  - statTrueRetention       — true retention by maturity bucket
  - statIntervalDistribution — histogram of review-card intervals
  - statEaseDistribution    — SM-2 ease factor histogram
  - statFutureDue           — future due counts per day
  - statReviewsByDay        — daily review counts from revlog
  - statAddedByDay          — daily card-addition counts
  - statTimeSpent           — time-spent statistics from revlog

All tests operate on:
  1. The committed fixture at tests/fixtures/test_collection.anki2
  2. An independent /tmp copy (``tmp_col`` fixture) to verify the same shapes
     on a copy, confirming no fixture-path dependency.

The committed fixture is NEVER modified directly.  Tests use the
``session``-scoped ``backup_copy`` fixture for the shared read-only copy and
the ``col`` per-test fixture that opens/closes it.

Fixture data summary (test_collection.anki2):
  - 9 cards total: 5 new (queue=0), 4 review (queue=2)
  - 40 revlog entries: 28 type=1 (review), 12 type=0 (learning)
    * All type=1 entries have ease=3 (Good) → 100% retention
    * All type=1 entries have lastIvl=0 → all classified as young
  - Review cards: due=0, today≈1, so all are overdue by 1 day
  - All review cards have ivl=10 (bucket "8-30") and factor=2500 (bucket "250-269%")
  - Revlog spans 2026-05-15 to 2026-05-21 (7 days, 40 entries)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src import collection as col_mod
from src.actions import ACTIONS

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_COMMITTED_FIXTURE = Path(__file__).parent / "fixtures" / "test_collection.anki2"
_DEFAULT_BACKUP = str(_COMMITTED_FIXTURE)
BACKUP = Path(os.environ.get("ANKI_TEST_BACKUP", _DEFAULT_BACKUP))


# ---------------------------------------------------------------------------
# Session fixture: shared read-only copy
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def backup_copy() -> Generator[Path, None, None]:
    """Copy the fixture once per session; yield its path; clean up after."""
    if not BACKUP.exists():
        pytest.fail(
            f"Test fixture not found: {BACKUP}. "
            "Set ANKI_TEST_BACKUP env var to point at a readable .anki2 file."
        )
    tmpdir = Path(tempfile.mkdtemp(prefix="acs-stats-test-", dir="/tmp"))
    col_path = tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)
    yield col_path
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# /tmp copy fixture: independent copy for cross-verification
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def tmp_col_path() -> Generator[Path, None, None]:
    """A second independent /tmp copy of the fixture (for duplicate-path tests)."""
    if not BACKUP.exists():
        pytest.skip("Fixture not found — skipping tmp_col_path fixture")
    tmpdir = Path(tempfile.mkdtemp(prefix="acs-stats-tmp-", dir="/tmp"))
    col_path = tmpdir / "collection.anki2"
    shutil.copy2(BACKUP, col_path)
    col_path.chmod(0o600)
    yield col_path
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Per-test collection open/close
# ---------------------------------------------------------------------------


@pytest.fixture()
def col(backup_copy: Path) -> Generator[None, None, None]:
    """Open the shared session copy before each test; close after."""
    try:
        col_mod.manager.close()
    except Exception:
        pass
    col_mod.manager.open(backup_copy)
    yield
    col_mod.manager.close()


@pytest.fixture()
def tmp_col(tmp_col_path: Path) -> Generator[None, None, None]:
    """Open the /tmp copy before each test; close after."""
    try:
        col_mod.manager.close()
    except Exception:
        pass
    col_mod.manager.open(tmp_col_path)
    yield
    col_mod.manager.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def invoke(action: str, **kwargs: object) -> object:
    """Call an ACTIONS handler by name with the given keyword params."""
    handler = ACTIONS[action]
    return handler(dict(kwargs))


def _card_count_from_db(col_path: Path) -> int:
    """Read the total card count directly from SQLite (no anki library)."""
    con = sqlite3.connect(str(col_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM cards")
        return cur.fetchone()[0]
    finally:
        con.close()


# ===========================================================================
# statCardCounts
# ===========================================================================


class TestStatCardCounts:
    def test_shape(self, col: None) -> None:
        """Result must contain the documented keys."""
        result = invoke("statCardCounts")
        assert isinstance(result, dict)
        for key in ("new", "learning", "review", "suspended", "buried", "total"):
            assert key in result, f"Missing key: {key!r}"

    def test_all_values_non_negative(self, col: None) -> None:
        result = invoke("statCardCounts")
        for key, val in result.items():
            assert isinstance(val, int), f"{key} must be int, got {type(val)}"
            assert val >= 0, f"{key} must be >= 0, got {val}"

    def test_total_matches_db_card_count(self, col: None, backup_copy: Path) -> None:
        """statCardCounts.total must equal the raw SQLite card count."""
        result = invoke("statCardCounts")
        db_total = _card_count_from_db(backup_copy)
        assert result["total"] == db_total, (
            f"statCardCounts total={result['total']} "
            f"but db has {db_total} cards"
        )

    def test_categories_sum_to_total(self, col: None) -> None:
        """new + learning + review + suspended + buried must equal total."""
        result = invoke("statCardCounts")
        parts_sum = (
            result["new"]
            + result["learning"]
            + result["review"]
            + result["suspended"]
            + result["buried"]
        )
        assert parts_sum == result["total"], (
            f"Parts sum {parts_sum} != total {result['total']}"
        )

    def test_fixture_known_counts(self, col: None) -> None:
        """Fixture has 5 new cards and 4 review cards; no learning/suspended/buried."""
        result = invoke("statCardCounts")
        assert result["new"] == 5
        assert result["review"] == 4
        assert result["learning"] == 0
        assert result["suspended"] == 0
        assert result["buried"] == 0
        assert result["total"] == 9

    def test_returns_same_on_tmp_copy(self, tmp_col: None, backup_copy: Path) -> None:
        """Same total on the /tmp copy as the committed fixture."""
        result = invoke("statCardCounts")
        db_total = _card_count_from_db(backup_copy)
        assert result["total"] == db_total


# ===========================================================================
# statTrueRetention
# ===========================================================================


class TestStatTrueRetention:
    def test_shape(self, col: None) -> None:
        """Result must have young/mature/overall buckets each with pass/total/retention."""
        result = invoke("statTrueRetention", days=30)
        assert isinstance(result, dict)
        for bucket in ("young", "mature", "overall"):
            assert bucket in result, f"Missing bucket: {bucket!r}"
            b = result[bucket]
            assert "pass" in b
            assert "total" in b
            assert "retention" in b

    def test_retention_range(self, col: None) -> None:
        """Retention must be 0-100 or None."""
        result = invoke("statTrueRetention", days=30)
        for bucket_name in ("young", "mature", "overall"):
            b = result[bucket_name]
            r = b["retention"]
            assert r is None or (0.0 <= r <= 100.0), (
                f"{bucket_name}.retention={r} out of [0,100]"
            )

    def test_pass_le_total(self, col: None) -> None:
        """Pass count must never exceed total count."""
        result = invoke("statTrueRetention", days=365)
        for bucket_name in ("young", "mature", "overall"):
            b = result[bucket_name]
            assert b["pass"] <= b["total"], (
                f"{bucket_name}: pass={b['pass']} > total={b['total']}"
            )

    def test_overall_equals_young_plus_mature(self, col: None) -> None:
        """overall.total == young.total + mature.total."""
        result = invoke("statTrueRetention", days=365)
        assert result["overall"]["total"] == (
            result["young"]["total"] + result["mature"]["total"]
        )
        assert result["overall"]["pass"] == (
            result["young"]["pass"] + result["mature"]["pass"]
        )

    def test_fixture_all_young_100pct(self, col: None) -> None:
        """Fixture: 28 type=1 reviews, all ease=3 (pass), all lastIvl=0 (young)."""
        result = invoke("statTrueRetention", days=365)
        young = result["young"]
        mature = result["mature"]
        overall = result["overall"]

        assert young["total"] == 28
        assert young["pass"] == 28
        assert young["retention"] == 100.0

        # All young → mature should be empty
        assert mature["total"] == 0
        assert mature["pass"] == 0
        assert mature["retention"] is None

        assert overall["total"] == 28
        assert overall["pass"] == 28
        assert overall["retention"] == 100.0

    def test_no_reviews_in_empty_window(self, col: None) -> None:
        """With days=1, the tiny window may have 0 reviews — retention=None is valid."""
        result = invoke("statTrueRetention", days=1)
        # The fixture revlog is months old; 1-day window should yield 0 reviews
        overall = result["overall"]
        assert overall["total"] >= 0
        if overall["total"] == 0:
            assert overall["retention"] is None

    def test_default_days_param(self, col: None) -> None:
        """Calling without 'days' param must not raise (defaults to 30)."""
        result = invoke("statTrueRetention")
        assert "overall" in result


# ===========================================================================
# statIntervalDistribution
# ===========================================================================


class TestStatIntervalDistribution:
    def test_shape(self, col: None) -> None:
        """Result must have a 'buckets' list with label/count entries."""
        result = invoke("statIntervalDistribution")
        assert isinstance(result, dict)
        assert "buckets" in result
        buckets = result["buckets"]
        assert isinstance(buckets, list)
        assert len(buckets) == 7
        for b in buckets:
            assert "label" in b
            assert "count" in b
            assert isinstance(b["count"], int)
            assert b["count"] >= 0

    def test_bucket_labels(self, col: None) -> None:
        """Expected bucket labels must all be present."""
        result = invoke("statIntervalDistribution")
        labels = [b["label"] for b in result["buckets"]]
        assert labels == ["1", "2-7", "8-30", "31-90", "91-180", "181-365", ">365"]

    def test_sum_equals_review_card_count(self, col: None) -> None:
        """Total of all buckets must equal the number of queue=2 cards."""
        result = invoke("statIntervalDistribution")
        bucket_sum = sum(b["count"] for b in result["buckets"])
        # From statCardCounts: 4 review cards
        counts = invoke("statCardCounts")
        assert bucket_sum == counts["review"], (
            f"Interval bucket sum {bucket_sum} != review count {counts['review']}"
        )

    def test_fixture_all_in_8_30_bucket(self, col: None) -> None:
        """Fixture: all 4 review cards have ivl=10 → bucket '8-30'."""
        result = invoke("statIntervalDistribution")
        buckets = {b["label"]: b["count"] for b in result["buckets"]}
        assert buckets["8-30"] == 4
        # All other buckets must be zero
        for label, count in buckets.items():
            if label != "8-30":
                assert count == 0, f"Bucket {label!r} expected 0, got {count}"


# ===========================================================================
# statEaseDistribution
# ===========================================================================


class TestStatEaseDistribution:
    def test_shape(self, col: None) -> None:
        """Result must have 'sm2' list and 'fsrs_note' string."""
        result = invoke("statEaseDistribution")
        assert isinstance(result, dict)
        assert "sm2" in result
        assert "fsrs_note" in result
        assert isinstance(result["fsrs_note"], str)
        sm2 = result["sm2"]
        assert isinstance(sm2, list)
        for b in sm2:
            assert "label" in b
            assert "count" in b
            assert isinstance(b["count"], int)
            assert b["count"] >= 0

    def test_all_counts_non_negative(self, col: None) -> None:
        result = invoke("statEaseDistribution")
        for b in result["sm2"]:
            assert b["count"] >= 0

    def test_sum_le_review_cards(self, col: None) -> None:
        """Sum of sm2 buckets <= review card count (only factor>0 cards counted)."""
        result = invoke("statEaseDistribution")
        counts = invoke("statCardCounts")
        sm2_sum = sum(b["count"] for b in result["sm2"])
        assert sm2_sum <= counts["review"]

    def test_fixture_ease_2500_bucket(self, col: None) -> None:
        """Fixture: all 4 review cards have factor=2500 → bucket '250-269%'."""
        result = invoke("statEaseDistribution")
        buckets = {b["label"]: b["count"] for b in result["sm2"]}
        assert buckets["250-269%"] == 4
        # All other buckets must be zero
        for label, count in buckets.items():
            if label != "250-269%":
                assert count == 0, f"Bucket {label!r} expected 0, got {count}"

    def test_fsrs_note_is_sm2(self, col: None) -> None:
        """Fixture uses SM-2 (no FSRS params set); fsrs_note must say so."""
        result = invoke("statEaseDistribution")
        note = result["fsrs_note"].lower()
        # Should NOT mention FSRS is enabled
        assert "sm-2" in note or "sm2" in note


# ===========================================================================
# statFutureDue
# ===========================================================================


class TestStatFutureDue:
    def test_shape(self, col: None) -> None:
        """Result must be a list of {day_offset, count} dicts."""
        result = invoke("statFutureDue", days=30)
        assert isinstance(result, list)
        assert len(result) == 31  # days+1 entries (0 through 30)
        for entry in result:
            assert "day_offset" in entry
            assert "count" in entry
            assert isinstance(entry["day_offset"], int)
            assert isinstance(entry["count"], int)

    def test_all_counts_non_negative(self, col: None) -> None:
        result = invoke("statFutureDue", days=30)
        for entry in result:
            assert entry["count"] >= 0, (
                f"day_offset={entry['day_offset']} count={entry['count']} is negative"
            )

    def test_day_offsets_are_sequential(self, col: None) -> None:
        """day_offset must start at 0 and increment by 1."""
        result = invoke("statFutureDue", days=14)
        for i, entry in enumerate(result):
            assert entry["day_offset"] == i, (
                f"entry[{i}].day_offset={entry['day_offset']}, expected {i}"
            )

    def test_length_equals_days_plus_one(self, col: None) -> None:
        """Length of result == days + 1."""
        for days in (1, 7, 30, 90):
            result = invoke("statFutureDue", days=days)
            assert len(result) == days + 1, (
                f"days={days}: got {len(result)} entries, expected {days + 1}"
            )

    def test_fixture_no_future_cards(self, col: None) -> None:
        """Fixture review cards are all overdue (due=0, today≈1).
        No cards are in the future → all day_offset counts should be 0."""
        result = invoke("statFutureDue", days=30)
        for entry in result:
            assert entry["count"] == 0, (
                f"day_offset={entry['day_offset']} expected 0, got {entry['count']}"
            )

    def test_default_days_param(self, col: None) -> None:
        """Calling without 'days' param must not raise (defaults to 30)."""
        result = invoke("statFutureDue")
        assert len(result) == 31


# ===========================================================================
# statReviewsByDay
# ===========================================================================


class TestStatReviewsByDay:
    def test_shape(self, col: None) -> None:
        """Result must be a list of {date, count, timeMs} dicts."""
        result = invoke("statReviewsByDay", days=365)
        assert isinstance(result, list)
        for entry in result:
            assert "date" in entry
            assert "count" in entry
            assert "timeMs" in entry
            assert isinstance(entry["date"], str)
            assert isinstance(entry["count"], int)
            assert isinstance(entry["timeMs"], int)

    def test_counts_positive(self, col: None) -> None:
        """Every returned entry must have count > 0."""
        result = invoke("statReviewsByDay", days=365)
        for entry in result:
            assert entry["count"] > 0
            assert entry["timeMs"] >= 0

    def test_matches_get_num_cards_reviewed_by_day(self, col: None) -> None:
        """statReviewsByDay must agree with getNumCardsReviewedByDay for overlap.

        getNumCardsReviewedByDay covers ALL revlog; statReviewsByDay covers
        the last N days.  For days=365 (which covers all fixture revlog),
        the date→count mapping must match exactly.
        """
        reviewed_by_day_raw = invoke("getNumCardsReviewedByDay")
        assert isinstance(reviewed_by_day_raw, list)
        # Convert [[date, count], ...] → {date: count}
        gn_map: dict[str, int] = {
            row[0]: row[1] for row in reviewed_by_day_raw
        }

        stats_result = invoke("statReviewsByDay", days=365)
        # Convert to {date: count}
        sr_map: dict[str, int] = {entry["date"]: entry["count"] for entry in stats_result}

        # Every date in statReviewsByDay must be in getNumCardsReviewedByDay
        # with the same count (statReviewsByDay is a subset of the window).
        for date, count in sr_map.items():
            assert date in gn_map, f"date {date!r} from statReviewsByDay not in getNumCardsReviewedByDay"
            assert count == gn_map[date], (
                f"date={date}: statReviewsByDay count={count} "
                f"!= getNumCardsReviewedByDay count={gn_map[date]}"
            )

    def test_fixture_total_reviews(self, col: None) -> None:
        """Fixture has 40 revlog entries across 7 days; days=365 should capture all."""
        result = invoke("statReviewsByDay", days=365)
        total = sum(entry["count"] for entry in result)
        assert total == 40, f"Expected 40 total reviews, got {total}"

    def test_fixture_7_days(self, col: None) -> None:
        """Fixture revlog spans exactly 7 distinct days."""
        result = invoke("statReviewsByDay", days=365)
        assert len(result) == 7, f"Expected 7 date entries, got {len(result)}"

    def test_fixture_dates(self, col: None) -> None:
        """Fixture revlog dates must be 2026-05-15 through 2026-05-21."""
        result = invoke("statReviewsByDay", days=365)
        dates = [entry["date"] for entry in result]
        expected = [
            "2026-05-15", "2026-05-16", "2026-05-17",
            "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21",
        ]
        assert dates == expected, f"Dates mismatch: {dates}"

    def test_default_days_param(self, col: None) -> None:
        """Calling without 'days' param must not raise (defaults to 365)."""
        result = invoke("statReviewsByDay")
        assert isinstance(result, list)


# ===========================================================================
# statAddedByDay
# ===========================================================================


class TestStatAddedByDay:
    def test_shape(self, col: None) -> None:
        """Result must be a list of {date, count} dicts."""
        result = invoke("statAddedByDay", days=365)
        assert isinstance(result, list)
        for entry in result:
            assert "date" in entry
            assert "count" in entry
            assert isinstance(entry["date"], str)
            assert isinstance(entry["count"], int)
            assert entry["count"] > 0

    def test_total_equals_card_count(self, col: None, backup_copy: Path) -> None:
        """Sum of all counts must equal the total number of cards in DB."""
        result = invoke("statAddedByDay", days=365)
        total = sum(entry["count"] for entry in result)
        db_total = _card_count_from_db(backup_copy)
        assert total == db_total, (
            f"statAddedByDay total={total} != db card count={db_total}"
        )

    def test_dates_sorted(self, col: None) -> None:
        """Dates must be returned in ascending order."""
        result = invoke("statAddedByDay", days=365)
        dates = [entry["date"] for entry in result]
        assert dates == sorted(dates)

    def test_default_days_param(self, col: None) -> None:
        """Calling without 'days' param must not raise."""
        result = invoke("statAddedByDay")
        assert isinstance(result, list)


# ===========================================================================
# statTimeSpent
# ===========================================================================


class TestStatTimeSpent:
    def test_shape(self, col: None) -> None:
        """Result must have totalMs, perDayMs list, avgMsPerReview."""
        result = invoke("statTimeSpent", days=30)
        assert isinstance(result, dict)
        assert "totalMs" in result
        assert "perDayMs" in result
        assert "avgMsPerReview" in result
        assert isinstance(result["totalMs"], int)
        assert isinstance(result["perDayMs"], list)
        avg = result["avgMsPerReview"]
        assert avg is None or isinstance(avg, (int, float))

    def test_total_ms_non_negative(self, col: None) -> None:
        result = invoke("statTimeSpent", days=365)
        assert result["totalMs"] >= 0

    def test_per_day_sum_equals_total(self, col: None) -> None:
        """Sum of perDayMs[].ms must equal totalMs."""
        result = invoke("statTimeSpent", days=365)
        per_day_total = sum(entry["ms"] for entry in result["perDayMs"])
        assert per_day_total == result["totalMs"], (
            f"perDayMs sum={per_day_total} != totalMs={result['totalMs']}"
        )

    def test_avg_ms_per_review_range(self, col: None) -> None:
        """avgMsPerReview must be None (no reviews) or a positive number."""
        result = invoke("statTimeSpent", days=365)
        avg = result["avgMsPerReview"]
        if avg is not None:
            assert avg > 0, f"avgMsPerReview={avg} must be positive when not None"

    def test_fixture_known_time(self, col: None) -> None:
        """Fixture revlog: 28 type=1 reviews × 10000ms + 12 type=0 × 5000ms = 340000ms."""
        result = invoke("statTimeSpent", days=365)
        expected_total_ms = 28 * 10000 + 12 * 5000
        assert result["totalMs"] == expected_total_ms, (
            f"Expected totalMs={expected_total_ms}, got {result['totalMs']}"
        )

    def test_avg_ms_zero_window(self, col: None) -> None:
        """days=1 on old fixture data → 0 reviews → avgMsPerReview=None."""
        result = invoke("statTimeSpent", days=1)
        # Fixture revlog is months old; 1-day window should be empty
        if result["totalMs"] == 0:
            assert result["avgMsPerReview"] is None

    def test_default_days_param(self, col: None) -> None:
        """Calling without 'days' param must not raise (defaults to 30)."""
        result = invoke("statTimeSpent")
        assert "totalMs" in result

    def test_per_day_entries_have_date_and_ms(self, col: None) -> None:
        result = invoke("statTimeSpent", days=365)
        for entry in result["perDayMs"]:
            assert "date" in entry
            assert "ms" in entry
            assert isinstance(entry["ms"], int)
            assert entry["ms"] >= 0


# ===========================================================================
# Cross-action consistency
# ===========================================================================


class TestCrossActionConsistency:
    def test_stat_card_counts_total_consistent_with_interval_distribution(
        self, col: None
    ) -> None:
        """Interval distribution bucket sum must equal statCardCounts.review."""
        counts = invoke("statCardCounts")
        dist = invoke("statIntervalDistribution")
        bucket_sum = sum(b["count"] for b in dist["buckets"])
        assert bucket_sum == counts["review"]

    def test_reviews_by_day_subset_of_get_num_cards_by_day(self, col: None) -> None:
        """statReviewsByDay (days=365) must be a subset of getNumCardsReviewedByDay."""
        raw = invoke("getNumCardsReviewedByDay")
        all_dates: set[str] = {row[0] for row in raw}

        stats_result = invoke("statReviewsByDay", days=365)
        for entry in stats_result:
            assert entry["date"] in all_dates, (
                f"Date {entry['date']!r} in statReviewsByDay but not in getNumCardsReviewedByDay"
            )

    def test_future_due_non_negative_counts(self, col: None) -> None:
        """All future due counts must be non-negative integers."""
        result = invoke("statFutureDue", days=90)
        for entry in result:
            assert isinstance(entry["count"], int)
            assert entry["count"] >= 0
