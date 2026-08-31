from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from fastapi_backend.scripts import rollback_drill
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.providers import (
    HyperClovaXCallObserver,
    HyperClovaXFinalAnswerProvider,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSemanticResolverProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactGateEvidence,
    load_schema_embedding_candidate_link,
)
from finance_agent_core.normalization import normalize_bond_row
from finance_agent_core.observability import AuditEvent, AuditOutcome, AuditStage, MetricCounter
from finance_agent_core.retrieval.schema_adaptive import ProductionHybridSchemaLinker
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    EmbeddingProviderMetadata,
    SchemaDenseActivationPolicy,
    approve_schema_index_for_production,
    build_schema_field_entries,
)
from finance_agent_core.storage import write_bond_database

from app.config import Settings
from app.dependencies import build_agent
from app.main import create_app
from tests.conftest import stub_resolved_release

type FailureMode = Literal[
    "authentication_error",
    "rate_limited",
    "response_error",
    "service_error",
    "structured_response_error",
    "success",
    "timeout",
    "transport_error",
]

_QUESTION = "매수 가능한 국내채권을 매수수익률 높은 순으로 2개 보여줘"
_HCX_SETTINGS = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
_PRIVATE_FAILURE = "PRIVATE-HCX-FAILURE-MUST-NOT-ENTER-AUDIT"


class _AdaptiveKureContractProvider:
    def __init__(self) -> None:
        candidate = load_schema_embedding_candidate_link("kure-v1")
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="kure-official-get-contract-test",
            model_id=candidate.model_id,
            model_revision=candidate.revision,
            license_id="mit",
            dimension=1024,
            pooling="cls",
        )
        self._artifact_gate_evidence = SchemaEmbeddingArtifactGateEvidence(
            mode="production",
            candidate=candidate,
            snapshot_file_manifest_sha256=(
                "b0b6229e5d2593371b7ac31519da186ccac3fcdfa8fb4e98fa6a430cc92bd597"
            ),
            manifest_file_sha256="d" * 64,
        )
        self._vectors: dict[str, list[float]] = {}

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    @property
    def artifact_gate_evidence(self) -> SchemaEmbeddingArtifactGateEvidence:
        return self._artifact_gate_evidence

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for position, text in enumerate(texts):
            vector = [0.0] * 1024
            vector[position] = 1.0
            field_id = text.split(" | ", maxsplit=1)[0]
            self._vectors[field_id] = vector
            vectors.append(vector)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        del text
        return [
            left + right
            for left, right in zip(
                self._vectors["issue_amount"],
                self._vectors["duration_years"],
                strict=True,
            )
        ]


class _SemanticResolverTransport:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.operations.append(request.operation)
        assert request.operation == "semantic_resolver"
        return {
            "status_code": 200,
            "content": json.dumps(
                {
                    "decision": "resolve",
                    "selected_field_id": "issue_amount",
                    "operation": "rank",
                    "direction": "desc",
                    "reason_code": "candidate_context_match",
                    "evidence_spans": ["찍어낸 전체 물량"],
                    "ambiguity_reason": "",
                }
            ),
            "request_id": "fake-semantic-official-get",
            "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
        }


def _adaptive_bond_agent(database: Path) -> tuple[RoutedFinanceAgent, _SemanticResolverTransport]:
    provider = _AdaptiveKureContractProvider()
    policy = SchemaDenseActivationPolicy(
        dense_min_score=0.5,
        hclx_candidate_min_score=0.35,
        minimum_margin=0.1,
        top_k=5,
        calibration_report_sha256="e" * 64,
    )
    offline = DenseSchemaIndex.build(build_schema_field_entries(), provider)
    artifact = approve_schema_index_for_production(offline, policy)
    linker = ProductionHybridSchemaLinker(DenseSchemaIndex(artifact, provider), policy)
    transport = _SemanticResolverTransport()
    semantic_provider = HyperClovaXSemanticResolverProvider(
        _HCX_SETTINGS,
        transport,
    )
    return (
        RoutedFinanceAgent(
            {ProductFamily.BOND: database},
            adaptive_semantic_resolver=AdaptiveSemanticResolver(
                linker,
                hclx_provider=semantic_provider,
            ),
        ),
        transport,
    )


