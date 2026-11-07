"""Tests for source registration, profile boundaries, and enrollment lifecycle."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pkm_framework.config import (
    ConfigChangedError,
    StorageConfigError,
    atomic_review_hash,
    assert_snapshot,
    load_config,
    parse_config_bytes,
    require_owner,
    wiki_allowed,
)
from pkm_framework.index import KnowledgeIndex
from pkm_framework.mcp_server import profile_can_read_path, require_wiki_write
from pkm_framework.storage import (
    CONFIGURED_MARKER,
    LEGACY_MARKER,
    MARKER_NAME,
    resolve_storage,
)
from pkm_framework.wiki import WikiApplyError, WikiWriter, _validate_topic
from pkm_framework.windows_security import WindowsReplaceError


SYNC_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "pkm_framework"
    / "storage_resources"
    / "mise"
    / "scripts"
)
sys.path.insert(0, str(SYNC_DIR))
import sync as storage_sync


def config_text(*, people_wiki_source: bool = True, people_sync: bool = True) -> str:
    return f'''version = 1

[[sources]]
id = "clips"
vault_path = "raw/clips/"
kind = "web_clip"
index = true
wiki_source = true
sync = true

[[sources]]
id = "people"
vault_path = "raw/people/"
kind = "personal_record"
index = true
wiki_source = {str(people_wiki_source).lower()}
sync = {str(people_sync).lower()}

[wiki]
reviewed_policy_hash = ""

[profiles.clips_only]
source_ids = ["clips"]
[profiles.clips_only.wiki]
read = false
write = false

[profiles.personal]
source_ids = ["clips", "people"]
[profiles.personal.wiki]
read = true
write = true
'''


class StoragePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "My Vault"
        self.vault.mkdir()
        self.marker = self.vault / MARKER_NAME
        self.marker.write_text(CONFIGURED_MARKER + "\n", encoding="ascii")
        self._make_dirs()
        self.write_config(config_text())

    def _make_dirs(self) -> None:
        for relative in (
            "raw/clips",
            "raw/people",
            "wiki/topics",
            "wiki/analyses",
            "assets",
        ):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)

    def write_config(self, text: str) -> None:
        config_path = self.root / ".pkm" / "storage.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(text, encoding="utf-8")

    def approve_policy(self) -> None:
        path = self.root / ".pkm" / "storage.toml"
        current = path.read_text(encoding="utf-8")
        pending = parse_config_bytes(current.encode(), self.vault)
        path.write_text(current.replace('reviewed_policy_hash = ""', f'reviewed_policy_hash = "{pending.policy_hash}"'), encoding="utf-8")

    def test_strict_config_rejects_unknown_keys_paths_and_overlaps(self) -> None:
        valid = config_text()
        with self.assertRaises(StorageConfigError):
            parse_config_bytes(valid.replace('kind = "web_clip"', 'kind = "web_clip"\nextra = true').encode(), self.vault)
        with self.assertRaises(StorageConfigError):
            parse_config_bytes(valid.replace('vault_path = "raw/people/"', 'vault_path = "raw/clips/nested/"').encode(), self.vault)
        with self.assertRaises(StorageConfigError):
            parse_config_bytes(valid.replace('vault_path = "raw/people/"', 'vault_path = "raw/../people/"').encode(), self.vault)
        with self.assertRaises(StorageConfigError):
            parse_config_bytes(valid.replace('index = true\nwiki_source = true', 'index = "true"\nwiki_source = true', 1).encode(), self.vault)

    def test_strict_config_rejects_symlink_source_root(self) -> None:
        alias = self.vault / "raw" / "alias"
        alias.symlink_to(self.vault / "raw" / "people", target_is_directory=True)
        text = config_text().replace('vault_path = "raw/people/"', 'vault_path = "raw/alias/"')
        with self.assertRaises(StorageConfigError):
            parse_config_bytes(text.encode(), self.vault)

    def test_marker_enrollment_is_fail_closed_and_legacy_behavior_remains(self) -> None:
        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        with self.assertRaisesRegex(StorageConfigError, "pkm storage enroll"):
            resolve_storage(self.root)
        legacy_config = resolve_storage(self.root, allow_unenrolled_config=True)
        self.assertIsNone(legacy_config.config)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            from pkm_framework.cli import main

            self.assertEqual(main(["storage", "enroll", "--storage", str(self.root)]), 0)
        self.assertEqual(self.marker.read_text(encoding="ascii"), CONFIGURED_MARKER + "\n")
        self.assertIsNotNone(resolve_storage(self.root).config)

        config_path = self.root / ".pkm" / "storage.toml"
        valid_config = config_path.read_text(encoding="utf-8")
        config_path.write_text("version = 1\nunknown = true\n", encoding="utf-8")
        with self.assertRaises(StorageConfigError):
            resolve_storage(self.root)
        config_path.write_text(valid_config, encoding="utf-8")
        (self.root / ".pkm" / "storage.toml").unlink()
        with self.assertRaisesRegex(StorageConfigError, "refusing legacy fallback"):
            resolve_storage(self.root)

    def test_pinned_config_digest_rejects_mutation(self) -> None:
        pinned = load_config(self.root, self.vault)
        assert_snapshot(self.root, self.vault, pinned)
        path = self.root / ".pkm" / "storage.toml"
        path.write_text(path.read_text(encoding="utf-8").replace('sync = true', 'sync = false', 1), encoding="utf-8")
        with self.assertRaisesRegex(ConfigChangedError, "config changed"):
            assert_snapshot(self.root, self.vault, pinned)

    def test_policy_review_requires_owner_and_explicit_confirmation(self) -> None:
        config = load_config(self.root, self.vault)
        with patch("pkm_framework.config._storage_owner_matches", return_value=False):
            with self.assertRaisesRegex(StorageConfigError, "Storage owner"):
                require_owner(self.root)

        from pkm_framework.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["storage", "policy-review", "--storage", str(self.root)]), 0)
        self.assertEqual(load_config(self.root, self.vault).reviewed_policy_hash, "")
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["storage", "policy-review", "--confirm", "--storage", str(self.root)]), 0
            )
        reviewed = load_config(self.root, self.vault)
        self.assertEqual(reviewed.reviewed_policy_hash, config.policy_hash)
        self.assertTrue(wiki_allowed(reviewed, reviewed.profiles["personal"]))

    def test_policy_review_rejects_multiline_hash_without_modifying_config(self) -> None:
        self.write_config(config_text().replace('reviewed_policy_hash = ""', 'reviewed_policy_hash = """\n"""'))
        config = load_config(self.root, self.vault)
        config_path = self.root / ".pkm" / "storage.toml"
        original = config_path.read_bytes()

        with self.assertRaisesRegex(StorageConfigError, "single-line empty or SHA-256 string"):
            atomic_review_hash(self.root, config)

        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(list(config_path.parent.glob("storage.toml.*.tmp")), [])

    def test_policy_review_copies_target_acl_after_payload_and_before_replace(self) -> None:
        config = load_config(self.root, self.vault)
        events: list[tuple[str, bytes]] = []

        def observe_acl(_source: Path, temporary: Path) -> None:
            events.append(("acl", temporary.read_bytes()))

        def observe_replace(temporary: Path, destination: Path) -> None:
            events.append(("replace", temporary.read_bytes()))
            os.replace(temporary, destination)

        with (
            patch(
                "pkm_framework.config.preserve_atomic_replacement_acl",
                side_effect=observe_acl,
            ),
            patch(
                "pkm_framework.config.atomic_replace_file",
                side_effect=observe_replace,
            ),
        ):
            atomic_review_hash(self.root, config)

        self.assertEqual([name for name, _content in events], ["acl", "replace"])
        self.assertEqual(events[0][1], events[1][1])
        self.assertIn(config.policy_hash.encode("ascii"), events[0][1])

    def test_policy_review_fails_closed_when_temporary_acl_cannot_be_set(self) -> None:
        config = load_config(self.root, self.vault)
        config_path = self.root / ".pkm" / "storage.toml"
        original = config_path.read_bytes()
        observed: list[bytes] = []

        def fail_acl_copy(_source: Path, temporary: Path) -> None:
            observed.append(temporary.read_bytes())
            raise OSError("ACL copy failed")

        with patch(
            "pkm_framework.config.preserve_atomic_replacement_acl",
            side_effect=fail_acl_copy,
        ):
            with self.assertRaisesRegex(StorageConfigError, "Could not safely update"):
                atomic_review_hash(self.root, config)

        self.assertEqual(len(observed), 1)
        self.assertIn(config.policy_hash.encode("ascii"), observed[0])
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(list(config_path.parent.glob("storage.toml.*.tmp")), [])

    def test_policy_review_retains_staged_content_if_target_and_backup_are_missing(self) -> None:
        config = load_config(self.root, self.vault)
        config_path = self.root / ".pkm" / "storage.toml"
        temporary_paths: list[Path] = []

        def fail_without_recovery_copy(temporary: Path, destination: Path) -> None:
            temporary_paths.append(temporary)
            destination.unlink()
            raise WindowsReplaceError(
                f"Windows replacement failed; staged content remains at {temporary}",
                preserve_source=True,
            )

        with patch("pkm_framework.config.atomic_replace_file", side_effect=fail_without_recovery_copy):
            with self.assertRaisesRegex(StorageConfigError, "staged content remains"):
                atomic_review_hash(self.root, config)

        self.assertFalse(config_path.exists())
        self.assertTrue(temporary_paths[0].is_file())
        self.assertIn(config.policy_hash, temporary_paths[0].read_text(encoding="utf-8"))

    def test_enrollment_retains_staged_marker_if_target_and_backup_are_missing(self) -> None:
        from pkm_framework.cli import _enroll_storage

        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        temporary_paths: list[Path] = []

        def fail_without_recovery_copy(temporary: Path, destination: Path) -> None:
            temporary_paths.append(temporary)
            destination.unlink()
            raise WindowsReplaceError(
                f"Windows replacement failed; staged content remains at {temporary}",
                preserve_source=True,
            )

        with (
            patch("pkm_framework.config._storage_owner_matches", return_value=True),
            patch("pkm_framework.cli.atomic_replace_file", side_effect=fail_without_recovery_copy),
        ):
            with self.assertRaisesRegex(StorageConfigError, "staged content remains"):
                _enroll_storage(self.root, self.vault)

        self.assertFalse(self.marker.exists())
        self.assertTrue(temporary_paths[0].is_file())
        self.assertEqual(temporary_paths[0].read_text(encoding="ascii"), CONFIGURED_MARKER + "\n")

    def test_enrollment_copies_target_acl_after_marker_write_and_before_replace(self) -> None:
        from pkm_framework.cli import _enroll_storage

        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        events: list[tuple[str, bytes]] = []

        def observe_acl(_source: Path, temporary: Path) -> None:
            events.append(("acl", temporary.read_bytes()))

        def observe_replace(temporary: Path, destination: Path) -> None:
            events.append(("replace", temporary.read_bytes()))
            os.replace(temporary, destination)

        with (
            patch("pkm_framework.config._storage_owner_matches", return_value=True),
            patch(
                "pkm_framework.cli.preserve_atomic_replacement_acl",
                side_effect=observe_acl,
            ),
            patch(
                "pkm_framework.cli.atomic_replace_file",
                side_effect=observe_replace,
            ),
        ):
            _enroll_storage(self.root, self.vault)

        expected = (CONFIGURED_MARKER + "\n").encode("ascii")
        self.assertEqual([name for name, _content in events], ["acl", "replace"])
        self.assertEqual(events[0][1], expected)
        self.assertEqual(events[1][1], expected)
        self.assertEqual(self.marker.read_bytes(), expected)

    def test_legacy_v1_without_config_keeps_clips_and_wiki_roots(self) -> None:
        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        (self.root / ".pkm" / "storage.toml").unlink()
        layout = resolve_storage(self.root)
        self.assertEqual(layout.indexed_roots, (self.vault / "raw/clips", self.vault / "wiki"))
        self.assertIsNone(layout.config)
        (self.vault / "raw/clips/legacy.md").write_text("# Legacy\nlegacyword", encoding="utf-8")
        results, changed = KnowledgeIndex(layout).search("legacyword")
        self.assertEqual([result.path for result in results], ["raw/clips/legacy.md"])
        self.assertEqual(changed, 1)

    def test_profile_search_is_filtered_before_limit_and_refresh_stays_global(self) -> None:
        layout = resolve_storage(self.root)
        (self.vault / "raw/clips/clip.md").write_text("# Sushi source\nsushi destination", encoding="utf-8")
        (self.vault / "raw/people/person.md").write_text("# Sushi preference\nsushi destination", encoding="utf-8")
        (self.vault / "wiki/topics/sushi.md").write_text("# Sushi topic\nsushi destination", encoding="utf-8")
        index = KnowledgeIndex(layout)
        results, _ = index.search(
            "sushi", source_ids={"clips"}, allow_wiki=False, restricted=True, limit=1
        )
        self.assertEqual([result.path for result in results], ["raw/clips/clip.md"])
        connection = sqlite3.connect(index.database)
        try:
            indexed_paths = {row[0] for row in connection.execute("SELECT path FROM documents")}
        finally:
            connection.close()
        self.assertEqual(
            indexed_paths,
            {"raw/clips/clip.md", "raw/people/person.md", "wiki/topics/sushi.md"},
        )

    def test_missing_configured_source_is_empty_until_created(self) -> None:
        people_root = self.vault / "raw/people"
        people_root.rmdir()
        (self.vault / "raw/clips/clip.md").write_text("# Available\nclipword", encoding="utf-8")

        index = KnowledgeIndex(resolve_storage(self.root))
        results, _ = index.search("clipword")
        self.assertEqual([result.path for result in results], ["raw/clips/clip.md"])

        people_root.mkdir()
        (people_root / "person.md").write_text("# Favorite\npersonword", encoding="utf-8")
        results, _ = index.search("personword")
        self.assertEqual([result.path for result in results], ["raw/people/person.md"])

    def test_restricted_search_uses_unicode_casefold_and_nfc(self) -> None:
        layout = resolve_storage(self.root)
        (self.vault / "raw/clips/clip.md").write_text(
            "# ÉCOLE visit\nThe signs say ÉCOLE. A decomposed Cafe\u0301 appears here too.",
            encoding="utf-8",
        )
        (self.vault / "raw/people/private.md").write_text(
            "# A hidden ÉCOLE\nCafé is not authorized for this profile.",
            encoding="utf-8",
        )
        index = KnowledgeIndex(layout)

        for query in ("école", "café"):
            with self.subTest(query=query):
                results, _ = index.search(
                    query,
                    source_ids={"clips"},
                    allow_wiki=False,
                    restricted=True,
                    limit=1,
                )
                self.assertEqual([result.path for result in results], ["raw/clips/clip.md"])

    def test_profile_read_scope_and_wiki_all_or_nothing_policy(self) -> None:
        config = load_config(self.root, self.vault)
        clips = config.profiles["clips_only"]
        personal = config.profiles["personal"]
        layout = resolve_storage(self.root)
        self.assertTrue(profile_can_read_path(layout, clips, "raw/clips/article.md"))
        self.assertFalse(profile_can_read_path(layout, clips, "raw/people/person.md"))
        self.assertFalse(profile_can_read_path(layout, clips, "wiki/topics/example.md"))
        self.assertFalse(wiki_allowed(config, personal))  # review is pending
        self.approve_policy()
        reviewed = load_config(self.root, self.vault)
        self.assertTrue(wiki_allowed(reviewed, reviewed.profiles["personal"]))
        self.assertFalse(wiki_allowed(reviewed, reviewed.profiles["clips_only"]))

    def test_configured_mcp_requires_an_explicit_known_profile(self) -> None:
        from pkm_framework.mcp_server import build_server

        with patch.dict(os.environ, {"PKM_MCP_PROFILE": ""}):
            with self.assertRaisesRegex(StorageConfigError, "explicit PKM_MCP_PROFILE"):
                build_server(str(self.vault))
        with patch.dict(os.environ, {"PKM_MCP_PROFILE": "missing"}):
            with self.assertRaisesRegex(StorageConfigError, "does not name"):
                build_server(str(self.vault))

    def test_wiki_write_false_is_explicitly_rejected_even_with_python_optimization(self) -> None:
        text = config_text() + '''
[profiles.read_only]
source_ids = ["clips", "people"]
[profiles.read_only.wiki]
read = true
write = false
'''
        pending = parse_config_bytes(text.encode(), self.vault)
        text = text.replace(
            'reviewed_policy_hash = ""',
            f'reviewed_policy_hash = "{pending.policy_hash}"',
        )
        self.write_config(text)
        config = load_config(self.root, self.vault)
        self.assertTrue(wiki_allowed(config, config.profiles["read_only"]))
        with self.assertRaisesRegex(ValueError, "write"):
            require_wiki_write(config, config.profiles["read_only"])

        script = '''
import sys
from pkm_framework.mcp_server import build_server
server = build_server(sys.argv[1])
try:
    server._tool_manager.get_tool("wiki_apply").fn({})
except ValueError as exc:
    if "Wiki writes are unavailable" in str(exc):
        raise SystemExit(0)
    raise SystemExit(8)
raise SystemExit(9)
'''
        environment = os.environ.copy()
        environment["PKM_MCP_PROFILE"] = "read_only"
        result = subprocess.run(
            [sys.executable, "-O", "-c", script, str(self.vault)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_mcp_rejects_enrollment_changes_before_and_after_tools(self) -> None:
        from pkm_framework.mcp_server import build_server

        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        (self.root / ".pkm" / "storage.toml").unlink()
        calls = (
            ("search", {"query": "anything"}),
            ("read", {"path": "raw/clips/missing.md"}),
            ("wiki_context", {}),
            ("wiki_apply", {"request": {}}),
        )
        for name, arguments in calls:
            (self.root / ".pkm" / "storage.toml").unlink(missing_ok=True)
            server = build_server(str(self.vault))
            self.write_config(config_text())
            with self.subTest(tool=name), self.assertRaisesRegex(ValueError, "restart the MCP server"):
                server._tool_manager.get_tool(name).fn(**arguments)

        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        (self.root / ".pkm" / "storage.toml").unlink()
        server = build_server(str(self.vault))

        def add_config_during_search(_index, _query, _limit=10, **_kwargs):
            self.write_config(config_text())
            return [], 0

        with patch(
            "pkm_framework.mcp_server.KnowledgeIndex.search",
            autospec=True,
            side_effect=add_config_during_search,
        ):
            with self.assertRaisesRegex(ValueError, "restart the MCP server"):
                server._tool_manager.get_tool("search").fn(query="anything")

        self.marker.write_text(LEGACY_MARKER + "\n", encoding="ascii")
        (self.root / ".pkm" / "storage.toml").unlink()
        server = build_server(str(self.vault))

        def add_config_during_writer(writer, _request):
            self.write_config(config_text())
            if writer.config_guard is None:
                raise RuntimeError("legacy writer did not receive a config guard")
            try:
                writer.config_guard()
            except ConfigChangedError as exc:
                raise WikiApplyError("config changed", applied_paths=["wiki/questions.md"]) from exc
            raise RuntimeError("legacy writer guard accepted an enrollment change")

        with patch(
            "pkm_framework.wiki.WikiWriter.apply",
            autospec=True,
            side_effect=add_config_during_writer,
        ):
            result = server._tool_manager.get_tool("wiki_apply").fn(request={})
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["applied_paths"], ["wiki/questions.md"])

    def test_wiki_apply_preserves_partial_paths_when_config_changes(self) -> None:
        from pkm_framework.mcp_server import build_server

        self.approve_policy()
        config_path = self.root / ".pkm" / "storage.toml"

        def fail_after_partial_apply(_writer, _request):
            config_path.write_text(config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            raise WikiApplyError(
                "config guard detected a race",
                applied_paths=["wiki/questions.md", "wiki/.pending.tmp"],
            )

        with patch.dict(os.environ, {"PKM_MCP_PROFILE": "personal"}):
            # The environment is read while building the server, so rebuild with the profile set.
            server = build_server(str(self.vault))
        with patch(
            "pkm_framework.wiki.WikiWriter.apply",
            autospec=True,
            side_effect=fail_after_partial_apply,
        ):
            result = server._tool_manager.get_tool("wiki_apply").fn(request={})
        self.assertFalse(result["ok"])
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["applied_paths"], ["wiki/questions.md", "wiki/.pending.tmp"])

    def test_policy_review_blocks_hash_approval_for_disallowed_existing_refs(self) -> None:
        self.write_config(config_text(people_wiki_source=False))
        (self.vault / "raw/people/person.md").write_text("A source.\n", encoding="utf-8")
        topic_path = self.vault / "wiki/topics/person.md"
        topic_path.write_text(
            '''---
title: "Person"
created: 2026-11-07
updated: 2026-11-07
source_refs:
  - path: raw/people/person.md
---
# Person
''',
            encoding="utf-8",
        )
        from pkm_framework.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["storage", "policy-review", "--confirm", "--storage", str(self.root)])
        self.assertEqual(result, 2)
        self.assertIn("wiki/topics/person.md", output.getvalue())
        self.assertEqual(load_config(self.root, self.vault).reviewed_policy_hash, "")

        (self.vault / "raw/clips/clip.md").write_text("A replacement source.\n", encoding="utf-8")
        topic_path.write_text(topic_path.read_text(encoding="utf-8").replace(
            "raw/people/person.md", "raw/clips/clip.md"
        ), encoding="utf-8")
        with contextlib.redirect_stdout(output):
            result = main(["storage", "policy-review", "--confirm", "--storage", str(self.root)])
        self.assertEqual(result, 0)
        approved = load_config(self.root, self.vault)
        self.assertTrue(wiki_allowed(approved, approved.profiles["personal"]))

    def test_wiki_source_refs_accept_registered_source_only(self) -> None:
        (self.vault / "raw/people/person.md").write_text("A source.\n", encoding="utf-8")
        topic = '''---
title: "Person"
created: 2026-11-07
updated: 2026-11-07
source_refs:
  - path: raw/people/person.md
---
# Person
'''
        layout = resolve_storage(self.root)
        _validate_topic(self.vault, "wiki/topics/person.md", topic, layout)
        no_people = config_text(people_wiki_source=False)
        self.write_config(no_people)
        restricted_layout = resolve_storage(self.root)
        with self.assertRaisesRegex(ValueError, "registered Wiki source"):
            _validate_topic(self.vault, "wiki/topics/person.md", topic, restricted_layout)

    def test_writer_config_race_rolls_back_prior_replacement(self) -> None:
        (self.vault / "AGENTS.framework.md").write_text("Framework rules.\n", encoding="utf-8")
        (self.vault / "AGENTS.md").write_text("Local rules.\n", encoding="utf-8")
        index_path = self.vault / "wiki/index.md"
        question_path = self.vault / "wiki/questions.md"
        index_original = b"# Index\n"
        question_original = b"# Questions\n"
        index_path.write_bytes(index_original)
        question_path.write_bytes(question_original)
        calls = 0

        def guard() -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ConfigChangedError("Storage config changed during apply.")

        writer = WikiWriter(resolve_storage(self.root), config_guard=guard)
        request = {
            "operation": "lint",
            "writes": [
                {
                    "path": "wiki/questions.md",
                    "content": "# Updated questions\n",
                    "expected_sha256": hashlib.sha256(question_original).hexdigest(),
                },
                {
                    "path": "wiki/index.md",
                    "content": "# Updated index\n",
                    "expected_sha256": hashlib.sha256(index_original).hexdigest(),
                },
            ],
        }
        with patch("pkm_framework.wiki.verify_managed_resources_locked"):
            with self.assertRaises(WikiApplyError) as caught:
                writer.apply(request)
        self.assertFalse(caught.exception.partial)
        self.assertEqual(question_path.read_bytes(), question_original)
        self.assertEqual(index_path.read_bytes(), index_original)

    def test_sync_allowlist_separates_exact_files_from_directory_prefixes(self) -> None:
        policy = storage_sync.sync_policy(resolve_storage(self.root))
        self.assertTrue(policy.allows(".pkm/storage.toml"))
        self.assertTrue(policy.allows("My Vault/.pkm-storage"))
        self.assertTrue(policy.allows("My Vault/raw/people/person.md"))
        self.assertFalse(policy.allows("My Vault/raw/people-archive/person.md"))
        self.assertFalse(policy.allows(".pkm/cache/index.sqlite"))
        self.assertEqual(
            storage_sync.literal_pathspecs(["My Vault/raw/[people]/x.md", ".pkm/storage.toml"]),
            (":(literal).pkm/storage.toml", ":(literal)My Vault/raw/[people]/x.md"),
        )

    def test_sync_detects_exact_config_file_even_when_parent_is_ignored(self) -> None:
        (self.root / ".gitignore").write_text("/.pkm/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "add", ".gitignore"], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=PKM test",
                "-c",
                "user.email=pkm-test@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            cwd=self.root,
            check=True,
        )
        paths = storage_sync.changed_paths(self.root, storage_sync.sync_policy(resolve_storage(self.root)))
        self.assertIn(".pkm/storage.toml", paths)
        self.assertIn("My Vault/.pkm-storage", paths)


if __name__ == "__main__":
    unittest.main()
