#!/usr/bin/env bash
set -euo pipefail

REHEARSAL_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REHEARSAL_ENV_FILE="${COMPOSE_ENV_FILE:-${BACKEND_ENV_FILE:-fastapi_backend/.env}}"
REHEARSAL_PYTHON="${PYTHON_BIN:-python}"
REHEARSAL_BOUNDARY_PROFILE="development"
REHEARSAL_SKIP_BUILD=false

usage() {
    echo "Usage: ./rehearse.sh [--no-build] [--submission]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build)
            REHEARSAL_SKIP_BUILD=true
            ;;
        --submission)
            REHEARSAL_BOUNDARY_PROFILE="submission"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

cd "$REHEARSAL_ROOT"
if [ ! -r "$REHEARSAL_ENV_FILE" ]; then
    echo "Cannot read $REHEARSAL_ENV_FILE" >&2
    echo "Copy fastapi_backend/.env.example to fastapi_backend/.env first." >&2
    exit 2
fi

REHEARSAL_BACKEND_PORT="${BACKEND_PORT:-}"
if [ -z "$REHEARSAL_BACKEND_PORT" ]; then
    while IFS='=' read -r key value; do
        if [ "$key" = "BACKEND_PORT" ]; then
            REHEARSAL_BACKEND_PORT="${value%$'\r'}"
        fi
    done < "$REHEARSAL_ENV_FILE"
fi
REHEARSAL_BACKEND_PORT="${REHEARSAL_BACKEND_PORT:-18001}"
REHEARSAL_BASE_URL="http://127.0.0.1:${REHEARSAL_BACKEND_PORT}"
REHEARSAL_OUTPUT_DIR="finance_agent/artifacts/rehearsal"
mkdir -p "$REHEARSAL_OUTPUT_DIR"

compose_arguments=(up --detach --wait)
if [ "$REHEARSAL_SKIP_BUILD" = true ]; then
    compose_arguments=(up --no-build --detach --wait)
fi
./compose.sh "${compose_arguments[@]}"

"$REHEARSAL_PYTHON" - "$REHEARSAL_BASE_URL" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1]
with urllib.request.urlopen(f"{base_url}/health", timeout=10) as response:
    payload = json.load(response)
if payload.get("status") != "ok":
    raise SystemExit("Backend health status is not ok")
if payload.get("fund_execution_policy") != "locked":
    raise SystemExit("default rehearsal requires fund_execution_policy=locked")
print(json.dumps(payload, ensure_ascii=False))
PY

"$REHEARSAL_PYTHON" fastapi_backend/scripts/smoke.py \
    --base-url "$REHEARSAL_BASE_URL" \
    --expected-fund-execution-policy locked \
    --output "$REHEARSAL_OUTPUT_DIR/docker-http-smoke-v2.json"

PYTHONPATH="finance_agent/packages/finance_agent_core/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$REHEARSAL_PYTHON" finance_agent/scripts/check-submission-boundary.py \
    --profile "$REHEARSAL_BOUNDARY_PROFILE" \
    --output "$REHEARSAL_OUTPUT_DIR/submission-boundary.json"

"$REHEARSAL_PYTHON" -m pytest \
    finance_agent/packages/finance_agent_core/tests -q
"$REHEARSAL_PYTHON" -m pytest fastapi_backend/tests -q
"$REHEARSAL_PYTHON" finance_agent/scripts/check-docs.py

echo "Rehearsal passed"
echo "- Backend: $REHEARSAL_BASE_URL"
echo "- Boundary profile: $REHEARSAL_BOUNDARY_PROFILE"
echo "- Reports: $REHEARSAL_OUTPUT_DIR"
