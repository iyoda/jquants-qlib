"""Write-once storage of year-end listing master responses, one file per day."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
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
    if type(value) in (str, bool, int, float):
        return value
    raise ListingError(f"row {position}: {field} has an unsupported type {type(value).__name__}")


def _check(listing: YearEndListing) -> None:
    if listing.confirmation_requested != listing.day + dt.timedelta(days=1):
        raise ListingError(f"the confirmation is not for the day after {listing.day}")
    normalize_listing(listing.rows, requested=listing.day)
    confirm_year_end(listing.day, listing.confirmation_effective)


def _raw_bytes(listing: YearEndListing) -> bytes:
    _check(listing)
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


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_raw_listing(raw_dir: Path, listing: YearEndListing) -> Path:
    """Store ``listing`` under ``raw_dir`` and return its path; a stored day is never replaced.

    The response is checked again before anything is written. The file is completed under a
    temporary name and then given its final name by a hard link, which fails when that name is
    taken, so two writers cannot both win. Storing the same response again changes nothing. A
    different response for a stored day raises ``ListingError`` and leaves the stored file as it
    is: the API's history is a rolling window, so the first checked copy is the one a later
    rebuild must reproduce. The file and its directory entry are flushed to disk before returning.
    """
    payload = _raw_bytes(listing)
    path = raw_listing_path(raw_dir, listing.day)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                stored = path.read_bytes()
            except OSError as exc:
                raise ListingError(f"{path.name} exists but cannot be read") from exc
            if stored != payload:
                raise ListingError(f"{path.name} is already stored with different content") from None
            return path
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(path.parent)
    return path


def _stored_day(document: Mapping[str, object], key: str, path: Path) -> dt.date:
    return _as_day(document.get(key), f"{path.name}: {key}")


def read_raw_listing(raw_dir: Path, day: dt.date) -> YearEndListing:
    """Load the stored response for ``day`` and apply the checks a fresh response must pass.

    Raises ``ListingError`` when the file is absent, cannot be read, is not the stored shape, names
    another day, or does not pass ``normalize_listing`` and ``confirm_year_end``. These are consistency
    checks: an edit that keeps the file consistent (a removed row, another known market code) is not
    detected.
    """
    return _checked(_stored_bytes(raw_dir, day), raw_listing_path(raw_dir, day), day)


def checked_raw_listing_bytes(raw_dir: Path, day: dt.date) -> bytes:
    """Return the stored file for ``day`` as the exact bytes that passed ``read_raw_listing``'s checks.

    The file is read once, so a copy made from the result is the content that was checked even when
    the file changes afterwards. Raises ``ListingError`` as ``read_raw_listing`` does.
    """
    data = _stored_bytes(raw_dir, day)
    _checked(data, raw_listing_path(raw_dir, day), day)
    return data


def _stored_bytes(raw_dir: Path, day: dt.date) -> bytes:
    path = raw_listing_path(raw_dir, day)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ListingError(f"no stored listing for {day}") from exc
    except OSError as exc:
        raise ListingError(f"{path.name} cannot be read ({type(exc).__name__})") from exc


def _checked(data: bytes, path: Path, day: dt.date) -> YearEndListing:
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ListingError(f"{path.name} is not valid JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != _RAW_KEYS
        or type(document["schema"]) is not int
        or document["schema"] != RAW_LISTING_SCHEMA
    ):
        raise ListingError(f"{path.name} is not a stored listing of schema {RAW_LISTING_SCHEMA}")
    confirmation, rows = document["confirmation"], document["rows"]
    if not isinstance(confirmation, dict) or set(confirmation) != _CONFIRMATION_KEYS:
        raise ListingError(f"{path.name}: malformed confirmation")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ListingError(f"{path.name}: malformed rows")
    for position, row in enumerate(rows):
        for field, value in row.items():
            if value is not None and type(value) not in (str, bool, int, float):
                raise ListingError(f"{path.name}: row {position}: {field} is not a plain value")
    if _stored_day(document, "requested_date", path) != day or _stored_day(document, "effective_date", path) != day:
        raise ListingError(f"{path.name} does not hold the master for {day}")
    listing = YearEndListing(
        day=day,
        confirmation_requested=_stored_day(confirmation, "requested_date", path),
        confirmation_effective=_stored_day(confirmation, "effective_date", path),
        rows=tuple(rows),
    )
    _check(listing)
    return listing


def stored_listing_days(raw_dir: Path) -> list[dt.date]:
    """Return the days that have a stored file under ``raw_dir``, oldest first.

    A file whose name is not a ``YYYY-MM-DD.json`` day is ignored. Contents are not checked: a day
    counts as usable only after ``read_raw_listing`` accepts it.
    """
    days = []
    for path in Path(raw_dir).glob("*.json"):
        try:
            days.append(_as_day(path.stem, path.name))
        except ListingError:
            continue
    return sorted(days)
