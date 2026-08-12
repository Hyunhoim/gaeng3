from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest

from finance_agent_core.agent import (
    IntentRouter,
    RoutedFinanceAgent,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.providers import (
    HyperClovaXCallRecord,
    HyperClovaXQueryPlanProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
    HyperClovaXTimeoutError,
)
from finance_agent_core.answering import HyperClovaXGroundedAnswerProvider
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.backend import (
    BackendAnswerMode,
    BackendStatus,
    routed_result_to_backend,
)
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedBondRecord,
    NormalizedDomesticEtpRecord,
    NormalizedOverseasEtpRecord,
)
from finance_agent_core.normalization import normalize_bond_row
from finance_agent_core.storage import write_bond_database

_SETTINGS = HyperClovaXSettings(
    model="HCX-CONTRACT-E2E",
    timeout_seconds=12,
)
_VALID_ANSWER: Literal["valid"] = "valid"
_REVERSED_ANSWER: Literal["reversed"] = "reversed"


class OfflineHyperClovaXTransport:
    """Replay semantic HCX operations without HTTP or credentials."""

    def __init__(
        self,
        plan: QueryPlan,
        *,
        query_failure: BaseException | None = None,
        answer_mode: Literal["valid", "reversed"] = _VALID_ANSWER,
    ) -> None:
        self.plan = plan
        self.query_failure = query_failure
        self.answer_mode = answer_mode
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        if request.operation == "query_plan":
            if self.query_failure is not None:
                raise self.query_failure
            return self._response(self.plan.model_dump_json())
        if request.operation == "grounded_answer":
            return self._response(self._answer_content(request))
        raise AssertionError(f"unexpected offline HCX operation: {request.operation}")

    def _answer_content(self, request: HyperClovaXStructuredRequest) -> str:
        marker = "검증된 입력:\n"
        if marker not in request.system_prompt:
            raise AssertionError("grounded answer prompt omits the verified input")
        payload = json.loads(request.system_prompt.rsplit(marker, maxsplit=1)[1])
        products = []
        for product in payload["products"]:
            required = list(product["required_evidence_fields"])
            if not required:
                required = [product["available_evidence"][0]["canonical_field"]]
            products.append(
                {
                    "result_ref": product["result_ref"],
                    "evidence_fields": required,
                    "explanation": payload["safe_explanation"],
                }
            )
        if self.answer_mode == _REVERSED_ANSWER:
            products.reverse()
        return json.dumps(
            {
                "schema_version": "1.0",
                "lead": "검증된 조건과 데이터에 따라 결과를 정리했습니다.",
                "products": products,
                "acknowledged_warning_codes": payload["required_warning_codes"],
            },
            ensure_ascii=False,
        )

    def _response(self, content: str) -> dict[str, object]:
        return {
            "status_code": 200,
            "content": content,
            "request_id": f"offline-hcx-{len(self.requests):03d}",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        }


def _server_plan(
    *,
    database_path: Path,
    family: str,
    question: str,
    request_id: str,
) -> QueryPlan:
    decision = IntentRouter().route(question, request_id)
    return ServerQueryPlanCompiler({family: database_path}).compile(decision)


def _run_success_path(
    *,
    database_path: Path,
    family: str,
    question: str,
    request_id: str,
) -> tuple[list[HyperClovaXCallRecord], list[HyperClovaXStructuredRequest]]:
    plan = _server_plan(
        database_path=database_path,
        family=family,
        question=question,
        request_id=request_id,
    )
    transport = OfflineHyperClovaXTransport(plan)
    records: list[HyperClovaXCallRecord] = []
    result = RoutedFinanceAgent(
        {family: database_path},
        query_plan_provider=HyperClovaXQueryPlanProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        answer_provider=HyperClovaXGroundedAnswerProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        hclx_planning_enabled=True,
    ).answer(question, request_id)
    backend = routed_result_to_backend(result)

    assert result.status == "executed"
    assert result.query_plan == plan
    assert result.candidate_count is not None and result.candidate_count > 0
    assert result.products
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer_composition.verification.passed
    assert backend.status is BackendStatus.SUCCESS
    assert backend.answer_mode is BackendAnswerMode.LLM_GROUNDED
    assert not backend.fallback_used
    assert backend.provider_model == _SETTINGS.model
    assert backend.citations
    assert backend.as_of_dates
    assert [request.operation for request in transport.requests] == [
        "query_plan",
        "grounded_answer",
    ]
    assert [record.outcome for record in records] == ["success", "success"]
    assert all(record.usage is not None for record in records)
    assert all(
        "prompt" not in key and "content" not in key
        for record in records
        for key in record.model_dump()
    )
    return records, transport.requests


