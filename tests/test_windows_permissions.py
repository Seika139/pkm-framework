"""Cross-platform tests for Storage owner checks and atomic replacement."""

from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pkm_framework.config as storage_config
import pkm_framework.fileops as fileops
import pkm_framework.windows_security as windows_security
from pkm_framework.config import StorageConfigError


class WindowsPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_windows_owner_check_uses_sid_matcher_and_fails_closed(self) -> None:
        with patch.object(storage_config.os, "name", "nt"):
            with patch.object(storage_config, "_windows_storage_owner_matches", return_value=True) as match:
                storage_config.require_owner(self.root)
            match.assert_called_once_with(self.root)

            with patch.object(storage_config, "_windows_storage_owner_matches", return_value=False):
                with self.assertRaisesRegex(StorageConfigError, "Storage owner"):
                    storage_config.require_owner(self.root)

            with patch.object(
                storage_config,
                "_windows_storage_owner_matches",
                side_effect=OSError("SID lookup failed"),
            ):
                with self.assertRaisesRegex(StorageConfigError, "Cannot verify Storage ownership"):
                    storage_config.require_owner(self.root)

    def test_windows_owner_check_uses_exported_equal_sid(self) -> None:
        from ctypes import wintypes

        class FakeFunction:
            def __init__(self, result=None):
                self.argtypes = None
                self.restype = None
                self.calls = []
                self.result = result

            def __call__(self, *args):
                self.calls.append(args)
                if callable(self.result):
                    return self.result(*args)
                return self.result

        def get_named_security_info(*args):
            owner_sid_output = ctypes.cast(args[3], ctypes.POINTER(wintypes.LPVOID))
            owner_sid_output.contents.value = 1234
            return 0

        advapi32 = SimpleNamespace(
            GetNamedSecurityInfoW=FakeFunction(get_named_security_info),
            EqualSid=FakeFunction(1),
        )
        kernel32 = SimpleNamespace(LocalFree=FakeFunction())
        with (
            patch.object(ctypes, "WinDLL", side_effect=[advapi32, kernel32], create=True),
            patch.object(
                windows_security,
                "_current_user_sid",
                return_value=(object(), wintypes.LPVOID(5678)),
            ),
        ):
            self.assertTrue(windows_security.storage_owner_matches_current_user(self.root))

        self.assertEqual(len(advapi32.EqualSid.calls), 1)

    def test_windows_atomic_replace_keeps_acl_path_and_skips_fchmod(self) -> None:
        source = self.root / "temporary"
        destination = self.root / "target"
        calls: list[str] = []
        temporary_path = source

        def create_private_temp(*_args) -> tuple[int, str]:
            descriptor = os.open(temporary_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            calls.append("create")
            return descriptor, str(temporary_path)

        def copy_dacl(_target: Path, temporary: Path) -> None:
            self.assertEqual(temporary.read_text(encoding="utf-8"), "payload")
            calls.append("dacl")

        with (
            patch.object(fileops.os, "name", "nt"),
            patch.object(
                fileops,
                "_windows_create_private_temp_file",
                side_effect=create_private_temp,
            ) as create_temp,
            patch.object(
                fileops,
                "_windows_copy_file_dacl",
                side_effect=copy_dacl,
            ) as copy_dacl,
            patch.object(
                fileops,
                "_windows_replace_file",
                side_effect=lambda *_args: calls.append("replace"),
            ) as replace_file,
            patch.object(fileops.os, "fchmod", create=True) as fchmod,
        ):
            descriptor, temporary_name = fileops.create_atomic_replacement_temp(
                self.root, prefix="temp-", suffix=".tmp"
            )
            self.assertEqual(temporary_name, str(temporary_path))
            fileops.set_atomic_replacement_mode(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("payload")
            calls.append("write")
            fileops.preserve_atomic_replacement_acl(destination, temporary_path)
            fileops.atomic_replace_file(temporary_path, destination)

        fchmod.assert_not_called()
        create_temp.assert_called_once_with(self.root, "temp-", ".tmp")
        self.assertNotEqual(descriptor, -1)
        copy_dacl.assert_called_once_with(destination, source)
        replace_file.assert_called_once_with(source, destination)
        self.assertEqual(calls, ["create", "write", "dacl", "replace"])
        source.unlink(missing_ok=True)

    def test_atomic_replace_replaces_existing_file(self) -> None:
        destination = self.root / "target"
        destination.write_text("original", encoding="utf-8")
        descriptor, temporary_name = fileops.create_atomic_replacement_temp(
            self.root, prefix="temporary-", suffix=".tmp"
        )
        source = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("replacement")
            stream.flush()
        fileops.preserve_atomic_replacement_acl(destination, source)

        fileops.atomic_replace_file(source, destination)

        self.assertEqual(destination.read_text(encoding="utf-8"), "replacement")
        self.assertFalse(source.exists())

    def test_windows_replace_restores_backup_after_partial_failure(self) -> None:
        source = self.root / "temporary"
        destination = self.root / "target"
        source.write_text("replacement", encoding="utf-8")
        destination.write_text("original", encoding="utf-8")
        backup_paths: list[Path] = []

        def fail_after_moving_original(target: Path, replacement: Path, backup: Path) -> None:
            backup_paths.append(backup)
            target.replace(backup)
            raise OSError(1176, "Replacement could not be moved.")

        with patch.object(windows_security, "_call_replace_file", side_effect=fail_after_moving_original):
            with self.assertRaises(OSError):
                windows_security.replace_file(source, destination)

        self.assertEqual(destination.read_text(encoding="utf-8"), "original")
        self.assertTrue(source.exists())
        self.assertFalse(backup_paths[0].exists())

    def test_windows_replace_keeps_backup_if_recovery_fails(self) -> None:
        source = self.root / "temporary"
        destination = self.root / "target"
        source.write_text("replacement", encoding="utf-8")
        destination.write_text("original", encoding="utf-8")
        backup_paths: list[Path] = []

        def fail_after_moving_original(target: Path, replacement: Path, backup: Path) -> None:
            backup_paths.append(backup)
            target.replace(backup)
            raise OSError(1176, "Replacement could not be moved.")

        with (
            patch.object(windows_security, "_call_replace_file", side_effect=fail_after_moving_original),
            patch.object(windows_security, "_restore_backup", side_effect=OSError("restore failed")),
        ):
            with self.assertRaisesRegex(OSError, "restore the original file from"):
                windows_security.replace_file(source, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(backup_paths[0].read_text(encoding="utf-8"), "original")
        self.assertTrue(source.exists())

    def test_windows_replace_preserves_source_if_destination_and_backup_are_missing(self) -> None:
        source = self.root / "temporary"
        destination = self.root / "target"
        source.write_text("replacement", encoding="utf-8")
        destination.write_text("original", encoding="utf-8")

        def fail_without_moving_original(target: Path, _replacement: Path, _backup: Path) -> None:
            target.unlink()
            raise OSError(1176, "Replacement could not be moved.")

        with patch.object(windows_security, "_call_replace_file", side_effect=fail_without_moving_original):
            with self.assertRaises(windows_security.WindowsReplaceError) as caught:
                windows_security.replace_file(source, destination)

        self.assertTrue(caught.exception.preserve_source)
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "replacement")

    def test_windows_replace_removes_backup_after_success(self) -> None:
        source = self.root / "temporary"
        destination = self.root / "target"
        source.write_text("replacement", encoding="utf-8")
        destination.write_text("original", encoding="utf-8")
        backup_paths: list[Path] = []

        def succeed(target: Path, replacement: Path, backup: Path) -> None:
            backup_paths.append(backup)
            target.replace(backup)
            replacement.replace(target)

        with patch.object(windows_security, "_call_replace_file", side_effect=succeed):
            windows_security.replace_file(source, destination)

        self.assertEqual(destination.read_text(encoding="utf-8"), "replacement")
        self.assertFalse(source.exists())
        self.assertFalse(backup_paths[0].exists())

    @unittest.skipUnless(os.name == "nt", "Windows SID APIs are only available on Windows.")
    def test_windows_storage_directory_owner_matches_current_user(self) -> None:
        self.assertIs(type(storage_config._windows_storage_owner_matches(self.root)), bool)


if __name__ == "__main__":
    unittest.main()
