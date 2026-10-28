"""Advisory lock beside a Qlib provider: one writer or any number of readers at a time, never waiting."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path


class ProviderLockedError(RuntimeError):
    """Another process holds the lock of the provider, and this one does not wait for it."""


def provider_lock_path(provider: Path) -> Path:
    """Return the lock file of ``provider``: ``.<name>.write.lock`` in the resolved parent directory of ``provider``.

    The last path component is not resolved, so a provider symlink keeps one lock while ``publish`` points it at
    another build. The file is outside the provider tree, so a copy of the tree never contains it.
    """
    absolute = Path(os.path.abspath(provider.expanduser()))
    return absolute.parent.resolve() / f".{absolute.name}.write.lock"


@contextmanager
def _provider_lock(provider: Path, operation: int) -> Iterator[None]:
    lock_path = provider_lock_path(provider)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderLockedError(
                f"Qlib provider {provider} is in use by another jqqlib writer or reader (lock {lock_path}); "
                "not waiting. Run again after that process ends."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def provider_write_lock(provider: Path) -> AbstractContextManager[None]:
    """Hold the exclusive lock of ``provider`` while the ``with`` block writes it.

    Raises ``ProviderLockedError`` at once when another writer or a reader holds the lock. Every ``jqqlib`` command
    that writes a provider holds this lock for the whole command.
    """
    return _provider_lock(provider, fcntl.LOCK_EX)


def provider_read_lock(provider: Path) -> AbstractContextManager[None]:
    """Hold the shared lock of ``provider`` while the ``with`` block reads or copies it.

    Readers do not exclude each other. Raises ``ProviderLockedError`` at once when a writer holds the lock, and
    while the block runs, every writer command fails instead of changing the provider.
    """
    return _provider_lock(provider, fcntl.LOCK_SH)
