"""Bootstrap CSV source manifest, dataset manifest, and quality report."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .adjust import apply_vendor_adjustment
from .config import DEFAULT_QLIB_ADJUSTMENT, QLIB_ADJUSTMENT_NONE, QLIB_ADJUSTMENT_VENDOR_FACTOR
from .normalize import QLIB_FIELDS, QLIB_FIELDS_WITH_FACTOR

DATASET_MANIFEST_FILENAME = "dataset_manifest.json"
DATASET_QUALITY_REPORT_FILENAME = "dataset_quality_report.json"
CSV_SOURCE_MANIFEST_SUFFIX = ".manifest.json"
DATASET_MANIFEST_SCHEMA_VERSION = 2
DATASET_QUALITY_REPORT_SCHEMA_VERSION = 2
OHLCV_FIELDS = ["open", "high", "low", "close", "volume"]
PRICE_FIELDS = ["open", "high", "low", "close"]
QUALITY_SEVERITIES = ("fail", "warning", "info")


def _field_schema(adjustment: str = DEFAULT_QLIB_ADJUSTMENT) -> dict[str, Any]:
    price_adjustment = "none" if adjustment == QLIB_ADJUSTMENT_NONE else "split_reverse_split_rights_issue"
    volume_adjustment = "none" if adjustment == QLIB_ADJUSTMENT_NONE else "split_reverse_split"
    schema: dict[str, Any] = {
        "symbol": {
            "role": "instrument",
            "dtype": "string",
            "source_column": "Code",
            "nullable": False,
        },
        "date": {
            "role": "calendar_date",
            "dtype": "date",
            "source_column": "Date",
            "nullable": False,
        },
        "open": {
            "role": "price",
            "dtype": "float32",
            "source_column": "Open",
            "qlib_expression": "$open",
            "nullable": True,
            "adjustment": price_adjustment,
        },
        "high": {
            "role": "price",
            "dtype": "float32",
            "source_column": "High",
            "qlib_expression": "$high",
            "nullable": True,
            "adjustment": price_adjustment,
        },
        "low": {
            "role": "price",
            "dtype": "float32",
            "source_column": "Low",
            "qlib_expression": "$low",
            "nullable": True,
            "adjustment": price_adjustment,
        },
        "close": {
            "role": "price",
            "dtype": "float32",
            "source_column": "Close",
            "qlib_expression": "$close",
            "nullable": True,
            "adjustment": price_adjustment,
        },
        "volume": {
            "role": "volume",
            "dtype": "float32",
            "source_column": "Volume",
            "qlib_expression": "$volume",
            "nullable": True,
            "adjustment": volume_adjustment,
        },
        "amount": {
            "role": "turnover",
            "dtype": "float32",
            "source_column": "TurnoverValue",
            "qlib_expression": "$amount",
            "nullable": True,
            "adjustment": "none",
        },
    }
    if adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR:
        schema["factor"] = {
            "role": "adjustment_factor",
            "dtype": "float32",
            "source_column": "adjustment_factor",
            "qlib_expression": "$factor",
            "nullable": False,
            "adjustment": "cumulative_vendor_factor",
        }
    return schema


def _adjustment_policy(adjustment: str = DEFAULT_QLIB_ADJUSTMENT) -> dict[str, Any]:
    if adjustment == QLIB_ADJUSTMENT_NONE:
        return {
            "schema_version": 1,
            "price_adjustment": "none",
            "volume_adjustment": "none",
            "amount_adjustment": "none",
            "price_fields": PRICE_FIELDS,
            "volume_fields": ["volume"],
            "amount_fields": ["amount"],
            "producer_transform": "column_normalization_only",
            "known_limitations": [
                "qlib.adjustment is none: OHLC/volume are raw; no $factor series is emitted.",
            ],
        }
    return {
        "schema_version": 1,
        "price_adjustment": "vendor_cumulative_factor",
        "volume_adjustment": "vendor_cumulative_factor_excluding_rights_issue",
        "amount_adjustment": "none",
        "source": "J-Quants AdjFactor",
        "price_fields": PRICE_FIELDS,
        "volume_fields": ["volume"],
        "amount_fields": ["amount"],
        "producer_transform": "dump_time_cumulative_vendor_factor",
        "known_limitations": [
            "Dividends are not adjusted.",
            "Rights issues (ex_rights_type 3) adjust prices but not volume.",
        ],
    }


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quality_finding(
    *,
    check: str,
    severity: str,
    count: int,
    detail: str,
    **extra: Any,
) -> dict[str, Any]:
    if severity not in QUALITY_SEVERITIES:
        raise ValueError(f"Unknown quality severity: {severity}")
    payload: dict[str, Any] = {
        "check": check,
        "severity": severity,
        "count": int(count),
        "detail": detail,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _quality_status(findings: list[dict[str, Any]]) -> str:
    if any(finding.get("severity") == "fail" for finding in findings):
        return "fail"
    if any(finding.get("severity") == "warning" for finding in findings):
        return "warning"
    return "pass"


def _highest_severity(findings: list[dict[str, Any]]) -> str:
    for severity in QUALITY_SEVERITIES:
        if any(finding.get("severity") == severity for finding in findings):
            return severity
    return "pass"


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        severity: sum(1 for finding in findings if finding.get("severity") == severity)
        for severity in QUALITY_SEVERITIES
    }


def _build_quality_inspection(
    df: Any, qlib_dir: Path, *, adjustment: str = DEFAULT_QLIB_ADJUSTMENT
) -> dict[str, Any]:
    import pandas as pd

    findings: list[dict[str, Any]] = []
    if df.empty:
        findings.append(
            _quality_finding(
                check="empty_dataset",
                severity="fail",
                count=1,
                detail="Parquet file is empty",
            )
        )

    required_cols = ["symbol", "date", *QLIB_FIELDS]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        findings.append(
            _quality_finding(
                check="required_columns",
                severity="fail",
                count=len(missing_cols),
                detail=f"Missing columns: {', '.join(missing_cols)}",
                columns=missing_cols,
            )
        )

    parsed_dates: pd.Series = (
        pd.to_datetime(df["date"], errors="coerce") if "date" in df.columns else pd.Series([], dtype="datetime64[ns]")
    )
    invalid_date_count = int(parsed_dates.isna().sum()) if "date" in df.columns else None
    if invalid_date_count:
        findings.append(
            _quality_finding(
                check="invalid_date_rows",
                severity="fail",
                count=invalid_date_count,
                detail=f"Invalid date rows: {invalid_date_count}",
            )
        )

    empty_symbol_rows = int((df["symbol"].astype(str).str.strip() == "").sum()) if "symbol" in df.columns else None
    if empty_symbol_rows:
        findings.append(
            _quality_finding(
                check="empty_symbol_rows",
                severity="fail",
                count=empty_symbol_rows,
                detail=f"Empty symbol rows: {empty_symbol_rows}",
            )
        )

    duplicate_rows = (
        int(df.duplicated(subset=["symbol", "date"]).sum()) if {"symbol", "date"}.issubset(df.columns) else None
    )
    if duplicate_rows:
        findings.append(
            _quality_finding(
                check="duplicate_symbol_date_rows",
                severity="fail",
                count=duplicate_rows,
                detail=f"Duplicate (symbol, date) rows: {duplicate_rows}",
            )
        )

    qlib_fields = [field for field in QLIB_FIELDS if field in df.columns]
    numeric_fields: dict[str, Any] = {}
    non_numeric_counts: dict[str, int] = {}
    null_counts: dict[str, int] = {}
    for field in qlib_fields:
        converted = pd.to_numeric(df[field], errors="coerce")
        numeric_fields[field] = converted
        non_numeric_counts[field] = int((df[field].notna() & converted.isna()).sum())
        null_counts[field] = int(df[field].isna().sum())
        if non_numeric_counts[field]:
            findings.append(
                _quality_finding(
                    check="non_numeric_field_rows",
                    severity="fail",
                    count=non_numeric_counts[field],
                    detail=f"Non-numeric {field} rows: {non_numeric_counts[field]}",
                    field=field,
                )
            )

    ohlcv_null_classification: dict[str, Any] = {
        "rows_with_any_ohlcv_null": None,
        "all_ohlcv_null_rows": None,
        "partial_ohlcv_null_rows": None,
        "affected_symbol_count": None,
        "row_ratio": None,
        "date_concentration": None,
        "field_null_counts": {field: null_counts.get(field) for field in OHLCV_FIELDS if field in null_counts},
    }
    if set(OHLCV_FIELDS).issubset(df.columns):
        ohlcv_nulls = df[OHLCV_FIELDS].isna()
        rows_with_any_ohlcv_null = int(ohlcv_nulls.any(axis=1).sum())
        all_ohlcv_null_mask = ohlcv_nulls.all(axis=1)
        all_ohlcv_null_rows = int(all_ohlcv_null_mask.sum())
        partial_ohlcv_null_rows = rows_with_any_ohlcv_null - all_ohlcv_null_rows
        affected_symbol_count = (
            int(df.loc[all_ohlcv_null_mask, "symbol"].nunique()) if "symbol" in df.columns else None
        )
        row_ratio = float(all_ohlcv_null_rows / len(df)) if len(df) else 0.0
        date_concentration = None
        if "date" in df.columns:
            all_null_dates = parsed_dates.loc[all_ohlcv_null_mask].dropna().dt.strftime("%Y-%m-%d")
            date_concentration = [
                {"date": str(date), "rows": int(count)}
                for date, count in all_null_dates.value_counts().head(5).items()
            ]
        ohlcv_null_classification.update(
            {
                "rows_with_any_ohlcv_null": rows_with_any_ohlcv_null,
                "all_ohlcv_null_rows": all_ohlcv_null_rows,
                "partial_ohlcv_null_rows": partial_ohlcv_null_rows,
                "affected_symbol_count": affected_symbol_count,
                "row_ratio": row_ratio,
                "date_concentration": date_concentration,
            }
        )
        if all_ohlcv_null_rows:
            findings.append(
                _quality_finding(
                    check="ohlcv_all_null_rows",
                    severity="warning",
                    count=all_ohlcv_null_rows,
                    detail=f"Rows with all OHLCV fields null: {all_ohlcv_null_rows}",
                    fields=OHLCV_FIELDS,
                    interpretation="missing_bar_or_source_gap",
                    affected_symbol_count=affected_symbol_count,
                    row_ratio=row_ratio,
                    date_concentration=date_concentration,
                )
            )
        if partial_ohlcv_null_rows:
            findings.append(
                _quality_finding(
                    check="ohlcv_partial_null_rows",
                    severity="fail",
                    count=partial_ohlcv_null_rows,
                    detail=f"Rows with partial OHLCV null fields: {partial_ohlcv_null_rows}",
                    fields=OHLCV_FIELDS,
                    interpretation="incomplete_bar",
                )
            )

    amount_null_rows = null_counts.get("amount")
    amount_null_with_complete_ohlcv_rows = None
    if "amount" in df.columns and set(OHLCV_FIELDS).issubset(df.columns):
        amount_null_with_complete_ohlcv_rows = int((df["amount"].isna() & ~df[OHLCV_FIELDS].isna().any(axis=1)).sum())
        if amount_null_with_complete_ohlcv_rows:
            findings.append(
                _quality_finding(
                    check="amount_null_with_complete_ohlcv_rows",
                    severity="warning",
                    count=amount_null_with_complete_ohlcv_rows,
                    detail=f"Rows with null amount but complete OHLCV: {amount_null_with_complete_ohlcv_rows}",
                    field="amount",
                    interpretation="turnover_missing",
                )
            )

    invalid_price_order_rows = None
    if set(PRICE_FIELDS).issubset(numeric_fields):
        prices = pd.DataFrame({field: numeric_fields[field] for field in PRICE_FIELDS})
        complete_prices = prices.notna().all(axis=1)
        invalid_price_order_rows = int(
            (
                complete_prices
                & (
                    (prices["high"] < prices["low"])
                    | (prices["open"] < prices["low"])
                    | (prices["open"] > prices["high"])
                    | (prices["close"] < prices["low"])
                    | (prices["close"] > prices["high"])
                )
            ).sum()
        )
        if invalid_price_order_rows:
            findings.append(
                _quality_finding(
                    check="invalid_price_order_rows",
                    severity="fail",
                    count=invalid_price_order_rows,
                    detail=f"Rows violating low <= open/close <= high: {invalid_price_order_rows}",
                    fields=PRICE_FIELDS,
                )
            )

    negative_volume_rows = None
    if "volume" in numeric_fields:
        negative_volume_rows = int((numeric_fields["volume"] < 0).sum())
        if negative_volume_rows:
            findings.append(
                _quality_finding(
                    check="negative_volume_rows",
                    severity="fail",
                    count=negative_volume_rows,
                    detail=f"Negative volume rows: {negative_volume_rows}",
                    field="volume",
                )
            )

    negative_amount_rows = None
    if "amount" in numeric_fields:
        negative_amount_rows = int((numeric_fields["amount"] < 0).sum())
        if negative_amount_rows:
            findings.append(
                _quality_finding(
                    check="negative_amount_rows",
                    severity="fail",
                    count=negative_amount_rows,
                    detail=f"Negative amount rows: {negative_amount_rows}",
                    field="amount",
                )
            )

    dumped_factor_invalid_rows = None
    dumped_factor_latest_not_one_symbols = None
    if (
        adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR
        and not df.empty
        and set(QLIB_FIELDS).issubset(df.columns)
        and "adjustment_factor" in df.columns
    ):
        try:
            dumped = apply_vendor_adjustment(df)
        except ValueError:
            dumped = None
        if dumped is not None and "factor" in dumped.columns:
            factor = dumped["factor"]
            numeric_factor = pd.to_numeric(factor, errors="coerce")
            dumped_factor_invalid_rows = int(
                (
                    numeric_factor.isna()
                    | (numeric_factor <= 0)
                    | (numeric_factor == float("inf"))
                    | (numeric_factor == float("-inf"))
                ).sum()
            )
            dumped_factor_latest_not_one_symbols = 0
            if {"symbol", "date"}.issubset(dumped.columns) and len(dumped):
                latest_index = dumped.groupby("symbol", sort=False)["date"].idxmax()
                latest = numeric_factor.loc[latest_index]
                dumped_factor_latest_not_one_symbols = int((latest != 1.0).sum())
            invalid_count = dumped_factor_invalid_rows + dumped_factor_latest_not_one_symbols
            if invalid_count:
                findings.append(
                    _quality_finding(
                        check="dumped_factor",
                        severity="fail",
                        count=invalid_count,
                        detail=(
                            "Dumped factor must be finite, > 0, and 1.0 on each symbol's latest date: "
                            f"{dumped_factor_invalid_rows} invalid rows, "
                            f"{dumped_factor_latest_not_one_symbols} symbols whose latest factor is not 1.0"
                        ),
                        field="factor",
                    )
                )

    rows_by_date = (
        parsed_dates.dropna().dt.strftime("%Y-%m-%d").value_counts() if "date" in df.columns else pd.Series(dtype=int)
    )
    calendar_stats = _read_calendar_stats(qlib_dir)
    instrument_count = _read_instrument_count(qlib_dir)
    if calendar_stats["calendar_days"] == 0:
        findings.append(
            _quality_finding(
                check="qlib_calendar",
                severity="fail",
                count=1,
                detail=f"Qlib calendar is empty or missing: {calendar_stats['calendar_path']}",
            )
        )
    if instrument_count == 0:
        findings.append(
            _quality_finding(
                check="qlib_instruments",
                severity="fail",
                count=1,
                detail="Qlib instruments/all.txt is empty or missing",
            )
        )

    status = _quality_status(findings)
    fail_details = [finding["detail"] for finding in findings if finding.get("severity") == "fail"]
    checks = {
        "rows": int(len(df)),
        "duplicate_symbol_date_rows": duplicate_rows,
        "invalid_date_rows": invalid_date_count,
        "empty_symbol_rows": empty_symbol_rows,
        "non_numeric_counts": non_numeric_counts,
        "null_counts": null_counts,
        "ohlcv_null_classification": ohlcv_null_classification,
        "amount_null_rows": amount_null_rows,
        "amount_null_with_complete_ohlcv_rows": amount_null_with_complete_ohlcv_rows,
        "invalid_price_order_rows": invalid_price_order_rows,
        "negative_volume_rows": negative_volume_rows,
        "negative_amount_rows": negative_amount_rows,
        "dumped_factor_invalid_rows": dumped_factor_invalid_rows,
        "dumped_factor_latest_not_one_symbols": dumped_factor_latest_not_one_symbols,
        "rows_by_date_min": int(rows_by_date.min()) if not rows_by_date.empty else 0,
        "rows_by_date_max": int(rows_by_date.max()) if not rows_by_date.empty else 0,
        "calendar_days": calendar_stats["calendar_days"],
        "instrument_count": instrument_count,
    }
    return {
        "status": status,
        "highest_severity": _highest_severity(findings),
        "severity_counts": _severity_counts(findings),
        "validation_errors": fail_details,
        "findings": findings,
        "checks": checks,
    }


def _producer_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _read_calendar_stats(qlib_dir: Path) -> dict[str, Any]:
    calendar_path = qlib_dir / "calendars" / "day.txt"
    if not calendar_path.exists():
        return {
            "calendar_path": str(calendar_path),
            "calendar_start": None,
            "calendar_end": None,
            "calendar_days": 0,
        }

    values = [line.strip() for line in calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "calendar_path": str(calendar_path),
        "calendar_start": values[0] if values else None,
        "calendar_end": values[-1] if values else None,
        "calendar_days": len(values),
    }


def _read_instrument_count(qlib_dir: Path) -> int:
    instruments_path = qlib_dir / "instruments" / "all.txt"
    if not instruments_path.exists():
        return 0
    return sum(1 for line in instruments_path.read_text(encoding="utf-8").splitlines() if line.strip())


def csv_source_manifest_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(csv_path.suffix + CSV_SOURCE_MANIFEST_SUFFIX)


def dataset_manifest_path(qlib_dir: Path) -> Path:
    return qlib_dir / DATASET_MANIFEST_FILENAME


def dataset_quality_report_path(qlib_dir: Path) -> Path:
    return qlib_dir / DATASET_QUALITY_REPORT_FILENAME


def read_dataset_manifest(qlib_dir: Path) -> dict[str, Any] | None:
    path = dataset_manifest_path(qlib_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_dataset_quality_report(qlib_dir: Path) -> dict[str, Any] | None:
    path = dataset_quality_report_path(qlib_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def write_csv_source_manifest(
    csv_path: Path,
    *,
    source_kind: str,
    requested_start: dt.date | None = None,
    requested_end: dt.date | None = None,
) -> Path:
    payload = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "artifact_type": "bootstrap_csv_source",
        "csv_path": str(csv_path),
        "source": {
            "kind": source_kind,
            "requested_start": _date_or_none(requested_start),
            "requested_end": _date_or_none(requested_end),
        },
    }
    return write_json_atomic(csv_source_manifest_path(csv_path), payload)


def read_csv_source_manifest(csv_path: Path) -> dict[str, Any] | None:
    path = csv_source_manifest_path(csv_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_dataset_manifest(
    *,
    parquet_path: Path,
    qlib_dir: Path,
    source: dict[str, Any],
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> dict[str, Any]:
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    if df.empty:
        raise ValueError(f"Parquet file is empty: {parquet_path}")

    parsed_dates = pd.to_datetime(df["date"])
    calendar_stats = _read_calendar_stats(qlib_dir)
    parquet_sha256 = _sha256_file(parquet_path)
    quality = _build_quality_inspection(df, qlib_dir, adjustment=adjustment)
    dataset_identity = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "date_start": _date_or_none(parsed_dates.min()),
        "date_end": _date_or_none(parsed_dates.max()),
        "rows": int(len(df)),
        "instruments": int(df["symbol"].nunique()),
        "parquet_sha256": parquet_sha256,
        "source": source,
    }
    dataset_id = "jqqlib-v2-" + _sha256_json(dataset_identity)[:16]
    generated_at = _utc_now()

    payload = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "dataset_id": dataset_id,
        "build_id": (
            f"{generated_at.replace(':', '').replace('-', '').replace('Z', 'Z')}"
            f"-{dataset_id.rsplit('-', 1)[-1]}"
        ),
        "quality_status": quality["status"],
        "provider_uri": str(qlib_dir),
        "parquet_path": str(parquet_path),
        "dataset": {
            "date_start": _date_or_none(parsed_dates.min()),
            "date_end": _date_or_none(parsed_dates.max()),
            "rows": int(len(df)),
            "instruments": int(df["symbol"].nunique()),
            "fields": list(QLIB_FIELDS_WITH_FACTOR if adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR else QLIB_FIELDS),
        },
        "qlib": {
            **calendar_stats,
            "instrument_count": _read_instrument_count(qlib_dir),
        },
        "field_schema": _field_schema(adjustment),
        "adjustment_policy": _adjustment_policy(adjustment),
        "lineage": {
            "parquet_sha256": parquet_sha256,
            "producer_git_commit": _producer_git_commit(),
        },
        "source": source,
    }
    payload["lineage"]["manifest_sha256_scope"] = "payload_without_manifest_sha256"
    payload["lineage"]["manifest_sha256"] = _sha256_json(payload)
    return payload


def build_dataset_quality_report(
    *,
    parquet_path: Path,
    qlib_dir: Path,
    manifest: dict[str, Any] | None = None,
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> dict[str, Any]:
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    quality = _build_quality_inspection(df, qlib_dir, adjustment=adjustment)

    return {
        "schema_version": DATASET_QUALITY_REPORT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "provider_uri": str(qlib_dir),
        "parquet_path": str(parquet_path),
        "dataset_id": manifest.get("dataset_id") if manifest else None,
        "dataset_manifest_ref": str(dataset_manifest_path(qlib_dir)),
        "status": quality["status"],
        "highest_severity": quality["highest_severity"],
        "severity_counts": quality["severity_counts"],
        "validation_errors": quality["validation_errors"],
        "findings": quality["findings"],
        "checks": quality["checks"],
    }


def write_dataset_manifest(
    *,
    parquet_path: Path,
    qlib_dir: Path,
    source: dict[str, Any],
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> Path:
    payload = build_dataset_manifest(
        parquet_path=parquet_path, qlib_dir=qlib_dir, source=source, adjustment=adjustment
    )
    return write_json_atomic(dataset_manifest_path(qlib_dir), payload)


def write_dataset_quality_report(
    *,
    parquet_path: Path,
    qlib_dir: Path,
    manifest: dict[str, Any] | None = None,
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> Path:
    payload = build_dataset_quality_report(
        parquet_path=parquet_path, qlib_dir=qlib_dir, manifest=manifest, adjustment=adjustment
    )
    return write_json_atomic(dataset_quality_report_path(qlib_dir), payload)


def write_dataset_artifacts(
    *,
    parquet_path: Path,
    qlib_dir: Path,
    source: dict[str, Any],
    adjustment: str = DEFAULT_QLIB_ADJUSTMENT,
) -> tuple[Path, Path]:
    manifest = build_dataset_manifest(
        parquet_path=parquet_path, qlib_dir=qlib_dir, source=source, adjustment=adjustment
    )
    manifest_path = write_json_atomic(dataset_manifest_path(qlib_dir), manifest)
    quality_path = write_dataset_quality_report(
        parquet_path=parquet_path, qlib_dir=qlib_dir, manifest=manifest, adjustment=adjustment
    )
    # Bind the quality decision into the manifest after the report is final.
    manifest["lineage"]["quality_report_sha256"] = _sha256_file(quality_path)
    manifest["lineage"].pop("manifest_sha256", None)
    manifest["lineage"]["manifest_sha256"] = _sha256_json(manifest)
    write_json_atomic(manifest_path, manifest)
    return manifest_path, quality_path
