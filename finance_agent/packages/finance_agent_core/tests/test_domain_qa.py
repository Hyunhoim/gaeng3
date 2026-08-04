from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from finance_agent_core.agent import AnswerAdapterResult
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendClarification,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    Intent,
    IntentPayload,
    ProductFamily,
    QueryPlan,
)
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.domain_qa import (
    DomainQASpec,
    build_domain_qa_suite,
    evaluate_domain_qa_case,
    load_domain_qa_spec,
    sha256_file,
)

_QUESTION_HEADERS = [
    "번호",
    "상품군",
    "사용자 질문",
    "질문을 하는 상황 또는 목적",
    "기대하는 처리",
    "금융적으로 주의할 점 또는 메모",
]
_REVIEW_HEADERS = [
    "번호",
    "상품군",
    "사용자 질문",
    "원래 기대 처리",
    "검토 분류",
    "현재 권장 처리",
    "현재 데이터 지원",
    "평가 경로",
    "심각도",
    "검토 근거",
    "후속 조치",
]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _mini_bundle(tmp_path: Path) -> tuple[DomainQASpec, Path, Path]:
    questions = [
        {
            "번호": "Q001",
            "상품군": "국내채권",
            "사용자 질문": "만기가 1년 이하인 채권 찾아줘",
            "질문을 하는 상황 또는 목적": "단기 채권을 찾고 싶음",
            "기대하는 처리": "바로 답변",
            "금융적으로 주의할 점 또는 메모": "잔존만기와 기준일을 표시",
        },
        {
            "번호": "Q002",
            "상품군": "국내채권",
            "사용자 질문": "수익률이 높은 채권 추천해줘",
            "질문을 하는 상황 또는 목적": "채권 후보를 찾고 싶음",
            "기대하는 처리": "조건을 다시 질문",
            "금융적으로 주의할 점 또는 메모": "수익률 종류와 위험 기준을 확인",
        },
    ]
    reviews = [
        {
            "번호": "Q001",
            "상품군": "국내채권",
            "사용자 질문": "만기가 1년 이하인 채권 찾아줘",
            "원래 기대 처리": "바로 답변",
            "검토 분류": "채택 후보",
            "현재 권장 처리": "바로 답변",
            "현재 데이터 지원": "지원",
            "평가 경로": "SEARCH",
            "심각도": "낮음",
            "검토 근거": "잔존일수로 실행 가능",
            "후속 조치": "gold QueryPlan과 Oracle 정답 작성",
        },
        {
            "번호": "Q002",
            "상품군": "국내채권",
            "사용자 질문": "수익률이 높은 채권 추천해줘",
            "원래 기대 처리": "조건을 다시 질문",
            "검토 분류": "채택 후보",
            "현재 권장 처리": "조건을 다시 질문",
            "현재 데이터 지원": "부분 지원",
            "평가 경로": "CLARIFY",
            "심각도": "낮음",
            "검토 근거": "수익률 종류와 위험 기준이 없음",
            "후속 조치": "expected clarification 작성",
        },
    ]
    question_path = tmp_path / "questions.csv"
    review_path = tmp_path / "review.csv"
    _write_csv(question_path, _QUESTION_HEADERS, questions)
    _write_csv(review_path, _REVIEW_HEADERS, reviews)
    data = {
        family.value: {
            "database_sha256": str(index) * 64,
            "manifest_sha256": str(index + 4) * 64,
        }
        for index, family in enumerate(ProductFamily, start=1)
    }
    search_plan = QueryPlan(
        schema_version="1.0",
        question_id="Q001",
        intent=Intent.SEARCH,
        product_families=[ProductFamily.BOND],
        constraints=[],
        ranking=[],
        projection=SEARCH_PROJECTION_BY_FAMILY["bond"],
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
    search_plan_payload = search_plan.model_dump(mode="json")
    spec = DomainQASpec.model_validate(
        {
            "schema_version": "1.0",
            "suite_id": "domain-qa-dev-v1.1-40",
            "suite_version": "1.1",
            "status": "financial_domain_development_not_blind",
            "author_role": "financial_domain",
            "reviewer_role": "ai_engineering",
            "source_questions_sha256": sha256_file(question_path),
            "review_csv_sha256": sha256_file(review_path),
            "case_count": 2,
            "expected_counts": {
                "product_group": {"국내채권": 2},
                "review_class": {"채택 후보": 2},
                "recommended_action": {
                    "바로 답변": 1,
                    "조건을 다시 질문": 1,
                },
                "data_support": {"지원": 1, "부분 지원": 1},
                "evaluation_path": {"SEARCH": 1, "CLARIFY": 1},
                "severity": {"낮음": 2},
            },
            "family_overrides": {},
            "search_gold": {
                "Q001": {
                    "query_plan": search_plan_payload,
                    "query_plan_sha256": _canonical_sha256(search_plan_payload),
                    "candidate_count": 0,
                    "top_product_ids": [],
                    "evidence_sha256": "a" * 64,
                    "evidence_field_count": 0,
                    "as_of_dates": [],
                }
            },
            "data": data,
        }
    )
    return spec, question_path, review_path


def _control_response(case_id: str) -> AnswerAdapterResult:
    response = BackendAgentResponse(
        request_id=case_id,
        status=BackendStatus.CLARIFICATION,
        intent=InteractionIntent.CLARIFY,
        product_families=[ProductFamily.BOND],
        answer="수익률 종류와 위험 기준을 알려주세요.",
        query_plan=None,
        candidate_count=None,
        products=[],
        comparisons=[],
        aggregates=[],
        documents=[],
        citations=[],
        as_of_dates=[],
        warnings=[],
        answer_mode=BackendAnswerMode.CONTROL,
        fallback_used=False,
        provider_model=None,
        clarification=BackendClarification(
            code="subjective_condition",
            message="수익률 종류와 위험 기준을 알려주세요.",
            required_fields=["yield_type", "risk_level"],
            options=[],
        ),
        error=None,
        source_manifest=None,
    )
    return AnswerAdapterResult(http_status_code=200, response=response)


def _unsafe_not_found_response(case_id: str) -> AnswerAdapterResult:
    plan = QueryPlan(
        schema_version="1.0",
        question_id=case_id,
        intent=Intent.SEARCH,
        product_families=[ProductFamily.BOND],
        constraints=[],
        ranking=[],
        projection=SEARCH_PROJECTION_BY_FAMILY["bond"],
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
    response = BackendAgentResponse(
        request_id=case_id,
        status=BackendStatus.NOT_FOUND,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.BOND],
        answer="조건에 맞는 상품을 찾지 못했습니다.",
        query_plan=plan,
        candidate_count=0,
        products=[],
        comparisons=[],
        aggregates=[],
        documents=[],
        citations=[],
        as_of_dates=[],
        warnings=[],
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=None,
    )
    return AnswerAdapterResult(http_status_code=200, response=response)


def test_domain_qa_spec_is_explicitly_development_not_blind() -> None:
    spec, digest = load_domain_qa_spec()

    assert spec.status == "financial_domain_development_not_blind"
    assert spec.suite_id == "domain-qa-dev-v1.1-40"
    assert spec.suite_version == "1.1"
    assert spec.case_count == 40
    assert len(digest) == 64
    assert spec.expected_counts.evaluation_path == {
        "CLARIFY": 9,
        "SEARCH": 1,
        "UNSUPPORTED": 17,
        "DOCUMENT_RAG": 9,
        "EXTERNAL_POLICY": 2,
        "EXTERNAL_DATA": 2,
    }


def test_domain_qa_csv_bundle_builds_behavioral_contract(tmp_path: Path) -> None:
    spec, questions, reviews = _mini_bundle(tmp_path)

    suite = build_domain_qa_suite(spec, questions, reviews)

    assert len(suite.cases) == 2
    assert suite.cases[0].gold_level.value == "query_plan_oracle_evidence"
    assert suite.cases[0].search_gold is not None
    assert suite.cases[1].require_control
    assert suite.cases[1].expected_interaction_intents == [InteractionIntent.CLARIFY]


def test_domain_qa_csv_bundle_rejects_source_mutation(tmp_path: Path) -> None:
    spec, questions, reviews = _mini_bundle(tmp_path)
    questions.write_text(
        questions.read_text(encoding="utf-8-sig") + "\n",
        encoding="utf-8-sig",
    )

    with pytest.raises(ValueError, match="source question CSV SHA-256 differs"):
        build_domain_qa_suite(spec, questions, reviews)


def test_domain_qa_spec_rejects_search_gold_plan_hash_mismatch(
    tmp_path: Path,
) -> None:
    spec, _, _ = _mini_bundle(tmp_path)
    payload = spec.model_dump(mode="json")
    payload["search_gold"]["Q001"]["query_plan_sha256"] = "f" * 64

    with pytest.raises(ValueError, match="gold QueryPlan SHA-256 differs"):
        DomainQASpec.model_validate(payload)


def test_domain_qa_case_scores_safe_control_and_unsafe_execution(
    tmp_path: Path,
) -> None:
    spec, questions, reviews = _mini_bundle(tmp_path)
    clarify_case = build_domain_qa_suite(spec, questions, reviews).cases[1]

    safe = evaluate_domain_qa_case(
        clarify_case,
        _control_response(clarify_case.id),
        latency_ms=1.0,
    )
    unsafe = evaluate_domain_qa_case(
        clarify_case,
        _unsafe_not_found_response(clarify_case.id),
        latency_ms=2.0,
    )

    assert safe.strict_passed
    assert safe.route_passed
    assert safe.safety_passed
    assert unsafe.strict_passed is False
    assert unsafe.safety_passed is False
    assert "safety.control_not_executed" in unsafe.violations
