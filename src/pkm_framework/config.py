"""Strict per-vault source and MCP access policy configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pkm_framework.paths import VaultPathError, ensure_no_symlink_components, validate_vault_relative


CONFIG_RELATIVE_PATH = ".pkm/storage.toml"
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)
_RESERVED_COMPONENTS = {
    ".agents",
    ".git",
    ".github",
    ".obsidian",
    ".pkm",
    ".trash",
    "assets",
    "wiki",
}


class StorageConfigError(ValueError):
    """A configured Storage has an invalid or unsafe source policy."""


class ConfigChangedError(StorageConfigError):
    """The configured policy changed while a process was using its snapshot."""


@dataclass(frozen=True)
class Source:
    id: str
    vault_path: str
    kind: str
    index: bool
    wiki_source: bool
    sync: bool


@dataclass(frozen=True)
class WikiPermission:
    read: bool
    write: bool


@dataclass(frozen=True)
class Profile:
    source_ids: tuple[str, ...]
    wiki: WikiPermission


@dataclass(frozen=True)
class StorageConfig:
    version: int
    sources: tuple[Source, ...]
    profiles: dict[str, Profile]
    reviewed_policy_hash: str
    raw_sha256: str
    policy_hash: str

    @property
    def index_sources(self) -> tuple[Source, ...]:
        return tuple(source for source in self.sources if source.index)

    @property
    def sync_sources(self) -> tuple[Source, ...]:
        return tuple(source for source in self.sources if source.sync)


def _exact_keys(value: dict[str, Any], required: set[str], where: str) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise StorageConfigError(f"{where} has invalid keys ({'; '.join(detail)}).")


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise StorageConfigError(f"{where} must be a boolean.")
    return value


def _safe_source_path(vault: Path, value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("/"):
        raise StorageConfigError("source vault_path must be a canonical Vault-relative directory ending in '/'.")
    raw = value[:-1]
    try:
        relative = validate_vault_relative(raw)
    except VaultPathError as exc:
        raise StorageConfigError("source vault_path must be a safe Vault-relative path.") from exc
    parts = relative.parts
    if len(parts) < 2 or parts[0] != "raw" or relative.as_posix() != raw:
        raise StorageConfigError("source vault_path must identify a canonical directory below raw/.")
    if any(
        part.casefold() in _RESERVED_COMPONENTS
        or part.startswith(".")
        or any(character in part for character in '<>:"|?*')
        or part.endswith((".", " "))
        or _WINDOWS_RESERVED_RE.fullmatch(part)
        for part in parts[1:]
    ):
        raise StorageConfigError("source vault_path contains a reserved directory name.")
    path = vault.joinpath(*parts)
    try:
        ensure_no_symlink_components(path, vault)
    except (OSError, VaultPathError) as exc:
        raise StorageConfigError("source vault_path contains an unsafe or redirected component.") from exc
    if path.exists() and not path.is_dir():
        raise StorageConfigError("source vault_path must identify a directory when it exists.")
    return relative.as_posix() + "/"


def _policy_payload(version: int, sources: tuple[Source, ...], profiles: dict[str, Profile]) -> dict[str, Any]:
    return {
        "version": version,
        "sources": [
            {
                "id": item.id,
                "vault_path": item.vault_path,
                "kind": item.kind,
                "index": item.index,
                "wiki_source": item.wiki_source,
                "sync": item.sync,
            }
            for item in sorted(sources, key=lambda source: source.id)
        ],
        "profiles": {
            name: {
                "source_ids": list(profile.source_ids),
                "wiki": {"read": profile.wiki.read, "write": profile.wiki.write},
            }
            for name, profile in sorted(profiles.items())
        },
    }


def parse_config_bytes(data: bytes, vault: Path) -> StorageConfig:
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise StorageConfigError(".pkm/storage.toml must be valid UTF-8 TOML.") from exc
    if not isinstance(parsed, dict):
        raise StorageConfigError(".pkm/storage.toml must contain a TOML table.")
    _exact_keys(parsed, {"version", "sources", "profiles", "wiki"}, "storage config")
    version = parsed["version"]
    if type(version) is not int or version != 1:
        raise StorageConfigError("storage config version must be integer 1.")
    raw_sources = parsed["sources"]
    raw_profiles = parsed["profiles"]
    raw_wiki = parsed["wiki"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise StorageConfigError("storage config must register at least one [[sources]] entry.")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise StorageConfigError("storage config must define at least one [profiles.NAME] entry.")
    if not isinstance(raw_wiki, dict):
        raise StorageConfigError("[wiki] must be a table.")
    _exact_keys(raw_wiki, {"reviewed_policy_hash"}, "[wiki]")
    reviewed_hash = raw_wiki["reviewed_policy_hash"]
    if not isinstance(reviewed_hash, str) or (reviewed_hash and not _SHA256_RE.fullmatch(reviewed_hash)):
        raise StorageConfigError("wiki.reviewed_policy_hash must be an empty string or lowercase SHA-256.")

    sources: list[Source] = []
    ids: set[str] = set()
    roots: list[str] = []
    for number, item in enumerate(raw_sources, start=1):
        where = f"[[sources]] entry {number}"
        if not isinstance(item, dict):
            raise StorageConfigError(f"{where} must be a table.")
        _exact_keys(item, {"id", "vault_path", "kind", "index", "wiki_source", "sync"}, where)
        source_id = item["id"]
        kind = item["kind"]
        if not isinstance(source_id, str) or not _ID_RE.fullmatch(source_id):
            raise StorageConfigError(f"{where} id must match {_ID_RE.pattern}.")
        if source_id in ids:
            raise StorageConfigError(f"Duplicate source id: {source_id}.")
        ids.add(source_id)
        if not isinstance(kind, str) or not _ID_RE.fullmatch(kind):
            raise StorageConfigError(f"{where} kind must be a lowercase identifier.")
        root = _safe_source_path(vault, item["vault_path"])
        identity = unicodedata.normalize("NFC", root).casefold()
        for previous in roots:
            previous_identity = unicodedata.normalize("NFC", previous).casefold()
            if (
                identity == previous_identity
                or identity.startswith(previous_identity)
                or previous_identity.startswith(identity)
            ):
                raise StorageConfigError("source vault_path roots must not overlap or duplicate one another.")
        roots.append(root)
        sources.append(
            Source(
                source_id,
                root,
                kind,
                _bool(item["index"], f"{where} index"),
                _bool(item["wiki_source"], f"{where} wiki_source"),
                _bool(item["sync"], f"{where} sync"),
            )
        )

    profiles: dict[str, Profile] = {}
    for name, item in raw_profiles.items():
        if not isinstance(name, str) or not _ID_RE.fullmatch(name):
            raise StorageConfigError("profile names must be lowercase identifiers.")
        if not isinstance(item, dict):
            raise StorageConfigError(f"[profiles.{name}] must be a table.")
        _exact_keys(item, {"source_ids", "wiki"}, f"[profiles.{name}]")
        raw_ids = item["source_ids"]
        raw_permissions = item["wiki"]
        if not isinstance(raw_ids, list) or any(not isinstance(value, str) for value in raw_ids):
            raise StorageConfigError(f"profiles.{name}.source_ids must be an array of source IDs.")
        if len(raw_ids) != len(set(raw_ids)) or any(value not in ids for value in raw_ids):
            raise StorageConfigError(f"profiles.{name}.source_ids contains duplicate or unknown IDs.")
        if not isinstance(raw_permissions, dict):
            raise StorageConfigError(f"[profiles.{name}.wiki] must be a table.")
        _exact_keys(raw_permissions, {"read", "write"}, f"[profiles.{name}.wiki]")
        permission = WikiPermission(
            _bool(raw_permissions["read"], f"profiles.{name}.wiki.read"),
            _bool(raw_permissions["write"], f"profiles.{name}.wiki.write"),
        )
        if permission.write and not permission.read:
            raise StorageConfigError(f"profiles.{name}.wiki.write requires wiki.read.")
        profiles[name] = Profile(tuple(raw_ids), permission)

    source_tuple = tuple(sources)
    canonical = json.dumps(_policy_payload(version, source_tuple, profiles), sort_keys=True, separators=(",", ":")).encode()
    return StorageConfig(
        version=version,
        sources=source_tuple,
        profiles=profiles,
        reviewed_policy_hash=reviewed_hash,
        raw_sha256=hashlib.sha256(data).hexdigest(),
        policy_hash=hashlib.sha256(canonical).hexdigest(),
    )


def load_config(root: Path, vault: Path) -> StorageConfig:
    config_path = root / CONFIG_RELATIVE_PATH
    try:
        ensure_no_symlink_components(config_path, root)
        info = config_path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise StorageConfigError(".pkm/storage.toml must be a regular file.")
        raw = config_path.read_bytes()
    except StorageConfigError:
        raise
    except (OSError, VaultPathError) as exc:
        raise StorageConfigError("Cannot safely read .pkm/storage.toml.") from exc
    return parse_config_bytes(raw, vault)


def assert_snapshot(root: Path, vault: Path, pinned: StorageConfig) -> None:
    """Fail closed if a v2 config or its marker changed since process startup."""

    marker = vault / ".pkm-storage"
    try:
        ensure_no_symlink_components(marker, vault)
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ConfigChangedError("Storage enrollment marker changed; restart the MCP server.")
        marker_bytes = marker.read_bytes()
    except (OSError, UnicodeError, VaultPathError) as exc:
        raise ConfigChangedError("Storage enrollment marker changed; restart the MCP server.") from exc
    try:
        marker_value = marker_bytes.decode("ascii").rstrip("\r\n")
    except UnicodeError as exc:
        raise ConfigChangedError("Storage enrollment marker changed; restart the MCP server.") from exc
    if marker_value != "pkm-storage-vault-v2":
        raise ConfigChangedError("Storage enrollment changed; restart the MCP server.")
    try:
        current = load_config(root, vault)
    except StorageConfigError as exc:
        raise ConfigChangedError("Storage config changed or became invalid; restart the MCP server.") from exc
    if current.raw_sha256 != pinned.raw_sha256 or current.version != pinned.version:
        raise ConfigChangedError("Storage config changed; restart the MCP server.")


def assert_legacy_snapshot(root: Path, vault: Path) -> None:
    """Ensure a legacy MCP process still has the original v1 marker and no config."""

    marker = vault / ".pkm-storage"
    try:
        ensure_no_symlink_components(marker, vault)
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ConfigChangedError("Storage enrollment changed; restart the MCP server.")
        marker_value = marker.read_bytes().decode("ascii").rstrip("\r\n")
    except ConfigChangedError:
        raise
    except (OSError, UnicodeError, VaultPathError) as exc:
        raise ConfigChangedError("Storage enrollment changed; restart the MCP server.") from exc
    if marker_value != "pkm-storage-vault-v1":
        raise ConfigChangedError("Storage enrollment changed; restart the MCP server.")

    config_path = root / CONFIG_RELATIVE_PATH
    try:
        ensure_no_symlink_components(config_path, root)
        config_path.lstat()
    except FileNotFoundError:
        return
    except (OSError, VaultPathError) as exc:
        raise ConfigChangedError("Storage config appeared or became unsafe; restart the MCP server.") from exc
    raise ConfigChangedError("Storage config appeared; restart the MCP server.")


def wiki_policy_reviewed(config: StorageConfig) -> bool:
    return bool(config.reviewed_policy_hash) and config.reviewed_policy_hash == config.policy_hash


def wiki_allowed(config: StorageConfig, profile: Profile) -> bool:
    return (
        wiki_policy_reviewed(config)
        and profile.wiki.read
        and {source.id for source in config.sources if source.wiki_source}.issubset(profile.source_ids)
    )


def require_owner(root: Path) -> None:
    """Restrict explicit enrollment and review operations to the Storage owner."""

    try:
        owner = root.stat().st_uid
        effective = os.geteuid()
    except (OSError, AttributeError) as exc:
        raise StorageConfigError("Cannot verify Storage ownership for this operation.") from exc
    if owner != effective:
        raise StorageConfigError("This operation is available only to the Storage owner.")


def atomic_review_hash(root: Path, config: StorageConfig) -> None:
    """Update only reviewed_policy_hash while preserving the rest of the TOML text."""

    path = root / CONFIG_RELATIVE_PATH
    try:
        ensure_no_symlink_components(path, root)
        current = path.read_bytes()
        if hashlib.sha256(current).hexdigest() != config.raw_sha256:
            raise ConfigChangedError("Storage config changed; review it again before confirming.")
        text = current.decode("utf-8")
        lines = text.splitlines(keepends=True)
        section = None
        found: list[int] = []
        for index, line in enumerate(lines):
            header = re.match(r"^\s*\[([^\]]+)\]\s*(?:#.*)?(?:\r?\n)?$", line)
            if header:
                section = header.group(1)
            elif section == "wiki" and re.match(r'^\s*(?:reviewed_policy_hash|"reviewed_policy_hash")\s*=', line):
                found.append(index)
        if len(found) != 1:
            raise StorageConfigError("Cannot locate one [wiki].reviewed_policy_hash setting to update.")
        newline = "\r\n" if lines[found[0]].endswith("\r\n") else "\n"
        indent = re.match(r"^\s*", lines[found[0]]).group(0)
        lines[found[0]] = f'{indent}reviewed_policy_hash = "{config.policy_hash}"{newline}'
        payload = "".join(lines).encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(prefix="storage.toml.", suffix=".tmp", dir=path.parent)
        temporary = Path(temp_name)
        try:
            os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            ensure_no_symlink_components(path, root)
            if hashlib.sha256(path.read_bytes()).hexdigest() != config.raw_sha256:
                raise ConfigChangedError("Storage config changed while review approval was being recorded.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except ConfigChangedError:
        raise
    except (OSError, UnicodeError, VaultPathError) as exc:
        raise StorageConfigError("Could not safely update reviewed_policy_hash.") from exc

