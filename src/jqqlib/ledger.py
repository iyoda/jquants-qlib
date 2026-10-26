"""Append-only JSON Lines ledger of published datasets, the ``file`` baseline of ``publish-check``."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LEDGER_SCHEMA_VERSION = 1
LEDGER_KIND_PUBLISH = "publish"
_OPTIONAL_TEXT_FIELDS = (
    "build_id",
    "dataset_id",
    "generated_at",
    "date_end",
    "quality_status",
    "jqqlib_version",
    "jqqlib_source_commit",
)
_ENTRY_FIELDS = frozenset(
    {"schema_version", "kind", "recorded_at", "parquet_sha256", "adjustment_policy", *_OPTIONAL_TEXT_FIELDS}
)


class LedgerError(RuntimeError):
    """The publish ledger is missing, unreadable, malformed or unwritable, or an entry cannot be built."""


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_entry_from_manifest(
    manifest: dict[str, Any],
    *,
    jqqlib_version: str | None,
    jqqlib_source_commit: str | None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``publish`` ledger entry from a dataset manifest.

    The versions describe the jqqlib that produced the dataset; pass ``None`` when it is unknown, as for a ledger
    seeded from an older manifest. They are recorded only and never take part in the change decision. Raises
    ``LedgerError`` when the manifest has no ``lineage.parquet_sha256``, no object ``adjustment_policy`` or a
    non-string identity field.
    """
    lineage = manifest.get("lineage")
    parquet_sha256 = lineage.get("parquet_sha256") if isinstance(lineage, dict) else None
    if not isinstance(parquet_sha256, str) or not parquet_sha256:
        raise LedgerError("dataset manifest missing lineage.parquet_sha256")
    dataset = manifest.get("dataset")
    entry = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "kind": LEDGER_KIND_PUBLISH,
        "recorded_at": recorded_at or _utc_now_iso(),
        "build_id": manifest.get("build_id"),
        "dataset_id": manifest.get("dataset_id"),
        "generated_at": manifest.get("generated_at"),
        "date_end": dataset.get("date_end") if isinstance(dataset, dict) else None,
        "quality_status": manifest.get("quality_status"),
        "parquet_sha256": parquet_sha256,
        "adjustment_policy": manifest.get("adjustment_policy"),
        "jqqlib_version": jqqlib_version,
        "jqqlib_source_commit": jqqlib_source_commit,
    }
    problem = _entry_problem(entry)
    if problem is not None:
        raise LedgerError(f"dataset manifest cannot become a ledger entry: {problem}")
    return entry


