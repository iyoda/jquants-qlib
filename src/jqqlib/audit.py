"""XTKS session calendar gap audit against stored data."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .trading_calendar import is_mon_fri, is_session


@dataclass(frozen=True)
class GapAuditResult:
    """Outcome of a recent-window gap audit.

    ``real_gaps`` are XTKS sessions the dataset should have but does not — the
    only failing condition. ``suppressed_non_sessions`` are the exchange
    holidays inside the same window that the previous Mon-Fri heuristic would
    have reported as gaps; they are published so the suppression is auditable
    rather than invisible.
    """

    real_gaps: list[dt.date] = field(default_factory=list)
    suppressed_non_sessions: list[dt.date] = field(default_factory=list)
    window_business_days: int = 0


def find_recent_business_day_gaps(
    dates_present: Iterable[dt.date],
    as_of: dt.date,
    window_business_days: int,
    *,
    dataset_start: dt.date | None = None,
    dataset_end: dt.date | None = None,
) -> GapAuditResult:
    """Return the XTKS sessions missing from the dataset within the recent window.

    The window counts XTKS trading sessions walking back from ``as_of``
    (including ``as_of`` itself when it is a session), never weekdays.

    Only flags sessions that fall inside the dataset's covered span
    ``[dataset_start, dataset_end]`` -- it never flags the leading edge before the
    data begins, and never flags days after the dataset end (e.g. today may have
    no bar yet on a holiday or pre-market run). This makes the audit fail-safe:
    it surfaces genuine interior holes without raising false alarms when there is
    simply no expected business day yet.

    Raises ``ExactCalendarRequired`` when the XTKS calendar is unavailable: an
    audit that cannot tell a holiday from a hole must fail, not guess.
    """
    if window_business_days < 0:
        raise ValueError("window_business_days must be 0 or greater.")

    present = set(dates_present)
    if not present:
        return GapAuditResult(window_business_days=window_business_days)

    start = dataset_start if dataset_start is not None else min(present)
    end = dataset_end if dataset_end is not None else max(present)

    gaps: list[dt.date] = []
    suppressed: list[dt.date] = []
    cursor = as_of
    yielded = 0
    while yielded <= window_business_days:
        in_span_and_absent = start <= cursor <= end and cursor not in present
        if is_session(cursor):
            yielded += 1
            if in_span_and_absent:
                gaps.append(cursor)
        elif is_mon_fri(cursor) and in_span_and_absent:
            # A weekday that is not an XTKS session: the old heuristic would
            # have called this a gap. Record the suppression.
            suppressed.append(cursor)
        cursor -= dt.timedelta(days=1)

    return GapAuditResult(
        real_gaps=sorted(gaps),
        suppressed_non_sessions=sorted(suppressed),
        window_business_days=window_business_days,
    )


def read_dataset_dates(parquet_path: Path) -> set[dt.date]:
    """Read the distinct calendar dates present in the parquet dataset."""
    import pandas as pd

    if not parquet_path.exists():
        return set()
    df = pd.read_parquet(parquet_path, columns=["date"])
    if df.empty:
        return set()
    return set(pd.to_datetime(df["date"]).dt.date)


def audit_dataset_gaps(
    parquet_path: Path,
    as_of: dt.date,
    window_business_days: int,
) -> GapAuditResult:
    """Detect recent missing XTKS sessions in the parquet dataset.

    Returns an empty result when the dataset is absent/empty (validation owns the
    "missing dataset" failure; audit only reports interior gaps).
    """
    dates = read_dataset_dates(parquet_path)
    if not dates:
        return GapAuditResult(window_business_days=window_business_days)
    return find_recent_business_day_gaps(
        dates,
        as_of,
        window_business_days,
        dataset_start=min(dates),
        dataset_end=max(dates),
    )


def write_audit_sidecar(
    result: GapAuditResult,
    as_of: dt.date,
    *,
    path: Path,
    run_started_at: str = "",
) -> Path:
    """Atomically write the audit sidecar an external monitor may read.

    ``run_started_at`` ties the sidecar to one daily-import run so that a
    consumer can only match it against the run it belongs to; a leftover
    sidecar from an earlier run can never be read as fresh.
    """
    document = {
        "as_of": as_of.isoformat(),
        "window_business_days": result.window_business_days,
        "real_gaps": [d.isoformat() for d in result.real_gaps],
        "suppressed_non_sessions": [d.isoformat() for d in result.suppressed_non_sessions],
        "run_started_at": run_started_at or "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return path