class _OfficialFakeHyperClovaXTransport:
    def __init__(
        self,
        *,
        intent_mode: FailureMode = "success",
        generation_mode: FailureMode = "success",
    ) -> None:
        self.intent_mode = intent_mode
        self.generation_mode = generation_mode
        self.operations: list[str] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.operations.append(request.operation)
        mode = self.intent_mode if request.operation == "intent_analysis" else self.generation_mode
        if mode == "timeout":
            raise TimeoutError(_PRIVATE_FAILURE)
        if mode == "transport_error":
            raise ConnectionError(_PRIVATE_FAILURE)
        if mode == "response_error":
            return {"unexpected": _PRIVATE_FAILURE}
        if mode == "structured_response_error":
            return self._response(content='{"schema_version":"1.0"}')
        if mode != "success":
            status_code = {
                "authentication_error": 401,
                "rate_limited": 429,
                "service_error": 500,
            }[mode]
            return self._response(status_code=status_code, content=None, usage=False)
        if request.operation == "intent_analysis":
            return self._response(
                content=json.dumps(
                    {
                        "schema_version": "1.0",
                        "intent": "search",
                        "product_families": ["bond"],
                        "evidence_spans": ["국내채권", "보여줘"],
                        "needs_clarification": False,
                    },
                    ensure_ascii=False,
                )
            )
        if request.operation == "final_answer":
            properties = request.response_schema["properties"]
            return self._response(
                content=json.dumps(
                    {
                        "schema_version": "1.0",
                        "lead": properties["lead"]["enum"][0],
                        "answer_body_sha256": properties["answer_body_sha256"]["enum"][0],
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"unexpected HCX operation: {request.operation}")

    def _response(
        self,
        *,
        content: str | None,
        status_code: int = 200,
        usage: bool = True,
    ) -> dict[str, object]:
        return {
            "status_code": status_code,
            "content": content,
            "request_id": f"fake-hcx-{len(self.operations):03d}",
            "usage": (
                {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14} if usage else None
            ),
        }


def _make_bond_database(tmp_path: Path) -> Path:
    records = [
        normalize_bond_row(
            source_row=row,
            present_source_fields=40,
            values={
                "pd_no": f"KRTEST{row:06d}",
                "pd_exg_mkt": "장내",
                "info_base_dt": 20260824,
                "info_seq": row - 1,
                "pd_nm": f"테스트채권 {row}",
                "pd_abrv_nm": f"테스트 {row}",
                "pd_pbcm": "테스트발행사",
                "std_pd_mcls_nm": "회사채",
                "std_pd_scls_nm": "일반사채",
                "bd_knd": "일반회사채",
                "curr_cd": "KRW",
                "isu_bal_amt": 1_000_000_000,
                "isu_dt": 20260101,
                "mat_dt": 20280101,
                "srfc_irt": "3.5",
                "pd_risk_gcd": 4,
                "pd_std_info_update": 20260224,
                "buy_yield": buy_yield,
                "after_tax_yield": "3.0",
                "buyable_quantity": 100,
                "remaining_days": 9999,
                "dur": "0.5",
                "crd_grd": "AA-",
            },
        )
        for row, buy_yield in ((2, "4.5"), (3, "3.5"))
    ]
    path = tmp_path / "official-hcx-bond.sqlite3"
    write_bond_database(
        path,
        records,
        DatabaseManifest(
            dataset="bond",
            registry_schema_version="1.4",
            source_file_name="synthetic_official_hcx_bond.xlsx",
            source_file_sha256="c" * 64,
            source_file_size_bytes=1234,
            source_snapshot_date=date(2026, 8, 24),
            total_rows=len(records),
            searchable_rows=len(records),
            quarantined_rows=0,
        ),
    )
    return path


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    question_id: str,
    intent_mode: FailureMode = "success",
    generation_mode: FailureMode = "success",
):
    database = _make_bond_database(tmp_path)
    transport = _OfficialFakeHyperClovaXTransport(
        intent_mode=intent_mode,
        generation_mode=generation_mode,
    )
    audit_path = tmp_path / "audit" / "events.jsonl"
    audit_path.parent.mkdir(mode=0o700)
    settings = Settings(
        FINANCE_AUDIT_MODE="jsonl",
        FINANCE_AUDIT_FILE=audit_path,
        FINANCE_AUDIT_FSYNC_EACH_EVENT=False,
        OFFICIAL_ANSWER_TIMEOUT_SECONDS=5,
    )

    def build_test_agent(
        _settings: Settings,
        *,
        release_guard=None,
        audit_sink=None,
    ) -> RoutedFinanceAgent:
        del release_guard
        assert audit_sink is not None
        observer = HyperClovaXCallObserver(audit_sink)
        return RoutedFinanceAgent(
            {ProductFamily.BOND: database},
            intent_analysis_provider=HyperClovaXIntentAnalysisProvider(
                _HCX_SETTINGS,
                transport,
                on_call=observer,
            ),
            official_answer_provider=HyperClovaXFinalAnswerProvider(
                _HCX_SETTINGS,
                transport,
                on_call=observer,
            ),
            require_official_hclx=True,
            audit_sink=audit_sink,
        )

    monkeypatch.setattr("app.main.build_agent", build_test_agent)
    return create_app(settings=settings), audit_path, transport


def _events(path: Path) -> tuple[AuditEvent, ...]:
    return tuple(
        AuditEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _assert_official_contract(response, *, question_id: str) -> dict[str, object]:
    body = OfficialAnswerResponse.model_validate(response.json())
    assert body.question_id == question_id
    assert body.question == _QUESTION
    assert set(response.json()) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }
    assert all(isinstance(value, str) for value in response.json().values())
    return json.loads(body.think_trace)


def test_official_get_uses_unique_server_evidence_without_hclx(
    tmp_path: Path,
) -> None:
    database = _make_bond_database(tmp_path)
    agent, transport = _adaptive_bond_agent(database)
    application = create_app(
        settings=Settings(OFFICIAL_ANSWER_TIMEOUT_SECONDS=5),
        agent=agent,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={
                "question_id": "adaptive-official-get",
                "question": "발행사가 찍어낸 전체 물량이 큰 국내채권을 2개 보여줘",
            },
        )

    body = OfficialAnswerResponse.model_validate(response.json())
    assert response.status_code == 200, response.text
    assert transport.operations == []
    assert body.question_id == "adaptive-official-get"
    assert "issue_amount" in body.retrieved_context
    assert "제공 데이터" in body.answer


def test_build_agent_wires_release_bound_kure_without_eager_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AdaptiveKureContractProvider()
    policy = SchemaDenseActivationPolicy(
        dense_min_score=0.5,
        hclx_candidate_min_score=0.35,
        minimum_margin=0.1,
        top_k=5,
        calibration_report_sha256="e" * 64,
    )
    artifact = approve_schema_index_for_production(
        DenseSchemaIndex.build(build_schema_field_entries(), provider),
        policy,
    )
    monkeypatch.setattr(
        "app.dependencies._load_schema_dense_release",
        lambda settings: (artifact, policy),
    )
    monkeypatch.setattr(
        "app.dependencies.load_verified_schema_embedding_cpu_provider",
        lambda *args, **kwargs: provider,
    )
    settings = Settings(
        APP_ENV="evaluation",
        FINANCE_ADAPTIVE_SEMANTIC_ENABLED=True,
        FINANCE_DENSE_SCHEMA_LINKER_ENABLED=True,
        FINANCE_SCHEMA_DENSE_INDEX_FILE=tmp_path / "schema-index.json",
        FINANCE_SCHEMA_DENSE_INDEX_SHA256="a" * 64,
        FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256="e" * 64,
        FINANCE_SCHEMA_DENSE_MIN_SCORE=0.5,
        FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE=0.35,
        FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN=0.1,
        FINANCE_SCHEMA_DENSE_TOP_K=5,
        FINANCE_KURE_SNAPSHOT_DIR=tmp_path / "kure-cache/snapshot",
        FINANCE_KURE_SNAPSHOT_MANIFEST_FILE=tmp_path / "kure-manifest.json",
        FINANCE_KURE_TRUSTED_CACHE_ROOT=tmp_path / "kure-cache",
    )

    agent = build_agent(settings, release_guard=stub_resolved_release())

    assert type(agent.adaptive_semantic_resolver) is AdaptiveSemanticResolver
    assert type(agent.adaptive_semantic_resolver.schema_linker) is (ProductionHybridSchemaLinker)
    assert agent.adaptive_semantic_resolver.execution_enabled is False
    assert agent.router.adaptive_semantic_enabled is True


def test_official_get_uses_hclx_intent_and_final_generation_audit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_id = "Q-OFFICIAL-HCLX-PATH"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 200, response.text
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["status"] == "success"
    assert trace["answer_mode"] == "llm_grounded"
    assert transport.operations == ["intent_analysis", "final_answer"]
    observed_path = tuple(
        (event.stage.value, event.outcome.value, event.reason_code) for event in _events(audit_path)
    )
    assert observed_path == rollback_drill._EXPECTED_HCLX_ANSWER_PROBE_AUDIT_PATH


@pytest.mark.parametrize(
    ("mode", "reason", "counter"),
    [
        (
            "authentication_error",
            "intent_authentication_failed",
            MetricCounter.HCLX_AUTHENTICATION_FAILURES,
        ),
        ("rate_limited", "intent_rate_limited", MetricCounter.HCLX_RATE_LIMITS),
        ("service_error", "intent_service_failed", MetricCounter.HCLX_SERVICE_FAILURES),
        ("transport_error", "intent_transport_failed", MetricCounter.HCLX_TRANSPORT_FAILURES),
        ("response_error", "intent_response_rejected", MetricCounter.HCLX_RESPONSE_FAILURES),
        (
            "structured_response_error",
            "intent_response_rejected",
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
    ],
)
def test_official_get_intent_failures_stop_before_data_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: FailureMode,
    reason: str,
    counter: MetricCounter,
) -> None:
    question_id = f"Q-PLAN-{mode}"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        intent_mode=mode,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 503
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["status"] == "error"
    expected_operations = (
        ["intent_analysis", "intent_analysis"]
        if mode in {"response_error", "structured_response_error"}
        else ["intent_analysis"]
    )
    assert transport.operations == expected_operations
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX
        and event.outcome is AuditOutcome.FAILED
        and event.reason_code == reason
        for event in events
    )
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[counter.value] == len(expected_operations)
    assert counters.get(MetricCounter.HCLX_SUCCESSES.value, 0) == 0
    assert not any(event.stage in {AuditStage.SQL, AuditStage.ORACLE} for event in events)
    serialized = audit_path.read_text(encoding="utf-8")
    assert _PRIVATE_FAILURE not in serialized
    assert question_id not in serialized
    assert _QUESTION not in serialized


