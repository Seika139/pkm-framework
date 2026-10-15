# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Tagged Releases

- [unreleased](https://github.com/Seika139/pkm-framework/compare/v0.1.4...HEAD)
- [0.1.4](https://github.com/Seika139/pkm-framework/compare/v0.1.3...v0.1.4)
- [0.1.3](https://github.com/Seika139/pkm-framework/compare/v0.1.2...v0.1.3)
- [0.1.2](https://github.com/Seika139/pkm-framework/compare/v0.1.1...v0.1.2)
- [0.1.1](https://github.com/Seika139/pkm-framework/releases/tag/v0.1.1)

## [Unreleased]

### Changed

- Framework-managed Storage tasks are now Python file tasks instead of Bash scripts, so they run natively on Windows without WSL. Storage mise configuration must provide Python `3.12` and uv `0.12.23`, and Git must be installed separately for `mise run sync`.
- Schedule tasks now verify that the macOS LaunchAgent or Linux systemd unit was created for the current Storage before reporting status or uninstalling it.
- Storage sync reports a clear error when Git is not installed or not on `PATH`.

### Removed

- Removed the Bash variants of the distributed Storage tasks and scripts (`mise/tasks/*.sh`, `mise/scripts/schedule.sh`, `mise/scripts/storage.sh`); the installer retires previously managed copies from Storage.
- Removed the `grant-permissions` mise task from the Framework development setup; Python file tasks no longer need execute-permission fixes.

## [0.1.4] - 2026-10-15

### Fixed

- Hardened Storage path validation against symbolic links, Windows junctions, and files that are not single-link regular files.
- Improved scheduled-sync task ownership checks and handling of missing systemd user directories.
- Protected Git metadata and rotating sync logs against redirected paths and unsafe file types.

## [0.1.3] - 2026-10-09

### Fixed

- Corrected the packaged SHA-256 for `mise/scripts/sync.py` so Storage resource installation accepts the current file ([#7](https://github.com/Seika139/pkm-framework/issues/7)).

## [0.1.2] - 2026-10-09

### Fixed

- Fixed Git-tag installation failing when packaged Skills or Storage resources duplicated wheel paths ([#4](https://github.com/Seika139/pkm-framework/issues/4)).

## [0.1.1] - 2026-10-09
