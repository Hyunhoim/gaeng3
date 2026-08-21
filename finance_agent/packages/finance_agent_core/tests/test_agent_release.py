from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.knowledge_service import KnowledgeAgent
from finance_agent_core.execution import PlanAuthorityCode, PlanAuthorityError, SQLiteOracle
from finance_agent_core.release import (
    AgentReleaseCode,
    AgentReleaseError,
    AgentReleaseManifest,
    DeploymentBinding,
    KnowledgeRetrievalRelease,
    RelationRetrievalArtifactRelease,
    RollbackRelease,
    RuntimeReleaseInputs,
    build_agent_release_manifest,
    build_release_components,
    deployment_binding_file_bytes,
    load_relation_retrieval_artifact_release,
    manifest_file_bytes,
    relation_retrieval_artifact_file_bytes,
    resolve_agent_release,
    sha256_runtime_tree,
)

_SOURCE_COMMIT = "a" * 40
_IMAGE_REFERENCE = "registry.example/finance-agent@sha256:" + "b" * 64
_GENERATED_AT = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _inputs(*, backend_root: Path | None = None) -> RuntimeReleaseInputs:
    return RuntimeReleaseInputs(
        environment="evaluation",
        source_commit=_SOURCE_COMMIT,
        image_reference=_IMAGE_REFERENCE,
        backend_version="0.1.0",
        backend_root=backend_root or _repository_root() / "fastapi_backend" / "app",
        answer_provider="deterministic",
        hcx_queryplan_enabled=False,
        hcx_model=None,
        fund_execution_policy="locked",
    )


def _write_read_only(path: Path, data: bytes) -> None:
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)


def _relation_artifact() -> RelationRetrievalArtifactRelease:
    return RelationRetrievalArtifactRelease(
        index_sha256="1" * 64,
        approval_manifest_sha256="2" * 64,
        relation_set_sha256="3" * 64,
    )


def _write_release(
    tmp_path: Path,
    inputs: RuntimeReleaseInputs | None = None,
):
    runtime = inputs or _inputs()
    manifest = build_agent_release_manifest(
        runtime,
        release_id="finance-agent-test-v1",
        generated_at_utc=_GENERATED_AT,
    )
    manifest_path = tmp_path / "agent-release-manifest.json"
    manifest_data = manifest_file_bytes(manifest)
    _write_read_only(manifest_path, manifest_data)
    binding = DeploymentBinding(
        release_id=manifest.release_id,
        environment=manifest.environment,
        source_commit=manifest.source_commit,
        release_manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        image_reference=runtime.image_reference,
        platform="linux/amd64",
        activation_generation=1,
        rollback=RollbackRelease(mode="initial_bootstrap"),
    )
    binding_path = tmp_path / "deployment-binding.json"
    binding_data = deployment_binding_file_bytes(binding)
    _write_read_only(binding_path, binding_data)
    resolved = resolve_agent_release(
        manifest_path=manifest_path,
        binding_path=binding_path,
        expected_binding_sha256=hashlib.sha256(binding_data).hexdigest(),
        runtime_inputs=runtime,
    )
    return manifest, binding, resolved, manifest_path, binding_path


