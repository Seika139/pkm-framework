#!/usr/bin/env bash

#MISE description="このユーザーの1時間ごとのStorage同期を登録する"

set -euo pipefail

STORAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$STORAGE_ROOT/mise/scripts/storage.sh"
pkm_require_storage_ready "$STORAGE_ROOT"
exec bash "$STORAGE_ROOT/mise/scripts/schedule.sh" install
