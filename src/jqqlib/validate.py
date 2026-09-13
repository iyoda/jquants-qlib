"""Date-range checks and Qlib conversion readiness validation."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .normalize import PARQUET_BASE_COLUMNS, QLIB_FIELDS

OHLCV_FIELDS = ["open", "high", "low", "close", "volume"]


def validate_date_range(start_date: dt.date, end_date: dt.date) -> None:
    if start_date > end_date:
        raise ValueError(f"Invalid date range: start ({start_date}) must be <= end ({end_date}).")


def validate_qlib_readiness(parquet_path: Path) -> list[str]:
    """Validate whether parquet data satisfies dump_bin input expectations.

    Checks:
    - required columns exist
    - date is parseable
    - symbol is non-empty
    - OHLCV/amount columns are numeric when non-null
    - partial OHLCV null rows are rejected as incomplete bars
    """
    errors: list[str] = []

    if not parquet_path.exists():
        return [f"Parquet file not found: {parquet_path}"]

    import pandas as pd

    df = pd.read_parquet(parquet_path)
    if df.empty:
        return [f"Parquet file is empty: {parquet_path}"]

    required_cols = list(PARQUET_BASE_COLUMNS)
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        errors.append(f"Missing columns: {', '.join(missing_cols)}")
        return errors

    parsed_date = pd.to_datetime(df["date"], errors="coerce")
    invalid_date_count = int(parsed_date.isna().sum())
    if invalid_date_count:
        errors.append(f"Invalid date rows: {invalid_date_count}")

    symbol_series = df["symbol"].astype(str).str.strip()
    empty_symbol_count = int((symbol_series == "").sum())
    if empty_symbol_count:
        errors.append(f"Empty symbol rows: {empty_symbol_count}")

    for field in QLIB_FIELDS:
        converted = pd.to_numeric(df[field], errors="coerce")
        invalid_count = int((df[field].notna() & converted.isna()).sum())
        if invalid_count:
            errors.append(f"Non-numeric {field} rows: {invalid_count}")

    adjustment = pd.to_numeric(df["adjustment_factor"], errors="coerce")
    null_adjustment = int(df["adjustment_factor"].isna().sum())
    if null_adjustment:
        errors.append(f"Null adjustment_factor rows: {null_adjustment}")
    non_numeric_adjustment = int((df["adjustment_factor"].notna() & adjustment.isna()).sum())
    if non_numeric_adjustment:
        errors.append(f"Non-numeric adjustment_factor rows: {non_numeric_adjustment}")
    non_finite_adjustment = int(((adjustment == float("inf")) | (adjustment == float("-inf"))).sum())
    if non_finite_adjustment:
        errors.append(f"Non-finite adjustment_factor rows: {non_finite_adjustment}")
    non_positive_adjustment = int((adjustment.notna() & (adjustment <= 0)).sum())
    if non_positive_adjustment:
        errors.append(f"Non-positive adjustment_factor rows: {non_positive_adjustment}")

    ohlcv_nulls = df[OHLCV_FIELDS].isna()
    partial_ohlcv_null_rows = int((ohlcv_nulls.any(axis=1) & ~ohlcv_nulls.all(axis=1)).sum())
    if partial_ohlcv_null_rows:
        errors.append(f"Rows with partial OHLCV null fields: {partial_ohlcv_null_rows}")

    duplicate_rows = int(df.duplicated(subset=["symbol", "date"]).sum())
    if duplicate_rows:
        errors.append(f"Duplicate (symbol, date) rows: {duplicate_rows}")

    return errors
