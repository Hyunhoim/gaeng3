#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RELEASE_ENV_FILE="${RELEASE_ENV_FILE:-src/fastapi_backend/.env.release}"
RELEASE_SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/finance-agent-release.XXXXXXXX")"
RELEASE_ENV_SNAPSHOT="$RELEASE_SNAPSHOT_DIR/release.env"
RELEASE_ADAPTIVE_MARKER="$RELEASE_SNAPSHOT_DIR/adaptive.enabled"
RELEASE_DOCUMENT_CACHE_MARKER="$RELEASE_SNAPSHOT_DIR/document-cache.enabled"
RELEASE_LIVE_SEARCH_MARKER="$RELEASE_SNAPSHOT_DIR/live-search.enabled"

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
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath

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
if "NAVER_API_HUB_CLIENT_ID" in environment or "NAVER_API_HUB_CLIENT_SECRET" in environment:
    raise SystemExit(
        "release deployments forbid inline NAVER API HUB credentials; use secret files"
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
    "FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED",
    "FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED",
    "FINANCE_ADAPTIVE_SEMANTIC_ENABLED",
    "FINANCE_SCHEMA_DENSE_INDEX_HOST_FILE",
    "FINANCE_SCHEMA_DENSE_INDEX_SHA256",
    "FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256",
    "FINANCE_SCHEMA_DENSE_MIN_SCORE",
    "FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE",
    "FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN",
    "FINANCE_SCHEMA_DENSE_TOP_K",
    "FINANCE_KURE_CACHE_HOST_DIR",
    "FINANCE_KURE_SNAPSHOT_MANIFEST_HOST_FILE",
    "FINANCE_KURE_CPU_THREADS",
    "FINANCE_KURE_BATCH_SIZE",
    "FINANCE_AGENT_LLM_MODE",
    "LLM_PROVIDER",
    "HCX_MODEL",
    "HCX_TIMEOUT_SECONDS",
    "CLOVASTUDIO_API_KEY_HOST_FILE",
    "CLOVASTUDIO_API_KEY_FILE",
    "FINANCE_EXTERNAL_KNOWLEDGE_ENABLED",
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE",
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE",
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256",
    "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT",
    "NAVER_API_HUB_CLIENT_ID_HOST_FILE",
    "NAVER_API_HUB_CLIENT_ID_FILE",
    "NAVER_API_HUB_CLIENT_SECRET_HOST_FILE",
    "NAVER_API_HUB_CLIENT_SECRET_FILE",
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
    "FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED",
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

external_knowledge_enabled = environment.get(
    "FINANCE_EXTERNAL_KNOWLEDGE_ENABLED", "false"
).lower()
if external_knowledge_enabled not in {"true", "false"}:
    raise SystemExit("FINANCE_EXTERNAL_KNOWLEDGE_ENABLED must be true or false")
external_required = {
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE",
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE",
    "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE",
    "FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256",
}
live_search_required = {
    "NAVER_API_HUB_CLIENT_ID_HOST_FILE",
    "NAVER_API_HUB_CLIENT_ID_FILE",
    "NAVER_API_HUB_CLIENT_SECRET_HOST_FILE",
    "NAVER_API_HUB_CLIENT_SECRET_FILE",
}
document_cache_required = {
    "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR",
    "FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT",
}
configured_document_cache = {
    name for name in document_cache_required if environment.get(name)
}
if configured_document_cache and configured_document_cache != document_cache_required:
    missing_document_cache = sorted(document_cache_required - configured_document_cache)
    raise SystemExit(
        "incomplete Document Dense release settings: "
        + ", ".join(missing_document_cache)
    )
configured_live_search = {
    name for name in live_search_required if environment.get(name)
}
if configured_live_search and configured_live_search != live_search_required:
    missing_live_search = sorted(live_search_required - configured_live_search)
    raise SystemExit(
        "incomplete NAVER API HUB release settings: "
        + ", ".join(missing_live_search)
    )
if external_knowledge_enabled == "true":
    missing_external = sorted(name for name in external_required if not environment.get(name))
    if missing_external:
        raise SystemExit("missing external knowledge settings: " + ", ".join(missing_external))
    expected_container_paths = {
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE": (
            "/run/finance-release/external-knowledge-artifact.json"
        ),
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE": (
            "/run/finance-release/external-temporal-relations.json"
        ),
    }
    if any(environment[name] != value for name, value in expected_container_paths.items()):
        raise SystemExit("external knowledge container paths differ from the release contract")
    for name in (
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256",
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", environment[name]) is None:
            raise SystemExit(name + " is invalid")
else:
    unexpected_external = sorted(
        name
        for name in external_required | document_cache_required | live_search_required
        if environment.get(name)
    )
    if unexpected_external:
        raise SystemExit(
            "disabled external knowledge cannot configure: "
            + ", ".join(unexpected_external)
        )

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
hcx_intent_analysis = environment["FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED"].lower()
if hcx_intent_analysis not in {"true", "false"}:
    raise SystemExit("FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED must be true or false")
hcx_semantic_resolver = environment.get(
    "FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED", "false"
).lower()
adaptive_semantic = environment.get("FINANCE_ADAPTIVE_SEMANTIC_ENABLED", "false").lower()
if hcx_semantic_resolver not in {"true", "false"}:
    raise SystemExit("FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED must be true or false")
if adaptive_semantic not in {"true", "false"}:
    raise SystemExit("FINANCE_ADAPTIVE_SEMANTIC_ENABLED must be true or false")
if hcx_semantic_resolver == "true" and adaptive_semantic != "true":
    raise SystemExit("HCLX Semantic Resolver requires adaptive semantic mode")
if adaptive_semantic == "true" and hcx_semantic_resolver != "true":
    raise SystemExit("final adaptive release requires HCLX Semantic Resolver")
uses_hcx = (
    answer_provider == "hyperclova"
    or hcx_intent_analysis == "true"
    or hcx_semantic_resolver == "true"
)
if answer_provider not in {"deterministic", "hyperclova"}:
    raise SystemExit("release answer provider must be deterministic or hyperclova")
if answer_provider != "hyperclova" or hcx_intent_analysis != "true":
    raise SystemExit("official release requires HCLX Intent analysis and answer generation")
if hcx_query_plan != "false":
    raise SystemExit("full HCLX QueryPlan generation is retired")
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

adaptive_required = {
    "FINANCE_SCHEMA_DENSE_INDEX_HOST_FILE",
    "FINANCE_SCHEMA_DENSE_INDEX_SHA256",
    "FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256",
    "FINANCE_SCHEMA_DENSE_MIN_SCORE",
    "FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE",
    "FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN",
    "FINANCE_KURE_CACHE_HOST_DIR",
    "FINANCE_KURE_SNAPSHOT_MANIFEST_HOST_FILE",
}
adaptive_optional = {
    "FINANCE_SCHEMA_DENSE_TOP_K",
    "FINANCE_KURE_CPU_THREADS",
    "FINANCE_KURE_BATCH_SIZE",
}
if adaptive_semantic == "true":
    missing_adaptive = sorted(name for name in adaptive_required if not environment.get(name))
    if missing_adaptive:
        raise SystemExit("missing adaptive release settings: " + ", ".join(missing_adaptive))
    for name in (
        "FINANCE_SCHEMA_DENSE_INDEX_SHA256",
        "FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", environment[name]) is None:
            raise SystemExit(name + " is invalid")
    try:
        dense_min_score = float(environment["FINANCE_SCHEMA_DENSE_MIN_SCORE"])
        hclx_candidate_floor = float(
            environment["FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE"]
        )
        minimum_margin = float(environment["FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN"])
    except ValueError:
        raise SystemExit("adaptive semantic thresholds must be numeric") from None
    if not (
        -1 <= hclx_candidate_floor <= dense_min_score <= 1
        and 0 <= minimum_margin <= 2
    ):
        raise SystemExit("adaptive semantic thresholds are outside the allowed range")
    bounded_integer("FINANCE_SCHEMA_DENSE_TOP_K", 10, 2, 10)
    bounded_integer("FINANCE_KURE_CPU_THREADS", 2, 1, 8)
    bounded_integer("FINANCE_KURE_BATCH_SIZE", 16, 1, 64)

    def require_adaptive_file(name, *, immutable):
        path = Path(environment[name])
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            raise SystemExit(name + " is unavailable") from None
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (immutable and info.st_mode & stat.S_IWUSR)
        ):
            raise SystemExit(name + " is not a secure immutable artifact")

    # GitHub artifact download normalizes ordinary files to 0644.  Accept that
    # transport form only long enough to hash-address and install the bytes into
    # the root-controlled immutable Runtime store below.
    require_adaptive_file("FINANCE_SCHEMA_DENSE_INDEX_HOST_FILE", immutable=False)
    require_adaptive_file(
        "FINANCE_KURE_SNAPSHOT_MANIFEST_HOST_FILE",
        immutable=False,
    )
    cache_root = Path(environment["FINANCE_KURE_CACHE_HOST_DIR"])
    try:
        cache_info = cache_root.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("FINANCE_KURE_CACHE_HOST_DIR is unavailable") from None
    snapshot = (
        cache_root
        / "models--nlpai-lab--KURE-v1/snapshots"
        / "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
    )
    if (
        not cache_root.is_absolute()
        or not stat.S_ISDIR(cache_info.st_mode)
        or cache_root.is_symlink()
        or cache_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not snapshot.is_dir()
    ):
        raise SystemExit("FINANCE_KURE_CACHE_HOST_DIR is not a trusted KURE cache")
