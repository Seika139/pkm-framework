#!/usr/bin/env bash
#MISE description="vault内のMarkdownを読む"

set -euo pipefail

if (($# != 1)); then
  printf 'Usage: mise run read -- <vault-relative-path>\n' >&2
  exit 2
fi

STORAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$STORAGE_ROOT"
# shellcheck disable=SC1091
source "$STORAGE_ROOT/mise/scripts/storage.sh"
pkm_require_storage_ready "$STORAGE_ROOT"
export UV_PROJECT_ENVIRONMENT="$STORAGE_ROOT/.pkm/runtime"

uv run --locked --project "$STORAGE_ROOT/.pkm" pkm read "$1" --storage "$STORAGE_ROOT"
