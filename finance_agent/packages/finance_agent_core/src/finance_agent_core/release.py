from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from finance_agent_core import __version__
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan, load_hcx_queryplan_schema
from finance_agent_core.ontology import (
    ONTOLOGY_RENDERER_VERSION,
    ontology_bundle_sha256,
)
from finance_agent_core.storage.approval import load_approved_dataset_manifest

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SOURCE_COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_RELEASE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{7,127}$"
_IMAGE_REFERENCE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$"
_MAX_RELEASE_FILE_BYTES = 2 * 1024 * 1024
_RUNTIME_SUFFIXES = frozenset({".py", ".json", ".yaml", ".yml"})


class AgentReleaseCode(StrEnum):
    FILE_UNAVAILABLE = "file_unavailable"
    UNSAFE_FILE = "unsafe_file"
    INVALID_JSON = "invalid_json"
    BINDING_HASH_MISMATCH = "binding_hash_mismatch"
    MANIFEST_HASH_MISMATCH = "manifest_hash_mismatch"
    RELEASE_MISMATCH = "release_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    STALE_RELEASE = "stale_release"


class AgentReleaseError(RuntimeError):
    """Stable fail-closed error for release assembly and request binding."""

    def __init__(self, code: AgentReleaseCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactFingerprint(ReleaseModel):
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)


class QueryPlanRelease(ReleaseModel):
    schema_version: Literal["1.0"] = "1.0"
    schema_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    python_contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    hcx_response_schema_sha256: str = Field(pattern=_SHA256_PATTERN)


class OntologyRelease(ReleaseModel):
    renderer_version: Literal["registry-derived-turtle-v1"] = ONTOLOGY_RENDERER_VERSION
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)


class DatasetSnapshotRelease(ReleaseModel):
    source_id: str = Field(pattern=r"^[A-Z0-9]{5,32}$")
    source_snapshot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    manifest_schema_version: Literal["1.0", "1.1"]
    data_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_sha256: str = Field(pattern=_SHA256_PATTERN)


class ApprovedDatasetRelease(ReleaseModel):
    release_id: str = Field(pattern=_RELEASE_ID_PATTERN)
    registry_schema_version: str = Field(min_length=1, max_length=32)
    manifest: ArtifactFingerprint
    snapshots: dict[
        Literal["bond", "domestic_etp", "overseas_etp", "fund"],
        DatasetSnapshotRelease,
    ]

    @model_validator(mode="after")
    def require_all_product_families(self) -> ApprovedDatasetRelease:
        expected = {"bond", "domestic_etp", "overseas_etp", "fund"}
        if set(self.snapshots) != expected:
            raise ValueError("release datasets must contain exactly the four product families")
        return self


class PromptRelease(ReleaseModel):
    queryplan_bundle_version: Literal["queryplan-prompt-bundle-v1"] = "queryplan-prompt-bundle-v1"
    queryplan_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    answer_bundle_version: Literal["grounded-answer-prompt-bundle-v1"] = (
        "grounded-answer-prompt-bundle-v1"
    )
    answer_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    generation_contract_version: Literal["hcx-generation-contract-v1"] = (
        "hcx-generation-contract-v1"
    )
    generation_contract_sha256: str = Field(pattern=_SHA256_PATTERN)


class ModelRelease(ReleaseModel):
    provider: Literal["disabled", "hyperclova"]
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    revision_status: Literal["not_used", "provider_revision_not_exposed"]
    queryplan_operation_enabled: StrictBool
    grounded_answer_operation_enabled: StrictBool

    @model_validator(mode="after")
    def validate_provider_profile(self) -> ModelRelease:
        enabled = self.queryplan_operation_enabled or self.grounded_answer_operation_enabled
        if self.provider == "disabled":
            if enabled or self.model_id is not None or self.revision_status != "not_used":
                raise ValueError("disabled model profile cannot expose model operations")
            return self
        if not enabled or self.model_id != "HCX-007":
            raise ValueError("HyperCLOVA release requires an enabled HCX-007 operation")
        if self.revision_status != "provider_revision_not_exposed":
            raise ValueError("HCX release must state that an immutable revision is unavailable")
        return self