def _make_bond_record(
    *,
    row: int,
    product_id: str,
    buy_yield: str,
    quantity: int | None,
) -> NormalizedBondRecord:
    return normalize_bond_row(
        source_row=row,
        present_source_fields=40,
        values={
            "PD_NO": product_id,
            "PD_EXG_MKT": "장내",
            "PD_NM": f"테스트채권 {product_id}",
            "PD_ABRV_NM": f"테스트 {product_id}",
            "PD_PBCM": "테스트발행사",
            "STD_PD_MCLS_NM": "회사채",
            "STD_PD_SCLS_NM": "일반사채",
            "BD_KND": "일반회사채",
            "CURR_CD": "KRW",
            "ISU_BAL_AMT": 1_000_000_000,
            "ISU_DT": 20260101,
            "MAT_DT": 20280101,
            "SRFC_IRT": "3.5",
            "PD_RISK_GCD": 4,
            "PD_STD_INFO_UPDATE": 20260224,
            "BUY_YIELD": buy_yield,
            "AFTER_TAX_YIELD": "3.0",
            "BUYABLE_QUANTITY": quantity,
            "REMAINING_DAYS": 9999,
            "DUR": "0.5",
            "CRD_GRD": "AA-",
        },
    )


def _make_bond_database(tmp_path: Path) -> Path:
    records = [
        _make_bond_record(
            row=2,
            product_id="KRTEST000001",
            buy_yield="4.5",
            quantity=100,
        ),
        _make_bond_record(
            row=3,
            product_id="KRTEST000002",
            buy_yield="3.5",
            quantity=200,
        ),
        _make_bond_record(
            row=4,
            product_id="KRTEST000003",
            buy_yield="5.5",
            quantity=0,
        ),
    ]
    path = tmp_path / "hcx-contract-bond.sqlite3"
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version="1.2",
        source_file_name="synthetic_hcx_contract_bond.xlsx",
        source_file_sha256="c" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=len(records),
        searchable_rows=len(records),
        quarantined_rows=0,
    )
    write_bond_database(path, records, manifest)
    return path


