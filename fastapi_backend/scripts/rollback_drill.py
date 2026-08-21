from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any

_IMAGE_REFERENCE = re.compile(r"[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}")
_RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_PROJECT_NAME = re.compile(r"finance-agent-rollback-drill-[a-z0-9][a-z0-9-]{2,31}")
_PROTECTED_PROJECT = "hyunholim-finance-agent"
_BINDING_MOUNT = "/run/finance-release/deployment-binding.json"
_RELEASE_BACKEND_UID = 10001
_AUDIT_FILE_NAME = "events.jsonl"
_AUDIT_PROBE_TIMEOUT_SECONDS = 10.0
_MAX_AUDIT_PROBE_BYTES = 2 * 1024 * 1024
_MAX_AUDIT_EVENT_BYTES = 64 * 1024
_MAX_HCX_API_KEY_FILE_BYTES = 4096
_PROBE_REQUEST_ID = "rollback-drill-probe"
_PROBE_QUESTION = "매수 가능한 국내채권을 매수수익률 높은 순으로 1개 보여줘."
_PROBE_REQUEST_SHA256 = hashlib.sha256(_PROBE_REQUEST_ID.encode()).hexdigest()
_PROBE_QUESTION_SHA256 = hashlib.sha256(_PROBE_QUESTION.encode()).hexdigest()
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_AUDIT_REASON_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_AUDIT_SENSITIVE_REASON = re.compile(
    r"(?:question|prompt|answer|gold|expected|chain.?of.?thought|cot|reasoning|"
    r"authorization|cookie|secret|token|api.?key|database|sqlite|path|header|body)",
    re.IGNORECASE,
)
_AUDIT_STAGES = {
    "request",
    "route",
    "safety",
    "plan",
    "planning",
    "lexical",
    "dense",
    "schema_link_shadow",
    "hclx",
    "compiler",
    "authority",
    "execution",
    "oracle",
    "sql",
    "verifier",
    "renderer",
    "answer",
    "serialization",
}
_AUDIT_OUTCOMES = {
    "started",
    "succeeded",
    "clarified",
    "unsupported",
    "blocked",
    "timed_out",
    "failed",
}
_AUDIT_ROUTE_DISPOSITIONS = {"execute", "clarify", "unsupported"}
_AUDIT_INTERACTION_INTENTS = {
    "search",
    "detail",
    "compare",
    "aggregate",
    "explain",
    "clarify",
    "unsupported",
}
_AUDIT_PRODUCT_FAMILIES = {"bond", "domestic_etp", "overseas_etp", "fund"}
_AUDIT_EVENT_V12_FIELDS = frozenset(
    {
        "schema_version",
        "observed_at_utc",
        "stage",
        "outcome",
        "reason_code",
        "duration_ms",
        "request_id_sha256",
        "question_sha256",
        "invocation_id_sha256",
        "event_sequence",
        "route_disposition",
        "interaction_intent",
        "product_families",
        "agent_release_id_sha256",
        "agent_release_manifest_sha256",
        "deployment_binding_sha256",
        "release_context_sha256",
        "dataset_release_id_sha256",
        "approved_dataset_manifest_sha256",
        "database_manifest_sha256",
        "database_snapshot_sha256",
        "source_snapshot_sha256",
        "plan_sha256",
        "plan_bundle_sha256",
        "dataset_bundle_sha256",
        "model_revision_sha256",
        "model_snapshot_manifest_sha256",
        "index_manifest_sha256",
        "relation_set_sha256",
        "product_family_count",
        "candidate_count",
        "result_count",
        "evidence_count",
        "shadow_candidate_count",
        "product_id_sha256s",
        "evidence_id_sha256s",
    }
)
_AUDIT_OPTIONAL_SHA256_FIELDS = {
    "invocation_id_sha256",
    "agent_release_id_sha256",
    "agent_release_manifest_sha256",
    "deployment_binding_sha256",
    "release_context_sha256",
    "dataset_release_id_sha256",
    "approved_dataset_manifest_sha256",
    "database_manifest_sha256",
    "database_snapshot_sha256",
    "source_snapshot_sha256",
    "plan_sha256",
    "plan_bundle_sha256",
    "dataset_bundle_sha256",
    "model_revision_sha256",
    "model_snapshot_manifest_sha256",
    "index_manifest_sha256",
    "relation_set_sha256",
}
_DETERMINISTIC_PROBE_PREFIX = (
    ("request", "started", "received"),
    ("safety", "succeeded", "guard_allowed"),
    ("lexical", "succeeded", "lexical_completed"),
    ("planning", "succeeded", "policy_completed"),
    ("route", "succeeded", "routed_execute"),
)
_COMPILER_PROBE_PATH = (("compiler", "succeeded", "plan_compiled"),)
_QUERYPLAN_HCLX_PROBE_PATH = (("hclx", "succeeded", "provider_completed"),)
_EXECUTION_PROBE_PATH = (
    ("sql", "succeeded", "authority_connection_opened"),
    ("authority", "succeeded", "authority_granted"),
    ("sql", "succeeded", "oracle_connection_opened"),
    ("sql", "succeeded", "oracle_statements_completed"),
    ("sql", "succeeded", "parameterized_statement_completed"),
    ("oracle", "succeeded", "oracle_completed"),
    ("sql", "succeeded", "verifier_projection_connection_opened"),
    ("sql", "succeeded", "verifier_projection_fetched"),
    ("verifier", "succeeded", "verifier_rows_materialized"),
    ("verifier", "succeeded", "verifier_universe_loaded"),
    ("verifier", "succeeded", "pure_verification_passed"),
    ("verifier", "succeeded", "verification_passed"),
)
_RENDERER_PROBE_PATH = (("renderer", "succeeded", "rendering_completed"),)
_GROUNDED_ANSWER_HCLX_PROBE_PATH = (
    ("hclx", "succeeded", "generation_completed"),
    ("verifier", "succeeded", "composition_verified"),
)
_PROBE_SUFFIX = (
    ("answer", "succeeded", "execution_completed"),
    ("serialization", "succeeded", "citations_built"),
    ("serialization", "succeeded", "backend_dto_built"),
    ("serialization", "succeeded", "official_dto_built"),
    ("serialization", "succeeded", "http_response_serialized"),
    ("request", "succeeded", "response_completed"),
)
_EXPECTED_PROBE_AUDIT_PATH = (
    _DETERMINISTIC_PROBE_PREFIX
    + _COMPILER_PROBE_PATH
    + _EXECUTION_PROBE_PATH
    + _RENDERER_PROBE_PATH
    + _PROBE_SUFFIX
)
_EXPECTED_HCLX_ANSWER_PROBE_AUDIT_PATH = (
    _DETERMINISTIC_PROBE_PREFIX
    + _COMPILER_PROBE_PATH
    + _EXECUTION_PROBE_PATH
    + _RENDERER_PROBE_PATH
    + _GROUNDED_ANSWER_HCLX_PROBE_PATH
    + _PROBE_SUFFIX
)
_EXPECTED_HCLX_QUERYPLAN_ANSWER_PROBE_AUDIT_PATH = (
    _DETERMINISTIC_PROBE_PREFIX
    + _COMPILER_PROBE_PATH
    + _QUERYPLAN_HCLX_PROBE_PATH
    + _EXECUTION_PROBE_PATH
    + _RENDERER_PROBE_PATH
    + _GROUNDED_ANSWER_HCLX_PROBE_PATH
    + _PROBE_SUFFIX
)
_ANSWER_PROBE_MARKER = "FINANCE_ROLLBACK_PROBE_RESULT="
_ANSWER_PROBE = """
import json
import sys
import urllib.parse
import urllib.request

timeout_seconds = float(sys.argv[1])
expected_answer_mode = sys.argv[2]
query = urllib.parse.urlencode({
    "question_id": "rollback-drill-probe",
    "question": "매수 가능한 국내채권을 매수수익률 높은 순으로 1개 보여줘.",
})
request = urllib.request.Request(
    "http://127.0.0.1:8000/answer?" + query,
    method="GET",
)
with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
    body = json.load(response)
required_fields = {"question_id", "question", "retrieved_context", "think_trace", "answer"}
if set(body) != required_fields or any(not isinstance(body[field], str) for field in required_fields):
    raise SystemExit("representative official answer contract failed")
if (
    body["question_id"] != "rollback-drill-probe"
    or body["question"] != "매수 가능한 국내채권을 매수수익률 높은 순으로 1개 보여줘."
    or not body["retrieved_context"]
    or not body["answer"]
):
    raise SystemExit("representative official answer contract failed")
try:
    trace = json.loads(body["think_trace"])
    context = json.loads(body["retrieved_context"])
except (TypeError, ValueError):
    raise SystemExit("representative official answer contract failed") from None
if (
    not isinstance(trace, dict)
    or not isinstance(context, dict)
    or trace.get("status") != "success"
    or trace.get("intent") != "search"
    or trace.get("product_families") != ["bond"]
    or trace.get("answer_mode") != expected_answer_mode
    or trace.get("fallback_used") is not False
):
    raise SystemExit("representative official answer contract failed")
with urllib.request.urlopen(
    "http://127.0.0.1:8000/health",
    timeout=timeout_seconds,
) as response:
    health = json.load(response)
if health.get("status") != "ok" or health.get("audit_status") != "ok":
    raise SystemExit("representative health audit contract failed")
print("FINANCE_ROLLBACK_PROBE_RESULT=" + json.dumps({
    "answer_mode": trace["answer_mode"],
    "audit_status": health["audit_status"],
    "fallback_used": trace["fallback_used"],
    "intent": trace["intent"],
    "status": trace["status"],
}, separators=(",", ":")))
""".strip()
_ALLOWED_RELEASE_KEYS = {
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


class RollbackDrillError(RuntimeError):
    """A fail-closed rollback drill precondition or verification failure."""


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    env_file: Path
    environment: dict[str, str]
    binding_file: Path
    binding_sha256: str
    binding: dict[str, Any]
    hclx_secret_file: Path | None = field(default=None, repr=False)
    hclx_secret_fingerprint: tuple[int, ...] | None = field(default=None, repr=False)

    @property
    def release_id(self) -> str:
        return str(self.binding["release_id"])

    @property
    def image_reference(self) -> str:
        return str(self.binding["image_reference"])

    @property
    def data_volume(self) -> str:
        return self.environment["FINANCE_DATA_VOLUME_NAME"]

    @property
    def activation_generation(self) -> int:
        return int(self.binding["activation_generation"])

    @property
    def answer_provider(self) -> str:
        return self.environment.get("FINANCE_BACKEND_ANSWER_PROVIDER", "deterministic")

    @property
    def hclx_query_plan_enabled(self) -> bool:
        return (
            self.environment.get("FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED", "false").lower()
            == "true"
        )

    @property
    def uses_hclx(self) -> bool:
        return self.answer_provider == "hyperclova"

    @property
    def expected_answer_mode(self) -> str:
        return "llm_grounded" if self.uses_hclx else "deterministic"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackDrillError(f"duplicate JSON key in DeploymentBinding: {key}")
        result[key] = value
    return result


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_non_finite_json(value: str) -> None:
    raise RollbackDrillError(f"non-finite JSON number is forbidden: {value}")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_bounded_integer(value: object, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _require_audit_reader_identity() -> None:
    if os.geteuid() not in {0, _RELEASE_BACKEND_UID}:
        raise RollbackDrillError("rollback audit verification must run as root or UID 10001")


def _validate_audit_event_v12(event: dict[str, Any]) -> None:
    """Validate the complete serialized AuditEvent v1.2 wire contract.

    The rollback host intentionally cannot import the application package.  This
    closed, stdlib-only mirror therefore rejects omitted default fields as well
    as unknown fields; a real ``AuditEvent.model_dump_json()`` emits every key.
    """

    if set(event) != _AUDIT_EVENT_V12_FIELDS:
        raise RollbackDrillError("rollback audit event does not match AuditEvent v1.2")
    if event["schema_version"] != "1.2":
        raise RollbackDrillError("rollback audit event schema version is invalid")
    observed_at = event["observed_at_utc"]
    if not isinstance(observed_at, str):
        raise RollbackDrillError("rollback audit timestamp is invalid")
    try:
        parsed_at = datetime.fromisoformat(
            observed_at[:-1] + "+00:00" if observed_at.endswith("Z") else observed_at
        )
    except ValueError as error:
        raise RollbackDrillError("rollback audit timestamp is invalid") from error
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise RollbackDrillError("rollback audit timestamp is invalid")
    if event["stage"] not in _AUDIT_STAGES or event["outcome"] not in _AUDIT_OUTCOMES:
        raise RollbackDrillError("rollback audit stage or outcome is invalid")
    reason_code = event["reason_code"]
    if (
        not isinstance(reason_code, str)
        or _AUDIT_REASON_CODE.fullmatch(reason_code) is None
        or _AUDIT_SENSITIVE_REASON.search(reason_code) is not None
    ):
        raise RollbackDrillError("rollback audit reason code is invalid")
    duration_ms = event["duration_ms"]
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, (int, float))
        or not math.isfinite(float(duration_ms))
        or not 0 <= duration_ms <= 3_600_000
    ):
        raise RollbackDrillError("rollback audit duration is invalid")
    if not _is_sha256(event["request_id_sha256"]) or not _is_sha256(event["question_sha256"]):
        raise RollbackDrillError("rollback audit request linkage is invalid")
    for field_name in _AUDIT_OPTIONAL_SHA256_FIELDS:
        value = event[field_name]
        if value is not None and not _is_sha256(value):
            raise RollbackDrillError("rollback audit SHA-256 linkage is invalid")

    invocation = event["invocation_id_sha256"]
    sequence = event["event_sequence"]
    if (invocation is None) != (sequence is None) or (
        sequence is not None
        and not _is_bounded_integer(
            sequence,
            minimum=1,
            maximum=9_223_372_036_854_775_807,
        )
    ):
        raise RollbackDrillError("rollback audit invocation linkage is invalid")

    families = event["product_families"]
    if (
        not isinstance(families, list)
        or len(families) > 4
        or any(family not in _AUDIT_PRODUCT_FAMILIES for family in families)
        or len(families) != len(set(families))
    ):
        raise RollbackDrillError("rollback audit product families are invalid")
    if not _is_bounded_integer(event["product_family_count"], minimum=0, maximum=4) or event[
        "product_family_count"
    ] != len(families):
        raise RollbackDrillError("rollback audit product family count is invalid")

    disposition = event["route_disposition"]
    intent = event["interaction_intent"]
    if (disposition is None) != (intent is None):
        raise RollbackDrillError("rollback audit route linkage is incomplete")
    if disposition is not None and (
        disposition not in _AUDIT_ROUTE_DISPOSITIONS or intent not in _AUDIT_INTERACTION_INTENTS
    ):
        raise RollbackDrillError("rollback audit route linkage is invalid")
    if disposition == "execute" and (not families or intent in {"clarify", "unsupported"}):
        raise RollbackDrillError("rollback audit executable route is invalid")
    if disposition == "unsupported" and intent != "unsupported":
        raise RollbackDrillError("rollback audit unsupported route is invalid")
    if disposition != "execute" and event["plan_sha256"] is not None:
        raise RollbackDrillError("rollback audit non-executable route carries a plan")

    release_fields = (
        "agent_release_id_sha256",
        "agent_release_manifest_sha256",
        "deployment_binding_sha256",
        "release_context_sha256",
    )
    if any(event[field] is not None for field in release_fields) != all(
        event[field] is not None for field in release_fields
    ):
        raise RollbackDrillError("rollback audit release linkage is incomplete")
    dataset_fields = (
        "dataset_release_id_sha256",
        "approved_dataset_manifest_sha256",
        "database_manifest_sha256",
        "database_snapshot_sha256",
        "source_snapshot_sha256",
    )
    if any(event[field] is not None for field in dataset_fields) != all(
        event[field] is not None for field in dataset_fields
    ):
        raise RollbackDrillError("rollback audit dataset linkage is incomplete")

    count_limits = {
        "candidate_count": 1_000_000,
        "result_count": 100_000,
        "evidence_count": 100_000,
        "shadow_candidate_count": 100_000,
    }
    for field_name, maximum in count_limits.items():
        if not _is_bounded_integer(event[field_name], minimum=0, maximum=maximum):
            raise RollbackDrillError("rollback audit count is invalid")
    if event["result_count"] > event["candidate_count"]:
        raise RollbackDrillError("rollback audit result count exceeds candidates")

    linkage_fields = (
        ("product_id_sha256s", 100, "result_count"),
        ("evidence_id_sha256s", 2_000, "evidence_count"),
    )
    for field_name, maximum, count_field in linkage_fields:
        values = event[field_name]
        if (
            not isinstance(values, list)
            or len(values) > maximum
            or any(not _is_sha256(value) for value in values)
            or len(values) != len(set(values))
            or (values and len(values) != event[count_field])
        ):
            raise RollbackDrillError("rollback audit identifier linkage is invalid")