class RetrievalRelease(ReleaseModel):
    schema_dense: Literal["disabled_offline_only"] = "disabled_offline_only"
    schema_dense_manifest_sha256: None = None
    embedding_model_revision: None = None
    product_dense: Literal["disabled_not_implemented"] = "disabled_not_implemented"
    reranker: Literal["disabled_not_implemented"] = "disabled_not_implemented"
    document_bm25: Literal["disabled_no_approved_corpus"] = "disabled_no_approved_corpus"


class ExecutionRelease(ReleaseModel):
    plan_authority_version: Literal["plan-authority-v1"] = "plan-authority-v1"
    planning_policy_version: Literal["adaptive-shadow-v1"] = "adaptive-shadow-v1"
    compiler_versions: dict[
        Literal[
            "server_queryplan_compiler",
            "grounded_plan_gate",
            "legacy_provider",
            "internal_evaluation",
        ],
        str,
    ]
    verifier_version: Literal["result-verifier-v1"] = "result-verifier-v1"
    core_version: str = Field(min_length=1, max_length=32)
    backend_version: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_all_compilers(self) -> ExecutionRelease:
        expected = {
            "server_queryplan_compiler",
            "grounded_plan_gate",
            "legacy_provider",
            "internal_evaluation",
        }
        if set(self.compiler_versions) != expected:
            raise ValueError("release must pin every supported compiler")
        return self


class CodeRelease(ReleaseModel):
    core_package_sha256: str = Field(pattern=_SHA256_PATTERN)
    backend_package_sha256: str = Field(pattern=_SHA256_PATTERN)


class RuntimeFeatureRelease(ReleaseModel):
    fund_execution_policy: Literal["locked", "public_fund_v1_approved"]
    model: ModelRelease
    retrieval: RetrievalRelease = Field(default_factory=RetrievalRelease)


class RuntimeControlRelease(ReleaseModel):
    hcx_timeout_seconds: float = Field(gt=0, le=300)
    official_answer_timeout_seconds: float = Field(gt=0, lt=60)
    official_answer_max_inflight: StrictInt = Field(ge=1, le=8)
    worker_count: StrictInt = Field(ge=1, le=8)


class AgentReleaseComponents(ReleaseModel):
    code: CodeRelease
    queryplan: QueryPlanRelease
    field_registry: ArtifactFingerprint
    field_registry_schema_version: str = Field(min_length=1, max_length=32)
    capability_matrix: ArtifactFingerprint
    capability_matrix_version: str = Field(min_length=1, max_length=32)
    ontology: OntologyRelease
    approved_datasets: ApprovedDatasetRelease
    prompts: PromptRelease
    execution: ExecutionRelease
    runtime_features: RuntimeFeatureRelease
    runtime_controls: RuntimeControlRelease


class AgentReleaseManifest(ReleaseModel):
    """Image-bound payload. Container digest deliberately lives outside this model."""

    schema_version: Literal["1.0"] = "1.0"
    release_id: str = Field(pattern=_RELEASE_ID_PATTERN)
    environment: Literal["evaluation", "production"]
    generated_at_utc: datetime
    source_commit: str = Field(pattern=_SOURCE_COMMIT_PATTERN)
    components: AgentReleaseComponents

    @model_validator(mode="after")
    def require_utc_timestamp(self) -> AgentReleaseManifest:
        if self.generated_at_utc.tzinfo is None or self.generated_at_utc.utcoffset() != timedelta(
            0
        ):
            raise ValueError("release timestamp must use UTC")
        return self

    @property
    def contract_sha256(self) -> str:
        """Hash semantic fields only; deployment identity uses the canonical file hash."""

        return canonical_sha256(self.model_dump(mode="json"))


