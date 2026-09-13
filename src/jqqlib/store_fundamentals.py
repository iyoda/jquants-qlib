"""Atomic store for fundamentals parquet and PIT audit JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


DEFAULT_FUNDAMENTALS_PARQUET_FILENAME = "fundamentals_asof.parquet"
DEFAULT_PIT_AUDIT_FILENAME = "fundamentals_pit_audit.json"
# Raw per-code fetch cache -- lets a killed/restarted fetch resume instead of
# re-spending already-paid-for API calls (fetch_fundamentals.cache_path).
DEFAULT_RAW_CACHE_FILENAME = "fundamentals_raw_cache.parquet"


def write_fundamentals_parquet(panel: pd.DataFrame, parquet_dir: Path) -> Path:
    """Atomically write the as-of fundamentals panel (tmp + replace), so a
    reader never observes a partially-written file."""
    parquet_dir.mkdir(parents=True, exist_ok=True)
    output_path = parquet_dir / DEFAULT_FUNDAMENTALS_PARQUET_FILENAME
    temp_output = output_path.with_suffix(output_path.suffix + ".tmp")
    panel.to_parquet(temp_output)
    temp_output.replace(output_path)
    return output_path


def write_pit_audit_report(audit: dict[str, Any], parquet_dir: Path) -> Path:
    parquet_dir.mkdir(parents=True, exist_ok=True)
    output_path = parquet_dir / DEFAULT_PIT_AUDIT_FILENAME
    temp_output = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_output.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_output.replace(output_path)
    return output_path
