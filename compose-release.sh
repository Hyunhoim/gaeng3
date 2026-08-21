#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RELEASE_ENV_FILE="${RELEASE_ENV_FILE:-fastapi_backend/.env.release}"
RELEASE_SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/finance-agent-release.XXXXXXXX")"
RELEASE_ENV_SNAPSHOT="$RELEASE_SNAPSHOT_DIR/release.env"

cleanup_release_snapshot() {
    chmod -R u+rwX -- "$RELEASE_SNAPSHOT_DIR" 2>/dev/null || true
    rm -rf -- "$RELEASE_SNAPSHOT_DIR"
}
trap cleanup_release_snapshot EXIT HUP INT TERM
# 0711 permits the fixed non-root container UID to traverse a file bind source
# without allowing directory listings.  The sanitized env itself stays 0600.
chmod 0711 "$RELEASE_SNAPSHOT_DIR"

cd "$REPOSITORY_DIR"

if [ ! -r "$RELEASE_ENV_FILE" ]; then
    echo "Cannot read release environment file: $RELEASE_ENV_FILE" >&2
    exit 2
fi

# Parse only non-secret release identity fields.  The application repeats every
# check inside each worker before reading HCLX credentials or opening a DB.
python3 - "$RELEASE_ENV_FILE" "$RELEASE_ENV_SNAPSHOT" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

environment = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit("release environment contains an invalid line")
    key, value = line.split("=", 1)
    key = key.strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in environment:
        raise SystemExit("release environment contains an invalid or duplicate key")
    environment[key] = value.strip()

PERSISTENT_BINDING_ROOT = Path(
    "/var/lib/finance-agent-release/runtime-bindings"
)

if "CLOVASTUDIO_API_KEY" in environment:
    raise SystemExit(
        "release deployments forbid inline CLOVASTUDIO_API_KEY; use a secret file"
    )

allowed = {
    "APP_ENV",
    "FINANCE_IMAGE_REFERENCE",
    "FINANCE_SOURCE_COMMIT",
    "FINANCE_RUNTIME_PLATFORM",
    "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SHA256",
    "FINANCE_DATA_VOLUME_NAME",
    "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
    "FINANCE_RELEASE_MANIFEST_HOST_FILE",
    "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
    "FINANCE_AUDIT_HOST_DIR",
    "FINANCE_AUDIT_QUEUE_CAPACITY",
    "FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS",
    "FINANCE_AUDIT_FSYNC_EACH_EVENT",
    "BACKEND_BIND_ADDRESS",
    "BACKEND_PORT",
    "LOG_LEVEL",
    "WEB_CONCURRENCY",
    "OFFICIAL_ANSWER_TIMEOUT_SECONDS",
    "OFFICIAL_ANSWER_MAX_INFLIGHT",
    "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
    "FINANCE_BACKEND_ANSWER_PROVIDER",
    "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
    "FINANCE_AGENT_LLM_MODE",
    "LLM_PROVIDER",
    "HCX_MODEL",
    "HCX_TIMEOUT_SECONDS",
    "CLOVASTUDIO_API_KEY_HOST_FILE",
    "CLOVASTUDIO_API_KEY_FILE",
}
extra = sorted(set(environment) - allowed)
if extra:
    raise SystemExit("release environment contains unsupported settings: " + ", ".join(extra))

required = {
    "APP_ENV",
    "FINANCE_IMAGE_REFERENCE",
    "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SHA256",
    "FINANCE_SOURCE_COMMIT",
    "FINANCE_RUNTIME_PLATFORM",
    "FINANCE_DATA_VOLUME_NAME",
    "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
    "FINANCE_RELEASE_MANIFEST_HOST_FILE",
    "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
    "FINANCE_AUDIT_HOST_DIR",
    "WEB_CONCURRENCY",
    "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
    "FINANCE_BACKEND_ANSWER_PROVIDER",
    "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
    "HCX_TIMEOUT_SECONDS",
}
missing = sorted(name for name in required if not environment.get(name))
if missing:
    raise SystemExit("missing release settings: " + ", ".join(missing))
