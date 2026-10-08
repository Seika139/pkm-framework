"""Resolve the Storage repository and its Obsidian vault."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


MARKER_NAME = ".pkm-storage"


@dataclass(frozen=True)
class StorageLayout:
    root: Path
    vault: Path

    @property
    def indexed_roots(self) -> tuple[Path, Path]:
        return (self.vault / "raw" / "clips", self.vault / "wiki")

    @property
    def database(self) -> Path:
        return self.root / ".pkm" / "cache" / "index.sqlite"


def _from_candidate(candidate: Path) -> StorageLayout | None:
    candidate = Path(os.path.abspath(candidate.expanduser()))
    if (candidate / MARKER_NAME).is_file():
        return StorageLayout(root=candidate.parent, vault=candidate)

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
        return StorageLayout(root=candidate, vault=vaults[0])
    return None


def resolve_storage(storage_dir: str | Path | None = None) -> StorageLayout:
    """Resolve from --storage, PKM_STORAGE_DIR, or a nearby vault marker."""

    configured = storage_dir or os.environ.get("PKM_STORAGE_DIR")
    if configured:
        candidate = Path(configured)
        layout = _from_candidate(candidate)
        if layout is None:
            raise FileNotFoundError(
                f"Could not find {MARKER_NAME} in {candidate} or an immediate child directory. "
                "Pass the Storage repository root or a specific vault directory with --storage."
            )
        return layout

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        layout = _from_candidate(candidate)
        if layout is not None:
            return layout
    raise FileNotFoundError(
        f"No PKM vault found. Set PKM_STORAGE_DIR, pass --storage, or run from a directory "
        f"under a vault containing {MARKER_NAME} or from a Storage repository root "
        "with one marked vault directory."
    )