def test_release_manifest_pins_code_contract_prompt_data_model_and_disabled_indexes(
    tmp_path: Path,
) -> None:
    manifest, binding, resolved, _, _ = _write_release(tmp_path)

    assert resolved.release_id == manifest.release_id == binding.release_id
    assert manifest.schema_version == "1.2"
    assert binding.schema_version == "1.0"
    assert len(manifest.components.code.core_package_sha256) == 64
    assert len(manifest.components.code.backend_package_sha256) == 64
    assert manifest.components.queryplan.schema_version == "1.0"
    assert manifest.components.field_registry_schema_version == "1.3"
    assert manifest.components.approved_datasets.snapshots.keys() == {
        "bond",
        "domestic_etp",
        "overseas_etp",
        "fund",
    }
    assert manifest.components.runtime_features.model.provider == "disabled"
    assert manifest.components.runtime_controls.hcx_timeout_seconds == 45.0
    assert manifest.components.runtime_controls.official_answer_timeout_seconds == 270.0
    assert manifest.components.runtime_controls.official_answer_max_inflight == 2
    assert manifest.components.runtime_controls.worker_count == 1
    assert manifest.components.runtime_features.retrieval.schema_dense == "disabled_offline_only"
    assert (
        manifest.components.runtime_features.retrieval.product_dense == "disabled_not_implemented"
    )
    assert manifest.components.knowledge_retrieval.relation.status == "disabled_not_activated"
    assert manifest.components.knowledge_retrieval.relation.artifact is None
    assert manifest.components.knowledge_retrieval.document.status == "disabled_no_approved_corpus"
    assert "image_reference" not in manifest.model_dump(mode="json")
    assert binding.image_reference == _IMAGE_REFERENCE


@pytest.mark.parametrize("legacy_version", ["1.0", "1.1"])
def test_stage4_manifest_rejects_legacy_schema_while_binding_remains_v1(
    tmp_path: Path,
    legacy_version: str,
) -> None:
    manifest, binding, _, _, _ = _write_release(tmp_path)
    legacy_manifest = manifest.model_dump(mode="python")
    legacy_manifest["schema_version"] = legacy_version

    with pytest.raises(ValidationError):
        AgentReleaseManifest.model_validate(legacy_manifest)
    assert binding.schema_version == "1.0"


def test_public_relation_activation_requires_exact_canonical_artifact_hash() -> None:
    artifact = _relation_artifact()
    artifact_file_sha256 = hashlib.sha256(
        relation_retrieval_artifact_file_bytes(artifact)
    ).hexdigest()
    runtime = replace(
        _inputs(),
        relation_retrieval_artifact=artifact,
        relation_retrieval_artifact_file_sha256=artifact_file_sha256,
    )

    state = build_release_components(runtime).knowledge_retrieval

    assert state.relation.status == "activated"
    assert state.relation.artifact == artifact
    assert state.relation.artifact_file_sha256 == artifact_file_sha256
    assert state.document.status == "disabled_no_approved_corpus"


@pytest.mark.parametrize(
    ("artifact", "artifact_file_sha256", "expected_code"),
    [
        (_relation_artifact(), None, AgentReleaseCode.ARTIFACT_HASH_MISMATCH),
        (_relation_artifact(), "f" * 64, AgentReleaseCode.ARTIFACT_HASH_MISMATCH),
        (None, "f" * 64, AgentReleaseCode.RUNTIME_MISMATCH),
    ],
)
def test_public_relation_activation_fails_closed_on_incomplete_or_wrong_hash(
    artifact: RelationRetrievalArtifactRelease | None,
    artifact_file_sha256: str | None,
    expected_code: AgentReleaseCode,
) -> None:
    with pytest.raises(AgentReleaseError) as raised:
        build_release_components(
            replace(
                _inputs(),
                relation_retrieval_artifact=artifact,
                relation_retrieval_artifact_file_sha256=artifact_file_sha256,
            )
        )

    assert raised.value.code is expected_code


def test_internal_knowledge_release_cannot_masquerade_as_public_activation() -> None:
    internal = KnowledgeRetrievalRelease(relation=_relation_artifact())

    with pytest.raises(AgentReleaseError) as raised:
        build_release_components(
            replace(
                _inputs(),
                relation_retrieval_artifact=internal,  # type: ignore[arg-type]
                relation_retrieval_artifact_file_sha256="f" * 64,
            )
        )

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


