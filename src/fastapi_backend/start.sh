#!/usr/bin/env sh
set -eu

exec uvicorn app.main:app \
    --host "${BACKEND_PROCESS_HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --no-access-log \
    --log-level "${LOG_LEVEL:-info}"
