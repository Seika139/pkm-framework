#!/usr/bin/env bash

set -euo pipefail

if (($# != 1)); then
  printf 'Usage: bash mise/scripts/schedule.sh <install|status|uninstall>\n' >&2
  exit 2
fi

STORAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$STORAGE_ROOT"
export UV_PROJECT_ENVIRONMENT="$STORAGE_ROOT/.pkm/runtime"

exec uv run --locked --project "$STORAGE_ROOT/.pkm" python "$STORAGE_ROOT/mise/scripts/scheduler.py" \
  "$1" --storage "$STORAGE_ROOT"
