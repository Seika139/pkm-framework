#!/usr/bin/env bash

#MISE description="Framework の Python 依存関係を同期する"

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

uv sync
