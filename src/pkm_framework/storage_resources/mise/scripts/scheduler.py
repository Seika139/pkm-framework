#!/usr/bin/env python3
"""Install, inspect, or remove the current user's hourly PKM sync schedule."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from pkm_framework.paths import VaultPathError, ensure_no_symlink_components


MAC_LABEL = "org.pkm.storage.sync"
SYSTEMD_NAME = "pkm-storage-sync"


class SchedulerError(RuntimeError):
    """A user-level scheduler operation failed."""


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode != 0:
        command = Path(args[0]).name if args else "command"
        raise SchedulerError(f"{command} failed with exit code {result.returncode}.")
    return result


def safe_path(path: Path, *, directory: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    try:
        ensure_no_symlink_components(path, Path(path.anchor))
    except VaultPathError as exc:
        raise SchedulerError("Scheduler paths must not contain symbolic links.") from exc
    if path.exists():
        mode = path.stat().st_mode
        expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if not expected:
            kind = "directory" if directory else "regular file"
            raise SchedulerError(f"Existing scheduler path must be a {kind}.")
    return path


def safe_directory(path: Path, *, create: bool = False) -> Path:
    path = safe_path(path, directory=True)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        path = safe_path(path, directory=True)
    return path


def write_regular_file(path: Path, content: str | bytes) -> None:
    path = safe_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SchedulerError("Scheduler output path must be a regular file.")
        if isinstance(content, bytes):
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = -1
                stream.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def mise_executable() -> str:
    path = shutil.which("mise")
    if path is None:
        raise SchedulerError("Could not find mise on PATH. Run this task from the configured mise environment.")
    return os.path.abspath(path)


def prepare_log_dir(root: Path) -> Path:
    log_dir = root / ".pkm" / "cache" / "logs"
    try:
        ensure_no_symlink_components(log_dir, root)
        log_dir.mkdir(parents=True, exist_ok=True)
        ensure_no_symlink_components(log_dir, root)
    except (OSError, VaultPathError) as exc:
        raise SchedulerError("Sync log directory must be a real directory inside Storage.") from exc
    if not log_dir.is_dir():
        raise SchedulerError("Sync log path must be a directory.")
    return log_dir


def mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def install_macos(root: Path, mise: str, environment_path: str) -> None:
    log_dir = prepare_log_dir(root)
    plist_path = safe_path(mac_plist_path())
    safe_directory(plist_path.parent, create=True)
    temporary = safe_path(plist_path.with_suffix(".plist.tmp"))
    stdout_path = safe_path(log_dir / "scheduler.stdout.log")
    stderr_path = safe_path(log_dir / "scheduler.stderr.log")
    domain = f"gui/{os.getuid()}"
    document = {
        "Label": MAC_LABEL,
        "ProgramArguments": [mise, "run", "sync"],
        "WorkingDirectory": str(root),
        "StartInterval": 3600,
        "EnvironmentVariables": {"PATH": environment_path},
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    run(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"], check=False)
    write_regular_file(temporary, plistlib.dumps(document, sort_keys=True))
    safe_path(plist_path)
    safe_path(temporary)
    temporary.replace(plist_path)
    run(["launchctl", "bootstrap", domain, str(plist_path)])
    print(f"Installed hourly LaunchAgent: {MAC_LABEL}")


def status_macos() -> None:
    domain = f"gui/{os.getuid()}"
    safe_path(mac_plist_path())
    result = run(["launchctl", "print", f"{domain}/{MAC_LABEL}"], check=False)
    if result.returncode == 0:
        print(result.stdout.strip())
        return
    if not mac_plist_path().exists():
        print("PKM scheduled sync is not installed for this user.")
        return
    raise SchedulerError(result.stderr.strip() or "LaunchAgent file exists but is not loaded.")


def uninstall_macos() -> None:
    domain = f"gui/{os.getuid()}"
    plist_path = safe_path(mac_plist_path())
    result = run(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"], check=False)
    if result.returncode != 0 and plist_path.exists():
        loaded = run(["launchctl", "print", f"{domain}/{MAC_LABEL}"], check=False)
        if loaded.returncode == 0:
            raise SchedulerError(result.stderr.strip() or "Could not unload the LaunchAgent.")
    plist_path.unlink(missing_ok=True)
    print(f"Removed LaunchAgent: {MAC_LABEL}")


def systemd_user_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    if not config_home.is_absolute():
        raise SchedulerError("XDG_CONFIG_HOME must be an absolute path.")
    return config_home / "systemd" / "user"


def systemd_quote(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise SchedulerError("systemd unit values must not contain line breaks.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def systemd_service(root: Path, mise: str, environment_path: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Synchronize PKM Storage repository",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={systemd_quote(str(root))}",
            f"Environment={systemd_quote(f'PATH={environment_path}')}",
            f"ExecStart={systemd_quote(mise)} run sync",
            "",
        ]
    )


def systemd_timer() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Hourly synchronization for PKM Storage",
            "",
            "[Timer]",
            "OnCalendar=hourly",
            "Persistent=true",
            f"Unit={SYSTEMD_NAME}.service",
            "AccuracySec=1min",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def install_linux(root: Path, mise: str, environment_path: str) -> None:
    unit_dir = safe_directory(systemd_user_dir(), create=True)
    service = safe_path(unit_dir / f"{SYSTEMD_NAME}.service")
    timer = safe_path(unit_dir / f"{SYSTEMD_NAME}.timer")
    write_regular_file(service, systemd_service(root, mise, environment_path))
    write_regular_file(timer, systemd_timer())
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_NAME}.timer"])
    print(f"Installed hourly systemd user timer: {SYSTEMD_NAME}.timer")


def status_linux() -> None:
    unit_dir = safe_directory(systemd_user_dir())
    timer = safe_path(unit_dir / f"{SYSTEMD_NAME}.timer")
    if not timer.exists():
        print("PKM scheduled sync is not installed for this user.")
        return
    result = run(["systemctl", "--user", "list-timers", f"{SYSTEMD_NAME}.timer", "--all", "--no-pager"], check=False)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise SchedulerError("Could not query the systemd user timer.")


def uninstall_linux() -> None:
    unit_dir = safe_directory(systemd_user_dir())
    service = safe_path(unit_dir / f"{SYSTEMD_NAME}.service")
    timer = safe_path(unit_dir / f"{SYSTEMD_NAME}.timer")
    if not service.exists() and not timer.exists():
        print("PKM scheduled sync is not installed for this user.")
        return
    run(["systemctl", "--user", "show-environment"])
    run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_NAME}.timer"])
    (unit_dir / f"{SYSTEMD_NAME}.timer").unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    print(f"Removed systemd user timer: {SYSTEMD_NAME}.timer")


def windows_script(root: Path, action: str, mise: str | None = None) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise SchedulerError("PowerShell is required to manage Windows Task Scheduler.")
    script = Path(__file__).with_name("scheduler-windows.ps1")
    args = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action]
    if action == "install":
        if mise is None:
            raise SchedulerError("Could not resolve mise for the Windows scheduled task.")
        args.extend(
            [
                "-StorageRoot",
                str(root),
                "-MisePath",
                mise,
                "-PowerShellPath",
                powershell,
            ]
        )
    result = run(args)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def dispatch(action: str, root: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        if action == "install":
            install_macos(root, mise_executable(), os.environ.get("PATH", ""))
        elif action == "status":
            status_macos()
        else:
            uninstall_macos()
    elif system == "Windows":
        windows_script(root, action, mise_executable() if action == "install" else None)
    elif system == "Linux":
        if action == "install":
            install_linux(root, mise_executable(), os.environ.get("PATH", ""))
        elif action == "status":
            status_linux()
        else:
            uninstall_linux()
    else:
        raise SchedulerError(f"Unsupported operating system: {system}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--storage", required=True, type=Path, help="Storage repository root")
    args = parser.parse_args()
    try:
        root = args.storage.expanduser().absolute()
        if not root.is_dir() or root.is_symlink():
            raise SchedulerError(f"Storage root must be a real directory: {root}")
        dispatch(args.action, root)
        return 0
    except SchedulerError as exc:
        print(f"Schedule {args.action} failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Schedule {args.action} failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
