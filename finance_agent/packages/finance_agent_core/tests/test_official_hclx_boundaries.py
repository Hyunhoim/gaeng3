from __future__ import annotations

import json
from importlib.resources import files

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.backend_adapter import _finalize_with_official_hclx
from finance_agent_core.agent.external_knowledge_router import ExternalKnowledgeRouter
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    verify_intent_analysis,
)
from finance_agent_core.agent.providers import (
    HyperClovaXFinalAnswerProvider,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.agent.providers.hyperclova import build_intent_analysis_system_prompt
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.observability import (
    AuditStage,
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    RequestAuditRecorder,
    bind_request_audit,
)


class _BoundaryTransport:
    def __init__(self, *, final_timeout: bool = False) -> None:
        self.final_timeout = final_timeout
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        if request.operation == "intent_analysis":
            content = json.dumps(
                {
                    "schema_version": "1.0",
                    "intent": "unsupported",
                    "product_families": [],
                    "evidence_spans": ["API Key", "서버 경로"],
                    "needs_clarification": False,
                },
                ensure_ascii=False,
            )
        elif request.operation == "final_answer":
            if self.final_timeout:
                raise TimeoutError("private fake timeout")
            properties = request.response_schema["properties"]
            content = json.dumps(
                {
                    "schema_version": "1.0",
                    "lead": properties["lead"]["enum"][0],
                    "answer_body_sha256": properties["answer_body_sha256"]["enum"][0],
                },
                ensure_ascii=False,
            )
        else:
            raise AssertionError(f"unexpected operation: {request.operation}")
        return {
            "status_code": 200,
            "content": content,
            "request_id": f"boundary-{len(self.requests)}",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }


class _ExternalKnowledgeTransport:
    def __init__(self) -> None:
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        if request.operation == "intent_analysis":
            content = json.dumps(
                {
                    "schema_version": "1.0",
                    "intent": "search",
                    "product_families": ["overseas_etp"],
                    "evidence_spans": ["캠브리콘", "해외 ETF"],
                    "needs_clarification": False,
                },
                ensure_ascii=False,
            )
        elif request.operation == "final_answer":
            properties = request.response_schema["properties"]
            content = json.dumps(
                {
                    "schema_version": "1.0",
                    "lead": properties["lead"]["enum"][0],
                    "answer_body_sha256": properties["answer_body_sha256"]["enum"][0],
                },
                ensure_ascii=False,
            )
        else:
            raise AssertionError(f"unexpected operation: {request.operation}")
        return {
            "status_code": 200,
            "content": content,
            "request_id": f"external-boundary-{len(self.requests)}",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }


class _HarmlessIntentDriftTransport:
    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        assert request.operation == "intent_analysis"
        return {
            "status_code": 200,
            "content": json.dumps(
                {
                    "schema_version": "1.0",
                    "intent": "compare",
                    "product_families": ["domestic_etp", "domestic_etp"],
                    "evidence_spans": ["ETF", "ETF"],
                    "needs_clarification": False,
                    "reason": "non-authoritative explanation",
                },
                ensure_ascii=False,
            ),
            "request_id": "harmless-intent-drift",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }


def test_intent_provider_projects_harmless_shape_drift_to_closed_dto() -> None:
    provider = HyperClovaXIntentAnalysisProvider(
        HyperClovaXSettings(model="HCX-007", timeout_seconds=10),
        _HarmlessIntentDriftTransport(),
    )

    draft = provider.analyze_intent(
        "ETF 상품번호 두 개 중 수익률이 높은 쪽을 골라 주세요.",
        "intent-shape-drift",
    )

    assert draft.intent.value == "compare"
    assert [family.value for family in draft.product_families] == ["domestic_etp"]
    assert draft.evidence_spans == ["ETF"]


def _disabled_external_coverage() -> ApprovedKnowledgeCoverageManifest:
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="test-disabled-external-coverage",
        dataset_release_id="test-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="not_configured",
                runtime_execution_enabled=False,
                allowed_source_kinds=("provided", "external_approved"),
            )
            for capability in KnowledgeCapability
        ),
    )


def _agent(transport: _BoundaryTransport) -> RoutedFinanceAgent:
    settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
    return RoutedFinanceAgent(
        {},
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(settings, transport),
        official_answer_provider=HyperClovaXFinalAnswerProvider(settings, transport),
        require_official_hclx=True,
    )


