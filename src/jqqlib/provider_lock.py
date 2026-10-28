"""Advisory lock beside a Qlib provider: one writer or any number of readers at a time, never waiting."""

from __future__ import annotations

import enum
import fcntl
import os
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path


class ProviderLockedError(RuntimeError):
    """Another process holds the lock of the provider, and this one does not wait for it."""


class LockMode(enum.IntEnum):
    """How a provider is locked: shared by readers, or exclusive for one writer. Exclusive sorts higher."""

    SHARED = fcntl.LOCK_SH
    EXCLUSIVE = fcntl.LOCK_EX


def _lock_file_beside(path: Path) -> Path:
    return path.parent.resolve() / f".{path.name}.write.lock"


def provider_lock_paths(provider: Path) -> tuple[Path, ...]:
    """Return the lock files of ``provider``: one for its name, and one for the tree it resolves to.

    Each is ``.<name>.write.lock`` in the resolved parent directory. The name lock does not resolve the last path
    component, so a provider symlink keeps one lock while ``publish`` points it at another build. The tree lock
    resolves it, so a symlink name and the real build path it points at still exclude each other. When the provider
    is not a symlink, both are the same file. Both are outside the provider tree, so a copy of the tree never
    contains them.
    """
    absolute = Path(os.path.abspath(provider.expanduser()))
    name_lock = _lock_file_beside(absolute)
    tree_lock = _lock_file_beside(absolute.resolve())
    return (name_lock,) if tree_lock == name_lock else (name_lock, tree_lock)


def _flock(lock_path: Path, mode: LockMode, provider: Path, stack: ExitStack) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = stack.enter_context(lock_path.open("a"))
    try:
        fcntl.flock(lock.fileno(), mode | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ProviderLockedError(
            f"Qlib provider {provider} is in use by another jqqlib writer or reader (lock {lock_path}); "
            "not waiting. Run again after that process ends."
        ) from None
    stack.callback(fcntl.flock, lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def provider_locks(requests: Iterable[tuple[Path, LockMode]]) -> Iterator[None]:
    """Hold every lock of every requested provider while the ``with`` block runs.

    A lock file requested in both modes is taken exclusive. Lock files are taken in sorted path order. Raises
    ``ProviderLockedError`` at once on the first lock another process holds, and releases the ones already taken.
    """
    wanted: dict[Path, tuple[LockMode, Path]] = {}
    for provider, mode in requests:
        for lock_path in provider_lock_paths(provider):
            held = wanted.get(lock_path)
            if held is None or mode > held[0]:
                wanted[lock_path] = (mode, provider)
    with ExitStack() as stack:
        for lock_path in sorted(wanted):
            mode, provider = wanted[lock_path]
            _flock(lock_path, mode, provider, stack)
        yield


def provider_write_lock(provider: Path) -> AbstractContextManager[None]:
    """Hold the exclusive lock of ``provider`` while the ``with`` block writes it.

    Raises ``ProviderLockedError`` at once when another writer or a reader holds the lock. Every ``jqqlib`` command
    that writes a provider holds this lock for the whole command.
    """
    return provider_locks([(provider, LockMode.EXCLUSIVE)])


def provider_read_lock(provider: Path) -> AbstractContextManager[None]:
    """Hold the shared lock of ``provider`` while the ``with`` block reads or copies it.

    Readers do not exclude each other. Raises ``ProviderLockedError`` at once when a writer holds the lock, and
    while the block runs, every writer command fails instead of changing the provider.
    """
    return provider_locks([(provider, LockMode.SHARED)])
