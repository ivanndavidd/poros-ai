"""Stored timestamps are UTC and say so.

Regression tests for a bug a user reported from the console: an audit entry created at
10:47 Jakarta time displayed as "03:47". `store.now()` was `time.strftime(...)` with no
argument — the SERVER's local clock, rendered without any zone marker — and five views
printed the string verbatim, so the seven-hour gap was invisible and unexplainable.

The format check is the important one. Asserting "now() is close to UTC" would pass on a
UTC machine even with the bug restored, and CI runners are UTC — so it would have caught
nothing. The trailing Z is what actually makes the value self-describing, and every writer
has to agree on it or `expires >= now()` comparisons drift.
"""
import re
from datetime import datetime, timezone

from app import store

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parsed(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_now_is_marked_utc():
    assert ISO_UTC.match(store.now()), store.now()


def test_now_is_actually_utc_not_the_servers_local_clock():
    """Fails on a non-UTC machine if the old time.strftime() behaviour comes back."""
    delta = abs((_parsed(store.now()) - datetime.now(timezone.utc)).total_seconds())
    assert delta < 120, f"now() is {delta}s from UTC — reading the local clock again?"


def test_ts_after_shares_the_format_of_now():
    """Session and reservation expiry are compared to now() as STRINGS. A writer that
    emits a different shape would compare wrong at the boundary rather than fail loudly."""
    assert ISO_UTC.match(store.ts_after(3600))
    assert ISO_UTC.match(store.ts_after(-3600))


def test_ts_after_moves_in_the_right_direction():
    now = _parsed(store.now())
    assert 3500 < (_parsed(store.ts_after(3600)) - now).total_seconds() < 3700
    assert 3500 < (now - _parsed(store.ts_after(-3600))).total_seconds() < 3700


def test_future_expiry_sorts_after_now_as_a_plain_string():
    """The whole expiry mechanism is string comparison in SQL, so prove it there."""
    assert store.ts_after(3600) > store.now()
    assert store.ts_after(-3600) < store.now()


def test_month_start_is_marked_utc_too():
    """It is a bound for `ts >= ?` against the same column now() writes."""
    value = store._month_start()
    assert ISO_UTC.match(value), value
    assert value.endswith("-01T00:00:00Z")


def test_pre_z_rows_still_compare_correctly():
    """Rows written before the Z was added are UTC as well, just unmarked. The suffix sits
    after every field that differs, so mixing the two generations still orders by time."""
    old_row = "2026-08-13T03:47:18"          # written by the previous implementation
    assert "2026-08-13T03:47:19Z" > old_row
    assert "2026-08-13T03:47:17Z" < old_row
    assert "2026-08-14T00:00:00Z" > old_row
    assert "2026-08-12T23:59:59Z" < old_row