if environment["APP_ENV"] not in {"evaluation", "production"}:
    raise SystemExit("APP_ENV must be evaluation or production for a release deployment")
if environment["WEB_CONCURRENCY"] != "1":
    raise SystemExit(
        "evaluation/production requires WEB_CONCURRENCY=1 until audit aggregation exists"
    )

relation_artifact_sha256 = environment["FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256"]
if re.fullmatch(r"[0-9a-f]{64}", relation_artifact_sha256) is None:
    raise SystemExit("FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256 is invalid")

RELEASE_BACKEND_UID = 10001
audit_root = Path(environment["FINANCE_AUDIT_HOST_DIR"])
try:
    audit_root_stat = audit_root.stat(follow_symlinks=False)
except OSError:
    raise SystemExit("FINANCE_AUDIT_HOST_DIR is unavailable") from None
if (
    not audit_root.is_absolute()
    or not stat.S_ISDIR(audit_root_stat.st_mode)
    or audit_root.is_symlink()
    or audit_root_stat.st_uid != RELEASE_BACKEND_UID
    or audit_root_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
):
    raise SystemExit(
        "FINANCE_AUDIT_HOST_DIR must be a UID 10001 owner-only local directory"
    )

def bounded_integer(name, default, minimum, maximum):
    raw = environment.get(name, str(default))
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise SystemExit(name + " must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise SystemExit(name + " is outside its allowed range")

bounded_integer("FINANCE_AUDIT_QUEUE_CAPACITY", 2048, 1, 100000)
try:
    audit_shutdown_timeout = float(
        environment.get("FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS", "5")
    )
except ValueError:
    raise SystemExit("FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS must be numeric") from None
if not 0 < audit_shutdown_timeout <= 60:
    raise SystemExit("FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS is outside its allowed range")
if environment.get("FINANCE_AUDIT_FSYNC_EACH_EVENT", "true").lower() != "true":
    raise SystemExit("release audit fsync must remain enabled")

answer_provider = environment["FINANCE_BACKEND_ANSWER_PROVIDER"]
hcx_query_plan = environment["FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED"].lower()
if hcx_query_plan not in {"true", "false"}:
    raise SystemExit("FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED must be true or false")
uses_hcx = answer_provider == "hyperclova" or hcx_query_plan == "true"
if answer_provider not in {"deterministic", "hyperclova"}:
    raise SystemExit("release answer provider must be deterministic or hyperclova")
if uses_hcx:
    if (
        environment.get("FINANCE_AGENT_LLM_MODE") != environment["APP_ENV"]
        or environment.get("LLM_PROVIDER") != "hyperclova"
        or environment.get("HCX_MODEL") != "HCX-007"
        or not environment.get("CLOVASTUDIO_API_KEY_HOST_FILE")
        or environment.get("CLOVASTUDIO_API_KEY_FILE")
        != "/run/secrets/clovastudio_api_key"
    ):
        raise SystemExit("HyperCLOVA release provider profile is incomplete")
    secret = Path(environment["CLOVASTUDIO_API_KEY_HOST_FILE"])
    try:
        secret_stat = secret.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("HyperCLOVA release secret is unavailable") from None
    if (
        not secret.is_absolute()
        or not stat.S_ISREG(secret_stat.st_mode)
        or secret_stat.st_uid != 10001
        or secret_stat.st_nlink != 1
        or secret_stat.st_mode & 0o077
        or not 0 < secret_stat.st_size <= 4096
    ):
        raise SystemExit("HyperCLOVA release secret must be an absolute regular file")
elif any(
    environment.get(name)
    for name in ("CLOVASTUDIO_API_KEY_HOST_FILE", "CLOVASTUDIO_API_KEY_FILE", "HCX_MODEL")
):
    raise SystemExit("deterministic release must not configure HyperCLOVA credentials")

image = environment["FINANCE_IMAGE_REFERENCE"]
if re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}", image) is None:
    raise SystemExit("FINANCE_IMAGE_REFERENCE must be an immutable repository@sha256 reference")
