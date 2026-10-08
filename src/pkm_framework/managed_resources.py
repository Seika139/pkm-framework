"""Install the explicitly reserved Storage files shipped by PKM Framework."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import tempfile
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any

from pkm_framework.locking import StorageWriteLockError, storage_write_lock


RESOURCE_PACKAGE_DIR = "storage_resources"
RESOURCE_MANIFEST = "manifest.json"
INSTALLED_MANIFEST = ".pkm/managed-resources.json"
SETUP_MARKER = ".pkm/setup-incomplete"
TRANSACTION_JOURNAL = ".pkm/cache/managed-resources.transaction.json"
GITIGNORE_PATH = ".gitignore"
GITIGNORE_START = "# BEGIN PKM Framework managed files"
GITIGNORE_END = "# END PKM Framework managed files"
OWNER = "pkm-framework"
FORMAT_VERSION = 1
REPAIR_HINT = "Run `mise run setup` to restore the Framework runtime and managed files."


class ManagedResourceError(RuntimeError):
    """A managed resource could not be safely installed or verified."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative_path(raw_path: Any) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise ManagedResourceError("Framework resource manifest contains an invalid path.")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or path.as_posix() != raw_path or any(part in {"", ".", ".."} for part in path.parts):
        raise ManagedResourceError(f"Framework resource path is not relative and safe: {raw_path!r}")
    allowed = (path.parts[:2] == ("mise", "tasks") and path.as_posix() != "mise/tasks/setup.sh") or (
        path.parts[:2] == ("mise", "scripts")
    ) or path.as_posix() == "pkm-storage-vault/AGENTS.framework.md"
    if not allowed:
        raise ManagedResourceError(f"Framework resource path is outside the reserved paths: {raw_path}")
    return path


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_attribute)