def test_public_agent_requires_exact_signed_knowledge_runtime(tmp_path: Path) -> None:
    artifact = _relation_artifact()
    artifact_file_sha256 = hashlib.sha256(
        relation_retrieval_artifact_file_bytes(artifact)
    ).hexdigest()
    runtime = replace(
        _inputs(),
        relation_retrieval_artifact=artifact,
        relation_retrieval_artifact_file_sha256=artifact_file_sha256,
    )
    _, _, resolved, _, _ = _write_release(tmp_path, runtime)
    signed_release = resolved.manifest.components.knowledge_retrieval
    relation_path = tmp_path / "relations.sqlite3"
    product_path = tmp_path / "domestic-etp.sqlite3"

    matching_agent = KnowledgeAgent(
        release=signed_release,
        relation_index_path=relation_path,
        relation_database_paths={"domestic_etp": product_path},
    )
    service = RoutedFinanceAgent(
        {},
        release_guard=resolved,
        require_agent_release=True,
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=matching_agent,
    )
    assert service.knowledge_agent is matching_agent

    matching_agent.release = signed_release.model_copy(
        update={
            "relation": signed_release.relation.model_copy(
                update={"artifact_file_sha256": "e" * 64}
            )
        }
    )
    with pytest.raises(AgentReleaseError) as drifted:
        service._assert_signed_knowledge_current()
    assert drifted.value.code is AgentReleaseCode.RUNTIME_MISMATCH
    matching_agent.release = signed_release

    with pytest.raises(ValueError, match="requires its public router and Agent"):
        RoutedFinanceAgent(
            {},
            release_guard=resolved,
            require_agent_release=True,
        )

    internal_agent = KnowledgeAgent(
        release=KnowledgeRetrievalRelease(relation=artifact),
        relation_index_path=relation_path,
        relation_database_paths={"domestic_etp": product_path},
    )
    with pytest.raises(ValueError, match="signed public knowledge release"):
        RoutedFinanceAgent(
            {},
            release_guard=resolved,
            require_agent_release=True,
            knowledge_router=DeterministicKnowledgeRouter(),
            knowledge_agent=internal_agent,
        )

    mismatched_agent = KnowledgeAgent(
        release=signed_release.model_copy(
            update={
                "relation": signed_release.relation.model_copy(
                    update={
                        "artifact_file_sha256": "f" * 64,
                    }
                )
            }
        ),
        relation_index_path=relation_path,
        relation_database_paths={"domestic_etp": product_path},
    )
    with pytest.raises(ValueError, match="signed public knowledge release"):
        RoutedFinanceAgent(
            {},
            release_guard=resolved,
            require_agent_release=True,
            knowledge_router=DeterministicKnowledgeRouter(),
            knowledge_agent=mismatched_agent,
        )


def test_disabled_signed_relation_release_rejects_attached_agent(tmp_path: Path) -> None:
    _, _, resolved, _, _ = _write_release(tmp_path)
    with pytest.raises(ValueError, match="disabled signed relation release"):
        RoutedFinanceAgent(
            {},
            release_guard=resolved,
            require_agent_release=True,
            knowledge_router=DeterministicKnowledgeRouter(),
        )

    internal_agent = KnowledgeAgent(
        release=KnowledgeRetrievalRelease(relation=_relation_artifact()),
        relation_index_path=tmp_path / "relations.sqlite3",
        relation_database_paths={"domestic_etp": tmp_path / "domestic-etp.sqlite3"},
    )

    with pytest.raises(ValueError, match="disabled signed relation release"):
        RoutedFinanceAgent(
            {},
            release_guard=resolved,
            require_agent_release=True,
            knowledge_router=DeterministicKnowledgeRouter(),
            knowledge_agent=internal_agent,
        )


def test_constructed_invalid_relation_artifact_is_revalidated_before_activation() -> None:
    invalid = RelationRetrievalArtifactRelease.model_construct(
        index_sha256="not-a-sha256",
        approval_manifest_sha256="2" * 64,
        relation_set_sha256="3" * 64,
    )
    file_sha256 = hashlib.sha256(relation_retrieval_artifact_file_bytes(invalid)).hexdigest()

    with pytest.raises(AgentReleaseError) as raised:
        build_release_components(
            replace(
                _inputs(),
                relation_retrieval_artifact=invalid,
                relation_retrieval_artifact_file_sha256=file_sha256,
            )
        )

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