class RollbackRelease(ReleaseModel):
    mode: Literal["initial_bootstrap", "pinned_previous_release"]
    target_release_id: str | None = Field(default=None, pattern=_RELEASE_ID_PATTERN)
    target_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    target_binding_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    target_image_reference: str | None = Field(
        default=None,
        pattern=_IMAGE_REFERENCE_PATTERN,
    )
    target_activation_generation: StrictInt | None = Field(default=None, ge=1)
    target_environment: Literal["evaluation", "production"] | None = None
    target_platform: Literal["linux/amd64", "linux/arm64"] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> RollbackRelease:
        values = (
            self.target_release_id,
            self.target_manifest_sha256,
            self.target_binding_sha256,
            self.target_image_reference,
            self.target_activation_generation,
            self.target_environment,
            self.target_platform,
        )
        if self.mode == "initial_bootstrap" and any(item is not None for item in values):
            raise ValueError("initial release cannot claim a rollback target")
        if self.mode == "pinned_previous_release" and any(item is None for item in values):
            raise ValueError("rollback target must pin release, manifest and image")
        return self


class DeploymentBinding(ReleaseModel):
    """Detached post-build binding supplied by the deployment control plane."""

    schema_version: Literal["1.0"] = "1.0"
    release_id: str = Field(pattern=_RELEASE_ID_PATTERN)
    environment: Literal["evaluation", "production"]
    source_commit: str = Field(pattern=_SOURCE_COMMIT_PATTERN)
    release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_reference: str = Field(pattern=_IMAGE_REFERENCE_PATTERN)
    platform: Literal["linux/amd64", "linux/arm64"]
    activation_generation: StrictInt = Field(ge=1)
    rollback: RollbackRelease

    @model_validator(mode="after")
    def validate_generation_and_rollback(self) -> DeploymentBinding:
        if self.activation_generation == 1:
            if self.rollback.mode != "initial_bootstrap":
                raise ValueError("the first activation must use initial_bootstrap")
            return self
        if self.rollback.mode != "pinned_previous_release":
            raise ValueError("later activations must pin the previous release")
        if self.rollback.target_release_id == self.release_id:
            raise ValueError("rollback target must differ from the active release")
        if self.rollback.target_activation_generation != self.activation_generation - 1:
            raise ValueError("rollback target must be the immediately previous activation")
        if self.rollback.target_environment != self.environment:
            raise ValueError("rollback target environment must match the active release")
        if self.rollback.target_platform != self.platform:
            raise ValueError("rollback target platform must match the active release")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeReleaseInputs:
    environment: Literal["evaluation", "production"]
    source_commit: str
    image_reference: str
    backend_version: str
    backend_root: Path
    answer_provider: Literal["deterministic", "hyperclova"]
    hcx_queryplan_enabled: bool
    hcx_model: str | None
    fund_execution_policy: Literal["locked", "public_fund_v1_approved"]
    schema_dense_enabled: bool = False
    product_dense_enabled: bool = False
    platform: Literal["linux/amd64", "linux/arm64"] = "linux/amd64"
    hcx_timeout_seconds: float = 45.0
    official_answer_timeout_seconds: float = 55.0
    official_answer_max_inflight: int = 2
    worker_count: int = 1


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _raw_resource(package: str, name: str) -> bytes:
    return files(package).joinpath(name).read_bytes()


def _raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _model_contract_sha256(model: BaseModel) -> str:
    return canonical_sha256(model.model_dump(mode="json"))


