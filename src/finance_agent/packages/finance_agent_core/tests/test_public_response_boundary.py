from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.answering.final_generation import (
    FinalAnswerContext,
    FinalAnswerDraft,
    allowed_final_answer_leads,
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendErrorCode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.release import (
    DeploymentBinding,
    RollbackRelease,
    RuntimeReleaseInputs,
    build_agent_release_manifest,
    deployment_binding_file_bytes,
    manifest_file_bytes,
    resolve_agent_release,
)
from finance_agent_core.storage import DatasetApprovalError

_QUESTION = "해외 ETF 상세 정보를 알려줘."


class _ClarifyIntentProvider:
    provider_name = "fixed-hclx-intent"
    model_name = "HCX-007"
    require_execution_structure = True

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft:
        assert question == _QUESTION
        assert question_id
        return IntentAnalysisDraft(
            intent=InteractionIntent.CLARIFY,
            product_families=[ProductFamily.OVERSEAS_ETP],
            evidence_spans=["해외 ETF"],
            needs_clarification=True,
        )


class _MutatingFinalAnswerProvider:
    provider_name = "fixed-hclx-answer"
    model_name = "HCX-007"

    def __init__(self, mutate: Callable[[], None]) -> None:
        self._mutate = mutate
        self.calls = 0

    def generate_final_answer(self, context: FinalAnswerContext) -> FinalAnswerDraft:
        self.calls += 1
        self._mutate()
        return FinalAnswerDraft(
            lead=allowed_final_answer_leads(context)[0],
            answer_body_sha256=context.answer_body_sha256,
        )


def _write_read_only(path: Path, data: bytes) -> None:
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)


def _resolved_release(tmp_path: Path):
    backend_root = tmp_path / "backend-runtime"
    backend_root.mkdir()
    (backend_root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime = RuntimeReleaseInputs(
        environment="evaluation",
        source_commit="a" * 40,
        image_reference="registry.example/finance-agent@sha256:" + "b" * 64,
        backend_version="0.1.0",
        backend_root=backend_root,
        answer_provider="hyperclova",
        hcx_queryplan_enabled=False,
        hcx_intent_analysis_enabled=True,
        hcx_model="HCX-007",
        fund_execution_policy="public_fund_v1_approved",
    )
    manifest = build_agent_release_manifest(
        runtime,
        release_id="final-response-race-v1",
        generated_at_utc=datetime(2026, 9, 2, tzinfo=UTC),
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
    return resolved, binding_path


def test_binding_changed_during_final_hclx_is_discarded_as_safe_503(
    tmp_path: Path,
) -> None:
    resolved, binding_path = _resolved_release(tmp_path)

    def mutate_binding() -> None:
        changed = binding_path.read_bytes().replace(
            b'"activation_generation":1',
            b'"activation_generation":2',
        )
        _write_read_only(binding_path, changed)

    answer_provider = _MutatingFinalAnswerProvider(mutate_binding)
    agent = RoutedFinanceAgent(
        {},
        intent_analysis_provider=_ClarifyIntentProvider(),
        official_answer_provider=answer_provider,
        require_official_hclx=True,
        release_guard=resolved,
        require_agent_release=True,
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="final-binding-race-001", question=_QUESTION),
    )

    assert answer_provider.calls == 1
    assert result.http_status_code == 503
    assert result.response.status is BackendStatus.ERROR
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.INTERNAL_ERROR
    assert result.response.error.retryable
    assert "activation_generation" not in result.model_dump_json()


def test_database_changed_during_final_hclx_is_discarded_as_safe_503(
    sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path, _, _ = sample_database
    trusted_bytes = database_path.read_bytes()

    def require_current_database(paths: dict[ProductFamily, Path]) -> None:
        assert paths == {ProductFamily.OVERSEAS_ETP: database_path}
        if database_path.read_bytes() != trusted_bytes:
            raise DatasetApprovalError("approved database changed during final generation")

    monkeypatch.setattr(
        "finance_agent_core.agent.routed_service.require_approved_database_paths",
        require_current_database,
    )

    def mutate_database() -> None:
        with database_path.open("ab") as stream:
            stream.write(b"final-generation-race")

    answer_provider = _MutatingFinalAnswerProvider(mutate_database)
    agent = RoutedFinanceAgent(
        {ProductFamily.OVERSEAS_ETP: database_path},
        intent_analysis_provider=_ClarifyIntentProvider(),
        official_answer_provider=answer_provider,
        require_official_hclx=True,
        require_approved_databases=True,
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="final-database-race-001", question=_QUESTION),
    )

    assert answer_provider.calls == 1
    assert result.http_status_code == 503
    assert result.response.status is BackendStatus.ERROR
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.DATASET_UNAVAILABLE
    assert result.response.error.retryable
    assert "approved database changed" not in result.model_dump_json()
