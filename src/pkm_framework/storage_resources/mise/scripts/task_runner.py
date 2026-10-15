"""Shared entry point for portable Framework-managed Storage tasks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


USAGE = {
    "read": "Usage: mise run read -- <vault-relative-path>",
    "search": "Usage: mise run search -- <query>",
}


def _uv_command(root: Path, *args: str) -> list[str]:
    return ["uv", "run", "--locked", "--project", str(root / ".pkm"), *args]


def _environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(root / ".pkm" / "runtime")
    return environment


def _require_storage_ready(root: Path, environment: dict[str, str]) -> bool:
    marker = root / ".pkm" / "setup-incomplete"
    if marker.exists() or marker.is_symlink():
        print("Storage setup is incomplete. Run mise run setup to repair it.", file=sys.stderr)
        return False

    try:
        result = subprocess.run(
            _uv_command(root, "pkm", "storage", "verify", "--storage", str(root)),
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        print(f"Could not run uv to verify Storage readiness: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("Framework-managed files are not ready. Run mise run setup to repair them.", file=sys.stderr)
        return False
    return True


def run_storage_task(task: str, args: Sequence[str], task_file: Path) -> int:
    """Run one managed Storage task after checking setup readiness."""

    if task == "read" and len(args) != 1:
        print(USAGE[task], file=sys.stderr)
        return 2
    query = " ".join(args) if task == "search" else ""
    if task == "search" and not query:
        print(USAGE[task], file=sys.stderr)
        return 2

    root = task_file.resolve().parents[2]
    environment = _environment(root)
    if not _require_storage_ready(root, environment):
        return 2

    if task == "index":
        command = _uv_command(root, "pkm", "index", "--storage", str(root))
    elif task == "mcp":
        command = _uv_command(root, "pkm", "mcp", "--storage", str(root))
    elif task == "read":
        command = _uv_command(root, "pkm", "read", args[0], "--storage", str(root))
    elif task == "search":
        command = _uv_command(root, "pkm", "search", query, "--storage", str(root))
    elif task == "skills-install":
        command = _uv_command(
            root,
            "pkm",
            "skills",
            "install",
            "--target",
            str(root / ".agents" / "skills"),
        )
    elif task == "sync":
        command = _uv_command(
            root,
            "python",
            str(root / "mise" / "scripts" / "sync.py"),
            "--storage",
            str(root),
        )
    elif task in {"schedule-install", "schedule-status", "schedule-uninstall"}:
        action = task.removeprefix("schedule-")
        command = _uv_command(
            root,
            "python",
            str(root / "mise" / "scripts" / "scheduler.py"),
            action,
            "--storage",
            str(root),
        )
    else:
        raise ValueError(f"Unknown Framework Storage task: {task}")

    try:
        return subprocess.run(command, cwd=root, env=environment, check=False).returncode
    except OSError as exc:
        print(f"Could not run the Framework Storage task: {exc}", file=sys.stderr)
        return 2
