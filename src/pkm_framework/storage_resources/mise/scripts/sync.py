#!/usr/bin/env python3
"""Commit the approved PKM content and synchronize the configured Git upstream."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pkm_framework.locking import StorageWriteLockError, StorageWriteLockTimeout, storage_write_lock
from pkm_framework.paths import VaultPathError, ensure_no_symlink_components


ALLOWLIST = (
    "pkm-storage-vault/raw/clips/",
    "pkm-storage-vault/assets/",
    "pkm-storage-vault/wiki/",
)
GIT_COMMAND_TIMEOUT_SECONDS = 300
LOGGER = logging.getLogger("pkm-storage.sync")


class SyncError(RuntimeError):
    """The scheduled sync cannot safely continue."""


def configure_logging(root: Path) -> None:
    log_dir = root / ".pkm" / "cache" / "logs"
    ensure_no_symlink_components(log_dir, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    ensure_no_symlink_components(log_dir, root)
    log_file = log_dir / "sync.log"
    ensure_no_symlink_components(log_file, root)
    if log_file.exists() and not log_file.is_file():
        raise OSError("Sync log path must be a regular file.")

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    LOGGER.handlers.clear()
    LOGGER.addHandler(stream)
    LOGGER.addHandler(file_handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = args[0] if args else "command"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {command} timed out after {GIT_COMMAND_TIMEOUT_SECONDS} seconds.") from None
    if check and result.returncode != 0:
        raise SyncError(
            f"git {command} failed with exit code {result.returncode}; review Git state and hooks."
        )
    return result


def ensure_repository(root: Path) -> None:
    result = run_git(root, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != root.resolve():
        raise SyncError("Storage directory is not the Git repository root.")


def git_path(root: Path, name: str) -> Path:
    result = run_git(root, "rev-parse", "--git-path", name)
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else root / path


def ensure_no_operation_in_progress(root: Path) -> None:
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "BISECT_LOG"):
        if git_path(root, marker).exists():
            raise SyncError(f"Git operation is already in progress ({marker}); resolve it manually before sync.")


def ensure_no_staged_changes(root: Path) -> None:
    result = run_git(root, "diff", "--cached", "--quiet", check=False)
    if result.returncode == 1:
        raise SyncError("The index already contains staged changes; review and commit them manually before sync.")
    if result.returncode != 0:
        raise SyncError("Could not inspect the Git index safely.")


def changed_paths(root: Path) -> list[str]:
    result = run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    records = result.stdout.encode("utf-8", errors="surrogateescape").split(b"\0")
    paths: list[str] = []
    for record in records:
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise SyncError("Git returned an unrecognized status record; stopping without changing the repository.")
        status = record[:2]
        if b"R" in status or b"C" in status:
            raise SyncError("Renames and copies require manual review before scheduled sync.")
        if status not in {b"??", b" M", b" D", b" T"}:
            raise SyncError("Staged or unmerged changes were detected; review them manually before sync.")
        path = os.fsdecode(record[3:])
        if not path.startswith(ALLOWLIST):
            raise SyncError("Changes outside the scheduled commit allowlist require manual review.")
        paths.append(path)
    return paths


def configured_upstream(root: Path) -> tuple[str, str]:
    branch_result = run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch_result.returncode != 0:
        raise SyncError("The current branch is detached; check out a branch with an upstream before enabling sync.")
    branch = branch_result.stdout.strip()
    upstream_result = run_git(root, "rev-parse", "--symbolic-full-name", "--abbrev-ref", "@{upstream}", check=False)
    if upstream_result.returncode != 0:
        raise SyncError("No Git upstream is configured; set a remote and upstream before scheduled sync.")

    remote_result = run_git(root, "config", "--get", f"branch.{branch}.remote", check=False)
    merge_result = run_git(root, "config", "--get", f"branch.{branch}.merge", check=False)
    if remote_result.returncode != 0 or merge_result.returncode != 0:
        raise SyncError("No Git remote/upstream is configured; set it before scheduled sync.")
    remote = remote_result.stdout.strip()
    merge_ref = merge_result.stdout.strip()
    if not remote or remote == "." or not merge_ref.startswith("refs/heads/"):
        raise SyncError("The configured upstream is not a remote branch; scheduled sync requires a remote branch.")
    if run_git(root, "remote", "get-url", remote, check=False).returncode != 0:
        raise SyncError("The configured upstream remote does not exist.")
    return remote, merge_ref


def sync(root: Path) -> int:
    try:
        ensure_repository(root)
        with storage_write_lock(root, timeout=1.0):
            configure_logging(root)
            LOGGER.info("Starting scheduled sync.")
            ensure_no_operation_in_progress(root)
            ensure_no_staged_changes(root)
            initial_changes = changed_paths(root)
            remote, merge_ref = configured_upstream(root)

            if initial_changes:
                run_git(root, "add", "-A", "--", *ALLOWLIST)
                ensure_no_staged_changes_after_own_add(root)
                timestamp = datetime.now().astimezone().isoformat(timespec="minutes")
                # Keep the final commit boundary path-limited even if the index changes after preflight.
                run_git(
                    root,
                    "commit",
                    "--only",
                    "-m",
                    f"PKM storage scheduled sync ({timestamp})",
                    "--",
                    *ALLOWLIST,
                )
                LOGGER.info("Committed %d changed path(s) from the approved directories.", len(initial_changes))
            else:
                LOGGER.info("No local content changes to commit.")

            LOGGER.info("Fetching configured remote.")
            run_git(root, "fetch", remote)
            upstream = f"{remote}/{merge_ref.removeprefix('refs/heads/')}"
            LOGGER.info("Merging configured upstream.")
            run_git(root, "merge", "--no-edit", "--no-autostash", "--no-overwrite-ignore", upstream)
            LOGGER.info("Pushing the current branch to configured upstream.")
            run_git(root, "push", remote, f"HEAD:{merge_ref}")
            LOGGER.info("Scheduled sync completed.")
            return 0
    except StorageWriteLockTimeout:
        print("Storage is being updated; scheduled sync skipped and will retry on the next run.", file=sys.stderr)
        return 0
    except (StorageWriteLockError, VaultPathError) as exc:
        print(f"Scheduled sync stopped safely: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        if LOGGER.handlers:
            LOGGER.error("Scheduled sync stopped safely: %s", exc)
        else:
            print(f"Scheduled sync stopped safely: {exc}", file=sys.stderr)
        return 2
    except SyncError as exc:
        if LOGGER.handlers:
            LOGGER.error("Scheduled sync stopped safely: %s", exc)
        else:
            print(f"Scheduled sync stopped safely: {exc}", file=sys.stderr)
        return 2


def ensure_no_staged_changes_after_own_add(root: Path) -> None:
    result = run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    records = result.stdout.encode("utf-8", errors="surrogateescape").split(b"\0")
    for record in records:
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise SyncError("Git returned an unrecognized status record after staging.")
        status = record[:2]
        if b"R" in status or b"C" in status:
            raise SyncError("A rename or copy occurred while staging; review the index manually.")
        if status[0:1] not in {b"A", b"M", b"D", b"T"} or status[1:2] != b" ":
            raise SyncError("A file changed while being staged; review the working tree and index before sync.")
        path = os.fsdecode(record[3:])
        if not path.startswith(ALLOWLIST):
            raise SyncError("Staging included a path outside the scheduled allowlist.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", required=True, type=Path, help="Storage repository root")
    args = parser.parse_args()
    return sync(args.storage.expanduser().absolute())


if __name__ == "__main__":
    raise SystemExit(main())
