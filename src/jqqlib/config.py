"""YAML config load and qlib.adjustment validation."""

from __future__ import annotations

from pathlib import Path

QLIB_ADJUSTMENT_VENDOR_FACTOR = "vendor_factor"
QLIB_ADJUSTMENT_NONE = "none"
DEFAULT_QLIB_ADJUSTMENT = QLIB_ADJUSTMENT_VENDOR_FACTOR
VALID_QLIB_ADJUSTMENTS = frozenset({QLIB_ADJUSTMENT_VENDOR_FACTOR, QLIB_ADJUSTMENT_NONE})
DEFAULT_PUBLISH_CHECK_GIT_REF = "origin/main"
PUBLISH_CHECK_BASELINE_GIT = "git"
PUBLISH_CHECK_BASELINE_FILE = "file"
DEFAULT_PUBLISH_CHECK_BASELINE = PUBLISH_CHECK_BASELINE_GIT
VALID_PUBLISH_CHECK_BASELINES = frozenset({PUBLISH_CHECK_BASELINE_GIT, PUBLISH_CHECK_BASELINE_FILE})


def qlib_adjustment(config: dict) -> str:
    """Return the validated ``qlib.adjustment``, ``vendor_factor`` when it or the ``qlib`` section is unset or null.

    Raises ``ValueError`` when ``qlib`` is not a mapping or the value is neither ``vendor_factor`` nor ``none``, and
    ``TypeError`` when the value is unhashable, such as a list.
    """
    qlib = config.get("qlib", {})
    if qlib is None:
        qlib = {}
    if not isinstance(qlib, dict):
        raise ValueError("qlib configuration must be a mapping.")
    value = qlib.get("adjustment", DEFAULT_QLIB_ADJUSTMENT)
    if value is None:
        value = DEFAULT_QLIB_ADJUSTMENT
    if value not in VALID_QLIB_ADJUSTMENTS:
        allowed = ", ".join(sorted(VALID_QLIB_ADJUSTMENTS))
        raise ValueError(f"qlib.adjustment must be one of: {allowed}.")
    return str(value)


def load_config(path: Path) -> dict:
    """Load and validate a YAML configuration file.

    ``storage.parquet_dir`` and ``storage.qlib_dir`` are required; ``storage.audit_dir``, ``qlib.adjustment``,
    ``publish_check.git_ref`` and ``publish_check.baseline`` get their defaults. Relative storage and ledger paths
    stay relative to the working directory. An empty ``publish_check`` section (``null``) gets the defaults.

    Raises ``FileNotFoundError`` for a missing file and ``ValueError`` for invalid YAML, a document or a ``storage``
    or ``qlib`` section that is not a mapping, a ``publish_check`` section that is neither a mapping nor empty, a
    missing or invalid storage path, an unknown ``qlib.adjustment``, an invalid ``git_ref``, an unknown
    ``publish_check.baseline``, an invalid ``publish_check.ledger_path`` or a ``file`` baseline without one. An
    unhashable ``qlib.adjustment`` such as a list raises ``TypeError``.
    """
    import yaml

    with path.open("r", encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("configuration must be a mapping.")
    storage = config.setdefault("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("storage configuration must be a mapping.")
    for key in ("parquet_dir", "qlib_dir"):
        if key not in storage:
            raise ValueError(f"storage.{key} is required.")
    storage.setdefault("audit_dir", "./data/audit")
    for key in ("parquet_dir", "qlib_dir", "audit_dir"):
        value = storage[key]
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"storage.{key} must be a non-empty path string without null bytes.")
    qlib = config.setdefault("qlib", {})
    if not isinstance(qlib, dict):
        raise ValueError("qlib configuration must be a mapping.")
    qlib.setdefault("adjustment", DEFAULT_QLIB_ADJUSTMENT)
    qlib_adjustment(config)
    publish_check = config.setdefault("publish_check", {})
    if publish_check is None:
        publish_check = config["publish_check"] = {}
    if not isinstance(publish_check, dict):
        raise ValueError("publish_check configuration must be a mapping.")
    publish_check.setdefault("git_ref", DEFAULT_PUBLISH_CHECK_GIT_REF)
    publish_check_git_ref(config)
    if publish_check.get("baseline") is None:
        publish_check["baseline"] = DEFAULT_PUBLISH_CHECK_BASELINE
    baseline = publish_check_baseline(config)
    if baseline == PUBLISH_CHECK_BASELINE_FILE or publish_check.get("ledger_path") is not None:
        publish_check_ledger_path(config)
    return config


def publish_check_git_ref(config: dict) -> str:
    """Return the validated git revision that ``publish-check`` compares against."""
    section = config.get("publish_check") or {}
    if not isinstance(section, dict):
        raise ValueError("publish_check configuration must be a mapping.")
    ref = section.get("git_ref", DEFAULT_PUBLISH_CHECK_GIT_REF)
    if ref is None:
        ref = DEFAULT_PUBLISH_CHECK_GIT_REF
    if not isinstance(ref, str) or not ref.strip() or any(ch.isspace() for ch in ref) or ref.startswith("-"):
        raise ValueError("publish_check.git_ref must be a git revision without whitespace, e.g. origin/main.")
    return ref


def publish_check_baseline(config: dict) -> str:
    """Return the validated baseline ``publish-check`` compares against: ``git`` (the default) or ``file``."""
    section = config.get("publish_check") or {}
    if not isinstance(section, dict):
        raise ValueError("publish_check configuration must be a mapping.")
    value = section.get("baseline")
    if value is None:
        return DEFAULT_PUBLISH_CHECK_BASELINE
    if not isinstance(value, str) or value not in VALID_PUBLISH_CHECK_BASELINES:
        allowed = ", ".join(sorted(VALID_PUBLISH_CHECK_BASELINES))
        raise ValueError(f"publish_check.baseline must be one of: {allowed}.")
    return value


def publish_check_ledger_path(config: dict) -> Path:
    """Return the configured publish ledger path, relative to the working directory when it is relative.

    Raises ``ValueError`` when ``publish_check.ledger_path`` is unset or is not a non-empty path string.
    """
    section = config.get("publish_check") or {}
    if not isinstance(section, dict):
        raise ValueError("publish_check configuration must be a mapping.")
    value = section.get("ledger_path")
    if value is None:
        raise ValueError("publish_check.ledger_path is required for the publish ledger (publish_check.baseline: file).")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("publish_check.ledger_path must be a non-empty path string without null bytes.")
    return Path(value)