else:
    unexpected_adaptive = sorted(
        name for name in adaptive_required | adaptive_optional if environment.get(name)
    )
    if unexpected_adaptive:
        raise SystemExit(
            "disabled adaptive mode cannot configure: " + ", ".join(unexpected_adaptive)
        )

image = environment["FINANCE_IMAGE_REFERENCE"]
if re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}", image) is None:
    raise SystemExit("FINANCE_IMAGE_REFERENCE must be an immutable repository@sha256 reference")
expected = environment["FINANCE_DEPLOYMENT_BINDING_SHA256"]
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("FINANCE_DEPLOYMENT_BINDING_SHA256 is invalid")
binding_path = Path(environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"])

def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def read_secure(source, label, *, max_bytes=2 * 1024 * 1024, immutable=False):
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
        or (immutable and before.st_mode & stat.S_IWUSR)
    ):
        raise SystemExit(label + " is not a secure release artifact")
    descriptor = None
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        chunks = []
        remaining = min(before.st_size, max_bytes + 1)
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
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
        or len(payload) > max_bytes
        or len(payload) != before.st_size
    ):
        raise SystemExit(label + " changed while reading or has an invalid size")
    return payload

document_cache_enabled = False
live_search_enabled = False
approved_corpus_manifest = None
approved_corpus_manifest_payload = None
if external_knowledge_enabled == "true":
    external_payload = read_secure(
        Path(environment["FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE"]),
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE",
    )
    if hashlib.sha256(external_payload).hexdigest() != environment[
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256"
    ]:
        raise SystemExit(
            "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE differs from its trusted SHA-256"
        )
    relation_payload = read_secure(
        Path(environment["FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE"]),
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE",
        max_bytes=64 * 1024 * 1024,
    )
    if hashlib.sha256(relation_payload).hexdigest() != environment[
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256"
    ]:
        raise SystemExit(
            "FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE differs from its trusted SHA-256"
        )
    try:
        external_artifact = json.loads(
            external_payload,
            object_pairs_hook=strict_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit("external knowledge artifact is not strict JSON") from error
    if not isinstance(external_artifact, dict):
        raise SystemExit("external knowledge artifact must be a JSON object")
    retrieval_mode = external_artifact.get("retrieval_mode")
    if retrieval_mode not in {
        "approved_corpus_cache_only",
        "verified_live_official",
        "approved_corpus_cache_first",
        "verified_live_official_with_relation_graph",
    }:
        raise SystemExit("external knowledge retrieval mode is invalid")
    if retrieval_mode in {
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    }:
        if configured_document_cache != document_cache_required:
            raise SystemExit(
                retrieval_mode + " requires complete Document Dense artifacts"
            )
        if adaptive_semantic != "true":
            raise SystemExit("Document Dense release requires adaptive semantic mode")
        expected_document_paths = {
            "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE": (
                "/models/document-dense-approved.json"
            ),
            "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE": (
                "/run/finance-release/document-cache-activation.json"
            ),
            "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE": (
                "/run/finance-release/approved-corpus-manifest.json"
            ),
            "FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT": "/run/finance-corpus",
        }
        if any(
            environment[name] != value
            for name, value in expected_document_paths.items()
        ):
            raise SystemExit(
                "Document Dense container paths differ from the release contract"
            )
        document_hashes = (
            (
                "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_HOST_FILE",
                "document_dense_sha256",
                512 * 1024 * 1024,
            ),
            (
                "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE",
                "document_activation_receipt_sha256",
                2 * 1024 * 1024,
            ),
        )
        activation_payload = None
        for source_name, artifact_hash_name, max_bytes in document_hashes:
            expected_document_sha256 = external_artifact.get(artifact_hash_name)
            if (
                not isinstance(expected_document_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_document_sha256) is None
            ):
                raise SystemExit(
                    "external knowledge artifact lacks a Document Dense trust binding"
                )
            document_payload = read_secure(
                Path(environment[source_name]),
                source_name,
                max_bytes=max_bytes,
                immutable=False,
            )
            if hashlib.sha256(document_payload).hexdigest() != expected_document_sha256:
                raise SystemExit(source_name + " differs from the signed external artifact")
            if source_name == "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE":
                activation_payload = document_payload
            del document_payload

        expected_corpus_manifest_sha256 = external_artifact.get("corpus_manifest_sha256")
        if (
            not isinstance(expected_corpus_manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_corpus_manifest_sha256) is None
        ):
            raise SystemExit(
                "external knowledge artifact lacks an approved corpus manifest binding"
            )
        approved_corpus_manifest_payload = read_secure(
            Path(environment["FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE"]),
            "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE",
            immutable=False,
        )
        if (
            hashlib.sha256(approved_corpus_manifest_payload).hexdigest()
            != expected_corpus_manifest_sha256
        ):
            raise SystemExit(
                "approved corpus manifest differs from the signed external artifact"
            )
        try:
            approved_corpus_manifest = json.loads(
                approved_corpus_manifest_payload,
                object_pairs_hook=strict_object,
            )
            activation = json.loads(
                activation_payload,
                object_pairs_hook=strict_object,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise SystemExit("approved corpus release inputs are not strict JSON") from error
        if not isinstance(approved_corpus_manifest, dict) or not isinstance(activation, dict):
            raise SystemExit("approved corpus release inputs must be JSON objects")
        documents = approved_corpus_manifest.get("documents")
        document_count = approved_corpus_manifest.get("document_count")
        total_content_size_bytes = approved_corpus_manifest.get(
            "total_content_size_bytes"
        )
        file_manifest_sha256 = approved_corpus_manifest.get("file_manifest_sha256")
        if (
            approved_corpus_manifest.get("manifest_kind") != "external_approved_corpus"
            or not isinstance(documents, list)
            or not 1 <= len(documents) <= 1000
            or document_count != len(documents)
            or not isinstance(total_content_size_bytes, int)
            or isinstance(total_content_size_bytes, bool)
            or not 0 < total_content_size_bytes <= 128 * 1024 * 1024
            or not isinstance(file_manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", file_manifest_sha256) is None
        ):
            raise SystemExit("approved corpus manifest has invalid release metadata")
        file_entries = []
        document_ids = []
        relative_paths = []
        computed_total_size = 0
        for document in documents:
            if not isinstance(document, dict):
                raise SystemExit("approved corpus manifest contains an invalid document")
            document_id = document.get("document_id")
            relative_path = document.get("relative_path")
            content_size = document.get("content_size_bytes")
            content_sha256 = document.get("content_sha256")
            normalized_sha256 = document.get("normalized_text_sha256")
            if (
                not isinstance(document_id, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", document_id) is None
                or not isinstance(relative_path, str)
                or "\\" in relative_path
                or "\x00" in relative_path
                or PurePosixPath(relative_path).is_absolute()
                or PurePosixPath(relative_path).as_posix() != relative_path
                or any(part in {"", ".", ".."} for part in relative_path.split("/"))
                or PurePosixPath(relative_path).suffix.casefold() not in {".txt", ".md"}
                or not isinstance(content_size, int)
                or isinstance(content_size, bool)
                or not 0 < content_size <= 16 * 1024 * 1024
                or not isinstance(content_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
                or not isinstance(normalized_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", normalized_sha256) is None
            ):
                raise SystemExit("approved corpus manifest contains an invalid document")
            document_ids.append(document_id)
            relative_paths.append(relative_path)
            computed_total_size += content_size
            file_entries.append(
                {
                    "content_sha256": content_sha256,
                    "content_size_bytes": content_size,
                    "document_id": document_id,
                    "normalized_text_sha256": normalized_sha256,
                    "relative_path": relative_path,
                }
            )
        canonical_file_entries = (
            json.dumps(
                file_entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if (
            document_ids != sorted(document_ids)
            or len(set(document_ids)) != len(document_ids)
            or len(set(relative_paths)) != len(relative_paths)
            or computed_total_size != total_content_size_bytes
            or hashlib.sha256(canonical_file_entries).hexdigest()
            != file_manifest_sha256
            or activation.get("approved_corpus_manifest_sha256")
            != expected_corpus_manifest_sha256
            or activation.get("approved_corpus_file_manifest_sha256")
            != file_manifest_sha256
        ):
            raise SystemExit("approved corpus release trust bindings differ")
        document_cache_enabled = True
    elif configured_document_cache:
        raise SystemExit("live-only external knowledge rejects Document Dense artifacts")

    if retrieval_mode == "approved_corpus_cache_only":
        if configured_live_search:
            raise SystemExit(
                "approved_corpus_cache_only forbids NAVER API HUB credentials"
            )
    else:
        if configured_live_search != live_search_required:
            raise SystemExit(retrieval_mode + " requires complete NAVER API HUB credentials")
        expected_live_paths = {
            "NAVER_API_HUB_CLIENT_ID_FILE": "/run/secrets/naver_api_hub_client_id",
            "NAVER_API_HUB_CLIENT_SECRET_FILE": "/run/secrets/naver_api_hub_client_secret",
        }
        if any(
            environment[name] != value for name, value in expected_live_paths.items()
        ):
            raise SystemExit("NAVER API HUB container paths differ from the release contract")
        for secret_name in (
            "NAVER_API_HUB_CLIENT_ID_HOST_FILE",
            "NAVER_API_HUB_CLIENT_SECRET_HOST_FILE",
        ):
            secret_path = Path(environment[secret_name])
            try:
                secret_stat = secret_path.stat(follow_symlinks=False)
            except OSError:
                raise SystemExit("NAVER API HUB release secret is unavailable") from None
            if (
                not secret_path.is_absolute()
                or not stat.S_ISREG(secret_stat.st_mode)
                or secret_path.is_symlink()
                or secret_stat.st_uid != RELEASE_BACKEND_UID
                or secret_stat.st_nlink != 1
                or secret_stat.st_mode & 0o077
                or not 0 < secret_stat.st_size <= 4096
            ):
                raise SystemExit(
                    "NAVER API HUB release secret must be an owner-only regular file"
                )
        live_search_enabled = True

data = read_secure(binding_path, "FINANCE_DEPLOYMENT_BINDING_HOST_FILE")
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit("DeploymentBinding differs from its trusted SHA-256")

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

if adaptive_semantic == "true":
    marker = snapshot_root / "adaptive.enabled"
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    os.close(descriptor)

if document_cache_enabled:
    marker = snapshot_root / "document-cache.enabled"
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    os.close(descriptor)

if live_search_enabled:
    marker = snapshot_root / "live-search.enabled"
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    os.close(descriptor)

def snapshot_file(
    source_name,
    target_name,
    mode=0o444,
    *,
    max_bytes=2 * 1024 * 1024,
    immutable=False,
):
    source = Path(environment[source_name])
    payload = read_secure(
        source,
        source_name,
        max_bytes=max_bytes,
        immutable=immutable,
    )
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

def persistent_runtime_blob(source_name, *, max_bytes=2 * 1024 * 1024):
    """Install one verified Runtime mount source at its immutable content address.

    GitHub's artifact transport does not preserve 0444 modes.  Input files may
    therefore be owner-writable, but never group/other-writable, while they are
    read and fingerprinted.  The installed copy is root-owned 0444 and survives
    launcher cleanup, container restart, and Docker daemon restart.
    """

    payload = read_secure(
        Path(environment[source_name]),
        source_name,
        max_bytes=max_bytes,
    )
    digest = hashlib.sha256(payload).hexdigest()
    persistent_root = PERSISTENT_BINDING_ROOT.parent / "runtime-artifacts"
    try:
        persistent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_info = persistent_root.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("persistent Runtime artifact root is unavailable") from None
    if (
        not persistent_root.is_absolute()
        or persistent_root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or root_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise SystemExit("persistent Runtime artifact root is not owner-only")

    target = persistent_root / digest
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".runtime-artifact.",
        dir=persistent_root,
    )
    temporary = Path(temporary_name)
    created = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("cannot create persistent Runtime artifact")
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
            persistent_root,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    current = read_secure(
        target,
        "persistent Runtime artifact",
        max_bytes=max_bytes,
        immutable=True,
    )
    target_info = target.stat(follow_symlinks=False)
    if (
        current != payload
        or target_info.st_uid != os.geteuid()
        or target_info.st_nlink != 1
        or stat.S_IMODE(target_info.st_mode) != 0o444
        or target.name != hashlib.sha256(current).hexdigest()
    ):
        raise SystemExit("persistent Runtime artifact is invalid")
    environment[source_name] = str(target)

def read_corpus_document(root_descriptor, relative_path, expected_size):
    parts = relative_path.split("/")
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            following = os.open(
                part,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | getattr(os, "O_DIRECTORY", 0),
                dir_fd=current,
            )
            info = os.fstat(following)
            if not stat.S_ISDIR(info.st_mode):
                os.close(following)
                raise SystemExit("approved corpus path contains a non-directory")
            os.close(current)
            current = following
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=current,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != expected_size
            ):
                raise SystemExit("approved corpus document has an unsafe file identity")
            chunks = []
            remaining = expected_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if len(payload) != expected_size or identity(before) != identity(after):
                raise SystemExit("approved corpus document changed while reading")
            return payload
        finally:
            os.close(descriptor)
    except OSError:
        raise SystemExit("approved corpus document is unavailable or unsafe") from None
    finally:
        os.close(current)

def write_snapshot_payload(path, payload, mode):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit("cannot create immutable corpus snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def validate_persistent_corpus_bundle(bundle, manifest_payload, documents):
    try:
        bundle_info = bundle.stat(follow_symlinks=False)
        documents_root = bundle / "documents"
        documents_info = documents_root.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("persistent approved corpus snapshot is unavailable") from None
    if (
        bundle.is_symlink()
        or documents_root.is_symlink()
        or not stat.S_ISDIR(bundle_info.st_mode)
        or not stat.S_ISDIR(documents_info.st_mode)
        or bundle_info.st_uid != os.geteuid()
        or documents_info.st_uid != os.geteuid()
        or stat.S_IMODE(bundle_info.st_mode) != 0o555
        or stat.S_IMODE(documents_info.st_mode) != 0o555
    ):
        raise SystemExit("persistent approved corpus snapshot is not immutable")
    current_manifest = read_secure(
        bundle / "manifest.json",
        "persistent approved corpus manifest",
        immutable=True,
    )
    if current_manifest != manifest_payload:
        raise SystemExit("persistent approved corpus manifest differs")
    for document in documents:
        relative_path = document["relative_path"]
        candidate = documents_root.joinpath(*relative_path.split("/"))
        payload = read_secure(
            candidate,
            "persistent approved corpus document",
            max_bytes=16 * 1024 * 1024,
            immutable=True,
        )
        if (
            len(payload) != document["content_size_bytes"]
            or hashlib.sha256(payload).hexdigest() != document["content_sha256"]
        ):
            raise SystemExit("persistent approved corpus document differs")

def persistent_corpus_snapshot():
    source_root = Path(environment["FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR"])
    try:
        source_info = source_root.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR is unavailable") from None
    if (
        not source_root.is_absolute()
        or source_root.is_symlink()
        or not stat.S_ISDIR(source_info.st_mode)
        or source_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SystemExit("FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR is unsafe")
    root_descriptor = os.open(
        source_root,
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        payloads = []
        for document in approved_corpus_manifest["documents"]:
            payload = read_corpus_document(
                root_descriptor,
                document["relative_path"],
                document["content_size_bytes"],
            )
            if hashlib.sha256(payload).hexdigest() != document["content_sha256"]:
                raise SystemExit("approved corpus document differs from its manifest")
            payloads.append((document, payload))
    finally:
        os.close(root_descriptor)

    persistent_root = PERSISTENT_BINDING_ROOT.parent / "approved-corpora"
    try:
        persistent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_info = persistent_root.stat(follow_symlinks=False)
    except OSError:
        raise SystemExit("persistent approved corpus root is unavailable") from None
    if (
        persistent_root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or root_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise SystemExit("persistent approved corpus root is not owner-only")

    manifest_sha256 = hashlib.sha256(approved_corpus_manifest_payload).hexdigest()
    bundle = persistent_root / manifest_sha256
    if not bundle.exists():
        staging = Path(tempfile.mkdtemp(prefix=".approved-corpus.", dir=persistent_root))
        try:
            documents_root = staging / "documents"
            documents_root.mkdir(mode=0o700)
            write_snapshot_payload(
                staging / "manifest.json",
                approved_corpus_manifest_payload,
                0o444,
            )
            for document, payload in payloads:
                target = documents_root.joinpath(*document["relative_path"].split("/"))
                write_snapshot_payload(target, payload, 0o444)
            for directory_name, directories, _files in os.walk(staging, topdown=False):
                directory = Path(directory_name)
                for child in directories:
                    os.chmod(directory / child, 0o555)
                os.chmod(directory, 0o555)
            try:
                os.rename(staging, bundle)
            except FileExistsError:
                shutil.rmtree(staging)
            directory = os.open(
                persistent_root,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            if staging.exists():
                chmod_targets = sorted(
                    (path for path in staging.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                )
                os.chmod(staging, 0o700)
                for target in chmod_targets:
                    os.chmod(target, 0o700)
                shutil.rmtree(staging)
            raise
    validate_persistent_corpus_bundle(
        bundle,
        approved_corpus_manifest_payload,
        approved_corpus_manifest["documents"],
    )
    environment["FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE"] = str(
        bundle / "manifest.json"
    )
    environment["FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR"] = str(
        bundle / "documents"
    )

persistent_binding_snapshot("FINANCE_DEPLOYMENT_BINDING_HOST_FILE")
if adaptive_semantic == "true":
    persistent_runtime_blob(
        "FINANCE_SCHEMA_DENSE_INDEX_HOST_FILE",
        max_bytes=128 * 1024 * 1024,
    )
    persistent_runtime_blob("FINANCE_KURE_SNAPSHOT_MANIFEST_HOST_FILE")
snapshot_file("FINANCE_RELEASE_MANIFEST_HOST_FILE", "agent-release-manifest.json")
snapshot_file(
    "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
    "agent-release-manifest.sigstore.json",
)
snapshot_file(
    "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
    "deployment-binding.sigstore.json",
)
if external_knowledge_enabled == "true":
    persistent_runtime_blob(
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE",
    )
    persistent_runtime_blob(
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE",
        max_bytes=64 * 1024 * 1024,
    )
if document_cache_enabled:
    persistent_corpus_snapshot()
    persistent_runtime_blob(
        "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_HOST_FILE",
        max_bytes=512 * 1024 * 1024,
    )
    persistent_runtime_blob(
        "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE",
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
    FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED \
    FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED \
    FINANCE_ADAPTIVE_SEMANTIC_ENABLED \
    FINANCE_AGENT_LLM_MODE \
    LLM_PROVIDER \
    HCX_MODEL \
    HCX_TIMEOUT_SECONDS \
    CLOVASTUDIO_API_KEY \
    CLOVASTUDIO_API_KEY_HOST_FILE \
    CLOVASTUDIO_API_KEY_FILE \
    FINANCE_EXTERNAL_KNOWLEDGE_ENABLED \
    FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_HOST_FILE \
    FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE \
    FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256 \
    FINANCE_EXTERNAL_TEMPORAL_RELATION_HOST_FILE \
    FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE \
    FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256 \
    FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_HOST_FILE \
    FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE \
    FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_HOST_FILE \
    FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE \
    FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_HOST_FILE \
    FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE \
    FINANCE_EXTERNAL_DOCUMENT_CORPUS_HOST_DIR \
    FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT \
    NAVER_API_HUB_CLIENT_ID \
    NAVER_API_HUB_CLIENT_ID_HOST_FILE \
    NAVER_API_HUB_CLIENT_ID_FILE \
    NAVER_API_HUB_CLIENT_SECRET \
    NAVER_API_HUB_CLIENT_SECRET_HOST_FILE \
    NAVER_API_HUB_CLIENT_SECRET_FILE

# Compose gives inherited shell variables precedence over --env-file.  Remove
# every namespaced control variable, including future additions, so the private
# validated snapshot remains the only release configuration source.
for release_variable in ${!FINANCE_@} ${!HCX_@} ${!CLOVASTUDIO_@} ${!NAVER_@} ${!COMPOSE_@}; do
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
    python3 src/fastapi_backend/scripts/release_trust.py --env-file "$RELEASE_ENV_SNAPSHOT"
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
    python3 src/fastapi_backend/scripts/release_activation.py \
        --env-file "$RELEASE_ENV_SNAPSHOT" \
        -- \
        "${before_up[@]}" up "${after_up[@]}" \
        --no-build --force-recreate --wait
    exit $?
fi

compose_files=(
    -f docker-compose.yml
    -f src/fastapi_backend/docker-compose.release.yml
)
if [ -f "$RELEASE_ADAPTIVE_MARKER" ]; then
    compose_files+=( -f src/fastapi_backend/docker-compose.adaptive.yml )
fi
if [ -f "$RELEASE_DOCUMENT_CACHE_MARKER" ]; then
    compose_files+=( -f src/fastapi_backend/docker-compose.document-cache.yml )
fi
if [ -f "$RELEASE_LIVE_SEARCH_MARKER" ]; then
    compose_files+=( -f src/fastapi_backend/docker-compose.live-search.yml )
fi

docker compose \
    -p hyunholim-finance-agent \
    --env-file "$RELEASE_ENV_SNAPSHOT" \
    "${compose_files[@]}" \
    "$@"