def test_official_get_intent_timeout_returns_504_before_oracle_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_id = "Q-PLAN-timeout"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        intent_mode="timeout",
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 504
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["control_code"] == "request_timeout"
    assert transport.operations == ["intent_analysis"]
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX
        and event.outcome is AuditOutcome.TIMED_OUT
        and event.reason_code == "intent_timed_out"
        for event in events
    )
    assert not any(event.stage in {AuditStage.SQL, AuditStage.ORACLE} for event in events)
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[MetricCounter.HCLX_TIMEOUTS.value] == 1
    assert _PRIVATE_FAILURE not in audit_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "reason", "outcome", "counter"),
    [
        (
            "authentication_error",
            "final_generation_authentication_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_AUTHENTICATION_FAILURES,
        ),
        (
            "rate_limited",
            "final_generation_rate_limited",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RATE_LIMITS,
        ),
        (
            "service_error",
            "final_generation_service_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_SERVICE_FAILURES,
        ),
        (
            "transport_error",
            "final_generation_transport_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_TRANSPORT_FAILURES,
        ),
        (
            "response_error",
            "final_generation_response_rejected",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
        (
            "structured_response_error",
            "final_generation_response_rejected",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
        (
            "timeout",
            "final_generation_timed_out",
            AuditOutcome.TIMED_OUT,
            MetricCounter.HCLX_TIMEOUTS,
        ),
    ],
)
def test_official_get_generation_failures_never_serve_deterministic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: FailureMode,
    reason: str,
    outcome: AuditOutcome,
    counter: MetricCounter,
) -> None:
    question_id = f"Q-GENERATION-{mode}"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        generation_mode=mode,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == (504 if mode == "timeout" else 503)
    trace = _assert_official_contract(response, question_id=question_id)
    if mode == "timeout":
        assert trace["control_code"] == "request_timeout"
    else:
        assert trace["status"] == "error"
        assert trace["fallback_used"] is False
    assert transport.operations == ["intent_analysis", "final_answer"]
    assert "테스트채권" not in response.json()["answer"]
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX and event.outcome is outcome and event.reason_code == reason
        for event in events
    )
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[counter.value] == 1
    assert counters[MetricCounter.HCLX_SUCCESSES.value] == 1
    serialized = audit_path.read_text(encoding="utf-8")
    assert _PRIVATE_FAILURE not in serialized
    assert question_id not in serialized
    assert _QUESTION not in serialized
