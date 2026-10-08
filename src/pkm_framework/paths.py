"""Public path-safety helpers shared by vault readers, indexers, and writers."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


class VaultPathError(ValueError):
    """A path is not a safe path inside the selected vault."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def ensure_no_symlink_components(path: Path, anchor: Path) -> Path:
    """Return an absolute path after ensuring it stays below a real anchor."""

    path = _absolute(path)
    anchor = _absolute(anchor)
    if anchor.is_symlink() or not anchor.is_dir():
        raise VaultPathError("Path anchor must be a real directory.")
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise VaultPathError("Path is outside its allowed root.") from exc

    current = anchor
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise VaultPathError("Symbolic links are not allowed in vault paths.")
    try:
        resolved_anchor = anchor.resolve(strict=True)
        existing = path
        while not existing.exists() and existing != anchor:
            existing = existing.parent
        resolved_existing = existing.resolve(strict=True)
    except OSError as exc:
        raise VaultPathError("Path does not have a resolvable parent.") from exc
    if not resolved_existing.is_relative_to(resolved_anchor):
        raise VaultPathError("Path resolves outside its allowed root.")
    return path


def validate_vault_relative(relative_path: str) -> PurePosixPath:
    """Parse a strict Vault-relative POSIX path used by API payloads."""

    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise VaultPathError("Path must be a non-empty Vault-relative path.")
    if "\\" in relative_path or relative_path.startswith("/"):
        raise VaultPathError("Path must use Vault-relative POSIX separators.")
    if re.match(r"^[A-Za-z]:", relative_path):
        raise VaultPathError("Absolute paths are not allowed.")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise VaultPathError("Path must not contain empty, '.' or '..' components.")
    return PurePosixPath(*parts)


def resolve_vault_path(
    vault: Path,
    relative_path: str,
    *,
    must_exist: bool = True,
    allow_directory: bool = False,
) -> Path:
    """Resolve a Vault-relative path while rejecting traversal and symlinks."""

    relative = validate_vault_relative(relative_path)
    root = _absolute(vault)
    if root.is_symlink() or not root.is_dir():
        raise VaultPathError("Selected Vault must be a real directory.")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise VaultPathError("Selected Vault cannot be resolved.") from exc

    candidate = root.joinpath(*relative.parts)
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        if current.is_symlink():
            raise VaultPathError("Symbolic links are not allowed in vault paths.")
        final = index == len(relative.parts) - 1
        if current.exists():
            if not final and not current.is_dir():
                raise VaultPathError("A parent component is not a directory.")
            if final and not allow_directory and not current.is_file():
                raise VaultPathError("Path is not a regular file.")
        elif final and must_exist:
            raise VaultPathError("File does not exist in the selected Vault.")

    existing = candidate
    while not existing.exists() and existing != root:
        existing = existing.parent
    try:
        resolved_existing = existing.resolve(strict=True)
    except OSError as exc:
        raise VaultPathError("Path does not have a resolvable parent.") from exc
    if not resolved_existing.is_relative_to(resolved_root):
        raise VaultPathError("Path resolves outside the selected Vault.")
    return candidate


def resolve_markdown_link(
    vault: Path,
    source_relative: str,
    target: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a relative Markdown target; '..' is allowed only within Vault."""

    if not target or "\x00" in target or "\\" in target:
        raise VaultPathError("Markdown link has an invalid target.")
    if target.startswith("/") or re.match(r"^[A-Za-z]:", target):
        raise VaultPathError("Absolute Markdown links are not allowed inside the Vault.")
    source = validate_vault_relative(source_relative)
    target_path = PurePosixPath(target)
    stack = list(source.parent.parts)
    for component in target_path.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not stack:
                raise VaultPathError("Markdown link escapes the selected Vault.")
            stack.pop()
        else:
            stack.append(component)
    if not stack:
        raise VaultPathError("Markdown link does not identify a file.")
    return resolve_vault_path(
        vault, "/".join(stack), must_exist=must_exist, allow_directory=True
    )