def test_public_knowledge_retrieval_is_required_in_manifest() -> None:
    manifest = build_agent_release_manifest(
        _inputs(),
        release_id="finance-agent-test-v1",
        generated_at_utc=_GENERATED_AT,
    ).model_dump(mode="python")
    manifest["components"].pop("knowledge_retrieval")

    with pytest.raises(ValidationError):
        AgentReleaseManifest.model_validate(manifest)


def test_relation_artifact_loader_rejects_hash_mismatch_and_internal_contract(
    tmp_path: Path,
) -> None:
    artifact = _relation_artifact()
    artifact_path = tmp_path / "relation-artifact.json"
    artifact_data = relation_retrieval_artifact_file_bytes(artifact)
    _write_read_only(artifact_path, artifact_data)
    artifact_sha256 = hashlib.sha256(artifact_data).hexdigest()

    assert (
        load_relation_retrieval_artifact_release(
            artifact_path=artifact_path,
            expected_file_sha256=artifact_sha256,
        )
        == artifact
    )
    with pytest.raises(AgentReleaseError) as mismatched:
        load_relation_retrieval_artifact_release(
            artifact_path=artifact_path,
            expected_file_sha256="f" * 64,
        )
    assert mismatched.value.code is AgentReleaseCode.ARTIFACT_HASH_MISMATCH

    internal = KnowledgeRetrievalRelease(relation=artifact)
    internal_data = (
        json.dumps(
            internal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    _write_read_only(artifact_path, internal_data)
    with pytest.raises(AgentReleaseError) as internal_error:
        load_relation_retrieval_artifact_release(
            artifact_path=artifact_path,
            expected_file_sha256=hashlib.sha256(internal_data).hexdigest(),
        )
    assert internal_error.value.code is AgentReleaseCode.INVALID_JSON


def test_manifest_bytes_are_canonical_for_the_same_release(tmp_path: Path) -> None:
    first = build_agent_release_manifest(
        _inputs(),
        release_id="finance-agent-test-v1",
        generated_at_utc=_GENERATED_AT,
    )
    second = build_agent_release_manifest(
        _inputs(),
        release_id="finance-agent-test-v1",
        generated_at_utc=_GENERATED_AT,
    )

    assert manifest_file_bytes(first) == manifest_file_bytes(second)


def test_release_rejects_a_changed_binding_trust_anchor(tmp_path: Path) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256="f" * 64,
            runtime_inputs=_inputs(),
        )

    assert raised.value.code is AgentReleaseCode.BINDING_HASH_MISMATCH


def test_release_rejects_duplicate_json_keys_before_schema_validation(tmp_path: Path) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)
    duplicate = binding_path.read_bytes().replace(
        b'"schema_version":"1.0"',
        b'"schema_version":"1.0","schema_version":"1.0"',
    )
    _write_read_only(binding_path, duplicate)

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(duplicate).hexdigest(),
            runtime_inputs=_inputs(),
        )

    assert raised.value.code is AgentReleaseCode.INVALID_JSON


def test_changing_manifest_and_its_binding_together_cannot_forge_runtime(
    tmp_path: Path,
) -> None:
    manifest, binding, _, manifest_path, binding_path = _write_release(tmp_path)
    code = manifest.components.code.model_copy(update={"backend_package_sha256": "f" * 64})
    components = manifest.components.model_copy(update={"code": code})
    forged_manifest = manifest.model_copy(update={"components": components})
    forged_manifest_data = manifest_file_bytes(forged_manifest)
    _write_read_only(manifest_path, forged_manifest_data)
    forged_binding = binding.model_copy(
        update={"release_manifest_sha256": hashlib.sha256(forged_manifest_data).hexdigest()}
    )
    forged_binding_data = deployment_binding_file_bytes(forged_binding)
    _write_read_only(binding_path, forged_binding_data)

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(forged_binding_data).hexdigest(),
            runtime_inputs=_inputs(),
        )

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


