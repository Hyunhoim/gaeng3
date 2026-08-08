from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_agent_core.agent.grounded_planning import (
    GroundedConstraintProposal,
    GroundedPlanGate,
    GroundedPlanProposal,
    GroundedPlanRejectedError,
    GroundedRankingProposal,
    canonicalize_grounded_plan_proposal_payload,
    grounded_plan_proposal_schema,
)
from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.agent.providers.local_test import LocalProviderError
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    Intent,
    ProductFamily,
    SortDirection,
)
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import write_database

_QUESTION = (
    "현재 거래 가능한 미국 채권형 해외 ETF 중 총보수 0.2% 이하인 상품을 AUM 큰 순서로 5개 보여줘"
)


def _proposal(question_id: str = "grounded-unit-001") -> GroundedPlanProposal:
    return GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="investment_region",
                operator=ConstraintOperator.EQ,
                value="United States of America",
                evidence_span="미국",
            ),
            GroundedConstraintProposal(
                field="asset_type",
                operator=ConstraintOperator.EQ,
                value="Bond",
                evidence_span="채권형",
            ),
            GroundedConstraintProposal(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="ETF",
            ),
            GroundedConstraintProposal(
                field="sellable",
                operator=ConstraintOperator.EQ,
                value=True,
                evidence_span="현재 거래 가능한",
            ),
            GroundedConstraintProposal(
                field="trading_suspended",
                operator=ConstraintOperator.EQ,
                value=False,
                evidence_span="현재 거래 가능한",
            ),
            GroundedConstraintProposal(
                field="total_expense_ratio_pct",
                operator=ConstraintOperator.LTE,
                value=0.2,
                evidence_span="총보수 0.2% 이하",
            ),
        ],
        ranking=[
            GroundedRankingProposal(
                field="aum",
                direction=SortDirection.DESC,
                evidence_span="AUM 큰 순서로",
            )
        ],
        limit=5,
        limit_evidence_span="5개",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )


def _compile(proposal: GroundedPlanProposal, question: str = _QUESTION):
    decision = IntentRouter().route(question, proposal.question_id)
    return GroundedPlanGate({}).compile(question, decision, proposal)


def test_grounded_gate_compiles_verbatim_supported_search() -> None:
    plan = _compile(_proposal())

    assert plan.intent is Intent.SEARCH
    assert plan.product_families == [ProductFamily.OVERSEAS_ETP]
    assert [(item.field, item.operator.value, item.value) for item in plan.constraints] == [
        ("investment_region", "eq", "United States of America"),
        ("asset_type", "eq", "Bond"),
        ("product_type", "eq", "ETF"),
        ("sellable", "eq", True),
        ("trading_suspended", "eq", False),
        ("total_expense_ratio_pct", "lte", 0.2),
    ]
    assert plan.ranking[0].field == "aum"
    assert plan.limit == 5


def test_grounded_gate_rejects_invented_span_and_numeric_value() -> None:
    proposal = _proposal()
    invented_span = proposal.model_copy(
        update={
            "constraints": [
                *proposal.constraints[:-1],
                proposal.constraints[-1].model_copy(
                    update={"evidence_span": "질문에 없는 총보수 조건"}
                ),
            ]
        }
    )
    invented_number = proposal.model_copy(
        update={
            "constraints": [
                *proposal.constraints[:-1],
                proposal.constraints[-1].model_copy(update={"value": 0.3}),
            ]
        }
    )

    with pytest.raises(GroundedPlanRejectedError, match="not verbatim"):
        _compile(invented_span)
    with pytest.raises(GroundedPlanRejectedError, match="lexical grounding"):
        _compile(invented_number)


def test_grounded_gate_rejects_omitted_trusted_constraint() -> None:
    proposal = _proposal()
    missing_product_type = proposal.model_copy(
        update={
            "constraints": [item for item in proposal.constraints if item.field != "product_type"]
        }
    )

    with pytest.raises(GroundedPlanRejectedError, match="omitted trusted constraints"):
        _compile(missing_product_type)


