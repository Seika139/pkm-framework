"""Resolve the Storage repository and its Obsidian vault."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pkm_framework.config import StorageConfig, StorageConfigError, load_config
from pkm_framework.paths import VaultPathError, ensure_no_symlink_components

MARKER_NAME = ".pkm-storage"
LEGACY_MARKER = "pkm-storage-vault-v1"
CONFIGURED_MARKER = "pkm-storage-vault-v2"


@dataclass(frozen=True)
class StorageLayout:
    root: Path
    vault: Path
    marker_version: str = "v1"
    config: StorageConfig | None = None

    @property
    def indexed_roots(self) -> tuple[Path, ...]:
        if self.config is None:
            return (self.vault / "raw" / "clips", self.vault / "wiki")
        return tuple(self.vault / source.vault_path.rstrip("/") for source in self.config.index_sources) + (
            self.vault / "wiki",
        )

    def source_for_path(self, relative: str):
        if self.config is None:
            return "clips" if relative.startswith("raw/clips/") else None
        return next(
            (source for source in self.config.sources if relative.startswith(source.vault_path)),
            None,
        )

    @property
    def database(self) -> Path:
        return self.root / ".pkm" / "cache" / "index.sqlite"


def _from_candidate(candidate: Path, *, allow_unenrolled_config: bool = False) -> StorageLayout | None:
    candidate = Path(os.path.abspath(candidate.expanduser()))
    if (candidate / MARKER_NAME).is_file():
        return _make_layout(candidate.parent, candidate, allow_unenrolled_config=allow_unenrolled_config)

    if not candidate.is_dir():
        return None

    vaults = sorted(
        child
        for child in candidate.iterdir()
        if child.is_dir() and (child / MARKER_NAME).is_file()
    )
    if len(vaults) > 1:
        names = ", ".join(vault.name for vault in vaults)
        raise ValueError(
            f"Multiple PKM vaults found under {candidate}: {names}. "
            "Pass a specific vault directory with --storage or PKM_STORAGE_DIR."
        )
    if vaults:
        return _make_layout(candidate, vaults[0], allow_unenrolled_config=allow_unenrolled_config)
    return None


def _make_layout(root: Path, vault: Path, *, allow_unenrolled_config: bool) -> StorageLayout:
    marker = vault / MARKER_NAME
    try:
        ensure_no_symlink_components(marker, vault)
        marker_value = marker.read_bytes().decode("ascii").rstrip("\r\n")
    except (OSError, UnicodeError, VaultPathError) as exc:
        raise StorageConfigError("The Storage enrollment marker is unreadable or unsafe.") from exc
    has_config = (root / ".pkm" / "storage.toml").exists() or (root / ".pkm" / "storage.toml").is_symlink()
    if marker_value == LEGACY_MARKER:
        if has_config and not allow_unenrolled_config:
            raise StorageConfigError(
                "This legacy Storage has .pkm/storage.toml but is not enrolled. Run `pkm storage enroll` first."
            )
        if has_config:
            return StorageLayout(root=root, vault=vault, marker_version="v1")
        return StorageLayout(root=root, vault=vault, marker_version="v1")
    if marker_value == CONFIGURED_MARKER:
        if not has_config:
            raise StorageConfigError(
                "This configured Storage is missing .pkm/storage.toml; refusing legacy fallback."
            )
        try:
            config = load_config(root, vault)
        except StorageConfigError:
            raise
        return StorageLayout(root=root, vault=vault, marker_version="v2", config=config)
    raise StorageConfigError("Storage enrollment marker has an unsupported value.")


def resolve_storage(
    storage_dir: str | Path | None = None, *, allow_unenrolled_config: bool = False
) -> StorageLayout:
    """Resolve from --storage, PKM_STORAGE_DIR, or a nearby vault marker."""

    configured = storage_dir or os.environ.get("PKM_STORAGE_DIR")
    if configured:
        candidate = Path(configured)
        layout = _from_candidate(candidate, allow_unenrolled_config=allow_unenrolled_config)
        if layout is None:
            raise FileNotFoundError(
                f"Could not find {MARKER_NAME} in {candidate} or an immediate child directory. "
                "Pass the Storage repository root or a specific vault directory with --storage."
            )
        return layout

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        layout = _from_candidate(candidate, allow_unenrolled_config=allow_unenrolled_config)
        if layout is not None:
            return layout
    raise FileNotFoundError(
        f"No PKM vault found. Set PKM_STORAGE_DIR, pass --storage, or run from a directory "
        f"under a vault containing {MARKER_NAME} or from a Storage repository root "
        "with one marked vault directory."
    )
