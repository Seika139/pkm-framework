"""Cross-process lock for operations that write to a Storage repository."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_framework.paths import VaultPathError, ensure_no_symlink_components


class StorageWriteLockError(RuntimeError):
    """A Storage write lock could not be prepared or acquired."""


class StorageWriteLockTimeout(StorageWriteLockError):
    """Another process holds the Storage write lock past the requested timeout."""


def _lock_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageWriteLockError("Storage write lock path must be a single-link regular file.")
    return info.st_dev, info.st_ino


@contextmanager
def storage_write_lock(storage_root: str | Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Lock multi-file Storage writes through SQLite's cross-process transaction lock.

    The lock database is a local runtime file under ``.pkm/cache`` and is excluded
    from Git. The context manager deliberately hides its SQLite connection so callers
    cannot accidentally commit or release the transaction early.
    """

    if timeout < 0:
        raise ValueError("timeout must be non-negative.")

    root = Path(os.path.abspath(Path(storage_root).expanduser()))
    if root.is_symlink() or not root.is_dir():
        raise StorageWriteLockError("Storage root must be a real directory.")

    cache = root / ".pkm" / "cache"
    lock_path = cache / "wiki-writer.sqlite"
    connection: sqlite3.Connection | None = None
    try:
        ensure_no_symlink_components(cache, root)
        cache.mkdir(parents=True, exist_ok=True)
        ensure_no_symlink_components(lock_path, root)
        initial_identity = _lock_file_identity(lock_path)

        timeout_ms = int(timeout * 1000)
        connection = sqlite3.connect(lock_path, timeout=timeout, isolation_level=None)
        connected_identity = _lock_file_identity(lock_path)
        if connected_identity is None or (
            initial_identity is not None and connected_identity != initial_identity
        ):
            raise StorageWriteLockError("Storage write lock path changed while opening the database.")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        if _lock_file_identity(lock_path) != connected_identity:
            raise StorageWriteLockError("Storage write lock path changed before acquiring the lock.")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StorageWriteLockTimeout("Timed out waiting for the Storage write lock.") from exc
            raise StorageWriteLockError("Could not acquire the Storage write lock.") from exc

        ensure_no_symlink_components(lock_path, root)
        if _lock_file_identity(lock_path) != connected_identity:
            raise StorageWriteLockError("Storage write lock path changed while acquiring the lock.")
    except StorageWriteLockError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error, VaultPathError) as exc:
        if connection is not None:
            connection.close()
        raise StorageWriteLockError("Could not safely prepare the Storage write lock.") from exc

    try:
        yield
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()
