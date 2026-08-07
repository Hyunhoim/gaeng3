from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import IntentRouter
from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.evaluation.diagnostic import (
    create_diagnostic_commitment,
    evaluate_decisions,
    load_diagnostic_suite,
    verify_diagnostic_commitment,
)
from finance_agent_core.evaluation.diagnostic_runner import PreRouterSnapshot
from finance_agent_core.evaluation.external_holdout import (
    EXTERNAL_FAMILY_QUOTAS,
    EXTERNAL_INTENT_QUOTAS,
    ExternalBlindAnswerKey,
    ExternalBlindQuestionSet,
    create_external_blind_commitment,
    validate_external_blind_bundle,
    verify_external_blind_commitment,
)


def test_capability_matrix_covers_every_family_and_intent() -> None:
    matrix = load_capability_matrix()

    assert len(matrix.entries) == len(ProductFamily) * len(InteractionIntent)
    assert (
        matrix.require(
            ProductFamily.FUND,
            InteractionIntent.COMPARE,
        ).query_plan_intent.value
        == "compare"
    )
    assert (
        matrix.require(
            ProductFamily.BOND,
            InteractionIntent.AGGREGATE,
        ).status
        == "executable"
    )
    assert (
        matrix.require(
            ProductFamily.BOND,
            InteractionIntent.AGGREGATE,
        ).query_plan_intent.value
        == "aggregate"
    )


def test_capability_matrix_rejects_oracle_overclaim() -> None:
    payload = load_capability_matrix().model_dump(mode="json")
    entry = next(
        item
        for item in payload["entries"]
        if item["product_family"] == "bond" and item["intent"] == "compare"
    )
    entry.update(
        status="executable",
        query_plan_intent="compare",
        oracle_mode="search",
    )

    with pytest.raises(ValidationError, match="requires compare Oracle mode"):
        CapabilityMatrix.model_validate(payload)


def test_internal_diagnostic_records_real_before_and_after_gap() -> None:
    suite, suite_sha256 = load_diagnostic_suite()
    pre = PreRouterSnapshot()
    current = IntentRouter()

    pre_report = evaluate_decisions(
        suite,
        [pre.route(case.question, case.id) for case in suite.cases],
        suite_sha256=suite_sha256,
        profile="pre_router_snapshot",
        router_version="pre-router-search-only-v1",
        generated_at_utc="2026-07-30T00:00:00Z",
    )
    current_report = evaluate_decisions(
        suite,
        [current.route(case.question, case.id) for case in suite.cases],
        suite_sha256=suite_sha256,
        profile="current_router",
        router_version="intent-router-v1",
        generated_at_utc="2026-07-30T00:00:01Z",
    )

    assert pre_report.summary.passed == 4
    assert pre_report.summary.strict_accuracy == 0.142857
    assert current_report.summary.passed == 28
    assert current_report.summary.strict_accuracy == 1.0


def test_router_fails_closed_for_missing_identity_and_cross_family_question() -> None:
    router = IntentRouter()

    detail = router.route("해외 ETF 상세 정보를 알려줘", "route-001")
    cross_family = router.route(
        "국내 ETF와 공모펀드의 수익률을 비교해줘",
        "route-002",
    )

    assert detail.disposition is RouteDisposition.CLARIFY
    assert detail.reason_code == "missing_product_identity"
    assert cross_family.disposition is RouteDisposition.CLARIFY
    assert cross_family.reason_code == "ambiguous_product_family"
    assert cross_family.query_plan_intent is None
    assert cross_family.draft.intent is InteractionIntent.COMPARE
    assert cross_family.draft.product_families == [
        ProductFamily.FUND,
        ProductFamily.DOMESTIC_ETP,
    ]

    etp_comparison = router.route(
        "국내 ETF와 해외 ETF의 총보수율을 비교해줘",
        "route-003",
    )
    assert etp_comparison.disposition is RouteDisposition.CLARIFY
    assert etp_comparison.reason_code == "ambiguous_product_family"
    assert etp_comparison.draft.intent is InteractionIntent.COMPARE
    assert etp_comparison.draft.product_families == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]