def test_grounded_gate_rejects_existing_identity_not_typed_by_user(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    question = "해외 ETF 상세 정보를 보여줘"
    question_id = "grounded-invented-identity-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.EQ,
                value="B2",
                evidence_span="해외 ETF",
            )
        ],
        ranking=[],
        limit=1,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="identity evidence"):
        GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
            question,
            decision,
            proposal,
        )


def test_grounded_gate_rejects_negated_or_excluded_identity(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    question = "B2를 제외한 해외 ETF를 보여줘"
    question_id = "grounded-negated-identity-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.EQ,
                value="B2",
                evidence_span="B2",
            ),
            GroundedConstraintProposal(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="ETF",
            ),
        ],
        ranking=[],
        limit=5,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="identity evidence is negated"):
        GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
            question,
            decision,
            proposal,
        )


def test_grounded_gate_rejects_non_positive_identity_operator(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    question = "B2를 제외한 해외 ETF를 보여줘"
    question_id = "grounded-identity-operator-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.NEQ,
                value="B2",
                evidence_span="B2를 제외",
            )
        ],
        ranking=[],
        limit=5,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="operator must be eq or in"):
        GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
            question,
            decision,
            proposal,
        )


def test_grounded_gate_repairs_identity_only_from_verbatim_unique_token(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    question = "티커 B2인 해외 ETF 상세 정보 조회"
    question_id = "grounded-verbatim-identity-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.EQ,
                value="B",
                evidence_span="티커 B2",
            ),
            GroundedConstraintProposal(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="ETF",
            ),
        ],
        ranking=[],
        limit=1,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    plan = GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
        question,
        decision,
        proposal,
    )

    identity = next(item for item in plan.constraints if item.field == "product_id")
    assert identity.value == "AMX:B2"


def test_grounded_gate_does_not_treat_family_acronym_as_uncued_ticker(
    tmp_path: Path,
    sample_records: list[NormalizedOverseasEtpRecord],
) -> None:
    collision = sample_records[0].model_copy(
        update={
            "source_row": 99,
            "product_id": "AMX:ETF",
            "product_name": "Ticker collision ETF",
            "ticker": "ETF",
            "isin": "US0000000099",
        }
    )
    records = [*sample_records, collision]
    path = tmp_path / "identity_collision.sqlite3"
    manifest = DatabaseManifest(
        registry_schema_version="1.0",
        source_file_name="synthetic_identity_collision.xlsx",
        source_file_sha256="d" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=collision.source_snapshot_date,
        total_rows=len(records),
        searchable_rows=sum(not record.is_quarantined for record in records),
        quarantined_rows=sum(record.is_quarantined for record in records),
    )
    write_database(path, records, manifest)
    question = "해외 ETF 상세 정보를 보여줘"
    question_id = "grounded-reserved-identity-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="ETF",
            )
        ],
        ranking=[],
        limit=1,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="identity evidence"):
        GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
            question,
            decision,
            proposal,
        )


def test_grounded_gate_allows_reserved_ticker_with_explicit_identity_cue(
    tmp_path: Path,
    sample_records: list[NormalizedOverseasEtpRecord],
) -> None:
    collision = sample_records[0].model_copy(
        update={
            "source_row": 99,
            "product_id": "AMX:ETF",
            "product_name": "Ticker collision ETF",
            "ticker": "ETF",
            "isin": "US0000000099",
        }
    )
    records = [*sample_records, collision]
    path = tmp_path / "explicit_identity_collision.sqlite3"
    manifest = DatabaseManifest(
        registry_schema_version="1.0",
        source_file_name="synthetic_explicit_identity_collision.xlsx",
        source_file_sha256="e" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=collision.source_snapshot_date,
        total_rows=len(records),
        searchable_rows=sum(not record.is_quarantined for record in records),
        quarantined_rows=sum(record.is_quarantined for record in records),
    )
    write_database(path, records, manifest)
    question = "티커 ETF인 해외 ETF 상세 정보를 보여줘"
    question_id = "grounded-explicit-reserved-identity-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.OVERSEAS_ETP,
        family_evidence_span="해외 ETF",
        constraints=[
            GroundedConstraintProposal(
                field="ticker",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="티커 ETF",
            ),
            GroundedConstraintProposal(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                evidence_span="해외 ETF",
            ),
        ],
        ranking=[],
        limit=1,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    plan = GroundedPlanGate({ProductFamily.OVERSEAS_ETP: path}).compile(
        question,
        decision,
        proposal,
    )

    identity = next(item for item in plan.constraints if item.field == "product_id")
    assert identity.value == "AMX:ETF"