def _open_secure_audit_directory(audit_root: Path) -> int:
    """Open an absolute directory through a no-symlink component walk."""

    normalized = Path(os.path.abspath(audit_root))
    if not audit_root.is_absolute() or audit_root != normalized:
        raise RollbackDrillError("rollback audit directory path is not canonical")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open("/", flags)
        for component in audit_root.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _RELEASE_BACKEND_UID
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise RollbackDrillError(
                "rollback audit directory must be owned by UID 10001 and owner-only"
            )
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RollbackDrillError(
            "rollback audit directory is unavailable or uses a symlink"
        ) from error
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _audit_directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _require_current_audit_directory(audit_root: Path, descriptor: int) -> None:
    current_descriptor = _open_secure_audit_directory(audit_root)
    try:
        if _audit_directory_identity(current_descriptor) != _audit_directory_identity(descriptor):
            raise RollbackDrillError("rollback audit directory changed during verification")
    finally:
        os.close(current_descriptor)


def _secure_audit_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == _RELEASE_BACKEND_UID
        and metadata.st_nlink == 1
        and not metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    )


def _audit_file_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_regular_file(path: Path, *, read_only: bool) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise RollbackDrillError(f"release artifact path must be absolute: {path}")
    if resolved.is_symlink() or not resolved.is_file():
        raise RollbackDrillError(f"release artifact must be a regular non-symlink file: {path}")
    metadata = resolved.stat()
    if metadata.st_nlink != 1:
        raise RollbackDrillError(f"release artifact must not be hard-linked: {path}")
    if read_only and metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise RollbackDrillError(f"DeploymentBinding must be read-only: {path}")
    return resolved.resolve(strict=True)