expected = environment["FINANCE_DEPLOYMENT_BINDING_SHA256"]
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("FINANCE_DEPLOYMENT_BINDING_SHA256 is invalid")
binding_path = Path(environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"])

def read_secure(source, label):
    try:
        before = source.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit(label + " is unavailable") from None
    if (
        not source.is_absolute()
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid not in {0, os.geteuid()}
        or before.st_nlink != 1
        or before.st_mode & 0o022
    ):
        raise SystemExit(label + " is not a secure release artifact")
    descriptor = None
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        payload = os.read(descriptor, 2 * 1024 * 1024 + 1)
        after = os.fstat(descriptor)
        current = source.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit(label + " is unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    fingerprint = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        fingerprint(before) != fingerprint(after)
        or fingerprint(after) != fingerprint(current)
        or not payload
        or len(payload) > 2 * 1024 * 1024
    ):
        raise SystemExit(label + " changed while reading or has an invalid size")
    return payload

data = read_secure(binding_path, "FINANCE_DEPLOYMENT_BINDING_HOST_FILE")
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit("DeploymentBinding differs from its trusted SHA-256")
def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

try:
    binding = json.loads(data, object_pairs_hook=strict_object)
except (UnicodeError, json.JSONDecodeError, ValueError) as error:
    raise SystemExit("DeploymentBinding is not strict JSON") from error
if binding.get("image_reference") != image:
    raise SystemExit("DeploymentBinding and FINANCE_IMAGE_REFERENCE differ")
if binding.get("source_commit") != environment["FINANCE_SOURCE_COMMIT"]:
    raise SystemExit("DeploymentBinding and FINANCE_SOURCE_COMMIT differ")
platform = environment["FINANCE_RUNTIME_PLATFORM"]
if platform != "linux/amd64" or binding.get("platform") != platform:
    raise SystemExit("DeploymentBinding and FINANCE_RUNTIME_PLATFORM differ")
release_id = str(binding.get("release_id", ""))
manifest_sha256 = str(binding.get("release_manifest_sha256", ""))
if re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,127}", release_id) is None:
    raise SystemExit("DeploymentBinding release_id is invalid")
if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
    raise SystemExit("DeploymentBinding manifest SHA-256 is invalid")
expected_volume = "finance-data-" + release_id + "-" + manifest_sha256[:12]
if environment["FINANCE_DATA_VOLUME_NAME"] != expected_volume:
    raise SystemExit("FINANCE_DATA_VOLUME_NAME must be release-specific: " + expected_volume)

snapshot_root = Path(sys.argv[2]).parent

