"""Point-in-time listing master: year-end dates, response checks and the per-date document.

The J-Quants listing master (``/equities/master``) answers "which issues were listed on this
date, on which market, as which product". This module holds the pure rules for turning one such
response into the document a research reader can trust. It performs no I/O.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

LISTING_DOCUMENT_SCHEMA = 1
KNOWN_PRODUCT_CATEGORIES = frozenset({"011", "012", "013", "014", "021", "022", "023", "024"})
KNOWN_MARKET_CODES = frozenset(
    {"0101", "0102", "0104", "0105", "0106", "0107", "0109", "0111", "0112", "0113"}
)
_REQUIRED_RESPONSE_FIELDS = ("Date", "Code", "Mkt", "ProdCat")
_LISTING_CODE = re.compile(r"[0-9A-Z]{5}", re.ASCII)
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_PANDAS_MISSING_TYPE_NAMES = frozenset({"NAType", "NaTType"})
_MIDNIGHT = dt.time(0, 0)


class ListingError(ValueError):
    """Raised when a listing response or a year-end date cannot be trusted."""


def _is_missing(value: object) -> bool:
    if value is None or type(value).__name__ in _PANDAS_MISSING_TYPE_NAMES:
        return True
    return isinstance(value, float) and math.isnan(value)


def _as_day(value: object, what: str) -> dt.date:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None or value.time() != _MIDNIGHT:
            raise ListingError(f"{what}: {value!r} carries a time of day or a time zone")
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _ISO_DAY.fullmatch(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ListingError(f"{what}: {value!r} is not a calendar day") from exc
    raise ListingError(f"{what}: {value!r} is not a day")


@dataclass(frozen=True)
class YearEndListing:
    """One year-end master response together with the evidence that its day is the year end.

    ``rows`` is the response for ``day`` as received, every field included. ``confirmation_effective``
    is the date the API put on the master requested for ``confirmation_requested``, the day after.
    """

    day: dt.date
    confirmation_requested: dt.date
    confirmation_effective: dt.date
    rows: tuple[Mapping[str, object], ...]


def single_effective_day(rows: Iterable[Mapping[str, object]]) -> dt.date:
    """Return the one ``Date`` every row of a master response carries.

    Raises ``ListingError`` for an empty response, a missing ``Date``, or rows dated differently.
    """
    days = set()
    for position, row in enumerate(rows):
        if _is_missing(row.get("Date")):
            raise ListingError(f"row {position}: Date is missing")
        days.add(_as_day(row.get("Date"), f"row {position}: Date"))
    if len(days) != 1:
        raise ListingError(f"expected one effective date, got {sorted(day.isoformat() for day in days)}")
    return days.pop()


def completed_year_ends(sessions: Iterable[dt.date]) -> dict[int, dt.date]:
    """Map each finished year to its last session in ``sessions``.

    A year counts as finished only when ``sessions`` also holds a session of the following year.
    The result is a candidate: ``sessions`` comes from fetched prices, so a missing final day
    would make an earlier day look like the year end. ``confirm_year_end`` settles it.
    A ``datetime`` at midnight without a time zone counts as its day; any other is refused.
    """
    last_by_year: dict[int, dt.date] = {}
    for session in (_as_day(value, "session") for value in sessions):
        if session.year not in last_by_year or session > last_by_year[session.year]:
            last_by_year[session.year] = session
    return {year: last for year, last in sorted(last_by_year.items()) if year + 1 in last_by_year}


def confirm_year_end(candidate: dt.date, effective_date_for_next_day: dt.date) -> None:
    """Raise unless the master requested for the day after ``candidate`` is dated in the next year.

    The API answers a non-business day with the next business day's master. When that next
    business day falls in the following year, no session of ``candidate``'s year follows
    ``candidate``. This does not show that ``candidate`` is itself a session: the master
    requested for ``candidate`` must also pass ``normalize_listing``.
    """
    day = _as_day(candidate, "candidate")
    effective = _as_day(effective_date_for_next_day, "effective date")
    if effective.year != day.year + 1:
        raise ListingError(
            f"master for the day after {day} is dated {effective}, not in {day.year + 1}"
        )


def _text(row: Mapping[str, object], field: str, position: int) -> str:
    value = row.get(field)
    if _is_missing(value):
        raise ListingError(f"row {position}: {field} is missing")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ListingError(f"row {position}: {field} is not plain text: {value!r}")
    return value


def normalize_listing(rows: Iterable[Mapping[str, object]], *, requested: dt.date) -> list[dict[str, str]]:
    """Return the rows of one master response as sorted ``code`` / ``market_code`` / ``product_category``.

    Raises ``ListingError`` for an empty response, a row dated other than ``requested``, a missing
    field, a malformed or repeated code, or a market or product code outside the published tables.
    ``Date`` may be a ``YYYY-MM-DD`` string, a ``date``, or a ``datetime`` at midnight without a time
    zone; codes must be text exactly as published. Every other response field (names, sectors) is
    dropped.
    """
    day = _as_day(requested, "requested")
    by_code: dict[str, dict[str, str]] = {}
    for position, row in enumerate(rows):
        if _is_missing(row.get("Date")):
            raise ListingError(f"row {position}: Date is missing")
        row_day = _as_day(row.get("Date"), f"row {position}: Date")
        if row_day != day:
            raise ListingError(f"row {position}: dated {row_day}, requested {day}")
        values = {field: _text(row, field, position) for field in _REQUIRED_RESPONSE_FIELDS[1:]}
        code = values["Code"]
        if not _LISTING_CODE.fullmatch(code):
            raise ListingError(f"row {position}: malformed code {code!r}")
        if code in by_code:
            raise ListingError(f"row {position}: code {code!r} appears twice")
        if values["Mkt"] not in KNOWN_MARKET_CODES:
            raise ListingError(f"row {position}: unknown market code {values['Mkt']!r}")
        if values["ProdCat"] not in KNOWN_PRODUCT_CATEGORIES:
            raise ListingError(f"row {position}: unknown product category {values['ProdCat']!r}")
        by_code[code] = {"code": code, "market_code": values["Mkt"], "product_category": values["ProdCat"]}
    if not by_code:
        raise ListingError(f"empty master response for {day}")
    return [by_code[code] for code in sorted(by_code)]


def listing_document(rows: Iterable[Mapping[str, object]], *, requested: dt.date) -> bytes:
    """Serialise one checked master response as canonical JSON bytes.

    The same response yields the same bytes whatever the row order: keys are sorted, separators
    are compact, the text is UTF-8 and there is no trailing newline.
    """
    day = _as_day(requested, "requested")
    document = {
        "schema": LISTING_DOCUMENT_SCHEMA,
        "requested_date": day.isoformat(),
        "effective_date": day.isoformat(),
        "rows": normalize_listing(rows, requested=day),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
