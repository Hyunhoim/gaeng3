#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPOSITORY_DIR="$(dirname -- "$SCRIPT_DIR")"
BACKEND_ENV_FILE="${BACKEND_ENV_FILE:-fastapi_backend/.env}"

# Docker bind mounts preserve numeric ownership. Run the read-only Backend as
# the caller so private 0700/0600 normalized artifacts stay readable without
# making the competition data world-readable.
BACKEND_HOST_UID="$(id -u)"
BACKEND_HOST_GID="$(id -g)"
export BACKEND_HOST_UID BACKEND_HOST_GID

cd "$REPOSITORY_DIR"
if [ ! -r "$BACKEND_ENV_FILE" ]; then
    echo "Cannot read Compose environment file: $BACKEND_ENV_FILE" >&2
    echo "Copy fastapi_backend/.env.example to fastapi_backend/.env first." >&2
    exit 2
fi
exec docker compose --env-file "$BACKEND_ENV_FILE" "$@"