def test_grounded_gate_rejects_positive_boolean_inside_negated_phrase() -> None:
    question = _QUESTION.replace("현재 거래 가능한", "현재 거래 가능하지 않은")
    proposal = _proposal().model_copy(
        update={
            "constraints": [
                item.model_copy(update={"evidence_span": "현재 거래 가능하지 않은"})
                if item.field in {"sellable", "trading_suspended"}
                else item
                for item in _proposal().constraints
            ]
        }
    )

    with pytest.raises(GroundedPlanRejectedError, match="lexical grounding"):
        _compile(proposal, question)


def test_grounded_gate_rejects_ranking_subspan_hidden_inside_negation() -> None:
    question = _QUESTION.replace("AUM 큰 순서로", "AUM이 크지 않은 순서로")
    proposal = _proposal().model_copy(
        update={
            "ranking": [
                GroundedRankingProposal(
                    field="aum",
                    direction=SortDirection.DESC,
                    evidence_span="AUM이 크",
                )
            ]
        }
    )

    with pytest.raises(GroundedPlanRejectedError, match="ranking evidence is negated"):
        _compile(proposal, question)


def test_grounded_gate_does_not_turn_negated_public_fund_into_public_scope() -> None:
    question = "공모가 아닌 공모펀드를 보여줘"
    question_id = "grounded-negated-public-fund-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.FUND,
        family_evidence_span="공모펀드",
        constraints=[],
        ranking=[],
        limit=5,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="public-offering grounding"):
        GroundedPlanGate({}).compile(question, decision, proposal)


def test_grounded_gate_rejects_private_fund_constraint_even_if_verbatim() -> None:
    question = "사모인 공모펀드를 보여줘"
    question_id = "grounded-private-fund-001"
    decision = IntentRouter().route(question, question_id)
    proposal = GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.FUND,
        family_evidence_span="공모펀드",
        constraints=[
            GroundedConstraintProposal(
                field="public_offering",
                operator=ConstraintOperator.EQ,
                value=False,
                evidence_span="사모",
            )
        ],
        ranking=[],
        limit=5,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )

    with pytest.raises(GroundedPlanRejectedError, match="requires public_offering=true"):
        GroundedPlanGate({}).compile(question, decision, proposal)


def test_grounded_gate_cannot_override_unsupported_route() -> None:
    question = "내일 가장 오를 해외 ETF를 예측해서 매수 추천해줘"
    proposal = _proposal("grounded-unit-unsupported")
    decision = IntentRouter().route(question, proposal.question_id)

    with pytest.raises(GroundedPlanRejectedError, match="cannot be rescued"):
        GroundedPlanGate({}).compile(question, decision, proposal)


def test_grounded_schema_restricts_catalog_to_one_family() -> None:
    schema = grounded_plan_proposal_schema([ProductFamily.BOND])
    definitions = schema["$defs"]

    assert definitions["ProductFamily"]["enum"] == ["bond"]
    fields = definitions["GroundedConstraintProposal"]["properties"]["field"]["enum"]
    assert "buy_yield_pct" in fields
    assert "pension_eligible" not in fields
    assert "product_family" not in fields


