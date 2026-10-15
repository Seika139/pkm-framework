import sys
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

SCHEDULER_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "pkm_framework"
    / "storage_resources"
    / "mise"
    / "scripts"
)
sys.path.insert(0, str(SCHEDULER_DIR))

import scheduler


def macos_document(root: Path, mise: str | None = None) -> dict[str, object]:
    mise = mise or str(Path.cwd() / "bin" / "mise")
    return {
        "Label": scheduler.MAC_LABEL,
        "ProgramArguments": [mise, "run", "sync"],
        "WorkingDirectory": str(root),
        "StartInterval": 3600,
        "EnvironmentVariables": {"PATH": str(Path.cwd() / "bin")},
        "StandardOutPath": str(root / ".pkm" / "cache" / "logs" / "scheduler.stdout.log"),
        "StandardErrorPath": str(root / ".pkm" / "cache" / "logs" / "scheduler.stderr.log"),
    }


def launchctl_output(
    domain: str,
    plist_path: Path,
    document: dict[str, object],
    *,
    program: str | None = None,
) -> str:
    arguments = document["ProgramArguments"]
    environment = document["EnvironmentVariables"]
    assert isinstance(arguments, list) and isinstance(environment, dict)
    lines = [
        f"{domain}/{scheduler.MAC_LABEL} = {{",
        "\tactive count = 0",
        f"\tpath = {plist_path}",
        "\ttype = LaunchAgent",
        "\tstate = not running",
        f"\tprogram = {program or arguments[0]}",
        "\targuments = {",
        *[f"\t\t{argument}" for argument in arguments],
        "\t}",
        f"\tworking directory = {document['WorkingDirectory']}",
        f"\tstdout path = {document['StandardOutPath']}",
        f"\tstderr path = {document['StandardErrorPath']}",
        "\trun interval = 3600 seconds",
        "\tenvironment = {",
        f"\t\tPATH => {environment['PATH']}",
        "\t}",
        "\tproperties = inferred program",
        "}",
    ]
    return "\n".join(lines)


class SchedulerOwnershipTests(unittest.TestCase):
    def test_macos_job_must_belong_to_requested_storage(self) -> None:
        root = Path.cwd() / "storage" / "first"
        document = macos_document(root)
        scheduler.validate_macos_job(document, root)
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_macos_job(document, Path.cwd() / "storage" / "second")

    def test_macos_job_rejects_other_program_and_extra_keys(self) -> None:
        root = Path.cwd() / "storage" / "first"
        document = macos_document(root, str(Path.cwd() / "bin" / "other"))
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_macos_job(document, root)

        document = macos_document(root)
        document["KeepAlive"] = True
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_macos_job(document, root)

    def test_macos_job_requires_generated_interval_environment_and_log_paths(self) -> None:
        root = Path.cwd() / "storage" / "first"
        for field, value in (
            ("StartInterval", 120),
            ("RunAtLoad", False),
            ("EnvironmentVariables", {"PATH": "\nKeepAlive=true"}),
            ("StandardOutPath", str(root / "outside.log")),
        ):
            with self.subTest(field=field):
                document = macos_document(root)
                document[field] = value
                with self.assertRaises(scheduler.SchedulerError):
                    scheduler.validate_macos_job(document, root)

    def test_legacy_macos_plist_without_run_at_load_is_accepted_and_loaded(self) -> None:
        root = Path.cwd() / "storage" / "first"
        document = macos_document(root)
        self.assertNotIn("RunAtLoad", document)
        scheduler.validate_macos_job(document, root)
        domain = "gui/501"
        plist_path = Path.cwd() / "Library" / "LaunchAgents" / f"{scheduler.MAC_LABEL}.plist"
        scheduler.validate_loaded_macos_job(
            launchctl_output(domain, plist_path, document), domain, plist_path, document
        )
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_loaded_macos_job(
                launchctl_output(domain, plist_path, document, program=str(Path.cwd() / "bin" / "other")),
                domain,
                plist_path,
                document,
            )

    def test_uninstall_does_not_bootout_mismatched_loaded_macos_job(self) -> None:
        root = Path.cwd() / "storage" / "first"
        plist_path = Path.cwd() / "Library" / "LaunchAgents" / f"{scheduler.MAC_LABEL}.plist"
        document = macos_document(root)
        domain = "gui/501"
        loaded = subprocess.CompletedProcess(
            args=["launchctl", "print"],
            returncode=0,
            stdout=launchctl_output(
                domain, plist_path, document, program=str(Path.cwd() / "bin" / "other")
            ),
            stderr="",
        )
        with (
            patch.object(scheduler, "read_macos_job", return_value=document),
            patch.object(scheduler, "mac_plist_path", return_value=plist_path),
            patch.object(scheduler, "safe_path", return_value=plist_path),
            patch.object(scheduler.os, "getuid", return_value=501, create=True),
            patch.object(scheduler, "_query_macos_job", return_value=loaded),
            patch.object(scheduler, "run") as run_command,
            patch.object(scheduler.Path, "unlink") as unlink,
            self.assertRaises(scheduler.SchedulerError),
        ):
            scheduler.uninstall_macos(root)
        run_command.assert_not_called()
        unlink.assert_not_called()

    def test_linux_job_pair_must_match_requested_storage(self) -> None:
        root = Path.cwd() / "storage" / "first"
        mise = str(Path.cwd() / "bin" / "mise")
        service = scheduler.systemd_service(root, mise, str(Path.cwd() / "bin"))
        timer = scheduler.systemd_timer()
        self.assertTrue(scheduler.validate_linux_job(root, service, timer))
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_linux_job(Path.cwd() / "storage" / "second", service, timer)
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_linux_job(root, None, timer)

    def test_systemd_service_rejects_other_program_and_extra_directives(self) -> None:
        root = Path.cwd() / "storage" / "first"
        environment_path = str(Path.cwd() / "bin")
        service = scheduler.systemd_service(root, str(Path.cwd() / "bin" / "other"), environment_path)
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_systemd_service(service, root)

        service = scheduler.systemd_service(root, str(Path.cwd() / "bin" / "mise"), environment_path)
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.validate_systemd_service(service.replace("ExecStart=", "ExecStartPre=/bin/true\nExecStart="), root)

    def test_status_and_uninstall_dispatch_requested_storage(self) -> None:
        root = Path.cwd() / "storage" / "selected"
        for system, action, function in (
            ("Darwin", "status", "status_macos"),
            ("Darwin", "uninstall", "uninstall_macos"),
            ("Linux", "status", "status_linux"),
            ("Linux", "uninstall", "uninstall_linux"),
        ):
            with self.subTest(system=system, action=action):
                with (
                    patch.object(scheduler.platform, "system", return_value=system),
                    patch.object(scheduler, function) as operation,
                ):
                    scheduler.dispatch(action, root)
                    operation.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()
