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
    contains them. A ``..`` is followed the way the OS follows it, after the symlink before it.
    """
    absolute = _absolute(provider)
    name_lock = _lock_file_beside(absolute)
    tree_lock = _lock_file_beside(absolute.resolve())
    return (name_lock,) if tree_lock == name_lock else (name_lock, tree_lock)


def _absolute(provider: Path) -> Path:
    absolute = provider.expanduser().absolute()
    return absolute.resolve() if absolute.name == ".." else absolute


def _symlinked_ancestor_locks(provider: Path) -> list[Path]:
    return [
        _lock_file_beside(ancestor)
        for ancestor in reversed(_absolute(provider).parents)
        if ancestor.is_symlink() and os.access(ancestor.parent.resolve(), os.W_OK)
    ]


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

    A symlink above a provider is locked shared whatever the mode, so ``publish`` cannot point it at another build
    while the provider is in use, and two writers below one symlink do not exclude each other. A symlink in a
    directory this process cannot write, such as ``/var`` on macOS, is skipped: ``publish`` swaps a symlink by
    renaming a new one beside it, so it cannot swap that one either.

    A symlink can be pointed at another build between resolving it and locking it. So once every lock is held, the
    lock files are resolved again, and on a change all are released and taken again. While the name lock is held,
    ``publish`` cannot point the symlink anywhere else.
    """
    requested = tuple(requests)
    for _ in range(_RESOLVE_ATTEMPTS):
        wanted = _lock_files_of(requested)
        with ExitStack() as stack:
            for lock_path in sorted(wanted):
                mode, provider = wanted[lock_path]
                _flock(lock_path, mode, provider, stack)
            if _lock_files_of(requested) == wanted:
                yield
                return
    raise ProviderLockedError(
        f"Qlib providers {[str(provider) for provider, _ in requested]} kept resolving to another build while "
        "being locked; not waiting. Run again after the publish that moves them ends."
    )


_RESOLVE_ATTEMPTS = 3


def _lock_files_of(requests: tuple[tuple[Path, LockMode], ...]) -> dict[Path, tuple[LockMode, Path]]:
    wanted: dict[Path, tuple[LockMode, Path]] = {}
    for provider, mode in requests:
        locks = [(lock_path, mode) for lock_path in provider_lock_paths(provider)]
        locks += [(lock_path, LockMode.SHARED) for lock_path in _symlinked_ancestor_locks(provider)]
        for lock_path, lock_mode in locks:
            held = wanted.get(lock_path)
            if held is None or lock_mode > held[0]:
                wanted[lock_path] = (lock_mode, provider)
    return wanted


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
