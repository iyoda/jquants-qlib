"""Atomic symlink publish onto a prepared provider."""

from __future__ import annotations

import uuid
from pathlib import Path


def publish_dataset_build(build_provider_uri: Path, provider_uri: Path) -> Path:
    """Atomically point provider_uri at a prepared build directory.

    This only replaces a missing path or an existing symlink. It refuses to
    replace a real directory so existing datasets are not removed accidentally.
    """
    build_provider_uri = build_provider_uri.expanduser().resolve()
    provider_uri = provider_uri.expanduser()

    if not build_provider_uri.is_dir():
        raise FileNotFoundError(f"Build provider directory not found: {build_provider_uri}")
    if provider_uri.exists() and not provider_uri.is_symlink():
        raise FileExistsError(f"Refusing to replace non-symlink provider path: {provider_uri}")

    provider_uri.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = provider_uri.parent / f".{provider_uri.name}.tmp-{uuid.uuid4().hex}"
    tmp_link.symlink_to(build_provider_uri, target_is_directory=True)
    tmp_link.replace(provider_uri)
    return provider_uri
