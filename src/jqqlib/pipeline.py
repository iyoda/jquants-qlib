"""CLI orchestration, init-config, and user-error exit mapping."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import math
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .audit import GapAuditResult, audit_dataset_gaps, write_audit_sidecar
from .config import (
    PUBLISH_CHECK_BASELINE_FILE,
    QLIB_ADJUSTMENT_VENDOR_FACTOR,
    load_config,
    publish_check_baseline,
    publish_check_git_ref,
    publish_check_ledger_path,
    qlib_adjustment,
)
from .dump import parquet_to_qlib
from .fetch import _error_status, create_jquants_client, fetch_bulk_daily_quotes, fetch_daily_quotes
from .ledger import (
    LedgerError,
    append_ledger_entry,
    init_ledger,
    jqqlib_source_commit,
    ledger_entry_from_manifest,
    read_ledger,
)
from .manifest import (
    DATASET_MANIFEST_FILENAME,
    DATASET_QUALITY_REPORT_FILENAME,
    csv_source_manifest_path,
    read_csv_source_manifest,
    write_csv_source_manifest,
    write_dataset_artifacts,
    write_json_atomic,
)
from .provider_lock import ProviderLockedError, provider_write_lock
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
_AUTH_STATUSES = frozenset({401, 403})


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
    """Return the ``etl`` settings with their defaults applied.

    Raises ``ValueError`` when the configuration or its ``etl`` section is not a mapping, or a value is invalid: a
    boolean, a non-integral count, a non-finite or negative number, ``chunk_days`` below 1,
    ``retry_max_delay_sec`` below ``retry_base_delay_sec``, or an unknown ``merge_mode``.
    """
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
    """Fetch daily quotes of the configured universe and write them to the Parquet; return its path."""
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
    return write_parquet(
        df,
        parquet_dir,
        merge_mode=etl_conf["merge_mode"],
        require_adjustment_factor=qlib_adjustment(config) == QLIB_ADJUSTMENT_VENDOR_FACTOR,
    )


def run_bootstrap_csv(config_path: Path, csv_path: Path) -> Path:
    """Load a bootstrap CSV into the Parquet, dump the Qlib provider and write the dataset artifacts.

    Returns ``storage.qlib_dir``.
    """
    config = load_config(config_path)
    parquet_dir = Path(config["storage"]["parquet_dir"])
    qlib_dir = Path(config["storage"]["qlib_dir"])

    adjustment = qlib_adjustment(config)
    parquet_path = csv_to_parquet(
        csv_path, parquet_dir, require_adjustment_factor=adjustment == QLIB_ADJUSTMENT_VENDOR_FACTOR
    )
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
    """Download bulk daily quotes into a bootstrap CSV and write its source manifest; return the CSV path.

    Requires API-key authentication (``ClientV2``); raises ``ValueError`` otherwise.
    """
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
    """Run ``run_etl`` for the range, then dump the Qlib provider and write the dataset artifacts."""
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


def run_incremental(config_path: Path, start_date: dt.date, end_date: dt.date) -> None:
    """``run_all`` for ``run-daily``: refuses ``etl.merge_mode: replace``.

    The daily fetch window covers only the catch-up range, so replacing the
    Parquet with it would discard all earlier history on every run.
    """
    config = load_config(config_path)
    if _etl_config(config)["merge_mode"] != "upsert":
        raise ValueError(
            "run-daily requires etl.merge_mode: upsert; replace would discard the dataset history "
            "outside the daily fetch window."
        )
    run_all(config_path, start_date, end_date)


def read_existing_dataset_end(config_path: Path) -> dt.date | None:
    """Return the last date in the configured Parquet, or ``None`` when it is missing or empty."""
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
    """Return the day after ``existing_dataset_end``.

    Returns ``target_date`` when there is no dataset yet or it already reaches ``target_date``.
    """
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
    runner: RunAllCallable = run_incremental,
    dataset_end_reader: DatasetEndReader = read_existing_dataset_end,
) -> dt.date:
    """Import up to the latest XTKS session whose range has data; return that session, the end of the import.

    The range starts the day after the dataset end, or at the session itself when there is no dataset yet or the
    dataset already reaches it. ``runner`` gets the whole range at once and succeeds when any day in it has data,
    even if the returned session itself has none. Only when the whole range is empty is the next earlier session
    tried, up to ``max_lookback_business_days`` back. Raises ``NoDataFetchedError`` when every range is empty and
    ``ExactCalendarRequired`` when the XTKS calendar is unavailable.
    """
    last_no_data: NoDataFetchedError | None = None
    existing_dataset_end = dataset_end_reader(config_path)

    for target_date in iter_recent_business_dates(as_of, max_lookback_business_days):
        start_date = catch_up_start_date(existing_dataset_end, target_date)
        try:
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
    """Audit the configured Parquet for missing recent XTKS sessions.

    Without ``window_business_days`` the window is ``etl.audit_window_business_days`` (default 5), which must not
    exceed ``max_lookback_business_days``.
    """
    config = load_config(config_path)
    parquet_path = Path(config["storage"]["parquet_dir"]) / DEFAULT_PARQUET_FILENAME
    if window_business_days is None:
        window_business_days = _audit_window(config, max_lookback_business_days)
    return audit_dataset_gaps(parquet_path, as_of, window_business_days)


def decide_publish(current_hash: str | None, committed_hash: str | None) -> bool:
    """Decide whether the dataset content changed and should be published.

    Compares the content-only ``lineage.parquet_sha256`` of the freshly built
    dataset against the baseline: the manifest committed on
    ``publish_check.git_ref`` (default ``origin/main``) or the last entry of the
    publish ledger (``publish_check.baseline: file``); ``evaluate_publish_check``
    also compares the ``adjustment_policy``. This is independent of
    the per-run fetch window and of all manifest timestamps, so identical data
    never triggers a publish. A missing committed hash (first publish) counts as
    changed.
    """
    if current_hash is None:
        return False
    return committed_hash != current_hash


@dataclass(frozen=True)
class PublishCheckDecision:
    """Outcome of ``publish-check``.

    ``returncode`` is 0 when the dataset changed, 3 when it is unchanged and 1 on an error; ``reason`` and the
    remaining fields explain the decision. ``baseline`` is ``git`` or ``file``; ``ledger_path`` is set only for
    ``file``.
    """

    returncode: int
    reason: str
    current_hash: str | None = None
    committed_hash: str | None = None
    quality_status: str | None = None
    warning_summary: str | None = None
    git_ref: str = "origin/main"
    baseline: str = "git"
    ledger_path: str | None = None

    @property
    def compared_against(self) -> str:
        """Name the baseline for messages: the git ref, or ``ledger <path>`` for the file baseline."""
        if self.baseline == PUBLISH_CHECK_BASELINE_FILE:
            return f"ledger {self.ledger_path}"
        return self.git_ref


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


class _CommittedManifestLookupFailed(Exception):
    pass


def _git(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise _CommittedManifestLookupFailed(f"git is not available: {exc}") from exc


def _git_toplevel() -> Path:
    result = _git(["rev-parse", "--show-toplevel"], cwd=None)
    if result.returncode != 0:
        raise _CommittedManifestLookupFailed(f"not inside a git work tree: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def _manifest_path_from_repo_root(config: dict, toplevel: Path) -> str:
    tracked = Path(os.path.abspath(Path(config["storage"]["qlib_dir"]) / DATASET_MANIFEST_FILENAME))
    root = toplevel.resolve()
    parts = tracked.parts
    for size in range(len(parts) - 1, 0, -1):
        if Path(*parts[:size]).resolve() == root:
            return Path(*parts[size:]).as_posix()
    raise _CommittedManifestLookupFailed(f"dataset manifest {tracked} is outside the git work tree {root}")


def _committed_manifest(config: dict) -> dict[str, Any] | None:
    ref = publish_check_git_ref(config)
    toplevel = _git_toplevel()
    if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=toplevel).returncode != 0:
        raise _CommittedManifestLookupFailed(
            f"publish_check.git_ref {ref!r} does not name a commit; fetch it or fix publish_check.git_ref"
        )
    rel = _manifest_path_from_repo_root(config, toplevel)

    def read_committed_file_following_symlinks(path: str, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        result = _git(["show", f"{ref}:{path}"], cwd=toplevel)
        if result.returncode == 0:
            return result.stdout
        parts = Path(path).parts
        for size in range(len(parts) - 1, 0, -1):
            prefix = "/".join(parts[:size])
            tree = _git(["ls-tree", ref, "--", prefix], cwd=toplevel)
            entry = tree.stdout.strip().splitlines()
            if tree.returncode != 0 or len(entry) != 1:
                continue
            mode = entry[0].split(None, 1)[0]
            if mode != "120000":
                continue
            probe = _git(["show", f"{ref}:{prefix}"], cwd=toplevel)
            if probe.returncode != 0:
                continue
            target = probe.stdout.strip()
            if not target:
                continue
            replaced = os.path.normpath(os.path.join(os.path.dirname(prefix), target, *parts[size:]))
            return read_committed_file_following_symlinks(replaced.replace(os.sep, "/"), depth + 1)
        return None

    content = read_committed_file_following_symlinks(rel)
    if content is None:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


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


@dataclass(frozen=True)
class _PublishableDataset:
    manifest: dict[str, Any]
    report: dict[str, Any]
    current_hash: str
    status: str


def _check_current_dataset(config: dict) -> _PublishableDataset | PublishCheckDecision:
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

    return _PublishableDataset(manifest=manifest, report=report, current_hash=current, status=str(report_status))


def evaluate_publish_check(config_path: Path) -> PublishCheckDecision:
    """Compare the working-tree dataset manifest with the configured baseline.

    The baseline is the manifest committed on ``publish_check.git_ref`` (``publish_check.baseline: git``, the
    default) or the last entry of the ledger at ``publish_check.ledger_path`` (``file``); one is never a fallback
    for the other. Fails closed with ``returncode`` 1 when the manifest or the quality report is missing,
    unreadable or not a JSON object, the manifest lacks ``lineage.parquet_sha256``, a status is unknown or
    ``fail``, the two disagree on status or ``dataset_id``, the git lookup cannot run (no git, not a work tree,
    unknown ref, a manifest outside the work tree) or the ledger is missing or damaged. A committed manifest that
    is absent or not valid JSON, or a ledger without entries, counts as a first publish (``returncode`` 0).
    """
    config = load_config(config_path)
    return _with_configured_baseline(_decide_publish_check(config), config)


def _with_configured_baseline(decision: PublishCheckDecision, config: dict) -> PublishCheckDecision:
    return replace(
        decision,
        git_ref=publish_check_git_ref(config),
        baseline=publish_check_baseline(config),
        ledger_path=_configured_ledger_path(config),
    )


def _decide_publish_check(config: dict) -> PublishCheckDecision:
    dataset = _check_current_dataset(config)
    if isinstance(dataset, PublishCheckDecision):
        return dataset
    manifest, current, report_status = dataset.manifest, dataset.current_hash, dataset.status

    try:
        baseline_record = _baseline_for_publish_check(config)
    except (_CommittedManifestLookupFailed, LedgerError) as exc:
        return _publish_check_error(str(exc), current_hash=current, quality_status=report_status)
    committed = baseline_record.parquet_sha256 if baseline_record else None
    warning_summary = _quality_warning_summary(dataset.report)
    adjustment_policy_changed = baseline_record is not None and (
        manifest.get("adjustment_policy") != baseline_record.adjustment_policy
    )
    changed = decide_publish(current, committed) or adjustment_policy_changed
    return PublishCheckDecision(
        returncode=0 if changed else 3,
        reason="changed" if changed else "unchanged",
        current_hash=current,
        committed_hash=committed,
        quality_status=report_status,
        warning_summary=warning_summary,
    )


def append_current_dataset_to_ledger(config_path: Path) -> tuple[Path, bool]:
    """Record the working-tree dataset in the publish ledger at ``publish_check.ledger_path``.

    Runs the same dataset checks as ``publish-check`` first and raises ``LedgerError`` when they refuse the
    dataset or the ledger is missing or damaged. Returns the ledger path and whether an entry was appended:
    ``False`` when the last entry already records this dataset, so a re-run after a completed append is a no-op.
    """
    config = load_config(config_path)
    path = publish_check_ledger_path(config)
    dataset = _check_current_dataset(config)
    if isinstance(dataset, PublishCheckDecision):
        raise LedgerError(f"refusing to record the dataset in the publish ledger: {dataset.reason}")
    entry = ledger_entry_from_manifest(
        dataset.manifest,
        jqqlib_version=__version__,
        jqqlib_source_commit=jqqlib_source_commit(),
    )
    return path, append_ledger_entry(path, entry)


def init_publish_ledger(config_path: Path, from_manifest: Path | None = None) -> Path:
    """Create the publish ledger at ``publish_check.ledger_path``, empty or seeded from ``from_manifest``.

    A seeded entry records no producer versions because the manifest does not say which jqqlib built it. Raises
    ``FileExistsError`` when the ledger exists, ``OSError`` for an unreadable manifest, ``ValueError`` for a
    manifest that is not a JSON object or cannot become an entry (no ``lineage.parquet_sha256``, no object
    ``adjustment_policy``) and ``LedgerError`` when the ledger cannot be written.
    """
    config = load_config(config_path)
    path = publish_check_ledger_path(config)
    entry = None
    if from_manifest is not None:
        try:
            manifest = json.loads(from_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest {from_manifest} is not JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"manifest {from_manifest} is not a JSON object")
        try:
            entry = ledger_entry_from_manifest(manifest, jqqlib_version=None, jqqlib_source_commit=None)
        except LedgerError as exc:
            raise ValueError(f"manifest {from_manifest} cannot seed the publish ledger: {exc}") from exc
    return init_ledger(path, entry)


@dataclass(frozen=True)
class _Baseline:
    parquet_sha256: str | None
    adjustment_policy: Any


def _configured_ledger_path(config: dict) -> str | None:
    if publish_check_baseline(config) != PUBLISH_CHECK_BASELINE_FILE:
        return None
    return str(publish_check_ledger_path(config))


def _baseline_for_publish_check(config: dict) -> _Baseline | None:
    if publish_check_baseline(config) == PUBLISH_CHECK_BASELINE_FILE:
        entries = read_ledger(publish_check_ledger_path(config))
        if not entries:
            return None
        return _Baseline(entries[-1]["parquet_sha256"], entries[-1].get("adjustment_policy"))
    try:
        committed_manifest = _committed_manifest(config)
    except _CommittedManifestLookupFailed as exc:
        raise _CommittedManifestLookupFailed(f"cannot read the committed manifest: {exc}") from exc
    if committed_manifest is None:
        return None
    committed_lineage = committed_manifest.get("lineage", {})
    committed = committed_lineage.get("parquet_sha256") if isinstance(committed_lineage, dict) else None
    return _Baseline(committed, committed_manifest.get("adjustment_policy"))


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
    """Argparse type: parse a ``YYYY-MM-DD`` date."""
    return dt.datetime.strptime(v, "%Y-%m-%d").date()


def _now(tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.now(tz)


def parse_as_of_date(v: str) -> dt.date:
    """Argparse type for ``--as-of``: ``today`` is the current date in Asia/Tokyo, otherwise ``YYYY-MM-DD``."""
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


def _accept_audit_only_flags_forwarded_by_the_daily_wrapper(run_daily_parser: argparse.ArgumentParser) -> None:
    run_daily_parser.add_argument("--window-business-days", default=None, type=int, help=argparse.SUPPRESS)


def _add_result_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--result-json",
        default=None,
        type=Path,
        metavar="PATH",
        help=(
            "Also write the outcome and exit code as JSON to PATH "
            '(schema: jqqlib.contracts.load_schema("cli_result")).'
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``jqqlib`` command-line parser."""
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
    _accept_audit_only_flags_forwarded_by_the_daily_wrapper(daily)
    _add_result_json_flag(daily)

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
    _add_result_json_flag(audit)

    publish_check = sub.add_parser(
        "publish-check",
        help="Exit 0 if the dataset content changed vs the baseline (publish_check.git_ref, default origin/main, "
        "or the ledger with publish_check.baseline: file), 3 if unchanged (no-op gate)",
    )
    publish_check.add_argument("--config", required=True)

    ledger_cmd = sub.add_parser(
        "ledger",
        help="Manage the publish ledger at publish_check.ledger_path (the file baseline of publish-check)",
    )
    ledger_sub = ledger_cmd.add_subparsers(dest="ledger_command", required=True)
    ledger_init = ledger_sub.add_parser(
        "init",
        help="Create the ledger once: empty (next publish-check is a first publish) or seeded from a manifest",
    )
    ledger_init.add_argument("--config", required=True)
    ledger_init.add_argument(
        "--from-manifest",
        default=None,
        help="Seed the ledger with this dataset manifest, e.g. the last one published by the git baseline",
    )
    ledger_append = ledger_sub.add_parser(
        "append",
        help="Record the current dataset after a successful publish; a no-op when the last entry already has it",
    )
    ledger_append.add_argument("--config", required=True)

    publish = sub.add_parser("publish", help="Atomically publish a prepared Qlib dataset build via symlink")
    publish.add_argument("--build-provider-uri", required=True)
    publish.add_argument("--provider-uri", required=True)

    publish_paths = sub.add_parser("publish-paths", help="Print the exact validated git artifact pathspec")
    publish_paths.add_argument("--config", required=True)

    return parser


