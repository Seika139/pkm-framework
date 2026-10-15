import unittest
from unittest.mock import patch

from pkm_framework.managed_resources import (
    ManagedResourceError,
    _package_resources,
    _expected_state,
    _matches_expected,
    _safe_relative_path,
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
            "79729a8de9ea32e3fa3f007a531b4b0ba134c746f9fecdc31a13c924352ed19e",
        )

    def test_framework_task_resources_preserve_setup_exclusion(self) -> None:
        self.assertEqual(_safe_relative_path("mise/tasks/index.py").as_posix(), "mise/tasks/index.py")
        with self.assertRaises(ManagedResourceError):
            _safe_relative_path("mise/tasks/setup.sh")
        with self.assertRaises(ManagedResourceError):
            _safe_relative_path("mise/tasks/setup.py")

    def test_windows_journal_mode_uses_readonly_state(self) -> None:
        expected = _expected_state((b"resource", 0o755))
        with patch("pkm_framework.managed_resources.os.name", "nt"):
            self.assertTrue(_matches_expected((b"resource", 0o666), expected, "resource"))
            self.assertFalse(_matches_expected((b"resource", 0o444), expected, "resource"))


if __name__ == "__main__":
    unittest.main()
