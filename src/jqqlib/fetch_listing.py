"""Fetch the listing master for one year end and confirm that the day is the year end."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from .fetch import _RequestPacer, call_jquants_with_retry
from .listing import ListingError, YearEndListing, confirm_year_end, normalize_listing, single_effective_day


def fetch_listing_master(
    client: Any,
    day: dt.date,
    *,
    retries: int,
    retry_base_delay_sec: float,
    retry_max_delay_sec: float,
    retry_jitter_sec: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, object]]:
    """Return every row of the master the API serves for ``day``, all issues, as plain mappings.

    The rows are not checked here: the API answers a non-business day with a later day's master.
    Raises ``ListingError`` when ``client`` has no listing-master endpoint (the API v1 client).
    """
    if not hasattr(client, "get_eq_master"):
        raise ListingError("the listing master needs the J-Quants API v2 client (an API key)")
    frame = call_jquants_with_retry(
        lambda: client.get_eq_master(date=day.isoformat()),
        retries=retries,
        base_delay_sec=retry_base_delay_sec,
        max_delay_sec=retry_max_delay_sec,
        jitter_sec=retry_jitter_sec,
        sleeper=sleeper,
    )
    if frame is None:
        return []
    return [dict(record) for record in frame.to_dict("records")]


def fetch_year_end_listing(
    client: Any,
    candidate: dt.date,
    *,
    request_interval_sec: float,
    retries: int,
    retry_base_delay_sec: float,
    retry_max_delay_sec: float,
    retry_jitter_sec: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> YearEndListing:
    """Fetch the master for ``candidate`` and for the day after, and return it only when both hold.

    The first response must pass ``normalize_listing`` for ``candidate``, which also shows that
    ``candidate`` is a session. The second must carry one date, in the following year, which shows
    that no session of the year follows. A response that fails either check raises ``ListingError``;
    a network or authentication failure propagates as raised by the client, after the retries.
    """
    def master_for(day: dt.date) -> list[dict[str, object]]:
        pacer.before_request()
        return fetch_listing_master(
            client,
            day,
            retries=retries,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            retry_jitter_sec=retry_jitter_sec,
            sleeper=sleeper,
        )

    pacer = _RequestPacer(request_interval_sec, sleeper)
    rows = master_for(candidate)
    normalize_listing(rows, requested=candidate)
    next_day = candidate + dt.timedelta(days=1)
    confirmation_effective = single_effective_day(master_for(next_day))
    confirm_year_end(candidate, confirmation_effective)
    return YearEndListing(
        day=candidate,
        confirmation_requested=next_day,
        confirmation_effective=confirmation_effective,
        rows=tuple(rows),
    )
