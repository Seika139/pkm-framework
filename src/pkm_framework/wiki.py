"""Shared, guarded Markdown Wiki write service for CLI and MCP clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pkm_framework.paths import (
    VaultPathError,
    ensure_no_symlink_components,
    resolve_markdown_link,
    resolve_vault_path,
    validate_vault_relative,
)
from pkm_framework.fileops import (
    SafeVaultParent,
    StagedFile,
    open_vault_parent,
    remove_empty_vault_directory,
)
from pkm_framework.storage import StorageLayout
from pkm_framework.locking import StorageWriteLockError, storage_write_lock
from pkm_framework.managed_resources import ManagedResourceError, verify_managed_resources_locked


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")
_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
_REFERENCE_FIELD_RE = re.compile(r"^\s{2,}([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")


class WikiApplyError(RuntimeError):
    """A write failed after preparation; applied_paths describes final state."""

    def __init__(self, message: str, *, applied_paths: list[str] | None = None):
        super().__init__(message)
        self.applied_paths = applied_paths or []
        self.partial = bool(self.applied_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": str(self),
            "partial": self.partial,
            "applied_paths": self.applied_paths,
        }


class _StageFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        created_directories: tuple[str, ...] = (),
        residual_paths: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.created_directories = created_directories
        self.residual_paths = residual_paths


class _SafeOpenError(ValueError):
    def __init__(self, message: str, cleanup_paths: tuple[str, ...] = ()):
        super().__init__(message)
        self.cleanup_paths = cleanup_paths


@dataclass(frozen=True)
class WikiWrite:
    path: str
    content: str
    expected_sha256: str | None


@dataclass(frozen=True)
class WikiDelete:
    path: str
    expected_sha256: str


@dataclass(frozen=True)
class WikiChangeSet:
    operation: str
    writes: tuple[WikiWrite, ...]
    delete_analyses: tuple[WikiDelete, ...]
    delete_topics: tuple[WikiDelete, ...]
    summary: str | None = None

    @classmethod
    def parse(cls, request: str | dict[str, Any]) -> WikiChangeSet:
        if isinstance(request, str):
            try:
                data = json.loads(request)
            except json.JSONDecodeError as exc:
                raise ValueError("Request must be a valid JSON object.") from exc
        else:
            data = request
        if not isinstance(data, dict):
            raise ValueError("Request must be a JSON object.")
        allowed = {"operation", "summary", "writes", "delete_analyses", "delete_topics"}
        if set(data) - allowed:
            raise ValueError("Request contains unsupported fields.")

        operation = data.get("operation")
        if not isinstance(operation, str) or operation not in {"ingest", "query", "lint"}:
            raise ValueError("operation must be ingest, query, or lint.")
        summary = data.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("summary must be a string when provided.")
        if isinstance(summary, str) and len(summary) > 1000:
            raise ValueError("summary must be at most 1000 characters.")

        raw_writes = data.get("writes", [])
        raw_analysis_deletes = data.get("delete_analyses", [])
        raw_topic_deletes = data.get("delete_topics", [])
        if not all(isinstance(value, list) for value in (raw_writes, raw_analysis_deletes, raw_topic_deletes)):
            raise ValueError("writes, delete_analyses, and delete_topics must be arrays.")

        writes: list[WikiWrite] = []
        analysis_deletes: list[WikiDelete] = []
        topic_deletes: list[WikiDelete] = []
        seen: set[str] = set()
        for item in raw_writes:
            if not isinstance(item, dict) or set(item) != {"path", "content", "expected_sha256"}:
                raise ValueError("Each write must contain path, content, and expected_sha256.")
            path = item["path"]
            content = item["content"]
            expected = item["expected_sha256"]
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("Write path and content must be strings.")
            if expected is not None and (not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected)):
                raise ValueError("expected_sha256 must be null or a lowercase SHA-256 hex digest.")
            normalized = _normalize_api_path(path)
            if normalized in seen:
                raise ValueError("A path may appear only once in a changeset.")
            seen.add(normalized)
            writes.append(WikiWrite(normalized, content, expected))

        for field, raw_deletes, target_prefix, destination in (
            ("delete_analyses", raw_analysis_deletes, "wiki/analyses/", analysis_deletes),
            ("delete_topics", raw_topic_deletes, "wiki/topics/", topic_deletes),
        ):
            for item in raw_deletes:
                if not isinstance(item, dict) or set(item) != {"path", "expected_sha256"}:
                    raise ValueError(f"Each {field} entry must contain path and expected_sha256.")
                path = item["path"]
                expected = item["expected_sha256"]
                if not isinstance(path, str) or not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
                    raise ValueError(f"{field} requires a path and lowercase SHA-256 digest.")
                normalized = _normalize_api_path(path)
                if not normalized.startswith(target_prefix) or not normalized.lower().endswith(".md"):
                    kind = "Topic" if field == "delete_topics" else "analysis"
                    raise ValueError(f"Only Markdown files under {target_prefix} may be deleted as {kind}s.")
                if normalized in seen:
                    raise ValueError("A path may appear only once in a changeset.")
                seen.add(normalized)
                destination.append(WikiDelete(normalized, expected))
        return cls(operation, tuple(writes), tuple(analysis_deletes), tuple(topic_deletes), summary)


@dataclass
class _PlannedFile:
    path: str
    target: Path
    original: bytes | None
    new: bytes | None
    expected_sha256: str | None
    action: str


def _normalize_api_path(path: str) -> str:
    relative = validate_vault_relative(path)
    normalized = relative.as_posix()
    if normalized in {"wiki/index.md", "wiki/questions.md"}:
        return normalized
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == "wiki" and parts[1] in {"topics", "analyses"} and parts[-1].lower().endswith(".md"):
        return normalized
    raise ValueError("Writes are limited to wiki/topics, wiki/analyses, wiki/index.md, and wiki/questions.md.")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path_identity(relative: str) -> str:
    """Compare Vault path spelling conservatively across case/normalization-insensitive filesystems."""

    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", relative).casefold())


def _safe_read(vault: Path, relative: str) -> bytes:
    try:
        parent = open_vault_parent(vault, relative)
        try:
            content = parent.read_bytes()
        finally:
            parent.close()
        if content is None:
            raise FileNotFoundError(relative)
        return content
    except (OSError, VaultPathError) as exc:
        raise ValueError(f"Cannot safely read Vault file {relative}.") from exc


def _decode_utf8(content: bytes, relative: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"Vault Markdown must be UTF-8: {relative}.") from exc


def _yaml_scalar(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"Topic frontmatter field {field} must not be empty.")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Topic frontmatter field {field} has invalid quoted text.") from exc
        if not isinstance(parsed, str):
            raise ValueError(f"Topic frontmatter field {field} must be a string.")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'"):
            raise ValueError(f"Topic frontmatter field {field} has invalid quoted text.")
        return value[1:-1].replace("''", "'")
    return value


def _frontmatter_field_values(content: str, field: str) -> tuple[str | None, ...] | None:
    """Read raw top-level field values without requiring the rest of the schema to be valid."""

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if end is None:
        return None
    values: list[str | None] = []
    for line in lines[1:end]:
        match = _FRONTMATTER_KEY_RE.fullmatch(line)
        if match and match.group(1) == field:
            values.append(match.group(2))
    return tuple(values)


def _without_frontmatter_fields(content: str, fields: set[str]) -> str:
    """Return content with selected top-level YAML fields removed, preserving all other text."""

    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if end is None:
        return content
    kept = list(lines[:1])
    for line in lines[1:end]:
        match = _FRONTMATTER_KEY_RE.fullmatch(line.rstrip("\r\n"))
        if not match or match.group(1) not in fields:
            kept.append(line)
    kept.extend(lines[end:])
    return "".join(kept)


def _topic_source_refs(content: str, relative: str) -> list[dict[str, str | None]]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Topic requires YAML frontmatter: {relative}.")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if end is None:
        raise ValueError(f"Topic frontmatter is not closed: {relative}.")

    fields: dict[str, str | None] = {}
    references: list[dict[str, str | None]] = []
    in_refs = False
    in_tags = False
    current_ref: dict[str, str | None] | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            match = _FRONTMATTER_KEY_RE.fullmatch(line)
            if not match:
                raise ValueError(f"Topic frontmatter has an unsupported line: {relative}.")
            key, value = match.group(1), match.group(2)
            if key in fields:
                raise ValueError(f"Topic frontmatter repeats {key}: {relative}.")
            if key not in {"title", "created", "updated", "source_refs", "tags", "importance"}:
                raise ValueError(f"Topic frontmatter has unsupported field {key}: {relative}.")
            fields[key] = value
            in_refs = key == "source_refs"
            in_tags = key == "tags"
            current_ref = None
            if key == "importance" and value not in {"1", "2", "3", "4", "5"}:
                raise ValueError(f"Topic importance must be an integer from 1 to 5: {relative}.")
            if key == "source_refs" and value not in {None, ""}:
                raise ValueError(f"source_refs must be a YAML list: {relative}.")
            if key == "tags" and value not in {None, ""} and not (value.startswith("[") and value.endswith("]")):
                raise ValueError(f"Topic tags must be a YAML list: {relative}.")
            continue

        if in_tags and line.startswith("  - "):
            _yaml_scalar(line[4:], "tag")
            continue
        if not in_refs:
            raise ValueError(f"Topic frontmatter indentation is invalid: {relative}.")
        if line.startswith("  - "):
            item_match = re.fullmatch(r"\s*-\s+([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?", line)
            if not item_match:
                raise ValueError(f"source_refs item is invalid: {relative}.")
            current_ref = {}
            key, value = item_match.group(1), item_match.group(2)
            if key != "path":
                raise ValueError(f"source_refs item must start with path: {relative}.")
            current_ref[key] = _yaml_scalar(value or "", key)
            references.append(current_ref)
            continue
        if current_ref is None:
            raise ValueError(f"source_refs field must contain at least one path: {relative}.")
        ref_match = _REFERENCE_FIELD_RE.fullmatch(line)
        if not ref_match:
            raise ValueError(f"source_refs field is invalid: {relative}.")
        key, value = ref_match.group(1), ref_match.group(2)
        if key != "url" or key in current_ref:
            raise ValueError(f"source_refs items may contain only one optional url: {relative}.")
        current_ref[key] = _yaml_scalar(value or "", key)

    for required in ("title", "created", "updated", "source_refs"):
        if required not in fields:
            raise ValueError(f"Topic frontmatter requires {required}: {relative}.")
    title = _yaml_scalar(fields["title"] or "", "title")
    if not title.strip():
        raise ValueError(f"Topic title must not be empty: {relative}.")
    parsed_dates: dict[str, date] = {}
    for key in ("created", "updated"):
        value = _yaml_scalar(fields[key] or "", key)
        if not _DATE_RE.fullmatch(value):
            raise ValueError(f"Topic {key} must use YYYY-MM-DD: {relative}.")
        try:
            parsed_dates[key] = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Topic {key} is not a valid calendar date: {relative}.") from exc
    if parsed_dates["updated"] < parsed_dates["created"]:
        raise ValueError(f"Topic updated date must not precede created date: {relative}.")
    if not references:
        raise ValueError(f"Topic source_refs must identify at least one raw clip: {relative}.")
    return references


def _markdown_local_targets(content: str):
    for match in _LINK_RE.finditer(content):
        target = match.group(1) or match.group(2) or ""
        parsed = urlsplit(target)
        if parsed.scheme:
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                continue
            if parsed.scheme == "mailto" and parsed.path:
                continue
            raise ValueError("Markdown links may use only relative Vault paths, http(s), mailto, or anchors.")
        if parsed.netloc:
            raise ValueError("Protocol-relative Markdown links are not allowed.")
        if not parsed.path:
            continue
        yield unquote(parsed.path)


def _all_markdown_under(vault: Path, root_relative: str) -> list[str]:
    try:
        root = resolve_vault_path(vault, root_relative, must_exist=False, allow_directory=True)
    except VaultPathError as exc:
        raise ValueError("Cannot safely inspect wiki/topics.") from exc
    if not root.exists():
        return []
    result: list[str] = []

    def walk(directory: Path, relative: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ValueError("Cannot inspect wiki/topics directory.") from exc
        for entry in entries:
            child_relative = f"{relative}/{entry.name}"
            if entry.is_symlink():
                raise ValueError(f"Symbolic links are not allowed under {relative}.")
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path), child_relative)
            elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".md"):
                resolve_vault_path(vault, child_relative, must_exist=True)
                result.append(child_relative)

    walk(root, root_relative)
    return result


def _all_topic_paths(vault: Path) -> list[str]:
    return _all_markdown_under(vault, "wiki/topics")


def _all_analysis_paths(vault: Path) -> list[str]:
    return _all_markdown_under(vault, "wiki/analyses")


def _canonical_managed_path(vault: Path, relative: str, *, root: str) -> str:
    candidates = _all_topic_paths(vault) if root == "wiki/topics/" else _all_analysis_paths(vault)
    if relative in candidates:
        return relative
    if any(path.casefold() == relative.casefold() for path in candidates):
        raise ValueError("Deletion path must match the exact case of the canonical Vault-relative path.")
    raise ValueError("Deletion path must match an existing canonical Markdown path in the selected Wiki area.")


def _virtual_content(vault: Path, relative: str, writes: dict[str, bytes], deleted: set[str]) -> bytes:
    if relative in deleted:
        raise ValueError(f"Wiki link points to a file deleted by this changeset: {relative}.")
    if relative in writes:
        return writes[relative]
    return _safe_read(vault, relative)


def _virtual_target_exists(vault: Path, relative: str, writes: dict[str, bytes], deleted: set[str]) -> bool:
    if relative in deleted:
        raise ValueError(f"Wiki link points to a file deleted by this changeset: {relative}.")
    if relative in writes:
        return True
    if any(path.startswith(relative.rstrip("/") + "/") for path in writes):
        return True
    try:
        target = resolve_vault_path(vault, relative, must_exist=False, allow_directory=True)
    except VaultPathError as exc:
        raise ValueError(f"Unsafe Vault link target: {relative}.") from exc
    if target.is_file() or target.is_dir():
        return True
    raise ValueError(f"Wiki link points to a missing Vault path: {relative}.")


def _validate_links(vault: Path, relative: str, content: str, writes: dict[str, bytes], deleted: set[str]) -> None:
    for target in _markdown_local_targets(content):
        try:
            resolved = resolve_markdown_link(vault, relative, target, must_exist=False)
        except VaultPathError as exc:
            raise ValueError(f"Invalid Vault link in {relative}: {exc}.") from exc
        target_relative = resolved.relative_to(vault).as_posix()
        _virtual_target_exists(vault, target_relative, writes, deleted)


def _lexical_markdown_link_path(source_relative: str, target: str) -> str | None:
    """Resolve a relative link lexically so normalization aliases are caught on every filesystem."""

    if not target or target.startswith("/") or "\\" in target or "\x00" in target:
        return None
    stack = list(PurePosixPath(source_relative).parent.parts)
    for component in PurePosixPath(target).parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(component)
    return PurePosixPath(*stack).as_posix() if stack else None


def _validate_no_inbound_deleted_links(
    vault: Path, writes: dict[str, bytes], deleted: set[str]
) -> None:
    if not deleted:
        return
    sources = set(_all_topic_paths(vault)) | set(_all_analysis_paths(vault))
    sources.update(path for path in ("wiki/index.md", "wiki/questions.md") if (vault / path).exists())
    sources.update(path for path in writes if path.startswith(("wiki/topics/", "wiki/analyses/")))
    sources.difference_update(deleted)
    deleted_identities = {_path_identity(path) for path in deleted}
    for source in sorted(sources):
        content = _decode_utf8(_virtual_content(vault, source, writes, deleted), source)
        for target in _markdown_local_targets(content):
            target_relative = _lexical_markdown_link_path(source, target)
            if target_relative is None:
                continue
            if _path_identity(target_relative) in deleted_identities:
                raise ValueError(
                    f"Cannot delete a page referenced by a remaining Wiki page: {source} -> {target_relative}."
                )


def _validate_topic(vault: Path, relative: str, content: str) -> None:
    for ref in _topic_source_refs(content, relative):
        path = ref["path"]
        if not isinstance(path, str) or not path.startswith("raw/clips/") or not path.lower().endswith(".md"):
            raise ValueError(f"Topic source_refs must point to raw/clips Markdown: {relative}.")
        try:
            resolve_vault_path(vault, path, must_exist=True)
        except VaultPathError as exc:
            raise ValueError(f"Topic source_refs path is missing or unsafe in {relative}.") from exc
        url = ref.get("url")
        if url is not None:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"Topic source_refs url must be an http(s) URL: {relative}.")


def _validate_index(vault: Path, content: str, writes: dict[str, bytes], deleted: set[str]) -> None:
    index_relative = "wiki/index.md"
    _validate_links(vault, index_relative, content, writes, deleted)
    topic_paths = set(_all_topic_paths(vault)) - {path for path in deleted if path.startswith("wiki/topics/")}
    for path, value in writes.items():
        if path.startswith("wiki/topics/") and path.lower().endswith(".md"):
            topic_paths.add(path)
    topic_links: list[str] = []
    analyses_links: list[tuple[int, str]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for target in _markdown_local_targets(line):
            try:
                resolved = resolve_markdown_link(vault, index_relative, target, must_exist=False)
            except VaultPathError:
                continue  # _validate_links reports the actionable error.
            relative = resolved.relative_to(vault).as_posix()
            if relative.startswith("wiki/topics/") and relative.lower().endswith(".md"):
                topic_links.append(relative)
            elif relative.startswith("wiki/analyses/") and relative.lower().endswith(".md"):
                analyses_links.append((line_number, relative))

    counts: dict[str, int] = {}
    for relative in topic_links:
        counts[relative] = counts.get(relative, 0) + 1
    if set(counts) != topic_paths or any(count != 1 for count in counts.values()):
        missing = topic_paths - set(counts)
        duplicate = {path for path, count in counts.items() if count != 1}
        unknown = set(counts) - topic_paths
        pieces = []
        if missing:
            pieces.append("missing Topic links: " + ", ".join(sorted(missing)))
        if duplicate:
            pieces.append("duplicate Topic links: " + ", ".join(sorted(duplicate)))
        if unknown:
            pieces.append("unknown Topic links: " + ", ".join(sorted(unknown)))
        raise ValueError("wiki/index.md must contain each Topic exactly once (" + "; ".join(pieces) + ").")

    if analyses_links:
        headings: list[tuple[int, str]] = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
            if heading:
                headings.append((line_number, heading.group(1)))
        for line_number, path in analyses_links:
            section = next((title for start, title in reversed(headings) if start < line_number), "")
            if "暫定分析" not in section:
                raise ValueError(f"Analysis link must be under a 暫定分析 heading in wiki/index.md: {path}.")


class WikiWriter:
    """Validate and apply AI-authored changesets to one selected Vault."""

    def __init__(self, layout: StorageLayout):
        self.layout = layout
        self.vault = Path(os.path.abspath(layout.vault.expanduser()))
        self.root = Path(os.path.abspath(layout.root.expanduser()))
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Storage root must be a real directory.")
        if self.vault.is_symlink() or not self.vault.is_dir():
            raise ValueError("Selected Vault must be a real directory.")
        ensure_no_symlink_components(self.vault, self.root)

    def context(self) -> dict[str, Any]:
        """Return shared Framework rules, local Vault rules, and the write contract."""

        framework_rules, local_rules = self._read_rules()
        return {
            "vault": self.vault.name,
            "rules_path": "AGENTS.md",
            "rules_paths": ["AGENTS.framework.md", "AGENTS.md"],
            "rules_sources": [
                {"path": "AGENTS.framework.md", "scope": "shared Framework rules", "content": framework_rules},
                {"path": "AGENTS.md", "scope": "Storage-local rules", "content": local_rules},
            ],
            "rules": (
                "## Shared Framework rules (AGENTS.framework.md)\n\n"
                + framework_rules
                + "\n\n## Storage-local rules (AGENTS.md)\n\n"
                + local_rules
            ),
            "change_contract": {
                "operations": ["ingest", "query", "lint"],
                "write_paths": ["wiki/topics/**/*.md", "wiki/analyses/**/*.md", "wiki/index.md", "wiki/questions.md"],
                "delete_paths": ["wiki/analyses/**/*.md", "wiki/topics/**/*.md"],
                "write_fields": {"path": "Vault-relative path", "content": "Complete UTF-8 Markdown content", "expected_sha256": "null only for create; otherwise the full current file byte hash"},
                "delete_fields": {"path": "Vault-relative wiki/analyses or wiki/topics Markdown path", "expected_sha256": "Full current file byte hash"},
                "guards": [
                    "Read from offset 0 and follow next_offset until null; every page must report the same full-file SHA-256 and total_chars, and the assembled character count must equal total_chars before updating or deleting.",
                    "Use expected_sha256 null only to create a path that does not exist.",
                    "Use delete_analyses for analysis files and delete_topics for Topic files; deletion entries require the full current file SHA-256.",
                    "Only add, change, or remove importance with operation lint after the user explicitly asks to apply a review.",
                    "Topic frontmatter may include integer importance 1 through 5; an omitted value is unassessed. An importance-only change must keep updated unchanged and is not logged.",
                    "A Topic deletion requires a delete_topics entry and a wiki/index.md update in the same changeset. The writer rejects remaining inbound links from Topics, analyses, index, and questions, including case or Unicode-normalization variants; historical log links are allowed.",
                    "Deletion paths must exactly match the canonical spelling shown by Wiki search results or the actual Vault directory entry; do not change case or Unicode spelling.",
                    "Do not submit wiki/log.md; the writer appends its own entry after substantive ingest/query changes and lint Topic deletions.",
                    "The writer never commits or pushes; leave all changes in the working tree for user review.",
                    "Local links may target Vault-relative files or directories; external links may use http, https, or mailto, and anchors are allowed. Absolute paths, escaping paths, protocol-relative URLs, and other URL schemes are rejected.",
                    "One changeset is validated before application; multiple file replacements are not a single filesystem transaction.",
                    "When partial is true, applied_paths reports residual files, directories, or temporary files. If ok is true, Wiki updates were applied and only temporary-file cleanup remains; do not reapply the same changeset.",
                    "Do not include a Storage root or arbitrary destination in a changeset.",
                    "Both AGENTS.framework.md and AGENTS.md are required. If the shared Framework rules are missing, do not write; run `mise run setup` to repair the pinned Framework resources.",
                ],
            },
        }

    def _read_rules(self) -> tuple[str, str]:
        try:
            framework_rules = _safe_read(self.vault, "AGENTS.framework.md").decode("utf-8")
        except (OSError, UnicodeError, VaultPathError, ValueError) as exc:
            raise ValueError(
                "The selected Vault is missing readable AGENTS.framework.md. "
                "Wiki writes are disabled until `mise run setup` restores the pinned Framework rules."
            ) from exc
        try:
            local_rules = _safe_read(self.vault, "AGENTS.md").decode("utf-8")
        except (OSError, UnicodeError, VaultPathError, ValueError) as exc:
            raise ValueError("The selected Vault must contain a readable local AGENTS.md.") from exc
        return framework_rules, local_rules

    def apply(self, request: str | dict[str, Any]) -> dict[str, Any]:
        changeset = WikiChangeSet.parse(request)
        try:
            with storage_write_lock(self.root):
                try:
                    verify_managed_resources_locked(self.root)
                except ManagedResourceError as exc:
                    raise ValueError(str(exc)) from exc
                # Writes require both the pinned common policy and local policy to be present.
                self._read_rules()
                return self._apply_locked(changeset)
        except StorageWriteLockError as exc:
            raise ValueError("Could not acquire the cross-process Wiki writer lock.") from exc

    def _apply_locked(self, changeset: WikiChangeSet) -> dict[str, Any]:
        planned = self._prepare(changeset)
        self._validate_changeset(changeset, planned)
        changed = [item for item in planned if item.action == "write" and item.original != item.new]
        deleted = [item for item in planned if item.action == "delete"]
        if not changed and not deleted:
            return {
                "ok": True,
                "operation": changeset.operation,
                "changed_paths": [],
                "deleted_paths": [],
                "logged": False,
                "partial": False,
            }

        knowledge_paths = [item.path for item in changed] + [item.path for item in deleted]
        should_log = changeset.operation in {"ingest", "query"} or (
            changeset.operation == "lint" and any(item.path.startswith("wiki/topics/") for item in deleted)
        )
        log_item = self._make_log_plan(changeset, knowledge_paths) if should_log else None
        if log_item is not None:
            planned.append(log_item)
        return self._apply_plan(changeset.operation, planned, knowledge_paths, log_item is not None)

    def _prepare(self, changeset: WikiChangeSet) -> list[_PlannedFile]:
        planned: list[_PlannedFile] = []
        for write in changeset.writes:
            target = self._destination(write.path)
            original = self._read_target(write.path, missing_ok=True)
            if write.expected_sha256 is None:
                if original is not None:
                    raise ValueError(f"Create target already exists; read it and submit its SHA-256: {write.path}.")
            else:
                if original is None:
                    raise ValueError(f"Update target no longer exists; refresh the changeset: {write.path}.")
                if _digest(original) != write.expected_sha256:
                    raise ValueError(f"Stale Wiki update rejected because the file changed: {write.path}.")
            planned.append(
                _PlannedFile(write.path, target, original, write.content.encode("utf-8"), write.expected_sha256, "write")
            )

        for deletion in changeset.delete_analyses:
            relative = _canonical_managed_path(self.vault, deletion.path, root="wiki/analyses/")
            target = self._deletion_destination(relative, "wiki/analyses/")
            original = self._read_target(relative, missing_ok=True)
            if original is None:
                raise ValueError(f"Analysis deletion target does not exist: {relative}.")
            if _digest(original) != deletion.expected_sha256:
                raise ValueError(f"Stale analysis deletion rejected because the file changed: {relative}.")
            planned.append(_PlannedFile(relative, target, original, None, deletion.expected_sha256, "delete"))
        for deletion in changeset.delete_topics:
            relative = _canonical_managed_path(self.vault, deletion.path, root="wiki/topics/")
            target = self._deletion_destination(relative, "wiki/topics/")
            original = self._read_target(relative, missing_ok=True)
            if original is None:
                raise ValueError(f"Topic deletion target does not exist: {relative}.")
            if _digest(original) != deletion.expected_sha256:
                raise ValueError(f"Stale Topic deletion rejected because the file changed: {relative}.")
            planned.append(_PlannedFile(relative, target, original, None, deletion.expected_sha256, "delete"))
        return planned

    def _destination(self, relative: str) -> Path:
        if relative == "wiki/log.md":
            raise ValueError("wiki/log.md is maintained by the writer and cannot be supplied.")
        try:
            return resolve_vault_path(self.vault, relative, must_exist=False)
        except VaultPathError as exc:
            raise ValueError(f"Unsafe Wiki path: {relative}.") from exc

    def _deletion_destination(self, relative: str, prefix: str) -> Path:
        if not relative.startswith(prefix) or not relative.lower().endswith(".md"):
            raise ValueError(f"Only Markdown files under {prefix} may be deleted.")
        return self._destination(relative)

    def _read_target(self, relative: str, *, missing_ok: bool = False) -> bytes | None:
        try:
            parent = open_vault_parent(self.vault, relative)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ValueError(f"Cannot read Wiki path: {relative}.") from None
        except (OSError, VaultPathError) as exc:
            raise ValueError(f"Cannot safely read Wiki path: {relative}.") from exc
        try:
            content = parent.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read Wiki path: {relative}.") from exc
        except VaultPathError as exc:
            raise ValueError(f"Cannot read Wiki path: {relative}.") from exc
        finally:
            parent.close()
        if content is None and not missing_ok:
            raise ValueError(f"Wiki path does not exist: {relative}.")
        return content

    def _validate_changeset(self, changeset: WikiChangeSet, planned: list[_PlannedFile]) -> None:
        write_map = {item.path: item.new for item in planned if item.action == "write" and item.new is not None}
        deleted = {item.path for item in planned if item.action == "delete"}
        if changeset.delete_topics and "wiki/index.md" not in write_map:
            raise ValueError("Topic deletion requires a wiki/index.md update in the same changeset.")
        for item in planned:
            if item.action != "write" or item.new is None:
                continue
            content = _decode_utf8(item.new, item.path)
            if item.path.startswith("wiki/topics/"):
                _validate_topic(self.vault, item.path, content)
                original_text = _decode_utf8(item.original, item.path) if item.original is not None else None
                old_importance = _frontmatter_field_values(original_text, "importance") if original_text is not None else ()
                new_importance = _frontmatter_field_values(content, "importance")
                if old_importance != new_importance:
                    if changeset.operation != "lint":
                        raise ValueError("Adding, changing, or removing Topic importance requires operation lint.")
                    only_importance_and_updated_fields_differ = (
                        original_text is not None
                        and _without_frontmatter_fields(original_text, {"importance", "updated"})
                        == _without_frontmatter_fields(content, {"importance", "updated"})
                    )
                    if only_importance_and_updated_fields_differ and _frontmatter_field_values(
                        original_text, "updated"
                    ) != _frontmatter_field_values(content, "updated"):
                        raise ValueError("An importance-only change must keep the Topic updated field unchanged.")
            _validate_links(self.vault, item.path, content, write_map, deleted)

        try:
            index_bytes = _virtual_content(self.vault, "wiki/index.md", write_map, deleted)
        except (OSError, VaultPathError) as exc:
            raise ValueError("wiki/index.md is required and must be safely readable.") from exc
        _validate_index(self.vault, _decode_utf8(index_bytes, "wiki/index.md"), write_map, deleted)
        _validate_no_inbound_deleted_links(self.vault, write_map, deleted)

    def _make_log_plan(self, changeset: WikiChangeSet, paths: list[str]) -> _PlannedFile | None:
        if not paths:
            return None
        log_relative = "wiki/log.md"
        try:
            target = resolve_vault_path(self.vault, log_relative, must_exist=False)
        except VaultPathError as exc:
            raise ValueError("Cannot safely resolve wiki/log.md.") from exc
        original = self._read_target(log_relative, missing_ok=True)
        if original is not None:
            current = _decode_utf8(original, log_relative)
        else:
            current = "# 更新ログ\n"
        path_links = ", ".join(_log_path_link(path) for path in paths)
        summary = _log_summary(changeset.summary)
        description = f" — {summary}" if summary else ""
        line = f"- {date.today().isoformat()} [{changeset.operation}] {path_links}{description}\n"
        separator = "" if not current or current.endswith("\n") else "\n"
        new = (current + separator + line).encode("utf-8")
        return _PlannedFile(log_relative, target, original, new, _digest(original) if original is not None else None, "write")

    def _apply_plan(
        self,
        operation: str,
        planned: list[_PlannedFile],
        knowledge_paths: list[str],
        logged: bool,
    ) -> dict[str, Any]:
        staged: dict[str, StagedFile] = {}
        applied: list[_PlannedFile] = []
        current_path = "Wiki changeset"
        cleanup_residuals: list[str] = []
        staged_cleaned = False
        try:
            for item in planned:
                if item.action != "write" or item.original == item.new:
                    continue
                current_path = item.path
                staged[item.path] = self._stage(item.path, item.new or b"")

            for item in planned:
                if item.action == "write" and item.original == item.new:
                    continue
                current_path = item.path
                if item.action == "delete":
                    parent = self._open_parent(item.path, create=False)
                    try:
                        self._recheck_before_apply(item, parent)
                        parent.unlink()
                        applied.append(item)
                    finally:
                        parent.close()
                else:
                    staged_file = staged[item.path]
                    self._recheck_before_apply(item, staged_file.parent)
                    staged_file.parent.replace(staged_file)
                    applied.append(item)
        except Exception as exc:
            created_directories = {
                directory
                for staged_file in staged.values()
                for directory in staged_file.parent.created_directories
            }
            if isinstance(exc, _StageFailure):
                created_directories.update(exc.created_directories)
                cleanup_residuals.extend(exc.residual_paths)
            cleanup_residuals.extend(self._cleanup_staged(staged.values()))
            staged_cleaned = True
            remaining = self._rollback(applied)
            remaining.extend(cleanup_residuals)
            remaining.extend(self._remove_created_directories(created_directories))
            if isinstance(exc, ValueError):
                message = str(exc)
            elif isinstance(exc, _StageFailure):
                message = str(exc)
            elif isinstance(exc, (OSError, VaultPathError)):
                message = f"I/O failure while applying Wiki changes at {current_path}."
            else:
                message = f"Unexpected failure while applying Wiki changes at {current_path}."
            if remaining:
                message += " Rollback was incomplete; applied_paths reports residual files or directories."
            else:
                message += " Applied changes were rolled back."
            raise WikiApplyError(message, applied_paths=sorted(set(remaining))) from None
        finally:
            if not staged_cleaned:
                cleanup_residuals.extend(self._cleanup_staged(staged.values()))

        changed_paths = [item.path for item in planned if item.action == "write" and item.original != item.new]
        deleted_paths = [item.path for item in planned if item.action == "delete"]
        result = {
            "ok": True,
            "operation": operation,
            "changed_paths": changed_paths,
            "deleted_paths": deleted_paths,
            "logged": logged,
            "partial": bool(cleanup_residuals),
        }
        if cleanup_residuals:
            result["applied_paths"] = sorted(set(cleanup_residuals))
            result["cleanup_warning"] = "Wiki updates were applied, but temporary files could not be removed."
        return result

    def _open_parent(self, relative: str, *, create: bool = True) -> SafeVaultParent:
        try:
            return open_vault_parent(self.vault, relative, create=create)
        except (OSError, VaultPathError) as exc:
            raise _SafeOpenError(
                f"Could not safely open Wiki directory: {relative}.",
                tuple(getattr(exc, "cleanup_paths", ())),
            ) from exc

    def _stage(self, relative: str, content: bytes) -> StagedFile:
        parent: SafeVaultParent | None = None
        try:
            parent = self._open_parent(relative)
            return parent.stage(content)
        except Exception as exc:
            created_directories = (
                parent.created_directories
                if parent is not None
                else tuple(getattr(exc, "cleanup_paths", ()))
            )
            residual_paths = tuple(getattr(exc, "residual_paths", ()))
            if parent is not None:
                parent.close()
            raise _StageFailure(
                f"Could not safely stage Wiki path: {relative}.",
                created_directories,
                residual_paths,
            ) from None

    @staticmethod
    def _recheck_before_apply(item: _PlannedFile, parent: SafeVaultParent) -> None:
        try:
            current = parent.read_bytes()
        except (OSError, VaultPathError) as exc:
            raise ValueError(f"Wiki path changed during apply: {item.path}.") from exc
        if item.original is None:
            if current is not None:
                raise ValueError(f"Create target appeared during apply: {item.path}.")
        elif current is None or _digest(current) != item.expected_sha256:
            raise ValueError(f"Stale Wiki change rejected during apply: {item.path}.")

    def _rollback(self, applied: list[_PlannedFile]) -> list[str]:
        remaining: list[str] = []
        for item in reversed(applied):
            parent: SafeVaultParent | None = None
            temporary: StagedFile | None = None
            try:
                parent = open_vault_parent(self.vault, item.path, create=item.action == "delete")
                current = parent.read_bytes()
                if item.action == "write":
                    if current is None or _digest(current) != _digest(item.new or b""):
                        remaining.append(item.path)
                        continue
                    if item.original is None:
                        parent.unlink()
                    else:
                        temporary = parent.stage(item.original)
                        parent.replace(temporary)
                else:
                    if current is not None:
                        remaining.append(item.path)
                        continue
                    temporary = parent.stage(item.original or b"")
                    parent.replace(temporary)
            except (OSError, ValueError, VaultPathError) as exc:
                remaining.append(item.path)
                remaining.extend(getattr(exc, "residual_paths", ()))
            finally:
                if temporary is not None:
                    residual_path = temporary.cleanup()
                    if residual_path is not None:
                        remaining.append(residual_path)
                if parent is not None:
                    parent.close()
        return sorted(set(remaining))

    @staticmethod
    def _cleanup_staged(paths) -> list[str]:
        residual_paths: list[str] = []
        for staged in paths:
            residual_path = staged.cleanup()
            if residual_path is not None:
                residual_paths.append(residual_path)
            staged.parent.close()
        return sorted(set(residual_paths))

    def _remove_created_directories(self, directories: set[str]) -> list[str]:
        remaining: list[str] = []
        for directory in sorted(directories, key=lambda path: (path.count("/"), path), reverse=True):
            if not remove_empty_vault_directory(self.vault, directory):
                remaining.append(directory)
        return remaining


def _log_summary(summary: str | None) -> str:
    if not summary:
        return ""
    flattened = " ".join(summary.split())[:300]
    return flattened.replace("[", "\\[").replace("]", "\\]").replace("`", "\\`")


def _log_path_link(path: str) -> str:
    if not path.startswith("wiki/"):
        return path
    label = path.replace("[", "\\[").replace("]", "\\]")
    target = quote(path[5:], safe="/._-")
    return f"[{label}]({target})"
