"""The TSE (XTKS) session calendar adapter for the dataset lane.

Single source of "is this date a trading session?" for the pipeline and the gap
audit. It is deliberately fail-closed: there is NO weekday heuristic fallback
here. A weekday heuristic silently reclassifies exchange holidays as expected
business days, which is exactly the false-alarm/false-clear class the audit must
never produce, so a missing ``exchange_calendars`` raises ``ExactCalendarRequired``
and the caller surfaces it as a distinct failure instead of guessing.
"""

from __future__ import annotations

import datetime as dt


class ExactCalendarRequired(RuntimeError):
    """Raised when the authoritative XTKS calendar dependency is unavailable."""


def _xtks_calendar():
    try:
        import exchange_calendars as xc
    except ModuleNotFoundError as exc:
        raise ExactCalendarRequired(
            "exchange_calendars with XTKS is required for dataset calendar gates"
        ) from exc
    return xc.get_calendar("XTKS")


def is_session(day: dt.date) -> bool:
    """Return True iff ``day`` is a TSE (XTKS) trading session."""
    cal = _xtks_calendar()
    try:
        return bool(cal.is_session(day.isoformat()))
    except Exception as exc:
        # exchange_calendars raises DateOutOfBounds outside its precomputed
        # range. Treat that as "calendar cannot answer", never as a guess.
        if exc.__class__.__name__ != "DateOutOfBounds":
            raise
        raise ExactCalendarRequired(
            f"XTKS calendar has no session data for {day.isoformat()}"
        ) from exc


def is_mon_fri(day: dt.date) -> bool:
    """True for Monday–Friday. Not an XTKS session test."""
    return day.weekday() < 5