def _entry_problem(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "not a JSON object"
    version = entry.get("schema_version")
    if type(version) is not int or version != LEDGER_SCHEMA_VERSION:
        return f"unsupported schema_version {version!r} (expected {LEDGER_SCHEMA_VERSION})"
    if entry.get("kind") != LEDGER_KIND_PUBLISH:
        return f"unsupported kind {entry.get('kind')!r} (expected {LEDGER_KIND_PUBLISH!r})"
    missing = sorted(_ENTRY_FIELDS - entry.keys())
    if missing:
        return f"missing fields {', '.join(missing)}"
    for key in ("parquet_sha256", "recorded_at"):
        if not isinstance(entry[key], str) or not entry[key]:
            return f"{key} is not a non-empty string"
    if not isinstance(entry["adjustment_policy"], dict):
        return "adjustment_policy is not an object"
    for key in _OPTIONAL_TEXT_FIELDS:
        if entry[key] is not None and not isinstance(entry[key], str):
            return f"{key} is neither a string nor null"
    return None


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise LedgerError(
            f"publish ledger {path} is a symlink; point publish_check.ledger_path at the real file so reads, "
            "the atomic rewrite and the lock all use one path"
        )


def _require_existing_ledger(path: Path) -> None:
    try:
        _refuse_symlink(path)
        exists = path.exists()
    except OSError as exc:
        raise LedgerError(f"publish ledger unreadable: {path}: {exc}") from exc
    if not exists:
        raise LedgerError(f"publish ledger missing: {path} (create it with `jqqlib ledger init`)")


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Return every entry of the ledger at ``path``, oldest first; an empty file has no entries.

    Fails closed with ``LedgerError`` when the file is missing, a symlink or unreadable, does not end with a
    newline (a truncated write), or any line is blank, not JSON or not a complete valid entry, because a damaged
    ledger must never pass for a first publish.
    """
    _require_existing_ledger(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"publish ledger unreadable: {path}: {exc}") from exc
    if not text:
        return []
    if not text.endswith("\n"):
        raise LedgerError(f"publish ledger {path} does not end with a newline; the last write may be truncated")
    entries = []
    for number, line in enumerate(text[:-1].split("\n"), start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"publish ledger {path} line {number} is not JSON: {exc}") from exc
        problem = _entry_problem(entry)
        if problem is not None:
            raise LedgerError(f"publish ledger {path} line {number}: {problem}")
        entries.append(entry)
    return entries


def _encode(entry: dict[str, Any]) -> bytes:
    return (json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _replace_atomically(path: Path, content: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with tmp.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def init_ledger(path: Path, entry: dict[str, Any] | None = None) -> Path:
    """Create the ledger at ``path``, empty or seeded with ``entry``, creating its parent directory.

    An empty ledger means "initialised, nothing published yet", so the next ``publish-check`` reports a first
    publish. Raises ``FileExistsError`` when the ledger (or a symlink at its path) already exists and
    ``LedgerError`` for an invalid entry or a failed write.
    """
    if entry is not None:
        problem = _entry_problem(entry)
        if problem is not None:
            raise LedgerError(f"invalid ledger entry: {problem}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path):
            already_exists = path.exists() or path.is_symlink()
            if not already_exists:
                _replace_atomically(path, b"" if entry is None else _encode(entry))
    except OSError as exc:
        raise LedgerError(f"cannot write the publish ledger {path}: {exc}") from exc
    if already_exists:
        raise FileExistsError(f"{path} already exists; the publish ledger is initialised only once.")
    return path


def _records_the_same_dataset(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["parquet_sha256"] == right["parquet_sha256"] and left["adjustment_policy"] == right["adjustment_policy"]


def append_ledger_entry(path: Path, entry: dict[str, Any]) -> bool:
    """Append ``entry`` to the existing ledger at ``path``; return ``False`` when the last entry already records it.

    The whole file is rewritten to a temporary file and renamed under an exclusive lock. An interrupted append
    stops either before the rename (the previous ledger stays) or after it (the new ledger is complete), and a
    re-run after a completed append adds nothing. Raises ``LedgerError`` for a missing, symlinked or damaged
    ledger, an invalid entry or a failed write.
    """
    problem = _entry_problem(entry)
    if problem is not None:
        raise LedgerError(f"invalid ledger entry: {problem}")
    _require_existing_ledger(path)
    try:
        with _locked(path):
            entries = read_ledger(path)
            if entries and _records_the_same_dataset(entries[-1], entry):
                return False
            _replace_atomically(path, path.read_bytes() + _encode(entry))
    except OSError as exc:
        raise LedgerError(f"cannot write the publish ledger {path}: {exc}") from exc
    return True


_REPOSITORY_LOCATION_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def _environment_without_git_repository_overrides() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _REPOSITORY_LOCATION_VARIABLES}


def jqqlib_source_commit(package_dir: Path | None = None) -> str | None:
    """Return the git commit of the jquants-qlib checkout that ``package_dir`` (default: this package) lives in.

    Returns ``None`` when git is unavailable or the package is not the ``src/jqqlib`` directory of a work tree,
    such as an installed wheel inside some other repository's virtual environment. Inherited variables that select
    another repository (``GIT_DIR``, ``GIT_WORK_TREE`` and the like, set inside git hooks) are dropped; other git
    configuration from the environment, such as ``safe.directory`` via ``GIT_CONFIG_*``, still applies.
    """
    package = (package_dir or Path(__file__).parent).resolve()
    env = _environment_without_git_repository_overrides()
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(package), "rev-parse", "--show-toplevel"], capture_output=True, text=True, env=env
        )
        if toplevel.returncode != 0:
            return None
        if (Path(toplevel.stdout.strip()) / "src" / "jqqlib").resolve() != package:
            return None
        head = subprocess.run(
            ["git", "-C", str(package), "rev-parse", "HEAD"], capture_output=True, text=True, env=env
        )
    except OSError:
        return None
    commit = head.stdout.strip()
    return commit if head.returncode == 0 and commit else None
