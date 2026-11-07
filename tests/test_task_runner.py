"""Regression tests for Framework-managed Storage task arguments."""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TASK_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "pkm_framework"
    / "storage_resources"
    / "mise"
    / "scripts"
    / "task_runner.py"
)
TASK_RUNNER_SPEC = importlib.util.spec_from_file_location("pkm_task_runner", TASK_RUNNER_PATH)
assert TASK_RUNNER_SPEC is not None and TASK_RUNNER_SPEC.loader is not None
task_runner = importlib.util.module_from_spec(TASK_RUNNER_SPEC)
TASK_RUNNER_SPEC.loader.exec_module(task_runner)


class StorageTaskRunnerTests(unittest.TestCase):
    def test_policy_review_forwards_empty_and_confirm_arguments(self) -> None:
        for args, forwarded in (([], []), (["--confirm"], ["--confirm"])):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task_file = root / "mise" / "tasks" / "storage-policy-review.py"
                with (
                    patch.object(task_runner, "_require_storage_ready", return_value=True),
                    patch.object(
                        task_runner.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0),
                    ) as run,
                ):
                    result = task_runner.run_storage_task("storage-policy-review", args, task_file)

                self.assertEqual(result, 0)
                expected = [
                    "uv",
                    "run",
                    "--locked",
                    "--project",
                    str(root / ".pkm"),
                    "pkm",
                    "storage",
                    "policy-review",
                    *forwarded,
                    "--storage",
                    str(root),
                ]
                self.assertEqual(run.call_args.args[0], expected)


if __name__ == "__main__":
    unittest.main()
