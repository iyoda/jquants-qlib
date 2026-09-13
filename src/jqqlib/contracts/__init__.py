"""Packaged JSON schemas for dataset artifacts."""

import json
from importlib.resources import files
from typing import Any

_SCHEMA_NAMES = {"dataset_manifest", "dataset_quality_report"}


def load_schema(name: str) -> dict[str, Any]:
    """Load a dataset schema by name; raise ValueError for unknown names."""
    if name not in _SCHEMA_NAMES:
        raise ValueError(f"Unknown dataset schema: {name}")
    resource = files("jqqlib.contracts").joinpath(f"{name}.schema.json")
    schema: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    return schema