class _BelowWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def _configure_cli_logging() -> None:
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


def cli_error_exit_code(exc: BaseException) -> int:
    """Exit code for an error reported as one ``error:`` line: 1 = run failure, 2 = user error.

    ``ValueError``/``OSError`` are user errors (unreadable/invalid config, bad
    arguments, refused overwrite, rejected credentials: HTTP 401/403) except
    for the subclasses that report a failed run instead: no data fetched,
    other J-Quants API/network errors (``requests`` exceptions subclass
    ``OSError``), and unreadable Parquet (pyarrow errors subclass
    ``ValueError``). ``RuntimeError`` is a dataset-state failure (e.g. data
    that is not Qlib-ready), reported like a failed check. ``ProviderLockedError``
    exits 75: another process holds the provider lock, and running again later can succeed.
    """
    if isinstance(exc, ProviderLockedError):
        return _PROVIDER_LOCKED_EXIT_CODE
    if isinstance(exc, (RuntimeError, NoDataFetchedError)):
        return 1
    if isinstance(exc, Exception) and _error_status(exc) in _AUTH_STATUSES:
        return 2
    if _is_run_failure_from_an_already_imported_library(exc):
        return 1
    return 2


_PROVIDER_LOCKED_EXIT_CODE = 75
_RUN_FAILURE_EXCEPTION_CLASSES = (("requests", "RequestException"), ("pyarrow", "ArrowException"))