def test_hcx_offline_overseas_search_reaches_verified_backend(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    _run_success_path(
        database_path=path,
        family="overseas_etp",
        question=(
            "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
            "총보수 0.20% 이하를 AUM 높은 순으로 5개 보여줘"
        ),
        request_id="hcx-e2e-overseas-001",
    )


def test_hcx_offline_domestic_search_reaches_verified_backend(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    _run_success_path(
        database_path=path,
        family="domestic_etp",
        question=(
            "국내 주식형 ETF 중 판매 가능하고 거래정지가 아니며 "
            "연금 거래 가능한 상품을 1개월 수익률 높은 순으로 5개 보여줘"
        ),
        request_id="hcx-e2e-domestic-001",
    )


def test_hcx_offline_bond_search_reaches_verified_backend(
    tmp_path: Path,
) -> None:
    path = _make_bond_database(tmp_path)
    _run_success_path(
        database_path=path,
        family="bond",
        question="매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        request_id="hcx-e2e-bond-001",
    )


def test_hcx_offline_answer_verifier_falls_back_on_reordered_results(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    question = (
        "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
        "총보수 0.20% 이하를 AUM 높은 순으로 5개 보여줘"
    )
    request_id = "hcx-e2e-fallback-001"
    plan = _server_plan(
        database_path=path,
        family="overseas_etp",
        question=question,
        request_id=request_id,
    )
    transport = OfflineHyperClovaXTransport(
        plan,
        answer_mode=_REVERSED_ANSWER,
    )
    records: list[HyperClovaXCallRecord] = []
    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=HyperClovaXQueryPlanProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        answer_provider=HyperClovaXGroundedAnswerProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        hclx_planning_enabled=True,
    ).answer(question, request_id)
    backend = routed_result_to_backend(result)

    assert result.status == "executed"
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "deterministic_fallback"
    assert not result.answer_composition.verification.passed
    assert not result.answer_composition.verification.checks["product_order_exact"]
    assert backend.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK
    assert backend.fallback_used
    assert [record.outcome for record in records] == ["success", "success"]


def test_hcx_offline_query_timeout_stops_before_oracle_and_answer(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    question = "미국 채권형 해외 ETF를 AUM 높은 순으로 5개 보여줘"
    request_id = "hcx-e2e-timeout-001"
    plan = _server_plan(
        database_path=path,
        family="overseas_etp",
        question=question,
        request_id=request_id,
    )
    transport = OfflineHyperClovaXTransport(
        plan,
        query_failure=TimeoutError("DO_NOT_EXPOSE_TIMEOUT_DETAIL"),
    )
    records: list[HyperClovaXCallRecord] = []
    agent = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=HyperClovaXQueryPlanProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        answer_provider=HyperClovaXGroundedAnswerProvider(
            _SETTINGS,
            transport,
            on_call=records.append,
        ),
        hclx_planning_enabled=True,
    )

    with pytest.raises(HyperClovaXTimeoutError) as caught:
        agent.answer(question, request_id)

    assert "DO_NOT_EXPOSE" not in str(caught.value)
    assert [request.operation for request in transport.requests] == ["query_plan"]
    assert [record.outcome for record in records] == ["timeout"]


def test_hcx_offline_router_control_skips_all_model_calls() -> None:
    plan = _server_plan(
        database_path=Path("/tmp/not-used.sqlite3"),
        family="overseas_etp",
        question="미국 채권형 해외 ETF를 AUM 높은 순으로 5개 보여줘",
        request_id="hcx-e2e-unused-plan",
    )
    transport = OfflineHyperClovaXTransport(plan)
    result = RoutedFinanceAgent(
        {},
        query_plan_provider=HyperClovaXQueryPlanProvider(_SETTINGS, transport),
        answer_provider=HyperClovaXGroundedAnswerProvider(_SETTINGS, transport),
        hclx_planning_enabled=True,
    ).answer(
        "내일 가장 오를 해외 ETF를 예측해서 매수 추천해줘",
        "hcx-e2e-control-001",
    )
    backend = routed_result_to_backend(result)

    assert result.status == "unsupported"
    assert backend.status is BackendStatus.UNSUPPORTED
    assert backend.answer_mode is BackendAnswerMode.CONTROL
    assert transport.requests == []


def test_hcx_offline_disabled_fund_stops_before_model_and_database() -> None:
    plan = _server_plan(
        database_path=Path("/tmp/not-used-fund.sqlite3"),
        family="fund",
        question="해외 주식형 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        request_id="hcx-e2e-unused-fund-plan",
    )
    transport = OfflineHyperClovaXTransport(plan)
    result = RoutedFinanceAgent(
        {"fund": Path("/tmp/not-used-fund.sqlite3")},
        query_plan_provider=HyperClovaXQueryPlanProvider(_SETTINGS, transport),
        answer_provider=HyperClovaXGroundedAnswerProvider(_SETTINGS, transport),
        hclx_planning_enabled=True,
    ).answer(
        "해외 주식형 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "hcx-e2e-fund-disabled-001",
    )

    assert result.status == "unsupported"
    assert result.query_plan is None
    assert result.products == []
    assert result.answer_composition is None
    assert transport.requests == []


def test_hcx_offline_server_guard_blocks_mismatched_provider_plan(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    question = "미국 채권형 해외 ETF를 AUM 높은 순으로 5개 보여줘"
    request_id = "hcx-e2e-plan-mismatch-001"
    mismatched = _server_plan(
        database_path=Path("/tmp/not-used-domestic.sqlite3"),
        family="domestic_etp",
        question="국내 주식형 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        request_id=request_id,
    )

    class MismatchedProvider:
        @property
        def provider_name(self) -> Literal["hyperclova"]:
            return "hyperclova"

        def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
            return mismatched.model_copy(update={"question_id": question_id})

    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=MismatchedProvider(),
        hclx_planning_enabled=True,
    ).answer(question, request_id)

    assert result.status == "clarify"
    assert result.products == []
    assert result.answer_composition is None
    assert "model QueryPlan differs" in result.answer