def _hash_named_files(root: Path, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in names:
        path = root / name
        try:
            data = path.read_bytes()
        except OSError as error:
            raise AgentReleaseError(
                AgentReleaseCode.RUNTIME_MISMATCH,
                "a required prompt contract artifact is unavailable",
            ) from error
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def sha256_runtime_tree(root: str | Path) -> str:
    """Hash deployable Python/config bytes without trusting mtimes or caches."""

    path = Path(root)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "runtime code root is unavailable",
        ) from error
    if not resolved.is_dir():
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "runtime code root is not a directory",
        )
    entries: list[tuple[str, bytes]] = []
    for candidate in resolved.rglob("*"):
        if candidate.is_symlink():
            raise AgentReleaseError(
                AgentReleaseCode.UNSAFE_FILE,
                "runtime code tree contains a symbolic link",
            )
        if not candidate.is_file() or candidate.suffix not in _RUNTIME_SUFFIXES:
            continue
        relative = candidate.relative_to(resolved).as_posix()
        try:
            entries.append((relative, candidate.read_bytes()))
        except OSError as error:
            raise AgentReleaseError(
                AgentReleaseCode.RUNTIME_MISMATCH,
                "runtime code artifact became unavailable",
            ) from error
    if not entries:
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "runtime code tree has no deployable artifacts",
        )
    digest = hashlib.sha256()
    for relative, data in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _runtime_model(inputs: RuntimeReleaseInputs) -> ModelRelease:
    queryplan_enabled = bool(inputs.hcx_queryplan_enabled)
    answer_enabled = inputs.answer_provider == "hyperclova"
    if not queryplan_enabled and not answer_enabled:
        return ModelRelease(
            provider="disabled",
            model_id=None,
            revision_status="not_used",
            queryplan_operation_enabled=False,
            grounded_answer_operation_enabled=False,
        )
    return ModelRelease(
        provider="hyperclova",
        model_id=inputs.hcx_model,
        revision_status="provider_revision_not_exposed",
        queryplan_operation_enabled=queryplan_enabled,
        grounded_answer_operation_enabled=answer_enabled,
    )


def _approved_dataset_release() -> ApprovedDatasetRelease:
    approval_resource = _raw_resource(
        "finance_agent_core.config",
        "approved_dataset_manifest.json",
    )
    approval = load_approved_dataset_manifest()
    snapshots = {
        name: DatasetSnapshotRelease(
            source_id=item.source_id,
            source_snapshot_date=item.source_snapshot_date.isoformat(),
            manifest_schema_version=item.manifest_schema_version,
            data_file_sha256=item.data_file_sha256,
            schema_file_sha256=item.schema_file_sha256,
            database_sha256=item.database_sha256,
        )
        for name, item in approval.datasets.items()
    }
    return ApprovedDatasetRelease(
        release_id=approval.release_id,
        registry_schema_version=approval.registry_schema_version,
        manifest=ArtifactFingerprint(
            artifact_sha256=_raw_sha256(approval_resource),
            contract_sha256=approval.canonical_sha256,
        ),
        snapshots=snapshots,
    )