def test_release_rejects_runtime_image_mismatch(tmp_path: Path) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)
    binding_data = binding_path.read_bytes()
    mismatched = replace(
        _inputs(),
        image_reference="registry.example/other@sha256:" + "c" * 64,
    )

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(binding_data).hexdigest(),
            runtime_inputs=mismatched,
        )

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "linux/arm64"),
        ("hcx_timeout_seconds", 46.0),
        ("official_answer_timeout_seconds", 54.0),
        ("official_answer_max_inflight", 3),
        ("worker_count", 2),
    ],
)
def test_release_rejects_runtime_platform_or_control_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)
    binding_data = binding_path.read_bytes()

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(binding_data).hexdigest(),
            runtime_inputs=replace(_inputs(), **{field: value}),
        )

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


def test_release_rejects_noncanonical_json_even_with_matching_trust_anchor(
    tmp_path: Path,
) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)
    noncanonical = binding_path.read_bytes() + b"\n"
    _write_read_only(binding_path, noncanonical)

    with pytest.raises(AgentReleaseError) as raised:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(noncanonical).hexdigest(),
            runtime_inputs=_inputs(),
        )

    assert raised.value.code is AgentReleaseCode.INVALID_JSON


@pytest.mark.parametrize("flag", ["schema_dense_enabled", "product_dense_enabled"])
def test_release_v1_refuses_unapproved_dense_runtime(flag: str) -> None:
    with pytest.raises(AgentReleaseError) as raised:
        build_release_components(replace(_inputs(), **{flag: True}))

    assert raised.value.code is AgentReleaseCode.RUNTIME_MISMATCH


def test_release_rejects_symlink_and_group_writable_artifacts(tmp_path: Path) -> None:
    _, _, _, manifest_path, binding_path = _write_release(tmp_path)
    link = tmp_path / "binding-link.json"
    link.symlink_to(binding_path)

    with pytest.raises(AgentReleaseError) as symlink_error:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=link,
            expected_binding_sha256=hashlib.sha256(binding_path.read_bytes()).hexdigest(),
            runtime_inputs=_inputs(),
        )
    assert symlink_error.value.code is AgentReleaseCode.UNSAFE_FILE

    binding_path.chmod(0o664)
    with pytest.raises(AgentReleaseError) as permission_error:
        resolve_agent_release(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256=hashlib.sha256(binding_path.read_bytes()).hexdigest(),
            runtime_inputs=_inputs(),
        )
    assert permission_error.value.code is AgentReleaseCode.UNSAFE_FILE


def test_request_postcheck_detects_manifest_or_runtime_code_change(tmp_path: Path) -> None:
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    backend_code = backend_root / "main.py"
    backend_code.write_text("VALUE = 1\n", encoding="utf-8")
    runtime = _inputs(backend_root=backend_root)
    _, _, resolved, _, binding_path = _write_release(tmp_path, runtime)

    binding_data = binding_path.read_bytes().replace(
        b'"activation_generation":1',
        b'"activation_generation":2',
    )
    _write_read_only(binding_path, binding_data)
    with pytest.raises(AgentReleaseError) as binding_error:
        resolved.assert_current()
    assert binding_error.value.code is AgentReleaseCode.STALE_RELEASE

    _write_read_only(binding_path, deployment_binding_file_bytes(resolved.binding))
    backend_code.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(AgentReleaseError) as code_error:
        resolved.assert_current()
    assert code_error.value.code is AgentReleaseCode.STALE_RELEASE


def test_runtime_code_tree_rejects_symbolic_links(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    source = root / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "alias.py").symlink_to(source)

    with pytest.raises(AgentReleaseError) as raised:
        sha256_runtime_tree(root)

    assert raised.value.code is AgentReleaseCode.UNSAFE_FILE


def test_hcx_release_is_explicit_about_provider_revision_limit() -> None:
    runtime = replace(
        _inputs(),
        answer_provider="hyperclova",
        hcx_model="HCX-007",
    )
    profile = build_release_components(runtime).runtime_features.model

    assert profile.provider == "hyperclova"
    assert profile.model_id == "HCX-007"
    assert profile.revision_status == "provider_revision_not_exposed"