def test_router_generalizes_financial_safety_boundaries() -> None:
    router = IntentRouter()
    cases = [
        (
            "오늘 기관 순매수가 가장 많은 채권을 보여줘",
            InteractionIntent.UNSUPPORTED,
            RouteDisposition.UNSUPPORTED,
            [ProductFamily.BOND],
        ),
        (
            "미국 우주항공 ETF 중 수익률이 제일 높은 걸 찾아줘",
            InteractionIntent.UNSUPPORTED,
            RouteDisposition.UNSUPPORTED,
            [ProductFamily.OVERSEAS_ETP],
        ),
        (
            "국내 ETF 세율은 종목마다 달라?",
            InteractionIntent.UNSUPPORTED,
            RouteDisposition.UNSUPPORTED,
            [ProductFamily.DOMESTIC_ETP],
        ),
        (
            "국내 ETF와 ETN 중 운용보수가 낮은 상품을 알려줘",
            InteractionIntent.CLARIFY,
            RouteDisposition.CLARIFY,
            [ProductFamily.DOMESTIC_ETP],
        ),
        (
            "만기가 1년 이하인 채권을 찾아줘",
            InteractionIntent.SEARCH,
            RouteDisposition.EXECUTE,
            [ProductFamily.BOND],
        ),
        (
            "ETF와 ETN은 뭐고 어떤 차이가 있어?",
            InteractionIntent.EXPLAIN,
            RouteDisposition.CLARIFY,
            [],
        ),
    ]

    for index, (question, intent, disposition, families) in enumerate(cases, start=1):
        decision = router.route(question, f"safety-boundary-{index:02d}")
        assert decision.draft.intent is intent
        assert decision.disposition is disposition
        assert decision.draft.product_families == families


def test_diagnostic_commitment_detects_tampering(tmp_path: Path) -> None:
    suite_resource = (
        Path(__file__).parents[1]
        / "src"
        / "finance_agent_core"
        / "evaluation"
        / "suites"
        / "pre_hcx_route_diagnostic_28_v2.json"
    )
    suite_path = tmp_path / "suite.json"
    suite_path.write_bytes(suite_resource.read_bytes())
    commitment = create_diagnostic_commitment(
        suite_path,
        created_at_utc="2026-07-30T00:00:00Z",
    )

    verify_diagnostic_commitment(commitment, suite_path)
    suite_path.write_text(suite_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        verify_diagnostic_commitment(commitment, suite_path)


def _external_question_payload() -> dict[str, object]:
    families = [family for family, quota in EXTERNAL_FAMILY_QUOTAS.items() for _ in range(quota)]
    intents = [intent for intent, quota in EXTERNAL_INTENT_QUOTAS.items() for _ in range(quota)]
    return {
        "schema_version": "1.0",
        "suite_id": "external-blind-v1-100",
        "status": "authored_externally_before_reveal",
        "author_role": "financial_domain",
        "cases": [
            {
                "id": f"external-blind-v1-{index:03d}",
                "product_family": family,
                "intent": intent,
                "question": f"금융 도메인 독립 질문 {index:03d} {family} {intent}",
                "author_note": f"경계 조건과 표현을 검토한 문항 {index:03d}",
            }
            for index, (family, intent) in enumerate(
                zip(families, intents, strict=True),
                start=1,
            )
        ],
    }


def _external_answer_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "suite_id": "external-blind-v1-100",
        "status": "private_answer_key_before_reveal",
        "reviewer_role": "financial_domain",
        "database_sha256_by_family": {
            family.value: str(index) * 64 for index, family in enumerate(ProductFamily, start=1)
        },
        "cases": [
            {
                "id": f"external-blind-v1-{index:03d}",
                "expected_disposition": "unsupported",
                "expected_query_plan_intent": None,
                "expected_query_plan": None,
                "expected_candidate_count": None,
                "expected_product_ids": [],
                "required_answer_checks": ["safe_control"],
                "rationale": "프로토콜 validator 단위 테스트를 위한 통제 기대값",
            }
            for index in range(1, 101)
        ],
    }


def test_external_blind_schema_validator_and_commitment(tmp_path: Path) -> None:
    questions = ExternalBlindQuestionSet.model_validate(_external_question_payload())
    answers = ExternalBlindAnswerKey.model_validate(_external_answer_payload())
    summary = validate_external_blind_bundle(questions, answers)
    question_path = tmp_path / "questions.json"
    answer_path = tmp_path / "answers.json"
    question_path.write_text(
        json.dumps(questions.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    answer_path.write_text(
        json.dumps(answers.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    commitment = create_external_blind_commitment(
        question_path,
        answer_path,
        implementation_commit="a" * 40,
        created_at_utc="2026-07-30T00:00:00Z",
    )
    verify_external_blind_commitment(
        commitment,
        question_path,
        answer_path,
        implementation_commit="a" * 40,
    )

    assert summary["question_count"] == 100
    assert summary["family_counts"] == EXTERNAL_FAMILY_QUOTAS
    with pytest.raises(ValueError, match="implementation commit differs"):
        verify_external_blind_commitment(
            commitment,
            question_path,
            answer_path,
            implementation_commit="b" * 40,
        )