def _load_environment(path: Path) -> tuple[Path, dict[str, str]]:
    env_file = _require_regular_file(path, read_only=False)
    environment: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RollbackDrillError(
                f"invalid release environment line {line_number} in {env_file}"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RollbackDrillError(f"invalid environment key at line {line_number}")
        if key in environment:
            raise RollbackDrillError(f"duplicate environment key: {key}")
        environment[key] = value.strip()
    if "CLOVASTUDIO_API_KEY" in environment:
        raise RollbackDrillError(
            "rollback drill forbids inline CLOVASTUDIO_API_KEY; use a secret file"
        )
    extra = sorted(set(environment) - _ALLOWED_RELEASE_KEYS)
    if extra:
        raise RollbackDrillError(
            "rollback drill environment contains unsupported settings: " + ", ".join(extra)
        )
    return env_file, environment


def _require_pattern(value: str | None, pattern: re.Pattern[str], name: str) -> str:
    if value is None or pattern.fullmatch(value) is None:
        raise RollbackDrillError(f"invalid or missing {name}")
    return value


def _secret_file_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_hclx_secret_file(path_value: str) -> tuple[Path, tuple[int, ...]]:
    secret = Path(path_value)
    if not secret.is_absolute() or secret != Path(os.path.abspath(secret)):
        raise RollbackDrillError("HyperCLOVA release secret path is invalid")
    try:
        metadata = secret.stat(follow_symlinks=False)
    except OSError:
        raise RollbackDrillError("HyperCLOVA release secret is unavailable") from None
    if (
        secret.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _RELEASE_BACKEND_UID
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not 0 < metadata.st_size <= _MAX_HCX_API_KEY_FILE_BYTES
    ):
        raise RollbackDrillError("HyperCLOVA release secret is not a secure regular file")
    return secret, _secret_file_fingerprint(metadata)


def _validate_provider_profile(
    environment: dict[str, str],
) -> tuple[Path | None, tuple[int, ...] | None]:
    answer_provider = environment.get("FINANCE_BACKEND_ANSWER_PROVIDER", "deterministic")
    hcx_query_plan = environment.get("FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED", "false").lower()
    if answer_provider not in {"deterministic", "hyperclova"}:
        raise RollbackDrillError("release answer provider is invalid")
    if hcx_query_plan not in {"true", "false"}:
        raise RollbackDrillError("release HCLX QueryPlan flag is invalid")
    if answer_provider == "deterministic" and hcx_query_plan == "true":
        raise RollbackDrillError(
            "rollback audit drill does not support the QueryPlan-only HCLX profile"
        )
    if answer_provider == "hyperclova":
        if (
            environment.get("FINANCE_AGENT_LLM_MODE") != environment.get("APP_ENV")
            or environment.get("LLM_PROVIDER") != "hyperclova"
            or environment.get("HCX_MODEL") != "HCX-007"
            or not environment.get("CLOVASTUDIO_API_KEY_HOST_FILE")
            or environment.get("CLOVASTUDIO_API_KEY_FILE") != "/run/secrets/clovastudio_api_key"
        ):
            raise RollbackDrillError("HyperCLOVA release provider profile is incomplete")
        return _validate_hclx_secret_file(environment["CLOVASTUDIO_API_KEY_HOST_FILE"])

    if environment.get("FINANCE_AGENT_LLM_MODE", "disabled") != "disabled":
        raise RollbackDrillError("deterministic release must disable LLM mode")
    if environment.get("LLM_PROVIDER", "disabled") != "disabled":
        raise RollbackDrillError("deterministic release must disable LLM provider")
    if any(
        environment.get(name)
        for name in (
            "CLOVASTUDIO_API_KEY_HOST_FILE",
            "CLOVASTUDIO_API_KEY_FILE",
            "HCX_MODEL",
        )
    ):
        raise RollbackDrillError("deterministic release must not configure HCLX credentials")
    return None, None


def _require_hclx_secret_current(target: ReleaseTarget) -> None:
    if not target.uses_hclx:
        if target.hclx_secret_file is not None or target.hclx_secret_fingerprint is not None:
            raise RollbackDrillError("deterministic rollback target retained HCLX secret state")
        return
    if target.hclx_secret_file is None or target.hclx_secret_fingerprint is None:
        raise RollbackDrillError("HyperCLOVA rollback target lacks verified secret state")
    observed_file, observed_fingerprint = _validate_hclx_secret_file(str(target.hclx_secret_file))
    if (
        observed_file != target.hclx_secret_file
        or observed_fingerprint != target.hclx_secret_fingerprint
    ):
        raise RollbackDrillError("HyperCLOVA release secret changed during rollback verification")


def _validate_audit_profile(environment: dict[str, str]) -> Path:
    if environment.get("WEB_CONCURRENCY") != "1":
        raise RollbackDrillError("rollback releases require WEB_CONCURRENCY=1")
    if environment.get("FINANCE_AUDIT_FSYNC_EACH_EVENT", "true").lower() != "true":
        raise RollbackDrillError("rollback releases require durable audit fsync")
    queue_capacity = environment.get("FINANCE_AUDIT_QUEUE_CAPACITY", "2048")
    if re.fullmatch(r"[0-9]+", queue_capacity) is None or not 1 <= int(queue_capacity) <= 100_000:
        raise RollbackDrillError("rollback audit queue capacity is invalid")
    try:
        shutdown_timeout = float(environment.get("FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS", "5"))
    except ValueError as error:
        raise RollbackDrillError("rollback audit shutdown timeout is invalid") from error
    if not math.isfinite(shutdown_timeout) or not 0 < shutdown_timeout <= 60:
        raise RollbackDrillError("rollback audit shutdown timeout is invalid")
    audit_root = Path(environment["FINANCE_AUDIT_HOST_DIR"])
    descriptor = _open_secure_audit_directory(audit_root)
    try:
        _require_current_audit_directory(audit_root, descriptor)
    finally:
        os.close(descriptor)
    return audit_root


def _validate_timeout(
    environment: dict[str, str],
    name: str,
    *,
    default: float,
    maximum: float,
    maximum_inclusive: bool,
) -> float:
    try:
        value = float(environment.get(name, str(default)))
    except ValueError as error:
        raise RollbackDrillError(f"{name} is invalid") from error
    maximum_valid = value <= maximum if maximum_inclusive else value < maximum
    if not math.isfinite(value) or value <= 0 or not maximum_valid:
        raise RollbackDrillError(f"{name} is invalid")
    return value


def _official_probe_timeout_seconds(target: ReleaseTarget) -> float:
    configured = _validate_timeout(
        target.environment,
        "OFFICIAL_ANSWER_TIMEOUT_SECONDS",
        default=270.0,
        maximum=300.0,
        maximum_inclusive=False,
    )
    return min(299.0, configured + 1.0)


def _load_target(path: Path) -> ReleaseTarget:
    env_file, environment = _load_environment(path)
    required = {
        "APP_ENV",
        "FINANCE_IMAGE_REFERENCE",
        "FINANCE_SOURCE_COMMIT",
        "FINANCE_RUNTIME_PLATFORM",
        "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
        "FINANCE_DEPLOYMENT_BINDING_SHA256",
        "FINANCE_DATA_VOLUME_NAME",
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
        "FINANCE_AUDIT_HOST_DIR",
        "WEB_CONCURRENCY",
    }
    missing = sorted(name for name in required if not environment.get(name))
    if missing:
        raise RollbackDrillError("missing release settings: " + ", ".join(missing))
    if environment["APP_ENV"] not in {"evaluation", "production"}:
        raise RollbackDrillError("APP_ENV must be evaluation or production")
    if environment["FINANCE_RUNTIME_PLATFORM"] != "linux/amd64":
        raise RollbackDrillError("official rollback platform must be linux/amd64")
    hclx_secret_file, hclx_secret_fingerprint = _validate_provider_profile(environment)
    _validate_timeout(
        environment,
        "OFFICIAL_ANSWER_TIMEOUT_SECONDS",
        default=270.0,
        maximum=300.0,
        maximum_inclusive=False,
    )
    if hclx_secret_file is not None:
        _validate_timeout(
            environment,
            "HCX_TIMEOUT_SECONDS",
            default=45.0,
            maximum=300.0,
            maximum_inclusive=True,
        )
    environment["FINANCE_AUDIT_HOST_DIR"] = str(_validate_audit_profile(environment))
    _require_pattern(
        environment["FINANCE_IMAGE_REFERENCE"],
        _IMAGE_REFERENCE,
        "FINANCE_IMAGE_REFERENCE",
    )
    _require_pattern(
        environment["FINANCE_SOURCE_COMMIT"],
        _SOURCE_COMMIT,
        "FINANCE_SOURCE_COMMIT",
    )
    _require_pattern(
        environment["FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256"],
        _SHA256,
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
    )
    binding_sha256 = _require_pattern(
        environment["FINANCE_DEPLOYMENT_BINDING_SHA256"],
        _SHA256,
        "FINANCE_DEPLOYMENT_BINDING_SHA256",
    )
    binding_file = _require_regular_file(
        Path(environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"]),
        read_only=True,
    )
    binding_data = binding_file.read_bytes()
    if hashlib.sha256(binding_data).hexdigest() != binding_sha256:
        raise RollbackDrillError("DeploymentBinding differs from its trusted SHA-256")
    try:
        binding = json.loads(binding_data, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RollbackDrillError("DeploymentBinding is not strict JSON") from error
    if not isinstance(binding, dict):
        raise RollbackDrillError("DeploymentBinding root must be an object")
    canonical = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if canonical != binding_data:
        raise RollbackDrillError("DeploymentBinding must use canonical JSON encoding")

    release_id = _require_pattern(str(binding.get("release_id", "")), _RELEASE_ID, "release_id")
    manifest_sha256 = _require_pattern(
        str(binding.get("release_manifest_sha256", "")),
        _SHA256,
        "release_manifest_sha256",
    )
    image_reference = _require_pattern(
        str(binding.get("image_reference", "")),
        _IMAGE_REFERENCE,
        "binding image_reference",
    )
    source_commit = _require_pattern(
        str(binding.get("source_commit", "")),
        _SOURCE_COMMIT,
        "binding source_commit",
    )
    generation = binding.get("activation_generation")
    if type(generation) is not int or generation < 1:
        raise RollbackDrillError("activation_generation must be a positive integer")
    if not isinstance(binding.get("rollback"), dict):
        raise RollbackDrillError("DeploymentBinding rollback must be an object")
    if binding.get("environment") != environment["APP_ENV"]:
        raise RollbackDrillError("DeploymentBinding and APP_ENV differ")
    if binding.get("platform") != environment["FINANCE_RUNTIME_PLATFORM"]:
        raise RollbackDrillError("DeploymentBinding and runtime platform differ")
    if image_reference != environment["FINANCE_IMAGE_REFERENCE"]:
        raise RollbackDrillError("DeploymentBinding and image reference differ")
    if source_commit != environment["FINANCE_SOURCE_COMMIT"]:
        raise RollbackDrillError("DeploymentBinding and source commit differ")
    expected_volume = f"finance-data-{release_id}-{manifest_sha256[:12]}"
    if environment["FINANCE_DATA_VOLUME_NAME"] != expected_volume:
        raise RollbackDrillError(f"release data volume must be named {expected_volume}")
    return ReleaseTarget(
        env_file=env_file,
        environment=environment,
        binding_file=binding_file,
        binding_sha256=binding_sha256,
        binding=binding,
        hclx_secret_file=hclx_secret_file,
        hclx_secret_fingerprint=hclx_secret_fingerprint,
    )


def _verify_chain(previous: ReleaseTarget, current: ReleaseTarget) -> None:
    if previous.release_id == current.release_id:
        raise RollbackDrillError("rollback releases must have distinct release IDs")
    if previous.image_reference == current.image_reference:
        raise RollbackDrillError("rollback releases must have distinct image digests")
    if previous.data_volume == current.data_volume:
        raise RollbackDrillError("rollback releases must have distinct data volumes")
    if previous.binding_sha256 == current.binding_sha256:
        raise RollbackDrillError("rollback releases must have distinct Binding files")
    if previous.binding["environment"] != current.binding["environment"]:
        raise RollbackDrillError("rollback releases must use the same environment")
    if previous.binding["platform"] != current.binding["platform"]:
        raise RollbackDrillError("rollback releases must use the same platform")
    if current.activation_generation != previous.activation_generation + 1:
        raise RollbackDrillError("current generation must immediately follow the previous one")
    if (
        previous.environment["FINANCE_AUDIT_HOST_DIR"]
        != current.environment["FINANCE_AUDIT_HOST_DIR"]
    ):
        raise RollbackDrillError("rollback releases must preserve one append-only audit directory")
    rollback = current.binding["rollback"]
    expected = {
        "mode": "pinned_previous_release",
        "target_release_id": previous.release_id,
        "target_manifest_sha256": previous.binding["release_manifest_sha256"],
        "target_binding_sha256": previous.binding_sha256,
        "target_image_reference": previous.image_reference,
        "target_activation_generation": previous.activation_generation,
        "target_environment": previous.binding["environment"],
        "target_platform": previous.binding["platform"],
    }
    if rollback != expected:
        raise RollbackDrillError("current Binding does not pin the exact previous release")


def _expected_probe_audit_path(target: ReleaseTarget) -> tuple[tuple[str, str, str], ...]:
    if not target.uses_hclx:
        return _EXPECTED_PROBE_AUDIT_PATH
    if target.hclx_query_plan_enabled:
        return _EXPECTED_HCLX_QUERYPLAN_ANSWER_PROBE_AUDIT_PATH
    return _EXPECTED_HCLX_ANSWER_PROBE_AUDIT_PATH


def _snapshot_target(target: ReleaseTarget, root: Path, name: str) -> ReleaseTarget:
    target_root = root / name
    # The Binding is mounted into a container running as UID 10001.  Every host
    # directory in the bind source must therefore be traversable, while listing
    # stays disabled and the environment file remains host-only (0600).
    target_root.mkdir(mode=0o711)
    target_root.chmod(0o711)
    binding_file = target_root / "deployment-binding.json"
    binding_data = (
        json.dumps(target.binding, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = os.open(
        binding_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        # Shared research hosts commonly run with umask 0077.  The container's
        # fixed UID 10001 still needs read-only access to this bind-mounted file.
        os.fchmod(descriptor, 0o444)
        view = memoryview(binding_data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RollbackDrillError("cannot create rollback Binding snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    environment = dict(target.environment)
    environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"] = str(binding_file)
    env_file = target_root / "release.env"
    descriptor = os.open(
        env_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        for key in sorted(environment):
            value = environment[key]
            if any(character in value for character in "'$\r\n"):
                raise RollbackDrillError("rollback environment contains an unsafe value")
            payload = f"{key}={value}\n".encode()
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise RollbackDrillError("cannot create rollback environment snapshot")
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ReleaseTarget(
        env_file=env_file,
        environment=environment,
        binding_file=binding_file,
        binding_sha256=target.binding_sha256,
        binding=target.binding,
        hclx_secret_file=target.hclx_secret_file,
        hclx_secret_fingerprint=target.hclx_secret_fingerprint,
    )


class DockerClient:
    def __init__(self, *, root: Path, project: str, port: int) -> None:
        self.root = root
        self.project = project
        self.port = port
        self._all_release_keys: set[str] = set()
        self.audit_observations: list[dict[str, Any]] = []

    def register(self, *targets: ReleaseTarget) -> None:
        for target in targets:
            self._all_release_keys.update(target.environment)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if (
                key in self._all_release_keys
                or key.startswith(("FINANCE_", "COMPOSE_", "HCX_", "CLOVASTUDIO_"))
                or key
                in {
                    "APP_ENV",
                    "LLM_PROVIDER",
                    "BACKEND_BIND_ADDRESS",
                    "BACKEND_PORT",
                    "GITHUB_TOKEN",
                    "NCP_REGISTRY_PASSWORD",
                    "NCP_REGISTRY_USERNAME",
                }
            ):
                environment.pop(key, None)
        environment["BACKEND_BIND_ADDRESS"] = "127.0.0.1"
        environment["BACKEND_PORT"] = str(self.port)
        return environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        allow_failure: bool = False,
        timeout_seconds: float = 240.0,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["docker", *arguments],
            cwd=self.root,
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0 and not allow_failure:
            operation = " ".join(arguments[:3])
            raise RollbackDrillError(
                f"Docker operation failed closed ({completed.returncode}): {operation}"
            )
        return completed

    def compose(
        self,
        target: ReleaseTarget,
        arguments: Sequence[str],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            [
                "compose",
                "-p",
                self.project,
                "--env-file",
                str(target.env_file),
                "-f",
                "docker-compose.yml",
                "-f",
                "fastapi_backend/docker-compose.release.yml",
                *arguments,
            ],
            allow_failure=allow_failure,
        )

    def reject_existing_project(self) -> None:
        containers = self.run(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ]
        ).stdout.strip()
        if containers:
            raise RollbackDrillError("isolated rollback drill project already has containers")
        network = self.run(
            ["network", "inspect", f"{self.project}_default"],
            allow_failure=True,
        )
        if network.returncode == 0:
            raise RollbackDrillError("isolated rollback drill project network already exists")

    def require_artifacts(self, target: ReleaseTarget) -> None:
        _require_hclx_secret_current(target)
        trust = subprocess.run(
            [
                sys.executable,
                str(self.root / "fastapi_backend/scripts/release_trust.py"),
                "--env-file",
                str(target.env_file),
            ],
            cwd=self.root,
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if trust.returncode != 0:
            raise RollbackDrillError("release trust verification failed before rollback")
        self.run(["image", "inspect", target.image_reference])
        self.run(["volume", "inspect", target.data_volume])
        self.compose(target, ["config", "--quiet"])
        _require_hclx_secret_current(target)

    def activate_and_verify(self, target: ReleaseTarget) -> None:
        _require_hclx_secret_current(target)
        audit_checkpoint = self._audit_checkpoint(target)
        self.compose(
            target,
            ["up", "--detach", "--wait", "--no-build", "--force-recreate"],
        )
        container_id = self.compose(target, ["ps", "--quiet", "backend"]).stdout.strip()
        if not container_id or "\n" in container_id:
            raise RollbackDrillError("rollback drill did not resolve exactly one backend container")
        health = self.run(
            ["inspect", "--format", "{{.State.Health.Status}}", container_id]
        ).stdout.strip()
        image = self.run(["inspect", "--format", "{{.Config.Image}}", container_id]).stdout.strip()
        volume = self.run(
            [
                "inspect",
                "--format",
                '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}',
                container_id,
            ]
        ).stdout.strip()
        binding = self.run(
            [
                "inspect",
                "--format",
                (
                    "{{range .Mounts}}{{if eq .Destination "
                    f'"{_BINDING_MOUNT}"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}'
                ),
                container_id,
            ]
        ).stdout.strip()
        if health != "healthy":
            raise RollbackDrillError(f"release {target.release_id} did not become healthy")
        if image != target.image_reference:
            raise RollbackDrillError("active container image differs from DeploymentBinding")
        if volume != target.data_volume:
            raise RollbackDrillError("active container DB volume differs from the release volume")
        try:
            observed_binding = Path(binding).resolve(strict=True)
        except OSError as error:
            raise RollbackDrillError("active Binding mount source is not resolvable") from error
        if observed_binding != target.binding_file:
            raise RollbackDrillError("active container Binding mount differs from the release")
        probe_timeout = _official_probe_timeout_seconds(target)
        probe = self.run(
            [
                "exec",
                container_id,
                "python",
                "-c",
                _ANSWER_PROBE,
                str(probe_timeout),
                target.expected_answer_mode,
            ],
            timeout_seconds=min(300.0, probe_timeout + 1.0),
        ).stdout
        result_lines = [
            line.removeprefix(_ANSWER_PROBE_MARKER)
            for line in probe.splitlines()
            if line.startswith(_ANSWER_PROBE_MARKER)
        ]
        if len(result_lines) != 1:
            raise RollbackDrillError(
                "representative /answer probe returned an ambiguous result marker"
            )
        try:
            probe_result = json.loads(result_lines[0], object_pairs_hook=_strict_object)
        except json.JSONDecodeError as error:
            raise RollbackDrillError(
                "representative /answer probe returned invalid JSON"
            ) from error
        expected_probe_result = {
            "answer_mode": target.expected_answer_mode,
            "audit_status": "ok",
            "fallback_used": False,
            "intent": "search",
            "status": "success",
        }
        if probe_result != expected_probe_result:
            raise RollbackDrillError("representative /answer probe failed")
        self.audit_observations.append(self._wait_for_audit_chain(target, audit_checkpoint))
        _require_hclx_secret_current(target)

    @staticmethod
    def _audit_checkpoint(target: ReleaseTarget) -> tuple[tuple[int, int] | None, int]:
        _require_audit_reader_identity()
        audit_root = Path(target.environment["FINANCE_AUDIT_HOST_DIR"])
        directory_descriptor = _open_secure_audit_directory(audit_root)
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(_AUDIT_FILE_NAME, flags, dir_fd=directory_descriptor)
            except FileNotFoundError:
                _require_current_audit_directory(audit_root, directory_descriptor)
                return None, 0
            metadata = os.fstat(descriptor)
            current = os.stat(
                _AUDIT_FILE_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not _secure_audit_file(metadata)
                or not _secure_audit_file(current)
                or _audit_file_fingerprint(metadata) != _audit_file_fingerprint(current)
            ):
                raise RollbackDrillError("rollback audit file is not a secure regular file")
            _require_current_audit_directory(audit_root, directory_descriptor)
            return (metadata.st_dev, metadata.st_ino), metadata.st_size
        except OSError as error:
            raise RollbackDrillError("rollback audit file is unavailable") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory_descriptor)

    @staticmethod
    def _read_audit_append(
        target: ReleaseTarget,
        checkpoint: tuple[tuple[int, int] | None, int],
    ) -> bytes:
        _require_audit_reader_identity()
        expected_identity, start_offset = checkpoint
        audit_root = Path(target.environment["FINANCE_AUDIT_HOST_DIR"])
        directory_descriptor = _open_secure_audit_directory(audit_root)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(_AUDIT_FILE_NAME, flags, dir_fd=directory_descriptor)
        except OSError as error:
            os.close(directory_descriptor)
            raise RollbackDrillError("rollback audit file was not created") from error
        try:
            metadata = os.fstat(descriptor)
            current = os.stat(
                _AUDIT_FILE_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                not _secure_audit_file(metadata)
                or not _secure_audit_file(current)
                or _audit_file_fingerprint(metadata) != _audit_file_fingerprint(current)
                or (expected_identity is not None and identity != expected_identity)
                or metadata.st_size <= start_offset
                or metadata.st_size - start_offset > _MAX_AUDIT_PROBE_BYTES
            ):
                raise RollbackDrillError("rollback audit append boundary is invalid")
            appended = os.pread(descriptor, metadata.st_size - start_offset, start_offset)
            after = os.fstat(descriptor)
            try:
                current_after = os.stat(
                    _AUDIT_FILE_NAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                _require_current_audit_directory(audit_root, directory_descriptor)
            except OSError as error:
                raise RollbackDrillError(
                    "rollback audit file changed while it was verified"
                ) from error
            if (
                len(appended) != metadata.st_size - start_offset
                or _audit_file_fingerprint(after) != _audit_file_fingerprint(metadata)
                or _audit_file_fingerprint(after) != _audit_file_fingerprint(current_after)
                or not _secure_audit_file(after)
                or not _secure_audit_file(current_after)
            ):
                raise RollbackDrillError("rollback audit file changed while it was verified")
            return appended
        finally:
            os.close(descriptor)
            os.close(directory_descriptor)

    @classmethod
    def _wait_for_audit_chain(
        cls,
        target: ReleaseTarget,
        checkpoint: tuple[tuple[int, int] | None, int],
    ) -> dict[str, Any]:
        deadline = monotonic() + _AUDIT_PROBE_TIMEOUT_SECONDS
        last_error: RollbackDrillError | None = None
        while monotonic() < deadline:
            try:
                appended = cls._read_audit_append(target, checkpoint)
                return cls._verify_audit_chain(target, appended)
            except RollbackDrillError as error:
                last_error = error
                sleep(0.05)
        raise RollbackDrillError(
            "rollback audit chain did not become durable in time"
        ) from last_error

    @staticmethod
    def _verify_audit_chain(target: ReleaseTarget, appended: bytes) -> dict[str, Any]:
        if not appended.endswith(b"\n"):
            raise RollbackDrillError("rollback audit append has a partial final record")
        try:
            events = [
                json.loads(
                    line,
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_non_finite_json,
                )
                for line in appended.splitlines()
            ]
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RollbackDrillError("rollback audit append is not valid JSONL") from error
        if not events or any(not isinstance(event, dict) for event in events):
            raise RollbackDrillError("rollback audit append must contain JSON objects")
        if any(len(line) + 1 > _MAX_AUDIT_EVENT_BYTES for line in appended.splitlines()):
            raise RollbackDrillError("rollback audit event exceeds the AuditEvent size limit")
        for event in events:
            _validate_audit_event_v12(event)
        answer_events = [
            event
            for event in events
            if event.get("stage") == "answer"
            and event.get("outcome") == "succeeded"
            and event.get("request_id_sha256") == _PROBE_REQUEST_SHA256
        ]
        if len(answer_events) != 1:
            raise RollbackDrillError("rollback audit append lacks one successful answer event")
        answer = answer_events[0]
        invocation = answer.get("invocation_id_sha256")
        if not isinstance(invocation, str) or _SHA256.fullmatch(invocation) is None:
            raise RollbackDrillError("rollback audit invocation identity is invalid")
        chain = [event for event in events if event.get("invocation_id_sha256") == invocation]
        sequences = [event.get("event_sequence") for event in chain]
        if sequences != list(range(1, len(chain) + 1)):
            raise RollbackDrillError("rollback audit invocation sequence is not contiguous")
        if (
            chain[0].get("request_id_sha256") != _EMPTY_SHA256
            or chain[0].get("question_sha256") != _EMPTY_SHA256
            or any(
                event.get("request_id_sha256") != _PROBE_REQUEST_SHA256
                or event.get("question_sha256") != _PROBE_QUESTION_SHA256
                for event in chain[1:]
            )
        ):
            raise RollbackDrillError("rollback audit invocation is not linked to the probe")
        observed_path = tuple(
            (event["stage"], event["outcome"], event["reason_code"]) for event in chain
        )
        if observed_path != _expected_probe_audit_path(target):
            raise RollbackDrillError(
                "rollback audit invocation path differs from its frozen provider profile"
            )
        expected_release_id = hashlib.sha256(target.release_id.encode()).hexdigest()
        expected_release_context = _canonical_sha256(
            {
                "release_id": target.release_id,
                "manifest_file_sha256": target.binding["release_manifest_sha256"],
                "binding_file_sha256": target.binding_sha256,
                "image_reference": target.image_reference,
                "activation_generation": target.activation_generation,
            }
        )
        for event in events:
            if (
                event.get("agent_release_id_sha256") != expected_release_id
                or event.get("agent_release_manifest_sha256")
                != target.binding["release_manifest_sha256"]
                or event.get("deployment_binding_sha256") != target.binding_sha256
                or event.get("release_context_sha256") != expected_release_context
            ):
                raise RollbackDrillError("rollback audit release linkage is incomplete")
        if any(
            not isinstance(answer.get(field), str) or _SHA256.fullmatch(answer[field]) is None
            for field in (
                "dataset_release_id_sha256",
                "approved_dataset_manifest_sha256",
                "database_manifest_sha256",
                "database_snapshot_sha256",
                "source_snapshot_sha256",
                "plan_sha256",
            )
        ):
            raise RollbackDrillError("rollback audit answer lacks dataset or plan linkage")
        if (
            answer["route_disposition"] != "execute"
            or answer["interaction_intent"] != "search"
            or answer["product_families"] != ["bond"]
            or answer["product_family_count"] != 1
            or answer["candidate_count"] < 1
            or answer["result_count"] != 1
            or answer["evidence_count"] < 1
            or len(answer["product_id_sha256s"]) != 1
            or len(answer["evidence_id_sha256s"]) != answer["evidence_count"]
        ):
            raise RollbackDrillError("rollback audit answer semantics are incomplete")
        return {
            "release_id": target.release_id,
            "event_count": len(chain),
            "invocation_id_sha256": invocation,
            "terminal_sequence": sequences[-1],
        }

    def stop_isolated_project(self, target: ReleaseTarget) -> None:
        down = self.compose(target, ["down"], allow_failure=True)
        containers = self.run(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ]
        ).stdout.strip()
        network = self.run(
            ["network", "inspect", f"{self.project}_default"],
            allow_failure=True,
        )
        if down.returncode != 0 or containers or network.returncode == 0:
            raise RollbackDrillError("isolated rollback drill cleanup was incomplete")


def _short_digest(image_reference: str) -> str:
    return image_reference.rsplit("sha256:", 1)[1][:12]


def _result(
    *,
    mode: str,
    project: str,
    port: int,
    previous: ReleaseTarget,
    current: ReleaseTarget,
    stopped: bool,
    audit_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "verified" if mode == "execute" else "validated",
        "mode": mode,
        "isolated_project": project,
        "bind_address": "127.0.0.1",
        "port": port,
        "activation_sequence": [
            previous.release_id,
            current.release_id,
            previous.release_id,
        ],
        "previous": {
            "release_id": previous.release_id,
            "generation": previous.activation_generation,
            "image_digest_prefix": _short_digest(previous.image_reference),
            "data_volume": previous.data_volume,
        },
        "current": {
            "release_id": current.release_id,
            "generation": current.activation_generation,
            "image_digest_prefix": _short_digest(current.image_reference),
            "data_volume": current.data_volume,
        },
        "artifacts_preserved": mode == "execute",
        "containers_stopped_after_verification": stopped,
        "audit_chain_verified": mode == "execute" and len(audit_observations or []) == 3,
        "audit_observations": audit_observations or [],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed N-1 -> N -> N-1 release rollback drill",
    )
    parser.add_argument("--previous-env", type=Path, required=True)
    parser.add_argument("--current-env", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the isolated Docker drill; the default validates and prints the plan only.",
    )
    parser.add_argument(
        "--leave-running",
        action="store_true",
        help="Leave the verified N-1 container running; valid only with --execute.",
    )
    parser.add_argument(
        "--allow-billable-hclx",
        action="store_true",
        help=(
            "Explicitly authorize billable HCLX probes during --execute; dry-run never calls HCLX."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if _PROJECT_NAME.fullmatch(arguments.project_name) is None:
            raise RollbackDrillError(
                "project name must use finance-agent-rollback-drill-<unique-lowercase-suffix>"
            )
        if arguments.project_name == _PROTECTED_PROJECT:
            raise RollbackDrillError("the live finance project is protected from rollback drills")
        if not 1024 <= arguments.port <= 65535:
            raise RollbackDrillError("rollback drill port must be between 1024 and 65535")
        if arguments.leave_running:
            raise RollbackDrillError(
                "--leave-running is incompatible with immutable rollback snapshots"
            )
        if arguments.allow_billable_hclx and not arguments.execute:
            raise RollbackDrillError("--allow-billable-hclx is valid only with --execute")
        if arguments.execute:
            _require_audit_reader_identity()
        previous = _load_target(arguments.previous_env)
        current = _load_target(arguments.current_env)
        _verify_chain(previous, current)
        uses_hclx = previous.uses_hclx or current.uses_hclx
        if arguments.execute and uses_hclx and not arguments.allow_billable_hclx:
            raise RollbackDrillError(
                "HCLX rollback execution requires explicit billable-call authorization"
            )
        if arguments.allow_billable_hclx and not uses_hclx:
            raise RollbackDrillError(
                "billable HCLX authorization is invalid for deterministic rollback targets"
            )
        if not arguments.execute:
            print(
                json.dumps(
                    _result(
                        mode="dry_run",
                        project=arguments.project_name,
                        port=arguments.port,
                        previous=previous,
                        current=current,
                        stopped=False,
                        audit_observations=[],
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="finance-agent-rollback-snapshot-") as temporary:
            snapshot_root = Path(temporary)
            # 0711 lets the non-root container traverse the bind source without
            # exposing a directory listing of immutable control files.
            snapshot_root.chmod(0o711)
            previous = _snapshot_target(previous, snapshot_root, "previous")
            current = _snapshot_target(current, snapshot_root, "current")
            docker = DockerClient(root=root, project=arguments.project_name, port=arguments.port)
            docker.register(previous, current)
            docker.reject_existing_project()
            docker.require_artifacts(previous)
            docker.require_artifacts(current)
            activated = False
            try:
                activated = True
                docker.activate_and_verify(previous)
                docker.activate_and_verify(current)
                docker.run(["image", "inspect", previous.image_reference])
                docker.run(["volume", "inspect", previous.data_volume])
                docker.activate_and_verify(previous)
                docker.run(["image", "inspect", current.image_reference])
                docker.run(["volume", "inspect", current.data_volume])
            finally:
                if activated:
                    docker.stop_isolated_project(previous)
        print(
            json.dumps(
                _result(
                    mode="execute",
                    project=arguments.project_name,
                    port=arguments.port,
                    previous=previous,
                    current=current,
                    stopped=True,
                    audit_observations=docker.audit_observations,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RollbackDrillError, subprocess.TimeoutExpired) as error:
        print(f"rollback drill failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