def test_rollback_state_is_typed_instead_of_not_applicable() -> None:
    with pytest.raises(ValidationError):
        RollbackRelease(
            mode="pinned_previous_release",
            target_release_id=None,
            target_manifest_sha256=None,
            target_binding_sha256=None,
            target_image_reference=None,
            target_activation_generation=None,
            target_environment=None,
            target_platform=None,
        )


def test_deployment_generation_requires_a_distinct_previous_release() -> None:
    previous = RollbackRelease(
        mode="pinned_previous_release",
        target_release_id="finance-agent-old-v1",
        target_manifest_sha256="c" * 64,
        target_binding_sha256="e" * 64,
        target_image_reference="registry.example/finance-agent@sha256:" + "d" * 64,
        target_activation_generation=1,
        target_environment="evaluation",
        target_platform="linux/amd64",
    )
    with pytest.raises(ValidationError, match="first activation"):
        DeploymentBinding(
            release_id="finance-agent-new-v1",
            environment="evaluation",
            source_commit=_SOURCE_COMMIT,
            release_manifest_sha256="e" * 64,
            image_reference=_IMAGE_REFERENCE,
            platform="linux/amd64",
            activation_generation=1,
            rollback=previous,
        )
    with pytest.raises(ValidationError, match="later activations"):
        DeploymentBinding(
            release_id="finance-agent-new-v1",
            environment="evaluation",
            source_commit=_SOURCE_COMMIT,
            release_manifest_sha256="e" * 64,
            image_reference=_IMAGE_REFERENCE,
            platform="linux/amd64",
            activation_generation=2,
            rollback=RollbackRelease(mode="initial_bootstrap"),
        )
    with pytest.raises(ValidationError, match="must differ"):
        DeploymentBinding(
            release_id="finance-agent-new-v1",
            environment="evaluation",
            source_commit=_SOURCE_COMMIT,
            release_manifest_sha256="e" * 64,
            image_reference=_IMAGE_REFERENCE,
            platform="linux/amd64",
            activation_generation=2,
            rollback=previous.model_copy(update={"target_release_id": "finance-agent-new-v1"}),
        )
    with pytest.raises(ValidationError, match="immediately previous"):
        DeploymentBinding(
            release_id="finance-agent-new-v1",
            environment="evaluation",
            source_commit=_SOURCE_COMMIT,
            release_manifest_sha256="e" * 64,
            image_reference=_IMAGE_REFERENCE,
            platform="linux/amd64",
            activation_generation=3,
            rollback=previous,
        )


def test_validated_plan_binds_agent_release_through_oracle(
    tmp_path: Path,
    sample_database,
) -> None:
    database_path, _, _ = sample_database
    _, _, resolved, _, binding_path = _write_release(tmp_path)
    agent = RoutedFinanceAgent(
        {"overseas_etp": database_path},
        release_guard=resolved,
        require_agent_release=True,
    )
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "release-bound-plan-001",
    )
    proposal = agent.compiler.compile(trace.route_decision)
    validated = agent.plan_authority_gate.validate_routed(
        proposal,
        trace.route_decision,
        planning_decision=trace.planning_decision,
    )

    assert validated.receipt.agent_release_id == resolved.release_id
    assert validated.receipt.agent_release_manifest_sha256 == resolved.manifest_file_sha256
    assert validated.receipt.deployment_binding_sha256 == resolved.binding_file_sha256
    assert validated.receipt.release_context_sha256 == resolved.release_context_sha256
    assert (
        validated.receipt.ontology_bundle_sha256
        == resolved.manifest.components.ontology.bundle_sha256
    )
    assert SQLiteOracle(database_path).execute(validated).candidate_count > 0

    changed = binding_path.read_bytes().replace(
        b'"activation_generation":1',
        b'"activation_generation":2',
    )
    _write_read_only(binding_path, changed)
    with pytest.raises(PlanAuthorityError) as raised:
        SQLiteOracle(database_path).execute(validated)

    assert raised.value.code is PlanAuthorityCode.RELEASE_MISMATCH
