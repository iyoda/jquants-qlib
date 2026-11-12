"""Keep year-end listing masters for a provider: fetch the missing ones and write the provider files."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .fetch import _RequestPacer
from .fetch_listing import fetch_year_end_listing
from .listing import ListingError, completed_year_ends, listing_document
from .store_listing import raw_listing_path, read_raw_listing, write_raw_listing

LISTING_SUBTREE = "listing"
LISTING_MANIFEST_SCHEMA_VERSION = 1
REASON_NOT_STORED = "not_stored"
REASON_UNREADABLE = "unreadable"
REASON_FETCH_FAILED = "fetch_failed"
LISTING_ABSENT = "absent"
LISTING_REMOVED = "removed"
LISTING_KEPT = "kept"
_SCRATCH_PREFIXES = (f".{LISTING_SUBTREE}.tmp-", f".{LISTING_SUBTREE}.old-")


def _is_stored(raw_dir: Path, day: dt.date) -> bool:
    try:
        os.lstat(raw_listing_path(raw_dir, day))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def provider_sessions(qlib_dir: Path) -> list[dt.date]:
    """Return the sessions in the provider's day calendar, or none when the provider has no calendar."""
    calendar = Path(qlib_dir) / "calendars" / "day.txt"
    if not calendar.is_file():
        return []
    lines = calendar.read_text(encoding="utf-8").splitlines()
    return [dt.date.fromisoformat(line.strip()) for line in lines if line.strip()]


def target_year_ends(sessions: Iterable[dt.date], first_year: int | None) -> list[dt.date]:
    """Return the candidate year ends of the finished years in ``sessions``, from ``first_year`` on, oldest first."""
    return [
        day
        for year, day in completed_year_ends(sessions).items()
        if first_year is None or year >= first_year
    ]


def fetch_missing_year_ends(
    client: Any,
    days: Iterable[dt.date],
    raw_dir: Path,
    *,
    request_interval_sec: float,
    retries: int,
    retry_base_delay_sec: float,
    retry_max_delay_sec: float,
    retry_jitter_sec: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[dt.date, str]:
    """Fetch and store every day of ``days`` that has no stored file; return the failures by day.

    A day with a stored file is not requested again, even when the file is unreadable: a stored day is
    never replaced. A day whose stored name cannot be examined counts as stored. One day's failure (a
    refused response, a network or authentication error, a failed write) does not stop the others; its
    value names the exception type. The request interval holds across the days as well as within one.
    """
    failures: dict[dt.date, str] = {}
    pacer = _RequestPacer(request_interval_sec, sleeper)
    for day in days:
        if _is_stored(raw_dir, day):
            continue
        try:
            listing = fetch_year_end_listing(
                client,
                day,
                request_interval_sec=request_interval_sec,
                retries=retries,
                retry_base_delay_sec=retry_base_delay_sec,
                retry_max_delay_sec=retry_max_delay_sec,
                retry_jitter_sec=retry_jitter_sec,
                sleeper=sleeper,
                pacer=pacer,
            )
            write_raw_listing(raw_dir, listing)
        except Exception as exc:
            failures[day] = type(exc).__name__
    return failures


def _remove_scratch(provider: Path) -> None:
    for entry in provider.iterdir():
        if entry.name.startswith(_SCRATCH_PREFIXES):
            shutil.rmtree(entry, ignore_errors=True)


def _replace_subtree(provider: Path, staged: Path, *, keep: bool) -> None:
    current = provider / LISTING_SUBTREE
    retired = provider / f".{LISTING_SUBTREE}.old-{uuid.uuid4().hex}"
    had_current = os.path.lexists(current)
    if had_current:
        current.rename(retired)
    try:
        if keep:
            staged.rename(current)
    except BaseException:
        if had_current:
            retired.rename(current)
        raise
    if had_current:
        shutil.rmtree(retired, ignore_errors=True)


def remove_recorded_listings(qlib_dir: Path, recorded: Mapping[str, str]) -> str:
    """Remove ``<qlib_dir>/listing`` when every file in it is one ``recorded`` names; say what was done.

    ``recorded`` maps provider-relative paths to SHA-256, as the manifest of the build that wrote the
    subtree lists them. Returns ``"absent"`` when there is no ``listing`` entry, ``"removed"`` when
    the subtree was a real directory holding only recorded files with those hashes, and ``"kept"``
    otherwise: something this package cannot show it wrote is never deleted. An empty directory has
    nothing to show and counts as absent. The files are deleted in place, so a deletion that fails
    part way raises ``OSError`` and leaves the rest under ``listing``.
    """
    provider = Path(qlib_dir).resolve()
    current = provider / LISTING_SUBTREE
    if not os.path.lexists(current):
        return LISTING_ABSENT
    if current.is_symlink() or not current.is_dir():
        return LISTING_KEPT
    entries = list(current.iterdir())
    if not entries:
        return LISTING_ABSENT
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            return LISTING_KEPT
        expected = recorded.get(f"{LISTING_SUBTREE}/{entry.name}")
        if expected is None or hashlib.sha256(entry.read_bytes()).hexdigest() != expected:
            return LISTING_KEPT
    for entry in entries:
        entry.unlink()
    current.rmdir()
    return LISTING_REMOVED


def write_provider_listings(
    raw_dir: Path,
    qlib_dir: Path,
    days: Iterable[dt.date],
    fetch_failures: dict[dt.date, str] | None = None,
) -> dict[str, Any]:
    """Rebuild ``<qlib_dir>/listing`` from the stored responses and return the manifest block describing it.

    Each day of ``days`` whose stored response passes ``read_raw_listing`` becomes
    ``listing/<YYYY-MM-DD>.json`` (the ``listing_document`` bytes). The subtree is replaced as a whole,
    so it holds exactly the files the block lists; with no usable day it is removed. A day without a
    usable file is listed under ``unavailable`` with ``not_stored``, ``unreadable`` (a stored file that
    cannot be read or fails the checks) or, when ``fetch_failures`` names it, ``fetch_failed`` and the
    exception type. A failure to write under ``qlib_dir`` raises ``OSError``; the subtree that was there
    is put back first.
    """
    provider = Path(qlib_dir).resolve()
    provider.mkdir(parents=True, exist_ok=True)
    _remove_scratch(provider)
    ordered = sorted(set(days))
    staged = provider / f".{LISTING_SUBTREE}.tmp-{uuid.uuid4().hex}"
    staged.mkdir()
    files: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    try:
        for day in ordered:
            try:
                document = listing_document(read_raw_listing(raw_dir, day).rows, requested=day)
            except ListingError:
                if _is_stored(raw_dir, day):
                    unavailable.append({"day": day.isoformat(), "reason": REASON_UNREADABLE})
                elif fetch_failures and day in fetch_failures:
                    unavailable.append(
                        {"day": day.isoformat(), "reason": REASON_FETCH_FAILED, "error": fetch_failures[day]}
                    )
                else:
                    unavailable.append({"day": day.isoformat(), "reason": REASON_NOT_STORED})
                continue
            (staged / f"{day.isoformat()}.json").write_bytes(document)
            files.append(
                {
                    "day": day.isoformat(),
                    "path": f"{LISTING_SUBTREE}/{day.isoformat()}.json",
                    "sha256": hashlib.sha256(document).hexdigest(),
                }
            )
        _replace_subtree(provider, staged, keep=bool(files))
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return {
        "schema_version": LISTING_MANIFEST_SCHEMA_VERSION,
        "target_days": [day.isoformat() for day in ordered],
        "files": files,
        "unavailable": unavailable,
    }
