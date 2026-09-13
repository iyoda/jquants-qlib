"""Parquet to Qlib dataset conversion, applying the vendor factor at dump time by default."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .adjust import apply_vendor_adjustment
from .config import QLIB_ADJUSTMENT_VENDOR_FACTOR, VALID_QLIB_ADJUSTMENTS
from .normalize import QLIB_FIELDS, QLIB_FIELDS_WITH_FACTOR
from .validate import validate_qlib_readiness

GENERATED_QLIB_SUBTREES = ("features", "calendars", "instruments")
SUPPORTED_QLIB_FIELDS = frozenset(QLIB_FIELDS_WITH_FACTOR)


def _validate_generated_qlib_child(qlib_dir: Path, name: str) -> Path:
    base = qlib_dir.resolve()
    child = qlib_dir / name
    if not child.exists() and not child.is_symlink():
        return child

    if child.is_symlink() or not child.is_dir():
        raise FileExistsError(f"Refusing to replace non-directory qlib path: {child}")

    resolved_child = child.resolve()
    if resolved_child.parent != base:
        raise RuntimeError(f"Refusing to replace non-child qlib path: {child}")

    return child


def _replace_generated_qlib_subtrees(staged_qlib_dir: Path, qlib_dir: Path) -> None:
    qlib_dir.mkdir(parents=True, exist_ok=True)
    staged_children = []
    for name in GENERATED_QLIB_SUBTREES:
        source = staged_qlib_dir / name
        if source.is_symlink() or not source.is_dir():
            raise RuntimeError(f"Qlib dump did not create generated subtree: {source}")
        staged_children.append((name, source, _validate_generated_qlib_child(qlib_dir, name)))

    backup_root = Path(tempfile.mkdtemp(prefix=".qlib_generated_backup_", dir=str(qlib_dir)))
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for name, _, target in staged_children:
            if target.exists():
                backup = backup_root / name
                target.rename(backup)
                backups.append((target, backup))

        for _, source, target in staged_children:
            source.rename(target)
            installed.append(target)
    except Exception:
        for target in reversed(installed):
            if target.exists():
                shutil.rmtree(target)
        for target, backup in reversed(backups):
            if backup.exists() and not target.exists():
                backup.rename(target)
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def _requested_fields(
    fields: list[str] | tuple[str, ...] | None,
    *,
    default: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    requested = tuple(default if fields is None else fields)
    if not requested:
        raise ValueError("At least one Qlib field must be requested.")
    duplicates = sorted({field for field in requested if requested.count(field) > 1})
    if duplicates:
        raise ValueError(f"Duplicate Qlib fields requested: {', '.join(duplicates)}")
    unknown = sorted(set(requested) - SUPPORTED_QLIB_FIELDS)
    if unknown:
        raise ValueError(f"Unknown Qlib fields requested: {', '.join(unknown)}")
    return requested


def _preflight_parquet(parquet_path: Path, fields: tuple[str, ...]):
    import pandas as pd

    if not parquet_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet_path}")

    df = pd.read_parquet(parquet_path)
    if df.empty:
        raise ValueError(f"Parquet file is empty: {parquet_path}")

    required_cols = ["symbol", "date", *fields]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing columns for Qlib conversion: {', '.join(missing_cols)}")
    return df


def _dump_frame_to_staging(df, staged_qlib_dir: Path, fields: tuple[str, ...]) -> None:
    from .qlib_dump import DumpDataAll

    with tempfile.TemporaryDirectory(prefix="qlib_csv_") as tmp_dir:
        csv_dir = Path(tmp_dir)

        staged_files = 0
        for symbol, symbol_df in df.groupby("symbol"):
            out_csv = csv_dir / f"{symbol}.csv"
            symbol_df[["date", *fields]].sort_values("date").to_csv(out_csv, index=False)
            staged_files += 1

        if staged_files == 0:
            raise ValueError("No symbol data staged for Qlib conversion.")

        DumpDataAll(
            data_path=str(csv_dir),
            qlib_dir=str(staged_qlib_dir),
            include_fields=fields,
            date_field_name="date",
        ).dump()


def parquet_to_qlib(
    parquet_path: Path,
    qlib_dir: Path,
    fields: list[str] | tuple[str, ...] | None = None,
    *,
    adjustment: str = QLIB_ADJUSTMENT_VENDOR_FACTOR,
) -> None:
    """Build one Qlib provider from a Parquet file.

    ``adjustment="vendor_factor"`` (default) dumps split/reverse-split/rights-issue
    adjusted prices plus ``factor``. ``adjustment="none"`` dumps the six raw fields.
    """
    if adjustment not in VALID_QLIB_ADJUSTMENTS:
        allowed = ", ".join(sorted(VALID_QLIB_ADJUSTMENTS))
        raise ValueError(f"qlib.adjustment must be one of: {allowed}.")
    default_fields = QLIB_FIELDS_WITH_FACTOR if adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR else QLIB_FIELDS
    requested = _requested_fields(fields, default=default_fields)
    parquet_needed = tuple(field for field in requested if field != "factor") or tuple(QLIB_FIELDS)
    df = _preflight_parquet(parquet_path, parquet_needed)
    if adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR:
        errors = validate_qlib_readiness(parquet_path)
        if errors:
            raise RuntimeError("Parquet is not Qlib-ready: " + "; ".join(errors))
        df = apply_vendor_adjustment(df)
    elif "factor" in requested:
        raise ValueError("factor is not dumped when qlib.adjustment is none.")
    qlib_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qlib_build_", dir=str(qlib_dir.parent)) as build_dir:
        staged_qlib_dir = Path(build_dir) / "qlib"
        _dump_frame_to_staging(df, staged_qlib_dir, requested)
        _replace_generated_qlib_subtrees(staged_qlib_dir, qlib_dir)