def test_grounded_payload_canonicalizer_only_removes_cross_intent_authority() -> None:
    raw = _proposal().model_dump(mode="json")
    raw.update(
        {
            "intent": "compare",
            "ranking": [{"field": "aum", "direction": "desc", "evidence_span": "AUM 큰 순서로"}],
            "comparison_fields": [{"field": "aum", "evidence_span": "AUM"}],
            "group_by": [{"field": "product_type", "evidence_span": "ETF"}],
            "aggregations": [
                {"function": "count", "field": "product_id", "evidence_span": "상품 수"}
            ],
            "constraints": [
                *raw["constraints"],
                {
                    "field": "product_family",
                    "operator": "eq",
                    "value": "overseas_etp",
                    "evidence_span": "해외 ETF",
                },
            ],
        }
    )

    normalized = canonicalize_grounded_plan_proposal_payload(raw)

    assert normalized["ranking"] == []
    assert normalized["group_by"] == []
    assert normalized["aggregations"] == []
    assert normalized["comparison_fields"] == raw["comparison_fields"]
    assert all(item["field"] != "product_family" for item in normalized["constraints"])


def test_local_provider_returns_structured_grounded_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalTestProvider(
        LocalTestSettings(base_url="http://127.0.0.1:18000/v1", model="qwen-test")
    )
    expected = _proposal("wrong-model-question-id")
    captured: dict[str, object] = {}

    def fake_request(path: str, payload: dict[str, object]):
        captured.update(path=path, payload=payload)
        return {"choices": [{"message": {"content": expected.model_dump_json()}}]}

    monkeypatch.setattr(provider, "_request_json", fake_request)

    actual = provider.generate_grounded_plan(
        _QUESTION,
        "grounded-provider-001",
        ProductFamily.OVERSEAS_ETP,
    )

    assert actual.question_id == "grounded-provider-001"
    assert captured["path"] == "chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["$defs"]["ProductFamily"]["enum"] == ["overseas_etp"]
    messages = json.dumps(payload["messages"], ensure_ascii=False)
    assert "evidence_span" in messages


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "not-json"}}]},
        {"choices": [{"message": {"content": "[]"}}]},
    ],
)
def test_local_provider_rejects_malformed_grounded_transport_payload(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    provider = LocalTestProvider(
        LocalTestSettings(base_url="http://127.0.0.1:18000/v1", model="qwen-test")
    )
    monkeypatch.setattr(provider, "_request_json", lambda *_args, **_kwargs: response)

    with pytest.raises(LocalProviderError):
        provider.generate_grounded_plan(
            _QUESTION,
            "grounded-provider-malformed-001",
            ProductFamily.OVERSEAS_ETP,
        )


class _FailingGroundedPlanProvider:
    @property
    def provider_name(self) -> str:
        return "failing_test"

    @property
    def model_name(self) -> str:
        return "failing-grounded-model"

    def generate_grounded_plan(self, *args: object, **kwargs: object) -> GroundedPlanProposal:
        raise RuntimeError("simulated malformed provider output")


def test_grounded_provider_failure_reuses_server_plan(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        grounded_plan_provider=_FailingGroundedPlanProvider(),
    ).answer(
        "현재 거래 가능한 미국 채권형 해외 ETF 중 총보수 0.20% 이하를 AUM 높은 순으로 3개 보여줘",
        "grounded-provider-fallback-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.decision.reason_code != "grounded_model_plan_accepted"
    assert [product.ticker for product in result.products] == ["B6", "B5", "B4"]


def test_grounded_provider_failure_without_server_plan_fails_closed(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        grounded_plan_provider=_FailingGroundedPlanProvider(),
    ).answer(
        "B2 vs B3, 총보수율과 AUM 비교",
        "grounded-provider-fallback-002",
    )

    assert result.status == "clarify"
    assert result.query_plan is None
    assert result.products == []