def build_release_components(inputs: RuntimeReleaseInputs) -> AgentReleaseComponents:
    if inputs.schema_dense_enabled or inputs.product_dense_enabled:
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "Stage 3 v1 forbids production Dense indexes until their runtime is approved",
        )
    if not Path(inputs.backend_root).is_absolute():
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "backend runtime root must be absolute",
        )

    from finance_agent_core.config.capability import load_capability_matrix
    from finance_agent_core.execution.authority import (
        GROUNDED_PLAN_GATE_VERSION,
        INTERNAL_EVALUATION_COMPILER_VERSION,
        LEGACY_PROVIDER_COMPILER_VERSION,
        RESULT_VERIFIER_VERSION,
        SERVER_COMPILER_VERSION,
    )

    core_root = Path(__file__).resolve().parent
    registry_bytes = _raw_resource("finance_agent_core.config", "field_registry.yaml")
    capability_bytes = _raw_resource("finance_agent_core.config", "capability_matrix.json")
    queryplan_schema_bytes = _raw_resource(
        "finance_agent_core.contracts",
        "queryplan.hcx.schema.json",
    )
    registry = load_field_registry()
    capability = load_capability_matrix()

    queryplan_prompt_files = (
        "agent/grounded_planning.py",
        "agent/linker.py",
        "agent/providers/local_test.py",
        "agent/providers/hyperclova.py",
        "contracts/hcx_schema.py",
    )
    answer_prompt_files = (
        "answering/context.py",
        "answering/providers.py",
        "answering/verifier.py",
    )
    generation_files = (
        "agent/providers/hyperclova.py",
        "agent/providers/hyperclova_http.py",
        "answering/providers.py",
    )
    return AgentReleaseComponents(
        code=CodeRelease(
            core_package_sha256=sha256_runtime_tree(core_root),
            backend_package_sha256=sha256_runtime_tree(inputs.backend_root),
        ),
        queryplan=QueryPlanRelease(
            schema_artifact_sha256=_raw_sha256(queryplan_schema_bytes),
            python_contract_sha256=canonical_sha256(QueryPlan.model_json_schema()),
            hcx_response_schema_sha256=canonical_sha256(load_hcx_queryplan_schema()),
        ),
        field_registry=ArtifactFingerprint(
            artifact_sha256=_raw_sha256(registry_bytes),
            contract_sha256=_model_contract_sha256(registry),
        ),
        field_registry_schema_version=registry.schema_version,
        capability_matrix=ArtifactFingerprint(
            artifact_sha256=_raw_sha256(capability_bytes),
            contract_sha256=_model_contract_sha256(capability),
        ),
        capability_matrix_version=capability.matrix_version,
        ontology=OntologyRelease(
            bundle_sha256=ontology_bundle_sha256(registry),
        ),
        approved_datasets=_approved_dataset_release(),
        prompts=PromptRelease(
            queryplan_bundle_sha256=_hash_named_files(core_root, queryplan_prompt_files),
            answer_bundle_sha256=_hash_named_files(core_root, answer_prompt_files),
            generation_contract_sha256=_hash_named_files(core_root, generation_files),
        ),
        execution=ExecutionRelease(
            compiler_versions={
                "server_queryplan_compiler": SERVER_COMPILER_VERSION,
                "grounded_plan_gate": GROUNDED_PLAN_GATE_VERSION,
                "legacy_provider": LEGACY_PROVIDER_COMPILER_VERSION,
                "internal_evaluation": INTERNAL_EVALUATION_COMPILER_VERSION,
            },
            verifier_version=RESULT_VERIFIER_VERSION,
            core_version=__version__,
            backend_version=inputs.backend_version,
        ),
        runtime_features=RuntimeFeatureRelease(
            fund_execution_policy=inputs.fund_execution_policy,
            model=_runtime_model(inputs),
        ),
        runtime_controls=RuntimeControlRelease(
            hcx_timeout_seconds=inputs.hcx_timeout_seconds,
            official_answer_timeout_seconds=inputs.official_answer_timeout_seconds,
            official_answer_max_inflight=inputs.official_answer_max_inflight,
            worker_count=inputs.worker_count,
        ),
    )


def build_agent_release_manifest(
    inputs: RuntimeReleaseInputs,
    *,
    release_id: str,
    generated_at_utc: datetime | None = None,
) -> AgentReleaseManifest:
    return AgentReleaseManifest(
        release_id=release_id,
        environment=inputs.environment,
        generated_at_utc=generated_at_utc or datetime.now(UTC),
        source_commit=inputs.source_commit,
        components=build_release_components(inputs),
    )


def manifest_file_bytes(manifest: AgentReleaseManifest) -> bytes:
    payload = manifest.model_dump(mode="json")
    return (_canonical_json(payload) + "\n").encode("utf-8")


def deployment_binding_file_bytes(binding: DeploymentBinding) -> bytes:
    payload = binding.model_dump(mode="json")
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise AgentReleaseError(
            AgentReleaseCode.INVALID_JSON,
            f"{label} is not strict JSON",
        ) from error
    if not isinstance(payload, dict):
        raise AgentReleaseError(
            AgentReleaseCode.INVALID_JSON,
            f"{label} must contain one JSON object",
        )
    return payload


