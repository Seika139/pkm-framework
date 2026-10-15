"""Regression test for UnicodeEncodeError when stdout uses a non-UTF-8 locale encoding."""

import io
import sys
import tempfile
import unittest
from pathlib import Path

from pkm_framework.storage import MARKER_NAME
from pkm_framework import cli


class CliCp932StdoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / MARKER_NAME).write_text("", encoding="utf-8")
        # U+2014 (EM DASH) cannot be encoded in cp932; a real-world AGENTS.framework.md
        # already contains this character, so wiki context deterministically hits it.
        (self.vault / "AGENTS.framework.md").write_text(
            "Framework rules — shared.\n", encoding="utf-8"
        )
        (self.vault / "AGENTS.md").write_text("Local rules.\n", encoding="utf-8")

        self._original_stdout = sys.stdout
        self.buffer = io.BytesIO()
        self.cp932_stdout = io.TextIOWrapper(self.buffer, encoding="cp932", newline="")
        sys.stdout = self.cp932_stdout
        self.addCleanup(self._restore_stdout)

    def _restore_stdout(self) -> None:
        sys.stdout = self._original_stdout

    def test_wiki_context_does_not_raise_unicode_encode_error_on_cp932_stdout(self) -> None:
        exit_code = cli.main(["wiki", "context", "--storage", str(self.vault)])
        self.cp932_stdout.flush()

        self.assertEqual(exit_code, 0)
        written = self.buffer.getvalue().decode("utf-8")
        self.assertIn("—", written)


class CliStderrErrorsPreservedTests(unittest.TestCase):
    """reconfigure(encoding="utf-8") must not silently reset stderr's errors handler to "strict"."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / MARKER_NAME).write_text("", encoding="utf-8")
        (self.vault / "AGENTS.framework.md").write_text("Framework rules.\n", encoding="utf-8")
        (self.vault / "AGENTS.md").write_text("Local rules.\n", encoding="utf-8")

        self._original_stderr = sys.stderr
        self.buffer = io.BytesIO()
        self.backslashreplace_stderr = io.TextIOWrapper(
            self.buffer, encoding="utf-8", errors="backslashreplace", newline=""
        )
        sys.stderr = self.backslashreplace_stderr
        self.addCleanup(self._restore_stderr)

    def _restore_stderr(self) -> None:
        sys.stderr = self._original_stderr

    def test_reconfigure_keeps_backslashreplace_errors_handler(self) -> None:
        cli.main(["read", "raw/clips/does-not-exist.md", "--storage", str(self.vault)])

        self.assertEqual(sys.stderr.errors, "backslashreplace")

    def test_missing_path_reports_error_and_exit_code_without_crashing(self) -> None:
        exit_code = cli.main(["read", "raw/clips/does-not-exist.md", "--storage", str(self.vault)])
        self.backslashreplace_stderr.flush()

        self.assertEqual(exit_code, 2)
        written = self.buffer.getvalue().decode("utf-8")
        self.assertIn("pkm:", written)


if __name__ == "__main__":
    unittest.main()