def snapshot_file(source_name, target_name, mode=0o444):
    source = Path(environment[source_name])
    payload = read_secure(source, source_name)
    target = snapshot_root / target_name
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
        # Do not let a host umask silently make the container-mounted Binding
        # unreadable by the fixed runtime UID 10001.
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("cannot create immutable release snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    environment[source_name] = str(target)

def persistent_binding_snapshot(source_name):
    payload = read_secure(Path(environment[source_name]), source_name)
    expected_sha256 = environment["FINANCE_DEPLOYMENT_BINDING_SHA256"]
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise SystemExit("DeploymentBinding differs from its trusted SHA-256")
    try:
        PERSISTENT_BINDING_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = PERSISTENT_BINDING_ROOT.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("persistent DeploymentBinding root is unavailable") from None
    if (
        not PERSISTENT_BINDING_ROOT.is_absolute()
        or not stat.S_ISDIR(root_stat.st_mode)
        or PERSISTENT_BINDING_ROOT.is_symlink()
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise SystemExit("persistent DeploymentBinding root is not owner-only")

    target = PERSISTENT_BINDING_ROOT / f"{expected_sha256}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".deployment-binding.",
        dir=PERSISTENT_BINDING_ROOT,
    )
    temporary = Path(temporary_name)
    created = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("cannot create persistent DeploymentBinding snapshot")
            view = view[written:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, target, follow_symlinks=False)
            created = True
        except FileExistsError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    if created:
        directory = os.open(
            PERSISTENT_BINDING_ROOT,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    current = read_secure(target, "persistent DeploymentBinding snapshot")
    target_stat = target.stat(follow_symlinks=False)
    if (
        current != payload
        or target_stat.st_uid != os.geteuid()
        or target_stat.st_nlink != 1
        or stat.S_IMODE(target_stat.st_mode) != 0o444
    ):
        raise SystemExit("persistent DeploymentBinding snapshot is invalid")
    environment[source_name] = str(target)

persistent_binding_snapshot("FINANCE_DEPLOYMENT_BINDING_HOST_FILE")
snapshot_file("FINANCE_RELEASE_MANIFEST_HOST_FILE", "agent-release-manifest.json")
snapshot_file(
    "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
    "agent-release-manifest.sigstore.json",
)
snapshot_file(
    "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
    "deployment-binding.sigstore.json",
)

output = Path(sys.argv[2])
descriptor = os.open(
    output,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
    0o600,
)
try:
    os.fchmod(descriptor, 0o600)
    for key in sorted(environment):
        value = environment[key]
        if any(character in value for character in "'\r\n"):
            raise SystemExit("release environment contains an unsafe value")
        payload = f"{key}='{value}'\n".encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("cannot create sanitized release environment")
            view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

# Shell variables have higher Compose precedence than --env-file.  Remove the
# release control fields so only the verified file can supply their values.
unset APP_ENV \
    BACKEND_BIND_ADDRESS \
    BACKEND_PORT \
    LOG_LEVEL \
    WEB_CONCURRENCY \
    OFFICIAL_ANSWER_TIMEOUT_SECONDS \
    OFFICIAL_ANSWER_MAX_INFLIGHT \
    FINANCE_IMAGE_REFERENCE \
    FINANCE_SOURCE_COMMIT \
    FINANCE_RUNTIME_PLATFORM \
    FINANCE_DEPLOYMENT_BINDING_HOST_FILE \
    FINANCE_DEPLOYMENT_BINDING_SHA256 \
    FINANCE_DATA_VOLUME_NAME \
    FINANCE_RELEASE_MANIFEST_HOST_FILE \
    FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE \
    FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE \
    FINANCE_AUDIT_HOST_DIR \
    FINANCE_AUDIT_QUEUE_CAPACITY \
    FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS \
    FINANCE_AUDIT_FSYNC_EACH_EVENT \
    FINANCE_BACKEND_FUND_EXECUTION_POLICY \
    FINANCE_BACKEND_ANSWER_PROVIDER \
    FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED \
    FINANCE_AGENT_LLM_MODE \
    LLM_PROVIDER \
    HCX_MODEL \
    HCX_TIMEOUT_SECONDS \
    CLOVASTUDIO_API_KEY \
    CLOVASTUDIO_API_KEY_HOST_FILE \
    CLOVASTUDIO_API_KEY_FILE

# Compose gives inherited shell variables precedence over --env-file.  Remove
# every namespaced control variable, including future additions, so the private
# validated snapshot remains the only release configuration source.
for release_variable in ${!FINANCE_@} ${!HCX_@} ${!CLOVASTUDIO_@} ${!COMPOSE_@}; do
    unset "$release_variable"
done

arguments=("$@")
up_index=-1
subcommand_index=-1
subcommand=""
expects_global_value=false
for index in "${!arguments[@]}"; do
    argument="${arguments[$index]}"
    if [ "$expects_global_value" = true ]; then
        expects_global_value=false
        continue
    fi
    case "$argument" in
        build|--build|--build=*|--no-build|--no-build=*)
            echo "Release Compose forbids mutable local image builds." >&2
            exit 2
            ;;
        -v|-v=*|-v?*|--volumes|--volumes=*|--rmi|--rmi=*)
            echo "Release Compose forbids deleting rollback volumes or images." >&2
            exit 2
            ;;
        --no-recreate|--no-recreate=*|--force-recreate|--force-recreate=*|--no-deps|--no-start)
            echo "Release Compose forbids preserving or skipping release services." >&2
            exit 2
            ;;
        --profile|--profile=*)
            echo "Release Compose forbids activation profiles." >&2
            exit 2
            ;;
        -f|--file|--file=*|-f?*|--env-file|--env-file=*|-p|--project-name|--project-name=*|-p?*|--project-directory|--project-directory=*)
            echo "Release Compose forbids overriding its release control files." >&2
            exit 2
            ;;
        --ansi|--progress)
            expects_global_value=true
            ;;
        -*)
            if [ -z "$subcommand" ]; then
                echo "Release Compose forbids unknown global option: $argument" >&2
                exit 2
            fi
            ;;
        *)
            if [ -z "$subcommand" ]; then
                subcommand="$argument"
                subcommand_index="$index"
                if [ "$argument" = "up" ]; then
                    up_index="$index"
                fi
            fi
            ;;
    esac