def _check_path(path: Path, root: Path) -> Path:
    """Reject links/reparse points and paths resolving outside the Storage root."""

    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ManagedResourceError(f"Managed path escapes Storage root: {path}") from exc

    try:
        root_info = root.lstat()
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManagedResourceError(f"Storage root cannot be safely resolved: {root}") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or _is_reparse_point(root_info)
        or not stat.S_ISDIR(root_info.st_mode)
    ):
        raise ManagedResourceError(f"Storage root must be a real directory: {root}")

    current = root
    nearest_existing = root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ManagedResourceError(f"Managed path cannot be safely inspected: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise ManagedResourceError(f"Managed resource installation refuses links and reparse points: {current}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ManagedResourceError(f"Managed resource parent is not a directory: {current}")
        try:
            resolved_component = current.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ManagedResourceError(f"Managed path component cannot be safely resolved: {current}") from exc
        if not resolved_component.is_relative_to(resolved_root):
            raise ManagedResourceError(f"Managed path component resolves outside Storage root: {current}")
        nearest_existing = current

    try:
        resolved_existing = nearest_existing.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManagedResourceError(f"Managed path has no safely resolvable parent: {path}") from exc
    if not resolved_existing.is_relative_to(resolved_root):
        raise ManagedResourceError(f"Managed path resolves outside Storage root: {path}")
    return path


def _root(path: Path) -> Path:
    root = Path(os.path.abspath(path.expanduser()))
    try:
        info = root.lstat()
    except OSError as exc:
        raise ManagedResourceError("Storage root must be a real directory.") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise ManagedResourceError("Storage root must be a real directory.")
    return root


def _package_resources() -> dict[str, dict[str, Any]]:
    source_root = resources.files("pkm_framework").joinpath(RESOURCE_PACKAGE_DIR)
    try:
        manifest = json.loads(source_root.joinpath(RESOURCE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise ManagedResourceError("Framework Storage resource manifest is missing or unreadable.") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT_VERSION
        or manifest.get("owner") != OWNER
    ):
        raise ManagedResourceError("Framework Storage resource manifest has an unsupported format.")
    entries = manifest.get("resources")
    if not isinstance(entries, list):
        raise ManagedResourceError("Framework Storage resource manifest must contain a resources list.")

    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManagedResourceError("Framework Storage resource manifest contains an invalid entry.")
        relative = _safe_relative_path(entry.get("path")).as_posix()
        if relative in result:
            raise ManagedResourceError(f"Duplicate Framework resource path: {relative}")
        executable = entry.get("executable", False)
        if not isinstance(executable, bool):
            raise ManagedResourceError(f"Invalid executable flag for Framework resource: {relative}")
        source = source_root.joinpath(*PurePosixPath(relative).parts)
        try:
            content = source.read_bytes()
        except (OSError, AttributeError) as exc:
            raise ManagedResourceError(f"Packaged Framework resource is missing: {relative}") from exc
        declared_hash = entry.get("sha256")
        actual_hash = _sha256(content)
        if not isinstance(declared_hash, str) or declared_hash != actual_hash:
            raise ManagedResourceError(f"Packaged Framework resource hash does not match its manifest: {relative}")
        legacy_hash = entry.get("legacy_sha256")
        if legacy_hash is not None and (
            not isinstance(legacy_hash, str)
            or len(legacy_hash) != 64
            or any(c not in "0123456789abcdef" for c in legacy_hash)
        ):
            raise ManagedResourceError(f"Invalid legacy hash for Framework resource: {relative}")
        result[relative] = {
            "content": content,
            "sha256": actual_hash,
            "executable": executable,
            "legacy_sha256": legacy_hash,
        }
    if not result:
        raise ManagedResourceError("Framework Storage resource manifest is empty.")
    return result


def _read_installed_manifest(path: Path, root: Path) -> dict[str, Any] | None:
    _check_path(path, root)
    if not path.exists():
        return None
    if not path.is_file():
        raise ManagedResourceError(f"Installed resource manifest is not a regular file: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedResourceError(f"Cannot read installed resource manifest {path}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT_VERSION
        or manifest.get("owner") != OWNER
    ):
        raise ManagedResourceError(f"Installed resource manifest is unrecognized: {path}")
    owned = manifest.get("resources")
    if not isinstance(owned, dict):
        raise ManagedResourceError("Installed resource manifest must contain a resource hash map.")
    normalized: dict[str, str] = {}
    for raw_path, digest in owned.items():
        relative = _safe_relative_path(raw_path).as_posix()
        if (
            relative != raw_path
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ManagedResourceError("Installed resource manifest contains an invalid path or hash.")
        if relative in normalized:
            raise ManagedResourceError(f"Duplicate installed resource path: {relative}")
        normalized[relative] = digest
    return {**manifest, "resources": normalized}


def _atomic_write(path: Path, content: bytes, mode: int, root: Path) -> None:
    _check_path(path.parent, root)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _check_path(temporary, root)
        _check_path(path, root)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _file_state(path: Path, root: Path) -> tuple[bytes, int] | None:
    _check_path(path, root)
    if not path.exists():
        return None
    if not path.is_file():
        raise ManagedResourceError(f"Managed resource path is not a regular file: {path}")
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _journal_state(state: tuple[bytes, int] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"content": base64.b64encode(state[0]).decode("ascii"), "mode": state[1]}


def _decode_journal_state(state: Any, label: str) -> tuple[bytes, int] | None:
    if state is None:
        return None
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("content"), str)
        or not isinstance(state.get("mode"), int)
        or isinstance(state.get("mode"), bool)
        or not 0 <= state["mode"] <= 0o777
    ):
        raise ManagedResourceError(f"Framework install journal has an invalid backup for {label}.")
    try:
        content = base64.b64decode(state["content"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ManagedResourceError(f"Framework install journal backup is invalid for {label}.") from exc
    return content, state["mode"]


def _expected_state(state: tuple[bytes, int] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"sha256": _sha256(state[0]), "mode": state[1]}


def _matches_expected(state: tuple[bytes, int] | None, expected: Any, label: str) -> bool:
    if expected is None:
        return state is None
    if (
        not isinstance(expected, dict)
        or not isinstance(expected.get("sha256"), str)
        or len(expected["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in expected["sha256"])
        or not isinstance(expected.get("mode"), int)
        or isinstance(expected.get("mode"), bool)
        or not 0 <= expected["mode"] <= 0o777
    ):
        raise ManagedResourceError(f"Framework install journal has an invalid expected state for {label}.")
    return state is not None and _sha256(state[0]) == expected["sha256"] and state[1] == expected["mode"]


def _gitignore_block(packaged: dict[str, dict[str, Any]]) -> bytes:
    lines = [GITIGNORE_START, *(f"/{relative}" for relative in sorted(packaged)), GITIGNORE_END, ""]
    return "\n".join(lines).encode("utf-8")


def _normalize_line_endings(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _locate_gitignore_block(content: bytes) -> tuple[str, int, int, bytes] | None:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ManagedResourceError("Storage .gitignore must be UTF-8 to manage Framework outputs.") from exc
    lines = text.splitlines(keepends=True)
    start_indexes: list[int] = []
    end_indexes: list[int] = []
    offset = 0
    starts: list[int] = []
    ends: list[int] = []
    for index, line in enumerate(lines):
        line_content = line.rstrip("\r\n")
        if line_content == GITIGNORE_START:
            start_indexes.append(index)
            starts.append(offset)
        elif line_content == GITIGNORE_END:
            end_indexes.append(index)
            ends.append(offset + len(line))
        offset += len(line)
    if not start_indexes and not end_indexes:
        if GITIGNORE_START in text or GITIGNORE_END in text:
            raise ManagedResourceError("Storage .gitignore contains a malformed Framework managed block.")
        return None
    if len(start_indexes) != 1 or len(end_indexes) != 1 or start_indexes[0] >= end_indexes[0]:
        raise ManagedResourceError("Storage .gitignore has duplicate or malformed Framework managed markers.")
    start, end = starts[0], ends[0]
    return text, start, end, text[start:end].encode("utf-8")


def _prepare_gitignore(
    root: Path,
    packaged: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
) -> tuple[bytes, int, str]:
    path = root / GITIGNORE_PATH
    state = _file_state(path, root)
    original = state[0] if state is not None else b""
    desired_block = _gitignore_block(packaged)
    previous_hash = previous.get("gitignore_block_sha256") if previous else None
    located = _locate_gitignore_block(original)
    if located is None:
        if previous_hash is not None:
            raise ManagedResourceError("Framework .gitignore block is missing; restore it before setup.")
        if original:
            separator = b"" if original.endswith(b"\n\n") else b"\n" if original.endswith((b"\n", b"\r")) else b"\n\n"
            updated = original + separator + desired_block
        else:
            updated = desired_block
    else:
        text, start, end, current_block = located
        if previous_hash is None:
            if _normalize_line_endings(current_block) != desired_block:
                raise ManagedResourceError("Unowned Framework block in .gitignore differs from the current manifest.")
        elif _sha256(_normalize_line_endings(current_block)) != previous_hash:
            raise ManagedResourceError("Framework .gitignore block has local edits; refusing to overwrite it.")
        replacement = desired_block.decode("utf-8")
        updated = (text[:start] + replacement + text[end:]).encode("utf-8")
    mode = state[1] if state is not None else 0o644
    return updated, mode, _sha256(_normalize_line_endings(desired_block))


def _verify_gitignore(root: Path, installed: dict[str, Any], packaged: dict[str, dict[str, Any]]) -> None:
    declared_hash = installed.get("gitignore_block_sha256")
    if not isinstance(declared_hash, str) or len(declared_hash) != 64:
        raise ManagedResourceError(f"Framework .gitignore ownership is missing. {REPAIR_HINT}")
    state = _file_state(root / GITIGNORE_PATH, root)
    if state is None:
        raise ManagedResourceError(f"Storage .gitignore is missing. {REPAIR_HINT}")
    located = _locate_gitignore_block(state[0])
    expected_block = _gitignore_block(packaged)
    if (
        located is None
        or _sha256(_normalize_line_endings(located[3])) != declared_hash
        or _normalize_line_endings(located[3]) != expected_block
    ):
        raise ManagedResourceError(f"Framework .gitignore block is changed or stale. {REPAIR_HINT}")


def _restore_journal(root: Path) -> None:
    """Roll back a previous interrupted install before attempting a new one."""

    journal_path = root / TRANSACTION_JOURNAL
    _check_path(journal_path, root)
    if not journal_path.exists():
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedResourceError(
            f"Cannot read the interrupted Framework install journal at {journal_path}; preserve it and request manual recovery."
        ) from exc
    if not isinstance(journal, dict) or journal.get("format") != FORMAT_VERSION or journal.get("owner") != OWNER:
        raise ManagedResourceError(f"Unrecognized Framework install journal: {journal_path}")
    files = journal.get("files")
    if not isinstance(files, dict):
        raise ManagedResourceError(f"Framework install journal has no file recovery map: {journal_path}")

    records: list[tuple[str, Path, Any, Any]] = []
    for raw_path, record in files.items():
        relative = _safe_relative_path(raw_path)
        if relative.as_posix() != raw_path:
            raise ManagedResourceError(f"Framework install journal contains an invalid path: {raw_path!r}")
        if not isinstance(record, dict) or set(record) != {"backup", "expected"}:
            raise ManagedResourceError(f"Framework install journal has an invalid state record for {raw_path}.")
        records.append((raw_path, root / Path(*relative.parts), record["backup"], record["expected"]))
    for label, journal_key in ((INSTALLED_MANIFEST, "manifest"), (GITIGNORE_PATH, "gitignore")):
        record = journal.get(journal_key)
        if not isinstance(record, dict) or set(record) != {"backup", "expected"}:
            raise ManagedResourceError(f"Framework install journal has an invalid state record for {label}.")
        records.append((label, root / Path(label), record["backup"], record["expected"]))

    # Check every current file before changing any of them. A path can be in its
    # post-install state or already restored by an earlier interrupted recovery.
    prepared: list[tuple[str, Path, tuple[bytes, int] | None, bool]] = []
    for label, path, raw_backup, expected in records:
        _check_path(path, root)
        backup = _decode_journal_state(raw_backup, label)
        current = _file_state(path, root)
        matches_expected = _matches_expected(current, expected, label)
        if not (matches_expected or current == backup):
            raise ManagedResourceError(
                f"Storage file changed after the interrupted Framework install; preserving it and the journal: {label}"
            )
        prepared.append((label, path, backup, current == backup))

    for label, path, backup, already_restored in prepared:
        if already_restored:
            continue
        _check_path(path, root)
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, backup[0], backup[1], root)
    journal_path.unlink()


def _load_resource_bytes(root: Path, path: str) -> bytes:
    state = _file_state(root / path, root)
    if state is None:
        raise ManagedResourceError(f"Managed resource is missing: {path}. {REPAIR_HINT}")
    return state[0]


def _verify_locked(root: Path) -> dict[str, Any]:
    marker = root / SETUP_MARKER
    _check_path(marker, root)
    if marker.exists():
        raise ManagedResourceError(f"Storage setup did not finish. {REPAIR_HINT}")
    journal_path = root / TRANSACTION_JOURNAL
    _check_path(journal_path, root)
    if journal_path.exists():
        raise ManagedResourceError(f"A Framework resource recovery is pending. {REPAIR_HINT}")
    installed = _read_installed_manifest(root / INSTALLED_MANIFEST, root)
    if installed is None:
        raise ManagedResourceError(f"Framework-managed Storage files are not installed. {REPAIR_HINT}")
    try:
        installed_version = metadata.version("pkm-framework")
    except metadata.PackageNotFoundError as exc:
        raise ManagedResourceError(f"PKM Framework is not installed. {REPAIR_HINT}") from exc
    if installed.get("framework_version") != installed_version:
        raise ManagedResourceError(
            "The installed Framework version and Storage resources do not match. " + REPAIR_HINT
        )
    packaged = _package_resources()
    if set(installed["resources"]) != set(packaged):
        raise ManagedResourceError(f"Framework-managed Storage file set is stale. {REPAIR_HINT}")
    for relative, expected_hash in installed["resources"].items():
        actual_hash = _sha256(_load_resource_bytes(root, relative))
        if actual_hash != expected_hash or actual_hash != packaged[relative]["sha256"]:
            raise ManagedResourceError(f"Framework-managed file was changed or is stale: {relative}. {REPAIR_HINT}")
    _verify_gitignore(root, installed, packaged)
    return {"ok": True, "framework_version": installed_version, "resources": sorted(installed["resources"])}


def verify_managed_resources(storage_root: Path) -> dict[str, Any]:
    """Verify that installed resources match this environment's pinned package."""

    root = _root(storage_root)
    try:
        with storage_write_lock(root):
            return verify_managed_resources_locked(root)
    except StorageWriteLockError as exc:
        raise ManagedResourceError(f"Could not verify managed resources while Storage is being written. {REPAIR_HINT}") from exc


def verify_managed_resources_locked(storage_root: Path) -> dict[str, Any]:
    """Verify readiness while the caller already holds storage_write_lock."""

    return _verify_locked(_root(storage_root))


def install_managed_resources(storage_root: Path) -> dict[str, Any]:
    """Safely install or update only the files named in the packaged manifest."""

    root = _root(storage_root)
    try:
        with storage_write_lock(root):
            return _install_locked(root)
    except StorageWriteLockError as exc:
        raise ManagedResourceError("Could not install Framework resources while Storage is being written.") from exc


def _install_locked(root: Path) -> dict[str, Any]:
    _restore_journal(root)
    packaged = _package_resources()
    manifest_path = root / INSTALLED_MANIFEST
    previous = _read_installed_manifest(manifest_path, root)
    prior_hashes = previous["resources"] if previous else {}
    gitignore_content, gitignore_mode, gitignore_block_hash = _prepare_gitignore(root, packaged, previous)
    framework_version = metadata.version("pkm-framework")
    marker = root / SETUP_MARKER
    _check_path(marker, root)
    if not marker.exists():
        raise ManagedResourceError(f"Run `mise run setup` before installing Framework resources. {REPAIR_HINT}")

    current_paths = set(packaged)
    previous_paths = set(prior_hashes)
    writes: dict[str, bytes] = {}
    backups: dict[str, tuple[bytes, int] | None] = {}
    removals: dict[str, tuple[bytes, int]] = {}

    # Inspect every managed path before creating directories or writing files.
    for relative, entry in packaged.items():
        path = root / relative
        state = _file_state(path, root)
        current_hash = entry["sha256"]
        if relative in prior_hashes:
            if state is not None and _sha256(state[0]) != prior_hashes[relative]:
                raise ManagedResourceError(f"Managed file has local edits; refusing to overwrite it: {relative}")
        elif state is not None:
            accepted = {current_hash, entry["legacy_sha256"]} - {None}
            if _sha256(state[0]) not in accepted:
                raise ManagedResourceError(
                    f"Unowned file already occupies a Framework-managed path: {relative}. "
                    "Move it to a user-owned filename or restore the known Framework version before setup."
                )
        executable_missing = (
            os.name != "nt" and entry["executable"] and state is not None and not (state[1] & 0o111)
        )
        if state is None or _sha256(state[0]) != current_hash or executable_missing:
            writes[relative] = entry["content"]
        backups[relative] = state

    for relative in sorted(previous_paths - current_paths):
        path = root / relative
        state = _file_state(path, root)
        if state is None:
            continue
        if _sha256(state[0]) != prior_hashes[relative]:
            raise ManagedResourceError(f"Removed Framework resource has local edits; refusing to delete it: {relative}")
        removals[relative] = state

    staged: dict[str, Path] = {}
    gitignore_staged: Path | None = None
    created_directories: list[Path] = []
    try:
        for relative in sorted(writes):
            target = root / relative
            parent = target.parent
            _check_path(parent, root)
            cursor = parent
            missing: list[Path] = []
            while not cursor.exists() and cursor != root:
                missing.append(cursor)
                cursor = cursor.parent
            if cursor != root and not cursor.is_dir():
                raise ManagedResourceError(f"Managed resource parent is not a directory: {cursor}")
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
            temp_path = Path(temp_name)
            staged[relative] = temp_path
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(packaged[relative]["content"])
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o755 if packaged[relative]["executable"] else 0o644)
        gitignore_state = _file_state(root / GITIGNORE_PATH, root)
        if gitignore_state is None or gitignore_state[0] != gitignore_content:
            descriptor, temp_name = tempfile.mkstemp(prefix=".gitignore.", dir=root)
            gitignore_staged = Path(temp_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(gitignore_content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(gitignore_staged, gitignore_mode)
    except Exception:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        if gitignore_staged is not None:
            gitignore_staged.unlink(missing_ok=True)
        for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise

    manifest_content = json.dumps(
        {
            "format": FORMAT_VERSION,
            "owner": OWNER,
            "framework_version": framework_version,
            "resources": {path: packaged[path]["sha256"] for path in sorted(packaged)},
            "gitignore_block_sha256": gitignore_block_hash,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8") + b"\n"

    manifest_backup = _file_state(manifest_path, root)
    gitignore_backup = _file_state(root / GITIGNORE_PATH, root)
    journal_path = root / TRANSACTION_JOURNAL
    journal_content = json.dumps(
        {
            "format": FORMAT_VERSION,
            "owner": OWNER,
            "files": {
                relative: {
                    "backup": _journal_state(
                        removals[relative] if relative in removals else backups.get(relative)
                    ),
                    "expected": None
                    if relative in removals
                    else _expected_state(
                        (
                            packaged[relative]["content"],
                            0o755 if packaged[relative]["executable"] else 0o644,
                        )
                    ),
                }
                for relative in sorted(set(writes) | set(removals))
            },
            "manifest": {
                "backup": _journal_state(manifest_backup),
                "expected": _expected_state((manifest_content, 0o600)),
            },
            "gitignore": {
                "backup": _journal_state(gitignore_backup),
                "expected": _expected_state((gitignore_content, gitignore_mode)),
            },
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8") + b"\n"
    try:
        _atomic_write(journal_path, journal_content, 0o600, root)
        for relative, temporary in staged.items():
            target = root / relative
            _check_path(target, root)
            os.replace(temporary, target)
        if gitignore_staged is not None:
            _check_path(root / GITIGNORE_PATH, root)
            os.replace(gitignore_staged, root / GITIGNORE_PATH)
        for relative in sorted(removals):
            target = root / relative
            _check_path(target, root)
            target.unlink()
        _check_path(manifest_path.parent, root)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(manifest_path, manifest_content, 0o600, root)
        journal_path.unlink()
    except Exception as exc:
        try:
            _restore_journal(root)
        except Exception:
            if journal_path.exists():
                raise ManagedResourceError(
                    "Framework resource install failed and rollback is pending. Keep the setup marker and rerun setup; "
                    "if it continues to fail, preserve the transaction journal for manual recovery."
                ) from exc
            raise
        if journal_path.exists():
            raise ManagedResourceError(
                "Framework resource install failed and rollback is incomplete. Keep the setup marker and rerun setup."
            ) from exc
        raise ManagedResourceError(f"Framework resource installation failed and was rolled back: {exc}") from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        if gitignore_staged is not None:
            gitignore_staged.unlink(missing_ok=True)
        for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    return {"ok": True, "framework_version": framework_version, "installed": sorted(packaged), "removed": sorted(removals)}
