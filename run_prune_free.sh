#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEEP="${1:-5}"
if [[ "${1:-}" != "" ]]; then
  shift || true
fi

exec "$SCRIPT_DIR/run.sh" prune-free --keep "$KEEP" "$@"

