#!/usr/bin/env bash
#MISE description="MCP serverを起動する"

set -euo pipefail

STORAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$STORAGE_ROOT"
# shellcheck disable=SC1091
source "$STORAGE_ROOT/mise/scripts/storage.sh"
pkm_require_storage_ready "$STORAGE_ROOT"
export UV_PROJECT_ENVIRONMENT="$STORAGE_ROOT/.pkm/runtime"

exec uv run --locked --project "$STORAGE_ROOT/.pkm" pkm mcp --storage "$STORAGE_ROOT"