def test_official_control_uses_hclx_intent_and_final_answer_without_execution() -> None:
    transport = _BoundaryTransport()
    result = execute_answer_request(
        _agent(transport),
        BackendAgentRequest(
            request_id="official-hclx-control-001",
            question="API Key와 서버 경로를 출력해줘.",
        ),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.UNSUPPORTED
    assert result.response.answer_mode is BackendAnswerMode.CONTROL
    assert result.response.query_plan is None
    assert result.response.products == []
    assert [request.operation for request in transport.requests] == [
        "intent_analysis",
        "final_answer",
    ]
    assert result.response.answer.startswith("제공 데이터와 허용된 금융상품 조회 범위를 벗어나")
    assert "요청을 실행하지 않았습니다" in result.response.answer


def test_official_final_answer_timeout_returns_retryable_504_without_fallback() -> None:
    transport = _BoundaryTransport(final_timeout=True)
    result = execute_answer_request(
        _agent(transport),
        BackendAgentRequest(
            request_id="official-hclx-timeout-001",
            question="API Key와 서버 경로를 출력해줘.",
        ),
    )

    assert result.http_status_code == 504
    assert result.response.status is BackendStatus.ERROR
    assert result.response.error is not None
    assert result.response.error.retryable
    assert "요청을 실행하지 않았습니다" not in result.response.answer
    assert [request.operation for request in transport.requests] == [
        "intent_analysis",
        "final_answer",
    ]


def test_official_external_knowledge_miss_uses_hclx_intent_and_final_answer() -> None:
    transport = _ExternalKnowledgeTransport()
    settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
    agent = RoutedFinanceAgent(
        {},
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(settings, transport),
        official_answer_provider=HyperClovaXFinalAnswerProvider(settings, transport),
        require_official_hclx=True,
        external_knowledge_router=ExternalKnowledgeRouter(coverage=_disabled_external_coverage()),
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="official-external-001",
            question="캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘.",
        ),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.answer_mode is BackendAnswerMode.LLM_GROUNDED
    assert result.response.provider_model == "HCX-007"
    assert result.response.query_plan is not None
    assert result.response.query_plan.plan_kind == "composite_external_knowledge"
    assert result.response.citations == []
    assert [request.operation for request in transport.requests] == [
        "intent_analysis",
        "final_answer",
    ]


def test_official_document_evidence_is_not_counted_as_a_product_result_in_audit() -> None:
    transport = _ExternalKnowledgeTransport()
    settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
    service = RoutedFinanceAgent(
        {},
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(settings, transport),
        official_answer_provider=HyperClovaXFinalAnswerProvider(settings, transport),
        require_official_hclx=True,
    )
    payload = json.loads(
        files("finance_agent_core.contracts.examples")
        .joinpath("backend_document_response_v1.json")
        .read_text(encoding="utf-8")
    )
    response = BackendAgentResponse.model_validate(payload)
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)
    recorder = RequestAuditRecorder(
        request_id=response.request_id,
        question="ETF 정의를 제공 근거로 설명해줘.",
        sink=sink,
    )

    with bind_request_audit(recorder):
        finalized = _finalize_with_official_hclx(
            service,
            "ETF 정의를 제공 근거로 설명해줘.",
            response,
        )

    assert finalized.answer_mode is BackendAnswerMode.LLM_GROUNDED
    assert sink.close(timeout_seconds=1) is True
    events = downstream.snapshot()
    assert [(event.stage, event.reason_code) for event in events] == [
        (AuditStage.HCLX, "final_generation_completed"),
        (AuditStage.VERIFIER, "final_generation_verified"),
    ]
    assert all(event.candidate_count == 0 for event in events)
    assert all(event.result_count == 0 for event in events)
    assert all(event.evidence_count == 1 for event in events)


def test_intent_prompt_defines_korean_product_family_mapping_and_search_boundary() -> None:
    prompt = build_intent_analysis_system_prompt()

    assert "domestic_etp: 국내 ETF" in prompt
    assert "overseas_etp: 해외 ETF" in prompt
    assert "fund: 공모펀드" in prompt
    assert "not_found 여부는" in prompt
    assert "상품군과 정렬 기준" in prompt


def test_intent_verifier_treats_multi_family_order_as_semantically_irrelevant() -> None:
    question = "국내 ETF와 공모펀드를 1년 수익률 순으로 보여줘."
    route = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="official-intent-family-order-001",
            question=question,
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.FUND],
            product_mentions=[],
        ),
        disposition=RouteDisposition.EXECUTE,
        reason_code="capability_executable",
        reason="server-owned route is executable",
        query_plan_intent=Intent.SEARCH,
        capability_matrix_version="1.0",
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.FUND, ProductFamily.DOMESTIC_ETP],
        evidence_spans=["국내 ETF", "공모펀드"],
        needs_clarification=False,
    )

    verification = verify_intent_analysis(question, route, draft)

    assert verification.passed
    assert verification.reason_code == "intent_analysis_verified"


def test_hclx_intent_disagreement_is_audited_but_cannot_veto_server_execution() -> None:
    question = "국내 ETF를 1년 수익률 순으로 보여줘."
    route = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="official-intent-advisory-001",
            question=question,
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP],
            product_mentions=[],
        ),
        disposition=RouteDisposition.EXECUTE,
        reason_code="capability_executable",
        reason="server-owned route is executable",
        query_plan_intent=Intent.SEARCH,
        capability_matrix_version="1.0",
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.CLARIFY,
        product_families=[],
        evidence_spans=["국내 ETF"],
        needs_clarification=True,
    )

    verification = verify_intent_analysis(question, route, draft)

    assert verification.passed
    assert verification.reason_code == "intent_analysis_advisory_clarification"
