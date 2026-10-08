"""Install namespaced Framework skills without touching unowned skills."""

from __future__ import annotations

import json
import os
import re
import tempfile
from importlib import resources
from pathlib import Path


MANIFEST_NAME = ".pkm-framework-skills.json"
MANIFEST_OWNER = "pkm-framework"
_SKILL_NAME_RE = re.compile(r"^pkm-[a-z0-9]+(?:-[a-z0-9]+)*$")


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("Skill target paths cannot contain '..'.")
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _check_path(path: Path) -> None:
    """Reject symlinks in every existing component of a path we will touch."""

    path = _absolute(path)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"Skill installation refuses symbolic links: {current}")


def _load_manifest(path: Path) -> set[str]:
    _check_path(path)
    if not path.exists():
        return set()
    if not path.is_file():
        raise ValueError(f"Skill manifest must be a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read Framework skill manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("owner") != MANIFEST_OWNER or data.get("format") != 1:
        raise ValueError(f"Refusing to overwrite an unowned skill manifest: {path}")
    names = data.get("skills")
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name) for name in names
    ):
        raise ValueError(f"Invalid skill names in Framework manifest: {path}")
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate skill names in Framework manifest: {path}")
    return set(names)


def _source_skills() -> dict[str, bytes]:
    source_root = resources.files("pkm_framework").joinpath("skills")
    skills: dict[str, bytes] = {}
    for skill in sorted(source_root.iterdir(), key=lambda item: item.name):
        source_file = skill.joinpath("SKILL.md")
        if not skill.is_dir() or not source_file.is_file():
            continue
        if not _SKILL_NAME_RE.fullmatch(skill.name):
            raise ValueError(f"Packaged skill name is not namespaced: {skill.name}")
        skills[skill.name] = source_file.read_bytes()
    return skills


def _write_atomic(path: Path, content: bytes) -> None:
    _check_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_path(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _check_path(temporary)
        _check_path(path)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _preflight_skill(target: Path, name: str, was_owned: bool) -> Path:
    directory = target / name
    skill_file = directory / "SKILL.md"
    _check_path(directory)
    if directory.exists():
        if not directory.is_dir():
            raise ValueError(f"Skill path is not a directory: {directory}")
        if not was_owned:
            raise ValueError(f"Refusing to overwrite an unowned skill directory: {directory}")
    _check_path(skill_file)
    if skill_file.exists() and not skill_file.is_file():
        raise ValueError(f"Managed SKILL.md must be a regular file: {skill_file}")
    return directory


def install_skills(target: Path) -> list[str]:
    """Install owned skills, remove only stale owned skill files, and keep custom skills."""

    target = _absolute(target.expanduser())
    _check_path(target)
    if target.exists() and not target.is_dir():
        raise ValueError(f"Skill target must be a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    _check_path(target)

    manifest_path = target / MANIFEST_NAME
    previous = _load_manifest(manifest_path)
    packaged = _source_skills()
    current = set(packaged)

    # Check every path before changing any installed skill or manifest.
    for name in sorted(current | previous):
        _preflight_skill(target, name, name in previous)

    created_directories: list[Path] = []
    try:
        for name, content in packaged.items():
            directory = target / name
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)
            _write_atomic(directory / "SKILL.md", content)

        for stale_name in sorted(previous - current):
            directory = target / stale_name
            skill_file = directory / "SKILL.md"
            _check_path(skill_file)
            if skill_file.exists():
                skill_file.unlink()
            try:
                directory.rmdir()
            except OSError:
                # Preserve user-added files inside a formerly managed directory.
                pass

        manifest = json.dumps(
            {"format": 1, "owner": MANIFEST_OWNER, "skills": sorted(current)},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        _write_atomic(manifest_path, manifest)
    except Exception:
        # New names are not owned until the manifest is committed.
        for directory in reversed(created_directories):
            skill_file = directory / "SKILL.md"
            if skill_file.exists() and not skill_file.is_symlink():
                skill_file.unlink()
            try:
                directory.rmdir()
            except OSError:
                pass
        raise

    return sorted(current)
