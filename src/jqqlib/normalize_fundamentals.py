"""As-of/PIT fundamentals panel and fail-closed PIT audit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


# Numeric fundamental fields to carry (V2 /fins/summary columns). The set was
# validated against a real pull (483/500 universe codes, pit_violations == 0)
# before being fixed here.
CARRY_NUM = [
    "NP", "FNP", "NxFNP", "Eq", "EPS", "DEPS", "FEPS", "BPS",
    "Sales", "OP", "TA", "CFO", "ShOutFY", "TrShFY", "AvgSh",
]
CARRY_STR = ["CurPerType"]

SRC_DISC_DATE_COLUMN = "_src_DiscDate"


def normalize_fundamentals(raw: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Build the as-of (point-in-time) fundamentals panel.

    PIT rule (conservative, look-ahead-free): a disclosure dated ``DiscDate`` D
    becomes effective on the first trading day STRICTLY AFTER D (``DiscTime`` is
    ignored for this -- the conservative choice), then is forward-filled per
    code until the next disclosure supersedes it. Rows before a code's first
    disclosure are dropped (no fundamental is known as-of yet).

    Disclosures of one code that take effect on the same day (e.g. results and
    a forecast revision on one ``DiscDate``) are merged field by field in
    ``DiscDate`` / ``DiscTime`` / ``DiscNo`` order: the latest non-null value
    of each field wins, as the forward fill does across days.

    Returns a frame indexed by a 2-level ``["datetime", "instrument"]``
    MultiIndex (StaticDataLoader-ready), with a ``_src_DiscDate`` provenance
    column -- audit-only, must never be used as a feature (it is a future date
    relative to the trading days it was forward-filled onto before its own
    effective date, which is by construction, not a leak; see audit_pit).
    """
    import numpy as np
    import pandas as pd

    if raw.empty:
        raise ValueError("normalize_fundamentals received an empty raw fundamentals frame.")

    cal = pd.DatetimeIndex(sorted(pd.DatetimeIndex(calendar).unique()))
    cal_arr = cal.values

    working = raw.copy()
    working["Code"] = working["Code"].astype(str)
    working["DiscDate"] = pd.to_datetime(working["DiscDate"])
    for col in CARRY_NUM:
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce")
    # A blank string carries no value: make it null so it neither overrides a
    # same-day disclosure nor the forward-filled one.
    for col in CARRY_STR:
        if col in working.columns:
            blank = (working[col].astype("string").str.strip() == "").fillna(False)
            working[col] = working[col].mask(blank)

    # effective date = first trading day STRICTLY AFTER DiscDate (no same-day use)
    pos = np.searchsorted(cal_arr, working["DiscDate"].to_numpy(), side="right")
    in_cal = pos < len(cal_arr)
    working = working[in_cal].copy()
    working["eff_date"] = cal_arr[pos[in_cal]]

    present_num = [c for c in CARRY_NUM if c in working.columns]
    present_str = [c for c in CARRY_STR if c in working.columns]
    keep = ["Code", "eff_date", "DiscDate"] + present_num + present_str
    # Disclosure order within a day: DiscTime, then DiscNo (as an integer,
    # compared exactly by digit count then digits); a stable sort keeps the
    # input order only for disclosures that tie on all of them. A missing or
    # unusable key sorts first, so it never outranks a properly keyed row.
    order = ["Code", "DiscDate"]
    if "DiscTime" in working.columns:
        disc_time = working["DiscTime"].astype("string").str.strip()
        working["_disc_time"] = disc_time.mask(disc_time == "")
        order.append("_disc_time")
    if "DiscNo" in working.columns:
        disc_no = working["DiscNo"].astype("string").str.strip()
        digits = disc_no.where(disc_no.str.fullmatch(r"\d+").fillna(False)).str.lstrip("0").replace("", "0")
        working["_disc_no_len"] = digits.str.len()
        working["_disc_no"] = digits
        order += ["_disc_no_len", "_disc_no"]
    # Same-day disclosures merge field by field (GroupBy.last skips nulls), so
    # a forecast revision does not drop the results published alongside it.
    ev = (
        working.sort_values(order, kind="mergesort", na_position="first")[keep]
        .groupby(["Code", "eff_date"], sort=False)
        .last()
        .reset_index()
    )

    frames = []
    for code, group in ev.groupby("Code", sort=False):
        g = group.drop(columns=["Code"]).set_index("eff_date").sort_index()
        g = g.reindex(cal).ffill()
        g["instrument"] = code
        frames.append(g)

    panel = pd.concat(frames)
    panel.index.name = "datetime"
    panel = panel.rename(columns={"DiscDate": SRC_DISC_DATE_COLUMN})
    # Drop rows before a code's first disclosure (no fundamental as-of yet).
    panel = panel[panel[SRC_DISC_DATE_COLUMN].notna()].copy()
    panel = panel.set_index("instrument", append=True).reorder_levels(
        ["datetime", "instrument"]
    ).sort_index()
    return panel


def audit_pit(panel: pd.DataFrame) -> dict[str, Any]:
    """Compute the PIT audit report. Does not raise -- callers decide via
    ``assert_pit_audit_passes`` whether to fail closed on the result."""
    dt_level = panel.index.get_level_values("datetime")
    src = panel[SRC_DISC_DATE_COLUMN]
    violations = int(((src.notna()) & (src.values >= dt_level)).sum())
    n_codes = panel.index.get_level_values("instrument").nunique()

    return {
        "pit_rule": (
            "effective = first trading day strictly AFTER DiscDate; forward-filled; "
            "DiscTime ignored (conservative)."
        ),
        "panel_cells": int(len(panel)),
        "codes_with_fundamentals": int(n_codes),
        "datetime_min": str(dt_level.min().date()) if len(panel) else None,
        "datetime_max": str(dt_level.max().date()) if len(panel) else None,
        "pit_violations": violations,
        "pit_audit_pass": violations == 0,
    }


class PitAuditError(RuntimeError):
    """Raised when the fail-closed PIT audit detects look-ahead violations."""


def assert_pit_audit_passes(audit: dict[str, Any]) -> None:
    """Fail-closed gate: any look-ahead violation aborts the run rather than
    silently publishing a panel that could leak future information into a
    factor evaluation. This is a hard stop by design and must never be
    weakened to a warning."""
    if audit["pit_violations"]:
        raise PitAuditError(
            f"PIT AUDIT FAILED: {audit['pit_violations']} look-ahead violation(s) detected."
        )