def _is_run_failure_from_an_already_imported_library(exc: BaseException) -> bool:
    for module_name, class_name in _RUN_FAILURE_EXCEPTION_CLASSES:
        error_class = getattr(sys.modules.get(module_name), class_name, None)
        if isinstance(error_class, type) and isinstance(exc, error_class):
            return True
    return False


def main() -> None:
    """Entry point of the ``jqqlib`` console script.

    Errors are printed as one ``error: <message>`` line on stderr instead of a traceback; the exit code comes from
    ``cli_error_exit_code`` (2 = user error, 1 = failed run, 75 = provider locked by another process).
    """
    args = build_parser().parse_args()
    result_json = getattr(args, "result_json", None)
    _configure_cli_logging()
    try:
        _dispatch(args)
    except SystemExit as exc:
        if result_json is not None:
            outcome = _outcome_of_exit(args.command, exc)
            error = _unexpected_exit_message(exc) if outcome == "failed" else None
            _write_cli_result(result_json, args.command, outcome, _exit_status(exc), error)
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = cli_error_exit_code(exc)
        if result_json is not None:
            _write_cli_result(result_json, args.command, "failed", exit_code, str(exc))
        raise SystemExit(exit_code) from None
    else:
        if result_json is not None:
            _write_cli_result(result_json, args.command, "completed", 0, None)
    finally:
        _teardown_cli_logging()


