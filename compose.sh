#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${BACKEND_ENV_FILE:-fastapi_backend/.env}}"

# The generated data volume is private and owned by the caller. The one-shot
# initializer runs as root only long enough to assign these numeric IDs.
BACKEND_HOST_UID="$(id -u)"
BACKEND_HOST_GID="$(id -g)"
export BACKEND_HOST_UID BACKEND_HOST_GID

cd "$REPOSITORY_DIR"

build_backend_image() {
    # Some Docker Compose releases normalize Unicode paths before handing the
    # build context to Docker. Build directly so the original path is kept.
    docker build \
        --file fastapi_backend/Dockerfile \
        --tag gaeng3-backend:local \
        "$@" \
        .
}

if [ "${1:-}" = "build-image" ]; then
    shift
    exec docker build \
        --file fastapi_backend/Dockerfile \
        --tag gaeng3-backend:local \
        "$@" \
        .
fi

if [ ! -r "$COMPOSE_ENV_FILE" ]; then
    echo "Cannot read Compose environment file: $COMPOSE_ENV_FILE" >&2
    echo "Copy fastapi_backend/.env.example to fastapi_backend/.env first." >&2
    exit 2
fi

# `compose.sh up` is the portable one-command path. It builds with Docker
# directly, strips Compose's --build flag if present, and then starts Compose
# from the already-built image. `--no-build` explicitly skips this step.
if [ "${1:-}" = "up" ]; then
    shift
    compose_arguments=()
    skip_build=false
    for argument in "$@"; do
        case "$argument" in
            --build)
                ;;
            --no-build)
                skip_build=true
                compose_arguments+=("$argument")
                ;;
            *)
                compose_arguments+=("$argument")
                ;;
        esac
    done
    if [ "$skip_build" = false ]; then
        build_backend_image
        compose_arguments=(--no-build "${compose_arguments[@]}")
    fi
    exec docker compose --env-file "$COMPOSE_ENV_FILE" up "${compose_arguments[@]}"
fi

exec docker compose --env-file "$COMPOSE_ENV_FILE" "$@"
