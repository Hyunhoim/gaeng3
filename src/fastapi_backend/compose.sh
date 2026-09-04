#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

echo "Deprecated: run ./compose.sh from the repository root." >&2
exec "$SCRIPT_DIR/../compose.sh" "$@"
