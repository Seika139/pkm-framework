#!/usr/bin/env bash

set -euo pipefail

pkm_require_storage_ready() {
  local storage_root="${1:?Storage root is required}"
  local setup_marker="$storage_root/.pkm/setup-incomplete"

  if [[ -e "$setup_marker" ]]; then
    printf '%s\n' "Storage setup is incomplete. Run mise run setup to repair it." >&2
    return 1
  fi

  export UV_PROJECT_ENVIRONMENT="$storage_root/.pkm/runtime"
  if ! uv run --locked --project "$storage_root/.pkm" pkm storage verify --storage "$storage_root" >/dev/null; then
    printf '%s\n' "Framework-managed files are not ready. Run mise run setup to repair them." >&2
    return 1
  fi
}
