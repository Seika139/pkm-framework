"""SQLite FTS5 indexing and search for Markdown files in a PKM vault."""

from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pkm_framework.storage import StorageLayout
from pkm_framework.paths import (
    VaultPathError,
    ensure_no_symlink_components,
    resolve_vault_path,
)
from pkm_framework.fileops import open_vault_parent


SCHEMA_VERSION = 1
_METADATA_RE = re.compile(r"^(title|url|source|canonical_url):\s*(.*?)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


class IndexingError(RuntimeError):
    """Raised when an index cannot be safely refreshed or searched."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_components(path: Path, anchor: Path) -> None:
    try:
        ensure_no_symlink_components(path, anchor)
    except VaultPathError as exc:
        raise IndexingError(str(exc)) from exc


def _validate_cache_path(root: Path, path: Path, *, create_parent: bool) -> None:
    root = _absolute(root)
    path = _absolute(path)
    if root.is_symlink() or not root.is_dir():
        raise IndexingError(f"Storage root must be a real directory: {root}")
    _reject_symlink_components(path, root)
    parent = path.parent
    for directory in (root / ".pkm", root / ".pkm" / "cache"):
        if directory.exists() and not directory.is_dir():
            raise IndexingError(f"Index cache parent must be a directory: {directory}")
    if path.exists() and not path.is_file():
        raise IndexingError(f"Index database path must be a regular file: {path}")
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path, root)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise IndexingError(f"Cannot resolve index cache path {path}: {exc}") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise IndexingError(f"Index cache must remain under Storage root: {path}")


def _validate_storage_layout(layout: StorageLayout) -> StorageLayout:
    root = _absolute(layout.root)
    vault = _absolute(layout.vault)
    if root.is_symlink() or not root.is_dir():
        raise IndexingError(f"Storage root must be a real directory: {root}")
    _reject_symlink_components(vault, root)
    if not vault.is_dir():
        raise IndexingError(f"Vault must be a real directory under Storage: {vault}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_vault = vault.resolve(strict=True)
    except OSError as exc:
        raise IndexingError(f"Cannot resolve Storage vault: {exc}") from exc
    if not resolved_vault.is_relative_to(resolved_root):
        raise IndexingError(f"Vault must remain under Storage root: {vault}")

    normalized = StorageLayout(root=root, vault=vault)
    for indexed_root in normalized.indexed_roots:
        _reject_symlink_components(indexed_root, vault)
        if not indexed_root.is_dir():
            raise IndexingError(f"Indexed root must be a real directory under the vault: {indexed_root}")
        if not indexed_root.resolve(strict=True).is_relative_to(resolved_vault):
            raise IndexingError(f"Indexed root resolves outside the vault: {indexed_root}")
    return normalized


@dataclass(frozen=True)
class MarkdownFile:
    path: Path
    relative_path: str
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class SearchResult:
    path: str
    title: str
    url: str
    snippet: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "score": self.score,
        }


def _scan_markdown(layout: StorageLayout) -> list[MarkdownFile]:
    files: list[MarkdownFile] = []

    def walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise IndexingError(f"Cannot scan {directory}: {exc}") from exc

        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.name.lower().endswith(".md") and entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    files.append(
                        MarkdownFile(
                            path=path,
                            relative_path=path.relative_to(layout.vault).as_posix(),
                            mtime_ns=stat.st_mtime_ns,
                            size=stat.st_size,
                        )
                    )
            except OSError as exc:
                raise IndexingError(f"Cannot inspect {path}: {exc}") from exc

    for root in layout.indexed_roots:
        _reject_symlink_components(root, layout.vault)
        if not root.resolve(strict=True).is_relative_to(layout.vault.resolve(strict=True)):
            raise IndexingError(f"Indexed directory must remain inside the vault: {root}")
        if not root.is_dir():
            raise IndexingError(f"Required Markdown directory does not exist: {root}")
        walk(root)
    return files


def _connect(database: Path, storage_root: Path) -> sqlite3.Connection:
    _validate_cache_path(storage_root, database, create_parent=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        _validate_cache_path(storage_root, database, create_parent=False)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            "id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
            "url TEXT NOT NULL, body TEXT NOT NULL, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
            "title, url, body, path UNINDEXED, tokenize='trigram')"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        return connection
    except sqlite3.OperationalError as exc:
        connection.close()
        if "fts5" in str(exc).lower() or "tokenizer" in str(exc).lower():
            raise IndexingError(
                "SQLite FTS5 with the trigram tokenizer is required (SQLite 3.34 or newer)."
            ) from exc
        raise IndexingError(f"Cannot initialize SQLite index {database}: {exc}") from exc


def _frontmatter_value(text: str, key: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() in {"---", "..."}:
            break
        match = _METADATA_RE.match(line)
        if match and match.group(1).lower() == key.lower():
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return None


def _metadata(path: Path, text: str) -> tuple[str, str]:
    title = _frontmatter_value(text, "title")
    if not title:
        heading = _HEADING_RE.search(text)
        title = heading.group(1).strip() if heading else path.stem
    url = (
        _frontmatter_value(text, "url")
        or _frontmatter_value(text, "canonical_url")
        or _frontmatter_value(text, "source")
        or ""
    )
    return title, url


def _resolve_allowed_file(layout: StorageLayout, path: Path) -> Path:
    try:
        relative = _absolute(path).relative_to(_absolute(layout.vault)).as_posix()
        resolved = resolve_vault_path(layout.vault, relative, must_exist=True).resolve(strict=True)
    except (ValueError, OSError, VaultPathError) as exc:
        raise IndexingError(f"Cannot resolve Markdown path inside the vault: {path}") from exc
    allowed_roots = tuple(root.resolve(strict=True) for root in layout.indexed_roots)
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise IndexingError(f"Not a Markdown file: {path}")
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise IndexingError(f"Markdown path is outside indexed roots: {path}")
    return resolved


def _read_markdown(layout: StorageLayout, markdown: MarkdownFile) -> tuple[str, str, str]:
    try:
        path = _resolve_allowed_file(layout, markdown.path)
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
    except (OSError, UnicodeError) as exc:
        raise IndexingError(f"Cannot read Markdown file {markdown.path}: {exc}") from exc
    if stat.st_mtime_ns != markdown.mtime_ns or stat.st_size != markdown.size:
        raise IndexingError(f"File changed during indexing; retry: {markdown.path}")
    title, url = _metadata(markdown.path, text)
    return title, url, text


def _upsert(connection: sqlite3.Connection, layout: StorageLayout, markdown: MarkdownFile) -> None:
    title, url, body = _read_markdown(layout, markdown)
    connection.execute(
        "INSERT INTO documents(path, title, url, body, mtime_ns, size) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET title=excluded.title, url=excluded.url, body=excluded.body, "
        "mtime_ns=excluded.mtime_ns, size=excluded.size",
        (markdown.relative_path, title, url, body, markdown.mtime_ns, markdown.size),
    )
    document_id = connection.execute(
        "SELECT id FROM documents WHERE path = ?", (markdown.relative_path,)
    ).fetchone()["id"]
    connection.execute("DELETE FROM search_index WHERE rowid = ?", (document_id,))
    connection.execute(
        "INSERT INTO search_index(rowid, title, url, body, path) VALUES (?, ?, ?, ?, ?)",
        (document_id, title, url, body, markdown.relative_path),
    )


def _plain_snippet(body: str, term: str, width: int = 220) -> str:
    position = body.casefold().find(term.casefold())
    if position < 0:
        return body[:width].replace("\n", " ")
    start = max(0, position - width // 3)
    end = min(len(body), start + width)
    excerpt = body[start:end].replace("\n", " ")
    return ("…" if start else "") + excerpt + ("…" if end < len(body) else "")


class KnowledgeIndex:
    """Shared indexing service for the CLI and MCP server."""

    def __init__(self, layout: StorageLayout):
        self.layout = _validate_storage_layout(layout)
        self.database = self.layout.database

    def _validate_layout(self) -> None:
        """Recheck the vault boundary because the filesystem may have changed."""

        self.layout = _validate_storage_layout(self.layout)

    def rebuild(self) -> int:
        """Create a full replacement DB and swap it in only after success."""

        self._validate_layout()
        markdown_files = _scan_markdown(self.layout)
        _validate_cache_path(self.layout.root, self.database, create_parent=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f"{self.database.name}.building-", suffix=".sqlite", dir=self.database.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        connection: sqlite3.Connection | None = None
        try:
            _validate_cache_path(self.layout.root, temporary, create_parent=False)
            connection = _connect(temporary, self.layout.root)
            connection.execute("BEGIN IMMEDIATE")
            for markdown in markdown_files:
                _upsert(connection, self.layout, markdown)
            connection.commit()
            connection.close()
            connection = None
            _validate_cache_path(self.layout.root, temporary, create_parent=False)
            _validate_cache_path(self.layout.root, self.database, create_parent=False)
            os.replace(temporary, self.database)
            return len(markdown_files)
        except Exception:
            if connection is not None:
                connection.rollback()
                connection.close()
            temporary.unlink(missing_ok=True)
            raise

    def refresh(self) -> int:
        """Apply additions, updates, and deletions atomically."""

        self._validate_layout()
        markdown_files = _scan_markdown(self.layout)
        connection = _connect(self.database, self.layout.root)
        try:
            existing = {
                row["path"]: (row["id"], row["mtime_ns"], row["size"])
                for row in connection.execute("SELECT id, path, mtime_ns, size FROM documents")
            }
            paths = {item.relative_path for item in markdown_files}
            changed = [
                item
                for item in markdown_files
                if item.relative_path not in existing
                or existing[item.relative_path][1:] != (item.mtime_ns, item.size)
            ]
            deleted = set(existing) - paths
            connection.execute("BEGIN IMMEDIATE")
            for item in changed:
                _upsert(connection, self.layout, item)
            for relative_path in deleted:
                document_id = existing[relative_path][0]
                connection.execute("DELETE FROM search_index WHERE rowid = ?", (document_id,))
                connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            connection.commit()
            return len(changed) + len(deleted)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def search(self, query: str, limit: int = 10) -> tuple[list[SearchResult], int]:
        """Refresh the index and search using trigram FTS or short-query fallback."""

        self._validate_layout()
        normalized = " ".join(query.split())
        if not normalized:
            raise ValueError("Search query must not be empty.")
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100.")

        changed_count = self.refresh()
        terms = normalized.split()
        connection = _connect(self.database, self.layout.root)
        try:
            if all(len(term) >= 3 for term in terms):
                expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                try:
                    rows = connection.execute(
                        "SELECT d.path, d.title, d.url, "
                        "snippet(search_index, 2, '[', ']', '…', 18) AS snippet, "
                        "bm25(search_index) AS rank "
                        "FROM search_index JOIN documents d ON d.id = search_index.rowid "
                        "WHERE search_index MATCH ? ORDER BY rank LIMIT ?",
                        (expression, limit),
                    ).fetchall()
                    results = [
                        SearchResult(
                            path=row["path"], title=row["title"], url=row["url"],
                            snippet=row["snippet"], score=-float(row["rank"]),
                        )
                        for row in rows
                    ]
                    return results, changed_count
                except sqlite3.OperationalError:
                    # Punctuation-only strings may not form a valid trigram phrase.
                    pass

            conditions: list[str] = []
            parameters: list[str] = []
            for term in terms:
                conditions.append(
                    "(instr(lower(d.title), lower(?)) > 0 OR instr(lower(d.url), lower(?)) > 0 "
                    "OR instr(lower(d.path), lower(?)) > 0 OR instr(lower(d.body), lower(?)) > 0)"
                )
                parameters.extend((term, term, term, term))
            parameters.append(str(limit))
            rows = connection.execute(
                "SELECT d.path, d.title, d.url, d.body FROM documents d WHERE "
                + " AND ".join(conditions)
                + " ORDER BY d.title COLLATE NOCASE LIMIT ?",
                parameters,
            ).fetchall()
            results = [
                SearchResult(
                    path=row["path"],
                    title=row["title"],
                    url=row["url"],
                    snippet=_plain_snippet(row["body"], terms[0]),
                    score=0.0,
                )
                for row in rows
            ]
            return results, changed_count
        finally:
            connection.close()

    def read(
        self, relative_path: str, max_chars: int = 200_000, offset: int = 0
    ) -> dict[str, Any]:
        """Read a Markdown file under raw/clips or wiki without modifying it."""

        self._validate_layout()
        if not 1 <= max_chars <= 200_000:
            raise ValueError("max_chars must be between 1 and 200000.")
        if offset < 0:
            raise ValueError("offset must not be negative.")
        requested = Path(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("Path must be a vault-relative path without '..'.")
        try:
            candidate = _resolve_allowed_file(self.layout, self.layout.vault / requested)
        except IndexingError as exc:
            if "Symbolic links" in str(exc):
                raise ValueError(str(exc)) from exc
            raise FileNotFoundError(f"Markdown file not found or outside indexed roots: {relative_path}") from exc
        except OSError as exc:
            raise FileNotFoundError(f"Markdown file not found: {relative_path}") from exc
        relative = _absolute(self.layout.vault / requested).relative_to(
            _absolute(self.layout.vault)
        ).as_posix()
        try:
            parent = open_vault_parent(self.layout.vault, relative)
            try:
                raw_content = parent.read_bytes()
            finally:
                parent.close()
            if raw_content is None:
                raise FileNotFoundError(relative)
            content = raw_content.decode("utf-8")
        except (OSError, UnicodeError, VaultPathError) as exc:
            raise IndexingError(f"Cannot read Markdown file {candidate}: {exc}") from exc

        title, url = _metadata(candidate, content)
        end = min(offset + max_chars, len(content))
        if offset > len(content):
            raise ValueError(f"offset exceeds the Markdown file length ({len(content)} characters).")
        return {
            "path": relative,
            "title": title,
            "url": url,
            "content": content[offset:end],
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "total_chars": len(content),
            "truncated": offset > 0 or end < len(content),
            "sha256": hashlib.sha256(raw_content).hexdigest(),
            "sha256_scope": "full_file_bytes",
        }
