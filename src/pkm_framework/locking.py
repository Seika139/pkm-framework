"""Cross-process lock for operations that write to a Storage repository."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_framework.paths import VaultPathError, ensure_no_symlink_components


class StorageWriteLockError(RuntimeError):
    """A Storage write lock could not be prepared or acquired."""


class StorageWriteLockTimeout(StorageWriteLockError):
    """Another process holds the Storage write lock past the requested timeout."""


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
        if lock_path.exists() and not lock_path.is_file():
            raise StorageWriteLockError("Storage write lock path must be a regular file.")

        timeout_ms = int(timeout * 1000)
        connection = sqlite3.connect(lock_path, timeout=timeout, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StorageWriteLockTimeout("Timed out waiting for the Storage write lock.") from exc
            raise StorageWriteLockError("Could not acquire the Storage write lock.") from exc

        ensure_no_symlink_components(lock_path, root)
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
