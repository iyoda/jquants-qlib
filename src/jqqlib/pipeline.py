"""CLI orchestration, init-config, and user-error exit mapping."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .audit import GapAuditResult, audit_dataset_gaps, write_audit_sidecar
from .config import load_config, publish_check_git_ref, qlib_adjustment
from .dump import parquet_to_qlib
from .fetch import create_jquants_client, fetch_bulk_daily_quotes, fetch_daily_quotes
from .manifest import (
    DATASET_MANIFEST_FILENAME,
    DATASET_QUALITY_REPORT_FILENAME,
    csv_source_manifest_path,
    read_csv_source_manifest,
    write_csv_source_manifest,
    write_dataset_artifacts,
)
from .publish import publish_dataset_build
from .store import (
    DEFAULT_PARQUET_FILENAME,
    NoDataFetchedError,
    csv_to_parquet,
    write_bootstrap_csv,
    write_parquet,
)
from .trading_calendar import ExactCalendarRequired, is_session
from .validate import validate_date_range, validate_qlib_readiness

logger = logging.getLogger(__name__)
_CLI_HANDLERS: list[logging.Handler] = []

RunAllCallable = Callable[[Path, dt.date, dt.date], None]
DatasetEndReader = Callable[[Path], dt.date | None]
VALID_MERGE_MODES = {"replace", "upsert"}
QUALITY_STATUSES = {"pass", "warning", "fail"}


def _coerce_int(etl_conf: dict[str, Any], key: str, default: int) -> int:
    value = etl_conf.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"etl.{key} must be an integer, got {value!r}.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"etl.{key} must be an integer, got {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"etl.{key} must be an integer, got {value!r}.") from exc


def _coerce_float(etl_conf: dict[str, Any], key: str, default: float) -> float:
    value = etl_conf.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"etl.{key} must be a number, got {value!r}.")
    try:
        coerced = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"etl.{key} must be a number, got {value!r}.") from exc
    if not math.isfinite(coerced):
        raise ValueError(f"etl.{key} must be finite, got {value!r}.")
    return coerced


def validate_etl_config(config: dict) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("configuration must be a mapping.")
    etl_conf = config.get("etl", {})
    if not isinstance(etl_conf, dict):
        raise ValueError("etl configuration must be a mapping.")
    values: dict[str, Any] = {
        "chunk_days": _coerce_int(etl_conf, "chunk_days", 30),
        "retries": _coerce_int(etl_conf, "retries", 3),
        "retry_base_delay_sec": _coerce_float(etl_conf, "retry_base_delay_sec", 1.0),
        "retry_max_delay_sec": _coerce_float(etl_conf, "retry_max_delay_sec", 8.0),
        "retry_jitter_sec": _coerce_float(etl_conf, "retry_jitter_sec", 0.3),
        "merge_mode": str(etl_conf.get("merge_mode", "upsert")),
        "request_interval_sec": _coerce_float(etl_conf, "request_interval_sec", 0.0),
    }
    if values["chunk_days"] < 1:
        raise ValueError("etl.chunk_days must be 1 or greater.")
    if values["retries"] < 0:
        raise ValueError("etl.retries must be 0 or greater.")
    for key in ("retry_base_delay_sec", "retry_max_delay_sec", "retry_jitter_sec", "request_interval_sec"):
        if values[key] < 0:
            raise ValueError(f"etl.{key} must be 0 or greater.")
    if values["retry_max_delay_sec"] < values["retry_base_delay_sec"]:
        raise ValueError("etl.retry_max_delay_sec must be greater than or equal to etl.retry_base_delay_sec.")
    if values["merge_mode"] not in VALID_MERGE_MODES:
        modes = ", ".join(sorted(VALID_MERGE_MODES))
        raise ValueError(f"etl.merge_mode must be one of: {modes}.")
    return values


def _etl_config(config: dict) -> dict[str, Any]:
    return validate_etl_config(config)


def _api_source(start_date: dt.date, end_date: dt.date, codes: list[str]) -> dict[str, Any]:
    return {
        "kind": "jquants_api",
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "universe_codes": list(codes),
    }


def _csv_source(csv_path: Path) -> dict[str, Any]:
    source_manifest = read_csv_source_manifest(csv_path)
    if source_manifest is not None:
        source = dict(source_manifest.get("source", {}))
        source["csv_path"] = str(csv_path)
        source["csv_source_manifest"] = str(csv_source_manifest_path(csv_path))
        return source
    return {
        "kind": "csv",
        "csv_path": str(csv_path),
    }


def run_etl(config_path: Path, start_date: dt.date, end_date: dt.date) -> Path:
    validate_date_range(start_date, end_date)

    config = load_config(config_path)
    parquet_dir = Path(config["storage"]["parquet_dir"])
    codes = config.get("universe", {}).get("codes", [])
    etl_conf = _etl_config(config)

    client = create_jquants_client(config)
    df = fetch_daily_quotes(
        client,
        start_date,
        end_date,
        codes,
        chunk_days=etl_conf["chunk_days"],
        retries=etl_conf["retries"],
        retry_base_delay_sec=etl_conf["retry_base_delay_sec"],
        retry_max_delay_sec=etl_conf["retry_max_delay_sec"],
        retry_jitter_sec=etl_conf["retry_jitter_sec"],
        request_interval_sec=etl_conf["request_interval_sec"],
    )
    return write_parquet(df, parquet_dir, merge_mode=etl_conf["merge_mode"])


def run_bootstrap_csv(config_path: Path, csv_path: Path) -> Path:
    config = load_config(config_path)
    parquet_dir = Path(config["storage"]["parquet_dir"])
    qlib_dir = Path(config["storage"]["qlib_dir"])

    parquet_path = csv_to_parquet(csv_path, parquet_dir)
    adjustment = qlib_adjustment(config)
    parquet_to_qlib(parquet_path, qlib_dir, adjustment=adjustment)
    write_dataset_artifacts(
        parquet_path=parquet_path, qlib_dir=qlib_dir, source=_csv_source(csv_path), adjustment=adjustment
    )
    return qlib_dir


def download_bootstrap_csv(
    config_path: Path,
    start_date: dt.date,
    end_date: dt.date,
    output_path: Path,
) -> Path:
    validate_date_range(start_date, end_date)

    config = load_config(config_path)
    etl_conf = _etl_config(config)
    client = create_jquants_client(config)
    if not hasattr(client, "get_bulk_list"):
        raise ValueError("download-csv requires API key authentication with J-Quants ClientV2.")

    df = fetch_bulk_daily_quotes(
        client,
        start_date,
        end_date,
        retries=etl_conf["retries"],
        retry_base_delay_sec=etl_conf["retry_base_delay_sec"],
        retry_max_delay_sec=etl_conf["retry_max_delay_sec"],
        retry_jitter_sec=etl_conf["retry_jitter_sec"],
        request_interval_sec=etl_conf["request_interval_sec"],
    )
    csv_path = write_bootstrap_csv(df, output_path)
    write_csv_source_manifest(
        csv_path,
        source_kind="jquants_bulk",
        requested_start=start_date,
        requested_end=end_date,
    )
    return csv_path


def run_all(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
    config = load_config(config_path)
    qlib_dir = Path(config["storage"]["qlib_dir"])
    codes = config.get("universe", {}).get("codes", [])
    parquet_path = run_etl(config_path, start_date, end_date)
    adjustment = qlib_adjustment(config)
    parquet_to_qlib(parquet_path, qlib_dir, adjustment=adjustment)
    write_dataset_artifacts(
        parquet_path=parquet_path,
        qlib_dir=qlib_dir,
        source=_api_source(start_date, end_date, codes),
        adjustment=adjustment,
    )


def read_existing_dataset_end(config_path: Path) -> dt.date | None:
    import pandas as pd

    config = load_config(config_path)
    parquet_path = Path(config["storage"]["parquet_dir"]) / DEFAULT_PARQUET_FILENAME
    if not parquet_path.exists():
        return None

    df = pd.read_parquet(parquet_path, columns=["date"])
    if df.empty:
        return None

    return pd.to_datetime(df["date"]).dt.date.max()


def catch_up_start_date(existing_dataset_end: dt.date | None, target_date: dt.date) -> dt.date:
    if existing_dataset_end is None or existing_dataset_end >= target_date:
        return target_date
    return existing_dataset_end + dt.timedelta(days=1)


def iter_recent_business_dates(as_of: dt.date, max_lookback_business_days: int) -> Iterator[dt.date]:
    """Yield recent XTKS trading sessions, most recent first.

    Fail-closed on the calendar (``ExactCalendarRequired``): fetching against
    weekday-guessed dates would burn lookback attempts on exchange holidays that
    can never have data.
    """
    if max_lookback_business_days < 0:
        raise ValueError("--max-lookback-business-days must be 0 or greater.")

    cursor = as_of
    yielded = 0
    while yielded <= max_lookback_business_days:
        if is_session(cursor):
            yield cursor
            yielded += 1
        cursor -= dt.timedelta(days=1)


def run_daily(
    config_path: Path,
    as_of: dt.date,
    max_lookback_business_days: int = 10,
    runner: RunAllCallable = run_all,
    dataset_end_reader: DatasetEndReader = read_existing_dataset_end,
) -> dt.date:
    last_no_data: NoDataFetchedError | None = None
    existing_dataset_end = dataset_end_reader(config_path)

    for target_date in iter_recent_business_dates(as_of, max_lookback_business_days):
        start_date = catch_up_start_date(existing_dataset_end, target_date)
        try:
            # Fetch the full contiguous range [start_date, target_date] in a single
            # call. write_parquet is atomic (tmp+replace) and the manifest derives
            # its date_end from actual parquet content, so a failure here writes
            # nothing and leaves the dataset end unchanged for the next run to
            # refill -- no silently-advanced manifest with a hole. A run that
            # finds no data anywhere in the range still raises NoDataFetchedError
            # to drive the previous-business-day fallback below.
            if start_date < target_date:
                logger.info(
                    "Catching up missing range %s to %s.",
                    start_date.isoformat(), target_date.isoformat(),
                )
            runner(config_path, start_date, target_date)
        except NoDataFetchedError as exc:
            last_no_data = exc
            logger.info(
                "No data fetched for %s; trying previous business day.",
                target_date.isoformat(),
            )
            continue
        return target_date

    as_of_text = as_of.isoformat()
    raise NoDataFetchedError(
        f"No data fetched for any recent business date as of {as_of_text} "
        f"within {max_lookback_business_days} business days."
    ) from last_no_data


def _audit_window(config: dict, max_lookback_business_days: int = 10) -> int:
    window = int(config.get("etl", {}).get("audit_window_business_days", 5))
    if window > max_lookback_business_days:
        raise ValueError(
            f"etl.audit_window_business_days ({window}) must be <= the daily lookback "
            f"({max_lookback_business_days}); the audit window cannot exceed what one "
            "catch-up run can heal."
        )
    return window


def run_audit(
    config_path: Path,
    as_of: dt.date,
    window_business_days: int | None = None,
    max_lookback_business_days: int = 10,
) -> GapAuditResult:
    config = load_config(config_path)
    parquet_path = Path(config["storage"]["parquet_dir"]) / DEFAULT_PARQUET_FILENAME
    if window_business_days is None:
        window_business_days = _audit_window(config, max_lookback_business_days)
    return audit_dataset_gaps(parquet_path, as_of, window_business_days)


def decide_publish(current_hash: str | None, committed_hash: str | None) -> bool:
    """Decide whether the dataset content changed and should be published.

    Compares the content-only ``lineage.parquet_sha256`` of the freshly built
    dataset against what is committed on the configured ``publish_check.git_ref``
    (default ``origin/main``). This is independent of
    the per-run fetch window and of all manifest timestamps, so identical data
    never triggers a publish. A missing committed hash (first publish) counts as
    changed.
    """
    if current_hash is None:
        return False
    return committed_hash != current_hash


@dataclass(frozen=True)
class PublishCheckDecision:
    returncode: int
    reason: str
    current_hash: str | None = None
    committed_hash: str | None = None
    quality_status: str | None = None
    warning_summary: str | None = None
    git_ref: str = "origin/main"


def _manifest_repo_relpath(config: dict) -> str:
    tracked = Path(config["storage"]["qlib_dir"]) / DATASET_MANIFEST_FILENAME
    return os.path.normpath(str(tracked)).replace(os.sep, "/")


def _manifest_path(config: dict) -> Path:
    return Path(config["storage"]["qlib_dir"]) / DATASET_MANIFEST_FILENAME


def _quality_report_path(config: dict) -> Path:
    return Path(config["storage"]["qlib_dir"]) / DATASET_QUALITY_REPORT_FILENAME


def _read_json_object(path: Path, artifact_name: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"{artifact_name} missing: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{artifact_name} unreadable: {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{artifact_name} is not a JSON object: {path}"
    return payload, None


def _committed_parquet_sha256(config: dict) -> str | None:
    rel = _manifest_repo_relpath(config)
    ref = publish_check_git_ref(config)

    def resolve_git_path(path: str, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        result = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        parts = Path(path).parts
        # Find a symlink in any parent component, replace it, then recurse, so a
        # provider path committed as a symlink to a build directory still resolves.
        for size in range(len(parts) - 1, 0, -1):
            prefix = "/".join(parts[:size])
            tree = subprocess.run(
                ["git", "ls-tree", ref, "--", prefix], capture_output=True, text=True
            )
            entry = tree.stdout.strip().splitlines()
            if tree.returncode != 0 or len(entry) != 1:
                continue
            mode = entry[0].split(None, 1)[0]
            if mode != "120000":
                continue
            probe = subprocess.run(["git", "show", f"{ref}:{prefix}"], capture_output=True, text=True)
            if probe.returncode != 0:
                continue
            target = probe.stdout.strip()
            if not target:
                continue
            replaced = os.path.normpath(os.path.join(os.path.dirname(prefix), target, *parts[size:]))
            return resolve_git_path(replaced.replace(os.sep, "/"), depth + 1)
        return None

    content = resolve_git_path(rel)
    if content is None:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    lineage = data.get("lineage", {})
    return lineage.get("parquet_sha256") if isinstance(lineage, dict) else None


def _quality_warning_summary(report: dict[str, Any]) -> str | None:
    warnings = [
        finding
        for finding in report.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "warning"
    ]
    count = len(warnings)
    if count == 0:
        severity_counts = report.get("severity_counts", {})
        try:
            count = int(severity_counts.get("warning", 0)) if isinstance(severity_counts, dict) else 0
        except (TypeError, ValueError):
            count = 0
    if count == 0:
        return None
    first = str(warnings[0].get("detail", "")) if warnings else ""
    return f"quality warning: count={count}; first={first}"


def _publish_check_error(
    reason: str,
    *,
    current_hash: str | None = None,
    quality_status: str | None = None,
) -> PublishCheckDecision:
    return PublishCheckDecision(
        returncode=1,
        reason=reason,
        current_hash=current_hash,
        quality_status=quality_status,
    )


def evaluate_publish_check(config_path: Path) -> PublishCheckDecision:
    config = load_config(config_path)

    manifest, reason = _read_json_object(_manifest_path(config), "dataset manifest")
    if reason is not None or manifest is None:
        return _publish_check_error(reason or "dataset manifest unavailable")

    report, reason = _read_json_object(_quality_report_path(config), "dataset quality report")
    if reason is not None or report is None:
        return _publish_check_error(reason or "dataset quality report unavailable")

    lineage = manifest.get("lineage", {})
    current = lineage.get("parquet_sha256") if isinstance(lineage, dict) else None
    if not current:
        return _publish_check_error("dataset manifest missing lineage.parquet_sha256")

    manifest_status = manifest.get("quality_status")
    report_status = report.get("status")
    if manifest_status not in QUALITY_STATUSES:
        return _publish_check_error(
            f"dataset manifest has invalid quality_status: {manifest_status!r}",
            current_hash=current,
        )
    if report_status not in QUALITY_STATUSES:
        return _publish_check_error(
            f"dataset quality report has invalid status: {report_status!r}",
            current_hash=current,
            quality_status=str(manifest_status),
        )
    if manifest_status == "fail":
        return _publish_check_error(
            "dataset manifest quality_status is fail",
            current_hash=current,
            quality_status=str(manifest_status),
        )
    if report_status == "fail":
        return _publish_check_error(
            "dataset quality report status is fail",
            current_hash=current,
            quality_status=str(report_status),
        )
    if manifest_status != report_status:
        return _publish_check_error(
            f"manifest/report quality status mismatch: manifest={manifest_status!r} report={report_status!r}",
            current_hash=current,
            quality_status=str(report_status),
        )

    manifest_dataset_id = manifest.get("dataset_id")
    report_dataset_id = report.get("dataset_id")
    if manifest_dataset_id != report_dataset_id:
        return _publish_check_error(
            f"manifest/report dataset_id mismatch: manifest={manifest_dataset_id!r} report={report_dataset_id!r}",
            current_hash=current,
            quality_status=str(report_status),
        )

    committed = _committed_parquet_sha256(config)
    warning_summary = _quality_warning_summary(report)
    changed = decide_publish(current, committed)
    return PublishCheckDecision(
        returncode=0 if changed else 3,
        reason="changed" if changed else "unchanged",
        current_hash=current,
        committed_hash=committed,
        git_ref=publish_check_git_ref(config),
        quality_status=str(report_status),
        warning_summary=warning_summary,
    )


def run_publish_check(config_path: Path) -> int:
    """Return an exit code for the shell publish gate: 0=changed, 3=unchanged, 1=error.

    Fails closed: a missing/corrupt working-tree manifest (no content hash) is an
    error, not a silent no-op -- a successful run must have produced one, so the
    caller should alert rather than skip publishing without a trace.
    """
    return evaluate_publish_check(config_path).returncode


def publish_pathspecs(config_path: Path) -> list[str]:
    """Return the exact validated artifact set the daily wrapper may stage.

    Dataset DATA (Parquet, Qlib bin/calendars/instruments) is not git-tracked: it
    stays on disk (back it up separately if needed) and is reproducible from the
    J-Quants API. Only the manifest and the quality report describe the build, so
    only they -- plus the Parquet path the manifest hash refers to -- are returned.
    """
    config = load_config(config_path)
    return [
        str(Path(config["storage"]["parquet_dir"]) / DEFAULT_PARQUET_FILENAME),
        str(Path(config["storage"]["qlib_dir"]) / DATASET_MANIFEST_FILENAME),
        str(Path(config["storage"]["qlib_dir"]) / DATASET_QUALITY_REPORT_FILENAME),
    ]


def parse_date(v: str) -> dt.date:
    return dt.datetime.strptime(v, "%Y-%m-%d").date()


def _now(tz: dt.tzinfo) -> dt.datetime:
    """Return the current time in ``tz``. Patchable so as-of tests can freeze the clock."""
    return dt.datetime.now(tz)


def parse_as_of_date(v: str) -> dt.date:
    if v == "today":
        return _now(ZoneInfo("Asia/Tokyo")).date()
    return parse_date(v)


DEFAULT_CONFIG_FILENAME = "config.yaml"
PACKAGED_CONFIG_EXAMPLE = "config.example.yaml"


def packaged_config_example() -> bytes:
    """Return the example configuration shipped inside the wheel (``jqqlib/data``)."""
    return resource_files("jqqlib.data").joinpath(PACKAGED_CONFIG_EXAMPLE).read_bytes()


def init_config(path: Path, *, force: bool = False) -> Path:
    """Write the packaged example configuration to ``path``.

    Refuses to overwrite an existing file unless ``force`` is set, so a typo in
    the destination cannot silently discard a tuned configuration.
    """
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite it.")
    if path.exists() and not path.is_file():
        raise IsADirectoryError(f"{path} is not a regular file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(packaged_config_example())
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jqqlib", description="J-Quants to Qlib dataset pipeline")
    parser.add_argument("--version", action="version", version=f"jqqlib {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init-config",
        help=f"Write the example configuration to PATH (default: ./{DEFAULT_CONFIG_FILENAME})",
    )
    init.add_argument("path", nargs="?", default=DEFAULT_CONFIG_FILENAME)
    init.add_argument("--force", action="store_true", help="Overwrite PATH if it already exists")

    etl = sub.add_parser("run-etl", help="Fetch from J-Quants and write Parquet")
    etl.add_argument("--config", required=True)
    etl.add_argument("--start", required=True, type=parse_date)
    etl.add_argument("--end", required=True, type=parse_date)

    convert = sub.add_parser("convert", help="Convert Parquet to Qlib dataset")
    convert.add_argument("--config", required=True)

    validate = sub.add_parser(
        "validate",
        help="Validate whether the parquet data satisfies Qlib dump_bin input requirements",
    )
    validate.add_argument("--config", required=True)

    bootstrap = sub.add_parser(
        "bootstrap-csv",
        help="Initial bulk load: local CSV -> Parquet -> Qlib dataset",
    )
    bootstrap.add_argument("--config", required=True)
    bootstrap.add_argument("--csv", required=True)

    download_csv = sub.add_parser(
        "download-csv",
        help="Download daily quotes from J-Quants API and save a bootstrap CSV",
    )
    download_csv.add_argument("--config", required=True)
    download_csv.add_argument("--start", required=True, type=parse_date)
    download_csv.add_argument("--end", required=True, type=parse_date)
    download_csv.add_argument("--output", required=True)

    all_cmd = sub.add_parser("run-all", help="Run ETL + Qlib conversion")
    all_cmd.add_argument("--config", required=True)
    all_cmd.add_argument("--start", required=True, type=parse_date)
    all_cmd.add_argument("--end", required=True, type=parse_date)

    daily = sub.add_parser(
        "run-daily",
        help="Run daily ETL + Qlib conversion for the latest business day with data",
    )
    daily.add_argument("--config", required=True)
    daily.add_argument(
        "--as-of",
        default="today",
        type=parse_as_of_date,
        help="As-of date (YYYY-MM-DD). 'today' is the current date in Asia/Tokyo, not the host local date.",
    )
    daily.add_argument("--max-lookback-business-days", default=10, type=int)
    # Accepted but unused by run-daily: the daily wrapper forwards one shared
    # arg list to both run-daily and audit, so run-daily must tolerate the
    # audit-only flag rather than abort the whole job on an unknown argument.
    daily.add_argument("--window-business-days", default=None, type=int, help=argparse.SUPPRESS)

    audit = sub.add_parser(
        "audit",
        help="Check for missing recent business days in the parquet dataset (publish gate)",
    )
    audit.add_argument("--config", required=True)
    audit.add_argument(
        "--as-of",
        default="today",
        type=parse_as_of_date,
        help="As-of date (YYYY-MM-DD). 'today' is the current date in Asia/Tokyo, not the host local date.",
    )
    audit.add_argument("--window-business-days", default=None, type=int)
    audit.add_argument("--max-lookback-business-days", default=10, type=int)

    publish_check = sub.add_parser(
        "publish-check",
        help="Exit 0 if the dataset content changed vs publish_check.git_ref (default origin/main), "
        "3 if unchanged (no-op gate)",
    )
    publish_check.add_argument("--config", required=True)

    publish = sub.add_parser("publish", help="Atomically publish a prepared Qlib dataset build via symlink")
    publish.add_argument("--build-provider-uri", required=True)
    publish.add_argument("--provider-uri", required=True)

    publish_paths = sub.add_parser("publish-paths", help="Print the exact validated git artifact pathspec")
    publish_paths.add_argument("--config", required=True)

    return parser


class _BelowWarningFilter(logging.Filter):
    """Keep WARNING+ off the stdout handler so they appear only on stderr."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def _configure_cli_logging() -> None:
    """Attach stdout/stderr handlers for the ``jqqlib`` hierarchy at the CLI boundary.

    Library importers see no handlers (silence by default). ``main()`` installs
    these for the duration of one CLI invocation so progress ``INFO`` lines still
    print on stdout and warnings on stderr, matching the previous ``print()``
    behaviour. Never call ``logging.basicConfig`` from library code.
    """
    if _CLI_HANDLERS:
        return
    root = logging.getLogger("jqqlib")
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(_BelowWarningFilter())
    stdout_handler.setFormatter(formatter)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    root.propagate = False
    _CLI_HANDLERS.extend((stdout_handler, stderr_handler))