done

case "$subcommand" in
    up|down|config|ps|logs|images|pull|stop)
        ;;
    "")
        echo "Release Compose requires an explicit supported command." >&2
        exit 2
        ;;
    *)
        echo "Release Compose forbids command: $subcommand" >&2
        exit 2
        ;;
esac

if [ "$subcommand" = "pull" ]; then
    python3 fastapi_backend/scripts/release_trust.py --env-file "$RELEASE_ENV_SNAPSHOT"
fi

if [ "$subcommand" = "down" ] && [ "${#arguments[@]}" -ne "$((subcommand_index + 1))" ]; then
    echo "Release Compose down accepts no options; rollback volumes and images must be preserved." >&2
    exit 2
fi

if [ "$up_index" -ne -1 ]; then
    before_up=("${arguments[@]:0:up_index}")
    after_up=("${arguments[@]:up_index+1}")

    # A release activation always recreates the complete stack.  Accept only
    # options that cannot narrow the service set or weaken the activation.
    expects_up_value=false
    for argument in "${after_up[@]}"; do
        if [ "$expects_up_value" = true ]; then
            case "$argument" in
                ''|-*)
                    echo "Release Compose received an invalid up option value." >&2
                    exit 2
                    ;;
            esac
            expects_up_value=false
            continue
        fi
        case "$argument" in
            -d|--detach|--wait|--remove-orphans|--quiet-pull|--timestamps|--no-color|--yes)
                ;;
            --wait-timeout|--timeout|--pull)
                expects_up_value=true
                ;;
            --wait-timeout=*|--timeout=*|--pull=*)
                ;;
            --*)
                echo "Release Compose forbids unsafe up option: $argument" >&2
                exit 2
                ;;
            *)
                echo "Release Compose requires full-stack activation; service selection is forbidden." >&2
                exit 2
                ;;
        esac
    done
    if [ "$expects_up_value" = true ]; then
        echo "Release Compose received an up option without its value." >&2
        exit 2
    fi

    # The activation broker owns the fixed root-controlled host lock and state.
    # It verifies trust while holding that lock, rejects replay/generation gaps,
    # waits for health, and only then atomically commits the active Binding.
    python3 fastapi_backend/scripts/release_activation.py \
        --env-file "$RELEASE_ENV_SNAPSHOT" \
        -- \
        "${before_up[@]}" up "${after_up[@]}" \
        --no-build --force-recreate --wait
    exit $?
fi

docker compose \
    -p hyunholim-finance-agent \
    --env-file "$RELEASE_ENV_SNAPSHOT" \
    -f docker-compose.yml \
    -f fastapi_backend/docker-compose.release.yml \
    "$@"