def _reject_symlink_path(path: Path) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise AgentReleaseError(
                    AgentReleaseCode.UNSAFE_FILE,
                    "release artifact path contains a symbolic link",
                )
        except OSError as error:
            raise AgentReleaseError(
                AgentReleaseCode.FILE_UNAVAILABLE,
                "release artifact path is unavailable",
            ) from error
        if current.parent == current:
            return
        current = current.parent


def _read_release_file(path: str | Path) -> tuple[Path, bytes, str]:
    target = Path(path)
    if not target.is_absolute():
        raise AgentReleaseError(
            AgentReleaseCode.UNSAFE_FILE,
            "release artifact path must be absolute",
        )
    _reject_symlink_path(target)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise AgentReleaseError(
            AgentReleaseCode.FILE_UNAVAILABLE,
            "release artifact is unavailable",
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AgentReleaseError(
                AgentReleaseCode.UNSAFE_FILE,
                "release artifact must be a regular file",
            )
        if before.st_nlink != 1 or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise AgentReleaseError(
                AgentReleaseCode.UNSAFE_FILE,
                "release artifact must be single-linked and read-only",
            )
        if before.st_size <= 0 or before.st_size > _MAX_RELEASE_FILE_BYTES:
            raise AgentReleaseError(
                AgentReleaseCode.UNSAFE_FILE,
                "release artifact size is outside the approved range",
            )
        chunks: list[bytes] = []
        remaining = _MAX_RELEASE_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or len(data) != before.st_size:
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "release artifact changed while it was being verified",
            )
    finally:
        os.close(descriptor)
    return target, data, _raw_sha256(data)


@dataclass(frozen=True, slots=True)
class ResolvedAgentRelease:
    manifest: AgentReleaseManifest
    binding: DeploymentBinding
    manifest_path: Path
    binding_path: Path
    manifest_file_sha256: str
    binding_file_sha256: str
    release_context_sha256: str
    runtime_inputs: RuntimeReleaseInputs

    @property
    def release_id(self) -> str:
        return self.manifest.release_id

    @property
    def runtime_filesystems_read_only(self) -> bool:
        """Return whether the code roots are protected by read-only mounts."""

        read_only_flag = getattr(os, "ST_RDONLY", 1)
        try:
            return all(
                os.statvfs(path).f_flag & read_only_flag
                for path in (Path(__file__).resolve().parent, self.runtime_inputs.backend_root)
            )
        except OSError:
            return False

    def assert_request_current(self) -> None:
        """Use cheap checks only when the runtime code mounts are actually read-only."""

        self.assert_current(deep=not self.runtime_filesystems_read_only)

    def assert_current(self, *, deep: bool = True) -> None:
        """Recheck release files; deep startup/readiness also rehash runtime code."""

        _, manifest_data, manifest_sha256 = _read_release_file(self.manifest_path)
        _, binding_data, binding_sha256 = _read_release_file(self.binding_path)
        if (
            manifest_sha256 != self.manifest_file_sha256
            or binding_sha256 != self.binding_file_sha256
        ):
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "active release artifact changed after startup",
            )
        try:
            manifest = AgentReleaseManifest.model_validate(
                _strict_json_object(manifest_data, "AgentReleaseManifest")
            )
            binding = DeploymentBinding.model_validate(
                _strict_json_object(binding_data, "DeploymentBinding")
            )
        except AgentReleaseError:
            raise
        except ValueError as error:
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "active release contract became invalid",
            ) from error
        if manifest_data != manifest_file_bytes(
            manifest
        ) or binding_data != deployment_binding_file_bytes(binding):
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "active release artifact is not in canonical file form",
            )
        if manifest != self.manifest or binding != self.binding:
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "active release contract changed after startup",
            )
        if deep and build_release_components(self.runtime_inputs) != self.manifest.components:
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "runtime components changed after startup",
            )


