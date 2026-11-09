import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pkm_framework.managed_resources import (
    ManagedResourceError,
    _package_resources,
    _expected_state,
    _matches_expected,
    _safe_relative_path,
    install_managed_resources,
)


class ManagedResourceJournalTests(unittest.TestCase):
    def test_windows_scheduler_keeps_pre_migration_legacy_hash(self) -> None:
        scheduler = _package_resources()["mise/scripts/scheduler-windows.ps1"]
        self.assertEqual(
            scheduler["sha256"],
            "ecb0e4ecc765c2af81b206227b1301caca6cf630d305650154ca154fe97a617b",
        )
        self.assertEqual(
            scheduler["legacy_sha256"],
            "eead5e6388c82cf5d5b0eb6547aa98b8b07766d9b25ab45e9fa4e3230a05b467",
        )

    def test_scheduler_accepts_previous_release_hash(self) -> None:
        scheduler = _package_resources()["mise/scripts/scheduler.py"]
        self.assertEqual(
            scheduler["legacy_sha256"],
            "cbb98c7544d35db493142b2a7777df5547056c82887b787ac50c91fdcd9be053",
        )

    def test_framework_task_resources_preserve_setup_exclusion(self) -> None:
        self.assertEqual(
            _safe_relative_path("mise/tasks/index.py").as_posix(), "mise/tasks/index.py"
        )
        with self.assertRaises(ManagedResourceError):
            _safe_relative_path("mise/tasks/setup.sh")
        with self.assertRaises(ManagedResourceError):
            _safe_relative_path("mise/tasks/setup.py")

    @unittest.skipIf(
        os.name == "nt", "Windows does not expose POSIX executable mode bits."
    )
    def test_fresh_install_marks_python_file_tasks_executable(self) -> None:
        task_paths = [
            path
            for path in _package_resources()
            if path.startswith("mise/tasks/") and path.endswith(".py")
        ]
        self.assertTrue(task_paths)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".pkm").mkdir()
            (root / ".pkm" / "setup-incomplete").write_text("setup\n", encoding="ascii")
            install_managed_resources(root)

            for relative in task_paths:
                with self.subTest(path=relative):
                    mode = stat.S_IMODE((root / relative).stat().st_mode)
                    self.assertEqual(mode, 0o755)

    def test_windows_journal_mode_uses_readonly_state(self) -> None:
        expected = _expected_state((b"resource", 0o755))
        with patch("pkm_framework.managed_resources.os.name", "nt"):
            self.assertTrue(
                _matches_expected((b"resource", 0o666), expected, "resource")
            )
            self.assertFalse(
                _matches_expected((b"resource", 0o444), expected, "resource")
            )


if __name__ == "__main__":
    unittest.main()