def _teardown_cli_logging() -> None:
    root = logging.getLogger("jqqlib")
    for handler in _CLI_HANDLERS:
        root.removeHandler(handler)
        handler.close()
    _CLI_HANDLERS.clear()
    root.propagate = True


def main() -> None:
    args = build_parser().parse_args()
    _configure_cli_logging()
    try:
        _dispatch(args)
    except (ValueError, OSError) as exc:
        # User-facing failures (unreadable/invalid config, bad arguments, refused
        # overwrite) get a one-line message instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except RuntimeError as exc:
        # Dataset-state failures (e.g. a Qlib dump that produced no provider
        # subtree) are reported like a failed check, not as a crash.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        _teardown_cli_logging()


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "init-config":
        path = init_config(Path(args.path), force=args.force)
        print(f"Configuration written: {path}")
    elif args.command == "run-etl":
        path = run_etl(Path(args.config), args.start, args.end)
        print(f"Parquet created: {path}")
    elif args.command == "convert":
        conf = load_config(Path(args.config))
        parquet_dir = Path(conf["storage"]["parquet_dir"])
        qlib_dir = Path(conf["storage"]["qlib_dir"])
        parquet_path = parquet_dir / DEFAULT_PARQUET_FILENAME
        adjustment = qlib_adjustment(conf)
        parquet_to_qlib(parquet_path, qlib_dir, adjustment=adjustment)
        manifest_path, quality_path = write_dataset_artifacts(
            parquet_path=parquet_path,
            qlib_dir=qlib_dir,
            source={"kind": "parquet", "parquet_path": str(parquet_path)},
            adjustment=adjustment,
        )
        print(f"Qlib dataset created: {qlib_dir}")
        print(f"Dataset manifest created: {manifest_path}")
        print(f"Dataset quality report created: {quality_path}")
    elif args.command == "validate":
        conf = load_config(Path(args.config))
        parquet_dir = Path(conf["storage"]["parquet_dir"])
        parquet_path = parquet_dir / DEFAULT_PARQUET_FILENAME
        errors = validate_qlib_readiness(parquet_path)
        if errors:
            print("Validation failed:")
            for err in errors:
                print(f"- {err}")
            raise SystemExit(1)
        print(f"Validation passed: {parquet_path} is ready for qlib dump_bin conversion.")
    elif args.command == "bootstrap-csv":
        qlib_dir = run_bootstrap_csv(Path(args.config), Path(args.csv))
        print(f"Bootstrap completed. Qlib dataset created: {qlib_dir}")
        print(f"Dataset manifest created: {qlib_dir / 'dataset_manifest.json'}")
        print(f"Dataset quality report created: {qlib_dir / 'dataset_quality_report.json'}")
    elif args.command == "download-csv":
        csv_path = download_bootstrap_csv(Path(args.config), args.start, args.end, Path(args.output))
        print(f"Bootstrap CSV downloaded: {csv_path}")
        print(f"Bootstrap CSV source manifest created: {csv_source_manifest_path(csv_path)}")
    elif args.command == "run-all":
        run_all(Path(args.config), args.start, args.end)
        print("Pipeline completed.")
    elif args.command == "run-daily":
        try:
            target_date = run_daily(
                Path(args.config),
                args.as_of,
                max_lookback_business_days=args.max_lookback_business_days,
            )
        except ExactCalendarRequired as exc:
            print(f"Daily pipeline failed: XTKS trading calendar unavailable: {exc}",
                  file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"Daily pipeline completed for {target_date.isoformat()}.")
    elif args.command == "audit":
        config = load_config(Path(args.config))
        sidecar_path = Path(config["storage"]["audit_dir"]) / "last_audit.json"
        try:
            result = run_audit(
                Path(args.config),
                args.as_of,
                args.window_business_days,
                max_lookback_business_days=args.max_lookback_business_days,
            )
        except ExactCalendarRequired as exc:
            # Distinct from a gap: the audit could not run at all. rc=2 lets the
            # daily wrapper alert on "calendar" instead of a phantom data gap.
            print(f"Audit failed: XTKS trading calendar unavailable: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        try:
            write_audit_sidecar(
                result,
                args.as_of,
                path=sidecar_path,
                run_started_at=os.environ.get("JQQLIB_RUN_STARTED_AT", ""),
            )
        except OSError as exc:
            # The sidecar is a monitoring artifact; its absence already reads as
            # `audit: not_run` downstream and must not fail the import.
            print(f"Warning: could not write the audit sidecar {sidecar_path}: {exc}", file=sys.stderr)
        if result.suppressed_non_sessions:
            print("suppressed (XTKS non-session):")
            for day in result.suppressed_non_sessions:
                print(f"- {day.isoformat()}")
        if result.real_gaps:
            print("Audit failed: missing recent trading sessions:")
            for gap in result.real_gaps:
                print(f"- {gap.isoformat()}")
            raise SystemExit(1)
        print("Audit passed: no missing recent trading sessions.")
    elif args.command == "publish-check":
        decision = evaluate_publish_check(Path(args.config))
        rc = decision.returncode
        if decision.warning_summary:
            print(decision.warning_summary, file=sys.stderr)
        if rc == 0:
            print(f"Dataset content changed vs {decision.git_ref}; publish required.")
        elif rc == 3:
            print(f"No dataset content change vs {decision.git_ref}; skipping publish.")
        else:
            print(f"publish-check error: {decision.reason}", file=sys.stderr)
        raise SystemExit(rc)
    elif args.command == "publish":
        published = publish_dataset_build(Path(args.build_provider_uri), Path(args.provider_uri))
        print(f"Published provider URI: {published}")
    elif args.command == "publish-paths":
        for pathspec in publish_pathspecs(Path(args.config)):
            print(pathspec)


if __name__ == "__main__":
    main()
