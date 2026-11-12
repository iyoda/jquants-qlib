"""Point-in-time listing master: year-end dates, response checks and the per-date document.

The J-Quants listing master (``/equities/master``) answers "which issues were listed on this
date, on which market, as which product". This module holds the pure rules for turning one such
response into the document a research reader can trust. It performs no I/O.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping

LISTING_DOCUMENT_SCHEMA = 1
KNOWN_PRODUCT_CATEGORIES = frozenset({"011", "012", "013", "014", "021", "022", "023", "024"})
KNOWN_MARKET_CODES = frozenset(
    {"0101", "0102", "0104", "0105", "0106", "0107", "0109", "0111", "0112", "0113"}
)
_REQUIRED_RESPONSE_FIELDS = ("Date", "Code", "Mkt", "ProdCat")
_LISTING_CODE = re.compile(r"[0-9A-Z]{5}", re.ASCII)


class ListingError(ValueError):
    """Raised when a listing response or a year-end date cannot be trusted."""


def completed_year_ends(sessions: Iterable[dt.date]) -> dict[int, dt.date]:
    """Map each finished year to its last session in ``sessions``.

    A year counts as finished only when ``sessions`` also holds a session of the following year.
    The result is a candidate: ``sessions`` comes from fetched prices, so a missing final day
    would make an earlier day look like the year end. ``confirm_year_end`` settles it.
    """
    last_by_year: dict[int, dt.date] = {}
    for session in sessions:
        if session.year not in last_by_year or session > last_by_year[session.year]:
            last_by_year[session.year] = session
    return {year: last for year, last in sorted(last_by_year.items()) if year + 1 in last_by_year}


def confirm_year_end(candidate: dt.date, effective_date_for_next_day: dt.date) -> None:
    """Raise unless the master requested for the day after ``candidate`` is dated in a later year.

    The API answers a non-business day with the next business day's master. When that next
    business day falls in a later year, no session of ``candidate``'s year follows ``candidate``.
    """
    if effective_date_for_next_day <= candidate:
        raise ListingError(
            f"master for the day after {candidate} is dated {effective_date_for_next_day}, not later"
        )
    if effective_date_for_next_day.year == candidate.year:
        raise ListingError(
            f"{candidate} is not the last session of {candidate.year}: "
            f"{effective_date_for_next_day} is a later session of the same year"
        )


def _text(row: Mapping[str, object], field: str, position: int) -> str:
    value = row.get(field)
    if value is None or value != value or not str(value).strip():
        raise ListingError(f"row {position}: {field} is missing")
    return str(value).strip()


def normalize_listing(rows: Iterable[Mapping[str, object]], *, requested: dt.date) -> list[dict[str, str]]:
    """Return the rows of one master response as sorted ``code`` / ``market_code`` / ``product_category``.

    Raises ``ListingError`` for an empty response, a row dated other than ``requested``, a missing
    field, a malformed or repeated code, or a market or product code outside the published tables.
    Every other response field (names, sectors) is dropped.
    """
    by_code: dict[str, dict[str, str]] = {}
    for position, row in enumerate(rows):
        values = {field: _text(row, field, position) for field in _REQUIRED_RESPONSE_FIELDS}
        if values["Date"][:10] != requested.isoformat():
            raise ListingError(f"row {position}: dated {values['Date'][:10]}, requested {requested}")
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
        raise ListingError(f"empty master response for {requested}")
    return [by_code[code] for code in sorted(by_code)]


def listing_document(rows: Iterable[Mapping[str, object]], *, requested: dt.date) -> bytes:
    """Serialise one checked master response as canonical JSON bytes.

    The same response yields the same bytes whatever the row order: keys are sorted, separators
    are compact, the text is UTF-8 and there is no trailing newline.
    """
    document = {
        "schema": LISTING_DOCUMENT_SCHEMA,
        "requested_date": requested.isoformat(),
        "effective_date": requested.isoformat(),
        "rows": normalize_listing(rows, requested=requested),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
