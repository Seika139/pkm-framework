#!/usr/bin/env bash
#MISE description="Markdownを検索する"

set -euo pipefail

if (($# == 0)); then
  printf 'Usage: mise run search -- <query>\n' >&2
  exit 2
fi

STORAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$STORAGE_ROOT"
# shellcheck disable=SC1091
source "$STORAGE_ROOT/mise/scripts/storage.sh"
pkm_require_storage_ready "$STORAGE_ROOT"
export UV_PROJECT_ENVIRONMENT="$STORAGE_ROOT/.pkm/runtime"
QUERY="$*"

uv run --locked --project "$STORAGE_ROOT/.pkm" pkm search "$QUERY" --storage "$STORAGE_ROOT"
