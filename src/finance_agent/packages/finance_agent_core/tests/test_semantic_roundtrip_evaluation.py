from __future__ import annotations

import json

import pytest

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    NullPlacement,
    ProductFamily,
    QueryPlan,
    Ranking,
    SortDirection,
    Unit,
)
from finance_agent_core.evaluation.metamorphic import SEMANTIC_ROUNDTRIP_AXES
from finance_agent_core.evaluation.official_mock import load_official_mock_suite
from finance_agent_core.evaluation.semantic_roundtrip import (
    LocalQwenSemanticQuestionProvider,
    build_semantic_plan_spec,
    build_semantic_roundtrip_system_prompt,
    load_semantic_roundtrip_protocol,
    validate_semantic_question,
)
from finance_agent_core.evaluation.semantics import query_plan_semantic_sha256


def _complex_search_plan() -> QueryPlan:
    return QueryPlan(
        schema_version="1.0",
        question_id="semantic-test",
        intent=Intent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        constraints=[
            Constraint(
                field="total_expense_ratio_pct",
                operator=ConstraintOperator.LTE,
                value=0.2,
                unit=Unit.PCT_POINT,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="asset_type",
                operator=ConstraintOperator.EQ,
                value="Bond",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="investment_region",
                operator=ConstraintOperator.EQ,
                value="United States of America",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="sellable",
                operator=ConstraintOperator.EQ,
                value=True,
                unit=Unit.BOOLEAN,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="trading_suspended",
                operator=ConstraintOperator.EQ,
                value=False,
                unit=Unit.BOOLEAN,
                strength=ConstraintStrength.LOCKED,
            ),
        ],
        ranking=[
            Ranking(
                field="aum",
                direction=SortDirection.DESC,
                nulls=NullPlacement.LAST,
            )
        ],
        projection=[
            "product_id",
            "product_name",
            "ticker",
            "total_expense_ratio_pct",
            "aum",
            "trading_currency",
            "dynamic_as_of",
        ],
        limit=5,
        intent_payload=IntentPayload(
            comparison_fields=[],
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )


def test_protocol_hides_source_wording_and_accounts_for_all_cases() -> None:
    loaded = load_semantic_roundtrip_protocol()
    source = load_official_mock_suite()

    assert loaded.protocol.source_question_hidden_from_generator is True
    assert len(loaded.protocol.source_case_ids) == 25
    assert set(loaded.protocol.source_case_ids) | set(loaded.protocol.excluded_case_ids) == {
        case.id for case in source.suite.cases
    }
    assert loaded.protocol.axes == list(SEMANTIC_ROUNDTRIP_AXES)


def test_semantic_prompt_contains_plan_meaning_but_not_public_source_question() -> None:
    spec = build_semantic_plan_spec(_complex_search_plan())
    prompt = build_semantic_roundtrip_system_prompt(spec)
    public_source = (
        "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 총보수 0.20% 이하인 "
        "상품을 AUM 순으로 5개 보여줘."
    )

    assert public_source not in prompt
    assert "0.2%" in prompt
    assert "미국" in prompt
    assert "채권" in prompt
    assert '결과_개수": 5' in prompt
    assert "total_expense_ratio_pct" not in prompt


def test_semantic_validator_accepts_new_wording_and_rejects_meaning_loss() -> None:
    spec = build_semantic_plan_spec(_complex_search_plan())
    sources = [
        "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 총보수 0.20% 이하인 "
        "상품을 AUM 순으로 5개 보여줘."
    ]
    question = (
        "거래할 수 있는 미국 채권형 해외 ETF 가운데 보수율 0.2% 이하만 추려서 "
        "순자산이 큰 것부터 5개 찾아줘"
    )

    accepted = validate_semantic_question(question, spec, source_questions=sources)
    wrong_number = validate_semantic_question(
        question.replace("0.2%", "0.25%"),
        spec,
        source_questions=sources,
    )
    missing_limit = validate_semantic_question(
        question.replace("5개", "몇 개"),
        spec,
        source_questions=sources,
    )

    assert accepted.passed
    assert "numeric_constraints_present" in wrong_number.violations
    assert "result_limit_present" in missing_limit.violations


def test_plan_semantic_hash_ignores_request_and_commutative_order_but_not_looser_filter() -> None:
    plan = _complex_search_plan()
    reordered_payload = plan.model_dump(mode="json")
    reordered_payload["question_id"] = "another-request"
    reordered_payload["constraints"] = list(reversed(reordered_payload["constraints"]))
    reordered_payload["projection"] = list(reversed(reordered_payload["projection"]))
    reordered = QueryPlan.model_validate(reordered_payload)

    looser_payload = plan.model_dump(mode="json")
    product_type = next(
        item for item in looser_payload["constraints"] if item["field"] == "product_type"
    )
    product_type.update(operator="in", value=["ETN", "ETF"])
    looser = QueryPlan.model_validate(looser_payload)

    assert query_plan_semantic_sha256(plan) == query_plan_semantic_sha256(reordered)
    assert query_plan_semantic_sha256(plan) != query_plan_semantic_sha256(looser)


def test_plan_semantic_hash_treats_all_registered_etp_types_as_no_filter() -> None:
    plan = _complex_search_plan()
    no_type_payload = plan.model_dump(mode="json")
    no_type_payload["constraints"] = [
        item for item in no_type_payload["constraints"] if item["field"] != "product_type"
    ]
    all_types_payload = json.loads(json.dumps(no_type_payload))
    all_types_payload["constraints"].append(
        {
            "field": "product_type",
            "operator": "in",
            "value": ["ETN", "ETF"],
            "unit": "code",
            "strength": "locked",
        }
    )

    assert query_plan_semantic_sha256(
        QueryPlan.model_validate(no_type_payload)
    ) == query_plan_semantic_sha256(QueryPlan.model_validate(all_types_payload))


def test_local_qwen_semantic_provider_uses_only_semantic_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalQwenSemanticQuestionProvider(
        LocalTestSettings(base_url="http://127.0.0.1:18000/v1", model="qwen-test")
    )
    spec = build_semantic_plan_spec(_complex_search_plan())
    captured: dict[str, object] = {}
    questions = [
        "거래 가능한 미국 채권형 해외 ETF 중 총보수 0.2% 이하를 AUM 큰 순으로 5개 조회",
        "미국 채권 ETF인데 지금 거래되고 보수가 0.2% 이하인 것, 순자산 큰 순 5개 알려줘",
        "해외 ETF 미국 채권형 거래가능 총보수 0.2% 이하 AUM 내림차순 5개",
    ]

    def fake_request(path: str, payload: dict[str, object]):
        captured.update(path=path, payload=payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "variants": [
                                    {"axis": axis.value, "question": question}
                                    for axis, question in zip(
                                        SEMANTIC_ROUNDTRIP_AXES,
                                        questions,
                                        strict=True,
                                    )
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(provider._client, "_request_json", fake_request)

    generated = provider.generate_questions(spec, SEMANTIC_ROUNDTRIP_AXES)

    assert [item.question for item in generated] == questions
    assert captured["path"] == "chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0.8
    messages = payload["messages"]
    assert isinstance(messages, list)
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    assert "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서" not in serialized_messages
    assert payload["response_format"]["json_schema"]["strict"] is True