_CLI_RESULT_SCHEMA_VERSION = 1


def _exit_status(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _outcome_of_exit(command: str, exc: SystemExit) -> str:
    status = _exit_status(exc)
    if isinstance(exc.__cause__, ExactCalendarRequired):
        return "calendar_unavailable"
    if status == 0:
        return "completed"
    if command == "audit" and status == 1:
        return "gaps"
    return "failed"


def _unexpected_exit_message(exc: SystemExit) -> str:
    return exc.code if isinstance(exc.code, str) else f"exited with status {_exit_status(exc)}"


def _write_cli_result(path: Path, command: str, outcome: str, exit_code: int, error: str | None) -> None:
    payload = {
        "schema_version": _CLI_RESULT_SCHEMA_VERSION,
        "command": command,
        "outcome": outcome,
        "exit_code": exit_code,
        "error": error,
    }
    try:
        write_json_atomic(path, payload)
    except OSError as exc:
        print(f"Warning: could not write the result JSON {path}: {exc}", file=sys.stderr)


def _configured_qlib_dir(args: argparse.Namespace) -> Path:
    return Path(load_config(Path(args.config))["storage"]["qlib_dir"])


def _publish_target(args: argparse.Namespace) -> Path:
    return Path(args.provider_uri)


_PROVIDER_WRITTEN_BY_COMMAND: dict[str, Callable[[argparse.Namespace], Path] | None] = {
    "init-config": None,
    "run-etl": None,
    "convert": _configured_qlib_dir,
    "validate": None,
    "bootstrap-csv": _configured_qlib_dir,
    "download-csv": None,
    "run-all": _configured_qlib_dir,
    "run-daily": _configured_qlib_dir,
    "audit": None,
    "publish-check": None,
    "ledger": None,
    "publish": _publish_target,
    "publish-paths": None,
}


def _provider_lock_of(args: argparse.Namespace) -> contextlib.AbstractContextManager[None]:
    provider_of = _PROVIDER_WRITTEN_BY_COMMAND[args.command]
    if provider_of is None:
        return contextlib.nullcontext()
    return provider_write_lock(provider_of(args))


def _dispatch(args: argparse.Namespace) -> None:
    with _provider_lock_of(args):
        _run_command(args)


def _run_command(args: argparse.Namespace) -> None:
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
            print(f"Dataset content changed vs {decision.compared_against}; publish required.")
        elif rc == 3:
            print(f"No dataset content change vs {decision.compared_against}; skipping publish.")
        else:
            print(f"publish-check error: {decision.reason}", file=sys.stderr)
        raise SystemExit(rc)
    elif args.command == "ledger" and args.ledger_command == "init":
        from_manifest = Path(args.from_manifest) if args.from_manifest else None
        path = init_publish_ledger(Path(args.config), from_manifest)
        if from_manifest is None:
            print(f"Publish ledger initialised (empty): {path}")
        else:
            print(f"Publish ledger initialised: {path} (seeded from {from_manifest})")
    elif args.command == "ledger" and args.ledger_command == "append":
        path, appended = append_current_dataset_to_ledger(Path(args.config))
        if appended:
            print(f"Publish ledger entry appended: {path}")
        else:
            print(f"Publish ledger already records this dataset: {path}")
    elif args.command == "publish":
        published = publish_dataset_build(Path(args.build_provider_uri), Path(args.provider_uri))
        print(f"Published provider URI: {published}")
    elif args.command == "publish-paths":
        for pathspec in publish_pathspecs(Path(args.config)):
            print(pathspec)


if __name__ == "__main__":
    main()