def resolve_agent_release(
    *,
    manifest_path: str | Path,
    binding_path: str | Path,
    expected_binding_sha256: str,
    runtime_inputs: RuntimeReleaseInputs,
) -> ResolvedAgentRelease:
    if not isinstance(expected_binding_sha256, str) or not re.fullmatch(
        _SHA256_PATTERN,
        expected_binding_sha256,
    ):
        raise AgentReleaseError(
            AgentReleaseCode.BINDING_HASH_MISMATCH,
            "deployment binding trust anchor is invalid",
        )
    resolved_manifest_path, manifest_data, manifest_sha256 = _read_release_file(manifest_path)
    resolved_binding_path, binding_data, binding_sha256 = _read_release_file(binding_path)
    if binding_sha256 != expected_binding_sha256:
        raise AgentReleaseError(
            AgentReleaseCode.BINDING_HASH_MISMATCH,
            "deployment binding differs from the control-plane trust anchor",
        )
    try:
        manifest = AgentReleaseManifest.model_validate(
            _strict_json_object(manifest_data, "AgentReleaseManifest")
        )
        binding = DeploymentBinding.model_validate(
            _strict_json_object(binding_data, "DeploymentBinding")
        )
    except AgentReleaseError:
        raise
    except ValueError as error:
        raise AgentReleaseError(
            AgentReleaseCode.INVALID_JSON,
            "release artifact violates the strict schema",
        ) from error
    if manifest_data != manifest_file_bytes(
        manifest
    ) or binding_data != deployment_binding_file_bytes(binding):
        raise AgentReleaseError(
            AgentReleaseCode.INVALID_JSON,
            "release artifact is not in canonical file form",
        )
    if binding.release_manifest_sha256 != manifest_sha256:
        raise AgentReleaseError(
            AgentReleaseCode.MANIFEST_HASH_MISMATCH,
            "AgentReleaseManifest differs from DeploymentBinding",
        )
    if (
        binding.release_id != manifest.release_id
        or binding.environment != manifest.environment
        or binding.source_commit != manifest.source_commit
    ):
        raise AgentReleaseError(
            AgentReleaseCode.RELEASE_MISMATCH,
            "manifest and deployment binding identify different releases",
        )
    if (
        manifest.environment != runtime_inputs.environment
        or manifest.source_commit != runtime_inputs.source_commit
        or binding.image_reference != runtime_inputs.image_reference
        or binding.platform != runtime_inputs.platform
    ):
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "deployment identity differs from the active runtime",
        )
    if manifest.components != build_release_components(runtime_inputs):
        raise AgentReleaseError(
            AgentReleaseCode.RUNTIME_MISMATCH,
            "runtime code, contract, prompt, model, index or dataset release differs",
        )
    context = canonical_sha256(
        {
            "release_id": manifest.release_id,
            "manifest_file_sha256": manifest_sha256,
            "binding_file_sha256": binding_sha256,
            "image_reference": binding.image_reference,
            "activation_generation": binding.activation_generation,
        }
    )
    resolved = ResolvedAgentRelease(
        manifest=manifest,
        binding=binding,
        manifest_path=resolved_manifest_path,
        binding_path=resolved_binding_path,
        manifest_file_sha256=manifest_sha256,
        binding_file_sha256=binding_sha256,
        release_context_sha256=context,
        runtime_inputs=runtime_inputs,
    )
    # Close the startup TOCTOU window after all potentially expensive runtime
    # hashes have been computed and before the object can reach Agent assembly.
    resolved.assert_current()
    return resolved


__all__ = [
    "AgentReleaseCode",
    "AgentReleaseComponents",
    "AgentReleaseError",
    "AgentReleaseManifest",
    "DeploymentBinding",
    "ResolvedAgentRelease",
    "RollbackRelease",
    "RuntimeControlRelease",
    "RuntimeReleaseInputs",
    "build_agent_release_manifest",
    "build_release_components",
    "canonical_sha256",
    "deployment_binding_file_bytes",
    "manifest_file_bytes",
    "resolve_agent_release",
    "sha256_runtime_tree",
]
