"""Write-once storage of year-end listing master responses, one file per day."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .listing import (
    ListingError,
    YearEndListing,
    _as_day,
    _is_missing,
    confirm_year_end,
    normalize_listing,
)

RAW_LISTING_SCHEMA = 1
_RAW_KEYS = frozenset({"schema", "requested_date", "effective_date", "confirmation", "rows"})
_CONFIRMATION_KEYS = frozenset({"requested_date", "effective_date"})


def raw_listing_path(raw_dir: Path, day: dt.date) -> Path:
    """Return where the response for ``day`` is stored under ``raw_dir``."""
    return Path(raw_dir) / f"{day.isoformat()}.json"


def _raw_value(field: str, value: object, position: int) -> object:
    if _is_missing(value):
        return None
    if field == "Date":
        return _as_day(value, f"row {position}: Date").isoformat()
    if isinstance(value, (str, bool, int, float)):
        return value
    raise ListingError(f"row {position}: {field} has an unsupported type {type(value).__name__}")


def _raw_bytes(listing: YearEndListing) -> bytes:
    normalize_listing(listing.rows, requested=listing.day)
    confirm_year_end(listing.day, listing.confirmation_effective)
    rows = [
        {field: _raw_value(field, value, position) for field, value in row.items()}
        for position, row in enumerate(listing.rows)
    ]
    document = {
        "schema": RAW_LISTING_SCHEMA,
        "requested_date": listing.day.isoformat(),
        "effective_date": listing.day.isoformat(),
        "confirmation": {
            "requested_date": listing.confirmation_requested.isoformat(),
            "effective_date": listing.confirmation_effective.isoformat(),
        },
        "rows": sorted(rows, key=lambda row: str(row["Code"])),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def write_raw_listing(raw_dir: Path, listing: YearEndListing) -> Path:
    """Store ``listing`` under ``raw_dir`` and return its path; a stored day is never rewritten.

    The response is checked again before anything is written. Storing the same response twice
    changes nothing. A different response for a stored day raises ``ListingError`` and leaves the
    stored file as it is: the API's history is a rolling window, so the first checked copy is the
    one a later rebuild must reproduce.
    """
    payload = _raw_bytes(listing)
    path = raw_listing_path(raw_dir, listing.day)
    if path.exists():
        if path.read_bytes() != payload:
            raise ListingError(f"{path.name} is already stored with different content")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def _stored_day(document: Mapping[str, object], key: str, path: Path) -> dt.date:
    return _as_day(document.get(key), f"{path.name}: {key}")


def read_raw_listing(raw_dir: Path, day: dt.date) -> YearEndListing:
    """Load the stored response for ``day`` and check it as strictly as a fresh one.

    Raises ``ListingError`` when the file is absent, is not the stored shape, names another day, or
    no longer passes ``normalize_listing`` and ``confirm_year_end``.
    """
    path = raw_listing_path(raw_dir, day)
    try:
        document = json.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise ListingError(f"no stored listing for {day}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ListingError(f"{path.name} is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != _RAW_KEYS or document["schema"] != RAW_LISTING_SCHEMA:
        raise ListingError(f"{path.name} is not a stored listing of schema {RAW_LISTING_SCHEMA}")
    confirmation, rows = document["confirmation"], document["rows"]
    if not isinstance(confirmation, dict) or set(confirmation) != _CONFIRMATION_KEYS:
        raise ListingError(f"{path.name}: malformed confirmation")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ListingError(f"{path.name}: malformed rows")
    if _stored_day(document, "requested_date", path) != day or _stored_day(document, "effective_date", path) != day:
        raise ListingError(f"{path.name} does not hold the master for {day}")
    listing = YearEndListing(
        day=day,
        confirmation_requested=_stored_day(confirmation, "requested_date", path),
        confirmation_effective=_stored_day(confirmation, "effective_date", path),
        rows=tuple(rows),
    )
    if listing.confirmation_requested != day + dt.timedelta(days=1):
        raise ListingError(f"{path.name}: the confirmation is not for the day after {day}")
    normalize_listing(listing.rows, requested=day)
    confirm_year_end(day, listing.confirmation_effective)
    return listing


def stored_listing_days(raw_dir: Path) -> list[dt.date]:
    """Return the days that have a stored file under ``raw_dir``, oldest first.

    A file whose name is not a ``YYYY-MM-DD.json`` day is ignored; contents are not checked here.
    """
    days = []
    for path in Path(raw_dir).glob("*.json"):
        try:
            days.append(_as_day(path.stem, path.name))
        except ListingError:
            continue
    return sorted(days)
