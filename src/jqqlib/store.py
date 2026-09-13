"""Deterministic CSV and Parquet upsert for raw OHLCVA."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .normalize import (
    BOOTSTRAP_CSV_COLUMNS,
    PARQUET_BASE_COLUMNS,
    normalize_daily_quote_columns,
    normalize_for_qlib,
)

if TYPE_CHECKING:
    import pandas as pd


DEFAULT_PARQUET_FILENAME = "daily_quotes.parquet"


class NoDataFetchedError(ValueError):
    """Raised when an API fetch completed but returned no usable rows."""


def _write_parquet_atomic(frame: pd.DataFrame, output_path: Path) -> None:
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        frame.to_parquet(temporary, index=False)
        temporary.replace(output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_parquet(df: pd.DataFrame, parquet_dir: Path, merge_mode: str = "replace") -> Path:
    import pandas as pd

    parquet_dir.mkdir(parents=True, exist_ok=True)

    if df.empty:
        raise NoDataFetchedError("No data fetched from J-Quants API.")

    normalized = normalize_for_qlib(normalize_daily_quote_columns(df))
    output_path = parquet_dir / DEFAULT_PARQUET_FILENAME

    if merge_mode not in {"replace", "upsert"}:
        raise ValueError(f"Unsupported merge_mode: {merge_mode}. Use 'replace' or 'upsert'.")

    to_write = normalized
    if merge_mode == "upsert" and output_path.exists():
        existing = pd.read_parquet(output_path)
        combined = pd.concat([existing, normalized], ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"]).dt.strftime("%Y-%m-%d")
        to_write = combined.drop_duplicates(subset=["symbol", "date"], keep="last").copy()

    if "adjustment_factor" not in to_write.columns:
        to_write["adjustment_factor"] = 1.0
    else:
        to_write["adjustment_factor"] = pd.to_numeric(to_write["adjustment_factor"], errors="coerce").fillna(1.0)
    columns = list(PARQUET_BASE_COLUMNS)
    if "ex_rights_type" in to_write.columns:
        columns.append("ex_rights_type")
    # Deterministic ordering: identical logical content must produce identical
    # bytes so that the parquet content hash (lineage.parquet_sha256) is a stable
    # publish/no-op signal regardless of fetch order or fetch window.
    to_write = to_write[columns].sort_values(["symbol", "date"]).reset_index(drop=True)

    _write_parquet_atomic(to_write, output_path)
    return output_path


def csv_to_parquet(csv_path: Path, parquet_dir: Path) -> Path:
    """Bootstrap helper for initial bulk load from a downloaded CSV file."""
    import pandas as pd

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path, low_memory=False)
    if df.empty:
        raise ValueError(f"CSV file is empty: {csv_path}")

    return write_parquet(df, parquet_dir)


def write_bootstrap_csv(df: pd.DataFrame, output_path: Path) -> Path:
    import pandas as pd

    normalized_df = normalize_daily_quote_columns(df)

    if normalized_df.empty:
        raise NoDataFetchedError("No data fetched from J-Quants API.")

    missing = [col for col in BOOTSTRAP_CSV_COLUMNS if col not in normalized_df.columns]
    if missing:
        raise ValueError(f"Missing required columns for bootstrap CSV: {', '.join(missing)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # The required prefix is fixed, so the CSV shape stays stable. Columns the
    # bulk endpoint may add (the vendor's own adjusted values) are passed through
    # unchanged rather than dropped; the pipeline itself never reads them.
    optional_vendor_columns = [
        column for column in (
            "AdjFactor", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo",
            "AdjustmentFactor", "AdjustmentOpen", "AdjustmentHigh",
            "AdjustmentLow", "AdjustmentClose", "AdjustmentVolume",
            "ExRT",
        ) if column in normalized_df.columns
    ]
    to_write = normalized_df.loc[:, [*BOOTSTRAP_CSV_COLUMNS, *optional_vendor_columns]].copy()
    to_write["Date"] = pd.to_datetime(to_write["Date"]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
    to_write = to_write.sort_values(["Code", "Date"]).drop_duplicates(subset=["Code", "Date"], keep="last")
    to_write.to_csv(output_path, index=False)
    return output_path
