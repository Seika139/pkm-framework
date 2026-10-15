#!/usr/bin/env python3
"""Install, inspect, or remove the current user's hourly PKM sync schedule."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import re
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


def safe_path(path: Path, *, directory: bool = False, anchor: Path | None = None) -> Path:
    path = Path(os.path.abspath(path))
    anchor = Path(os.path.abspath(anchor)) if anchor is not None else path.parent
    try:
        ensure_no_symlink_components(path, anchor)
    except VaultPathError as exc:
        raise SchedulerError("Scheduler paths must not contain symbolic links.") from exc
    if path.exists():
        mode = path.stat().st_mode
        expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if not expected:
            kind = "directory" if directory else "regular file"
            raise SchedulerError(f"Existing scheduler path must be a {kind}.")
    return path


def safe_directory(path: Path, *, create: bool = False, anchor: Path | None = None) -> Path:
    path = Path(os.path.abspath(path))
    anchor = Path(os.path.abspath(anchor)) if anchor is not None else path.parent
    path = safe_path(path, directory=True, anchor=anchor)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        path = safe_path(path, directory=True, anchor=anchor)
    return path


def write_regular_file(path: Path, content: str | bytes, *, anchor: Path | None = None) -> None:
    path = safe_path(path, anchor=anchor)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
    ):
        raise SchedulerError("Scheduler output path must be a single-link regular file.")

    flags = os.O_WRONLY | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if before is None:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        safe_path(path, anchor=anchor)
        opened = os.fstat(descriptor)
        current = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or identity != (current.st_dev, current.st_ino)
            or (before is not None and identity != (before.st_dev, before.st_ino))
        ):
            raise SchedulerError("Scheduler output path changed or is not a single-link regular file.")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
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


def ensure_single_link_regular_file(path: Path, *, anchor: Path) -> Path:
    path = safe_path(path, anchor=anchor)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise SchedulerError("Scheduler log path must be a single-link regular file.")

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if before is None:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        safe_path(path, anchor=anchor)
        opened = os.fstat(descriptor)
        current = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or identity != (current.st_dev, current.st_ino)
            or (before is not None and identity != (before.st_dev, before.st_ino))
        ):
            raise SchedulerError("Scheduler log path changed or is not a single-link regular file.")
    finally:
        os.close(descriptor)
    return path


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


def validate_macos_job(document: object, root: Path) -> None:
    expected_keys = {
        "Label",
        "ProgramArguments",
        "WorkingDirectory",
        "StartInterval",
        "EnvironmentVariables",
        "StandardOutPath",
        "StandardErrorPath",
    }
    arguments = document.get("ProgramArguments") if isinstance(document, dict) else None
    environment = document.get("EnvironmentVariables") if isinstance(document, dict) else None
    stdout_path = root / ".pkm" / "cache" / "logs" / "scheduler.stdout.log"
    stderr_path = root / ".pkm" / "cache" / "logs" / "scheduler.stderr.log"
    if (
        not isinstance(document, dict)
        or set(document) != expected_keys
        or document.get("Label") != MAC_LABEL
        or document.get("WorkingDirectory") != str(root)
        or not isinstance(arguments, list)
        or len(arguments) != 3
        or not isinstance(arguments[0], str)
        or not Path(arguments[0]).is_absolute()
        or Path(arguments[0]).name != "mise"
        or arguments[1:] != ["run", "sync"]
        or type(document.get("StartInterval")) is not int
        or document.get("StartInterval") != 3600
        or not isinstance(environment, dict)
        or set(environment) != {"PATH"}
        or not isinstance(environment.get("PATH"), str)
        or "\n" in environment["PATH"]
        or "\r" in environment["PATH"]
        or document.get("StandardOutPath") != str(stdout_path)
        or document.get("StandardErrorPath") != str(stderr_path)
    ):
        raise SchedulerError("Existing LaunchAgent does not match this Storage; refusing to modify it.")


def read_macos_job(root: Path) -> dict[str, object] | None:
    plist_path = safe_path(mac_plist_path(), anchor=Path.home())
    if not plist_path.exists():
        return None
    try:
        document = plistlib.loads(plist_path.read_bytes())
    except Exception as exc:
        raise SchedulerError("Existing LaunchAgent plist cannot be safely read.") from exc
    validate_macos_job(document, root)
    return document


def _launchctl_service_missing(result: subprocess.CompletedProcess[str], service: str) -> bool:
    if result.returncode == 0:
        return False
    output = (result.stderr or result.stdout).strip()
    service_pattern = rf"(?:{re.escape(service)}|{re.escape(MAC_LABEL)})"
    return re.fullmatch(
        rf'Could not find service ["\']?{service_pattern}["\']? in domain for .+',
        output,
        flags=re.IGNORECASE,
    ) is not None


def _query_macos_job(domain: str) -> subprocess.CompletedProcess[str] | None:
    service = f"{domain}/{MAC_LABEL}"
    result = run(["launchctl", "print", service], check=False)
    if result.returncode == 0:
        return result
    if _launchctl_service_missing(result, service):
        return None
    raise SchedulerError("Could not confidently determine whether the LaunchAgent is loaded.")


def _launchctl_field(output: str, name: str) -> str:
    pattern = re.compile(rf"^\t{re.escape(name)} = ([^\r\n]*)$", re.MULTILINE)
    matches = pattern.findall(output)
    if len(matches) != 1:
        raise SchedulerError("Loaded LaunchAgent output cannot be safely matched to its plist.")
    return matches[0]


def _launchctl_block(output: str, name: str) -> list[str]:
    lines = output.splitlines()
    starts = [index for index, line in enumerate(lines) if line == f"\t{name} = {{"]
    if len(starts) != 1:
        raise SchedulerError("Loaded LaunchAgent output cannot be safely matched to its plist.")
    values: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line == "\t}":
            return values
        if not line.startswith("\t\t") or line == "\t\t":
            raise SchedulerError("Loaded LaunchAgent output cannot be safely matched to its plist.")
        values.append(line[2:])
    raise SchedulerError("Loaded LaunchAgent output cannot be safely matched to its plist.")


def validate_loaded_macos_job(
    output: str,
    domain: str,
    plist_path: Path,
    document: dict[str, object],
) -> None:
    first_line = output.splitlines()[0] if output.splitlines() else ""
    if first_line != f"{domain}/{MAC_LABEL} = {{":
        raise SchedulerError("Loaded LaunchAgent output cannot be safely matched to its plist.")
    arguments = document["ProgramArguments"]
    environment = document["EnvironmentVariables"]
    assert isinstance(arguments, list) and isinstance(environment, dict)
    expected_fields = {
        "path": str(plist_path),
        "type": "LaunchAgent",
        "program": arguments[0],
        "working directory": document["WorkingDirectory"],
        "stdout path": document["StandardOutPath"],
        "stderr path": document["StandardErrorPath"],
        "run interval": f'{document["StartInterval"]} seconds',
    }
    if any(_launchctl_field(output, name) != value for name, value in expected_fields.items()):
        raise SchedulerError("Loaded LaunchAgent does not match its on-disk plist; refusing to modify it.")

    properties = {value.strip().casefold() for value in _launchctl_field(output, "properties").split("|")}
    if properties != {"inferred program"}:
        raise SchedulerError("Loaded LaunchAgent properties do not match its on-disk plist; refusing to modify it.")

    if _launchctl_block(output, "arguments") != arguments:
        raise SchedulerError("Loaded LaunchAgent arguments do not match its on-disk plist; refusing to modify it.")

    environment_values: dict[str, str] = {}
    for line in _launchctl_block(output, "environment"):
        key, separator, value = line.partition(" => ")
        if not separator or not key or key in environment_values:
            raise SchedulerError("Loaded LaunchAgent environment cannot be safely matched to its plist.")
        environment_values[key] = value
    # launchd adds these values to the environment it reports for a job.
    allowed_launchd_values = {"XPC_SERVICE_NAME", "OSLogRateLimit"}
    expected_path = environment.get("PATH")
    if (
        environment_values.get("PATH") != expected_path
        or set(environment_values) - {"PATH"} - allowed_launchd_values
    ):
        raise SchedulerError("Loaded LaunchAgent environment does not match its on-disk plist; refusing to modify it.")


def install_macos(root: Path, mise: str, environment_path: str) -> None:
    plist_path = mac_plist_path()
    existing = read_macos_job(root)
    domain = f"gui/{os.getuid()}"
    loaded = _query_macos_job(domain)
    if loaded is not None and existing is None:
        raise SchedulerError("A loaded LaunchAgent has no attributable plist; refusing to replace it.")
    if loaded is not None and existing is not None:
        validate_loaded_macos_job(loaded.stdout, domain, plist_path, existing)

    log_dir = prepare_log_dir(root)
    safe_directory(plist_path.parent, create=True, anchor=Path.home())
    plist_path = safe_path(plist_path, anchor=plist_path.parent)
    temporary = safe_path(plist_path.with_suffix(".plist.tmp"), anchor=plist_path.parent)
    stdout_path = ensure_single_link_regular_file(log_dir / "scheduler.stdout.log", anchor=root)
    stderr_path = ensure_single_link_regular_file(log_dir / "scheduler.stderr.log", anchor=root)
    document = {
        "Label": MAC_LABEL,
        "ProgramArguments": [mise, "run", "sync"],
        "WorkingDirectory": str(root),
        "StartInterval": 3600,
        "EnvironmentVariables": {"PATH": environment_path},
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    if loaded is not None:
        run(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"])
    write_regular_file(temporary, plistlib.dumps(document, sort_keys=True), anchor=plist_path.parent)
    safe_path(plist_path, anchor=plist_path.parent)
    safe_path(temporary, anchor=plist_path.parent)
    temporary.replace(plist_path)
    run(["launchctl", "bootstrap", domain, str(plist_path)])
    print(f"Installed hourly LaunchAgent: {MAC_LABEL}")


def status_macos(root: Path) -> None:
    domain = f"gui/{os.getuid()}"
    document = read_macos_job(root)
    loaded = _query_macos_job(domain)
    if loaded is None and document is None:
        print("PKM scheduled sync is not installed for this user.")
        return
    if loaded is None:
        raise SchedulerError("LaunchAgent file exists but is not loaded.")
    if document is None:
        raise SchedulerError("A loaded LaunchAgent has no attributable plist; refusing to inspect it.")
    validate_loaded_macos_job(loaded.stdout, domain, mac_plist_path(), document)
    if loaded.returncode == 0:
        print(loaded.stdout.strip())
        return


def uninstall_macos(root: Path) -> None:
    domain = f"gui/{os.getuid()}"
    document = read_macos_job(root)
    loaded = _query_macos_job(domain)
    if document is None and loaded is None:
        print("PKM scheduled sync is not installed for this user.")
        return
    if document is None:
        raise SchedulerError("A loaded LaunchAgent has no attributable plist; refusing to remove it.")
    plist_path = safe_path(mac_plist_path(), anchor=Path.home())
    if loaded is not None:
        validate_loaded_macos_job(loaded.stdout, domain, plist_path, document)
        run(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"])
    plist_path.unlink(missing_ok=True)
    print(f"Removed LaunchAgent: {MAC_LABEL}")


def systemd_user_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    if not config_home.is_absolute():
        raise SchedulerError("XDG_CONFIG_HOME must be an absolute path.")
    return config_home / "systemd" / "user"


def systemd_config_anchor() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    if not config_home.is_absolute():
        raise SchedulerError("XDG_CONFIG_HOME must be an absolute path.")
    if config_home.exists():
        return config_home
    current = config_home.parent
    while not current.exists() and current != Path(current.anchor):
        current = current.parent
    return current


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


def read_systemd_unit(path: Path, unit_dir: Path) -> str | None:
    path = safe_path(path, anchor=unit_dir)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SchedulerError(f"Existing systemd unit cannot be safely read: {path.name}") from exc


def validate_systemd_service(content: str, root: Path) -> None:
    lines = content.splitlines()
    if (
        len(lines) != 8
        or lines[0] != "[Unit]"
        or lines[1] != "Description=Synchronize PKM Storage repository"
        or lines[2] != ""
        or lines[3] != "[Service]"
        or lines[4] != "Type=oneshot"
        or lines[5] != f"WorkingDirectory={systemd_quote(str(root))}"
        or not lines[6].startswith("Environment=")
        or not lines[7].startswith("ExecStart=")
    ):
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")

    environment = _systemd_unquote(lines[6][len("Environment=") :])
    exec_start = lines[7][len("ExecStart=") :]
    if not environment.startswith("PATH=") or not exec_start.endswith(" run sync"):
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
    mise = _systemd_unquote(exec_start[: -len(" run sync")])
    if not Path(mise).is_absolute() or Path(mise).name != "mise":
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
    if systemd_service(root, mise, environment[len("PATH=") :]) != content:
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")


def _systemd_unquote(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
    encoded = value[1:-1]
    decoded: list[str] = []
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == "\\":
            index += 1
            if index >= len(encoded) or encoded[index] not in {'\\', '"'}:
                raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
            decoded.append(encoded[index])
        elif char == "%":
            if index + 1 >= len(encoded) or encoded[index + 1] != "%":
                raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
            decoded.append("%")
            index += 1
        else:
            decoded.append(char)
        index += 1
    result = "".join(decoded)
    if systemd_quote(result) != value:
        raise SchedulerError("Existing systemd service does not match this Storage; refusing to modify it.")
    return result


def validate_linux_job(root: Path, service: str | None, timer: str | None) -> bool:
    if service is None and timer is None:
        return False
    if service is None or timer is None:
        raise SchedulerError("Existing systemd timer/service pair is incomplete; refusing to modify it.")
    validate_systemd_service(service, root)
    if timer != systemd_timer():
        raise SchedulerError("Existing systemd timer does not match the Framework schedule; refusing to modify it.")
    return True


def install_linux(root: Path, mise: str, environment_path: str) -> None:
    unit_dir = safe_directory(systemd_user_dir(), create=True, anchor=systemd_config_anchor())
    service = safe_path(unit_dir / f"{SYSTEMD_NAME}.service", anchor=unit_dir)
    timer = safe_path(unit_dir / f"{SYSTEMD_NAME}.timer", anchor=unit_dir)
    existing_service = read_systemd_unit(service, unit_dir)
    existing_timer = read_systemd_unit(timer, unit_dir)
    validate_linux_job(root, existing_service, existing_timer)
    write_regular_file(service, systemd_service(root, mise, environment_path), anchor=unit_dir)
    write_regular_file(timer, systemd_timer(), anchor=unit_dir)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_NAME}.timer"])
    print(f"Installed hourly systemd user timer: {SYSTEMD_NAME}.timer")


def status_linux(root: Path) -> None:
    unit_dir = safe_directory(
        systemd_user_dir(), create=False, anchor=systemd_config_anchor()
    )
    if not unit_dir.exists():
        print("PKM scheduled sync is not installed for this user.")
        return
    service = read_systemd_unit(unit_dir / f"{SYSTEMD_NAME}.service", unit_dir)
    timer = read_systemd_unit(unit_dir / f"{SYSTEMD_NAME}.timer", unit_dir)
    if not validate_linux_job(root, service, timer):
        print("PKM scheduled sync is not installed for this user.")
        return
    result = run(["systemctl", "--user", "list-timers", f"{SYSTEMD_NAME}.timer", "--all", "--no-pager"], check=False)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise SchedulerError("Could not query the systemd user timer.")


def uninstall_linux(root: Path) -> None:
    unit_dir = safe_directory(
        systemd_user_dir(), create=False, anchor=systemd_config_anchor()
    )
    if not unit_dir.exists():
        print("PKM scheduled sync is not installed for this user.")
        return
    service_path = safe_path(unit_dir / f"{SYSTEMD_NAME}.service", anchor=unit_dir)
    timer_path = safe_path(unit_dir / f"{SYSTEMD_NAME}.timer", anchor=unit_dir)
    service = read_systemd_unit(service_path, unit_dir)
    timer = read_systemd_unit(timer_path, unit_dir)
    if not validate_linux_job(root, service, timer):
        print("PKM scheduled sync is not installed for this user.")
        return
    run(["systemctl", "--user", "show-environment"])
    run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_NAME}.timer"])
    timer_path.unlink(missing_ok=True)
    service_path.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    print(f"Removed systemd user timer: {SYSTEMD_NAME}.timer")


def windows_script(root: Path, action: str, mise: str | None = None) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise SchedulerError("PowerShell is required to manage Windows Task Scheduler.")
    script = Path(__file__).with_name("scheduler-windows.ps1")
    args = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Action",
        action,
        "-StorageRoot",
        str(root),
    ]
    if action == "install":
        if mise is None:
            raise SchedulerError("Could not resolve mise for the Windows scheduled task.")
        args.extend(
            [
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
            status_macos(root)
        else:
            uninstall_macos(root)
    elif system == "Windows":
        windows_script(root, action, mise_executable() if action == "install" else None)
    elif system == "Linux":
        if action == "install":
            install_linux(root, mise_executable(), os.environ.get("PATH", ""))
        elif action == "status":
            status_linux(root)
        else:
            uninstall_linux(root)
    else:
        raise SchedulerError(f"Unsupported operating system: {system}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--storage", required=True, type=Path, help="Storage repository root")
    args = parser.parse_args()
    try:
        root = args.storage.expanduser().absolute()
        try:
            ensure_no_symlink_components(root, root)
        except VaultPathError as exc:
            raise SchedulerError(f"Storage root must not contain symbolic links or junctions: {root}") from exc
        if not root.is_dir():
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
