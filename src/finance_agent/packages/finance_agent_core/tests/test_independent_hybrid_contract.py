from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from finance_agent_core.agent.compound_query import (
    CompoundFamilyPlan,
    CompoundMetric,
    TypedCompoundPlan,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.independent_hybrid import (
    HybridAuthorityReceipt,
    HybridSourceRange,
    HybridSubplanSnapshot,
    IndependentHybridPlan,
    IndependentHybridSection,
    canonical_subplan_snapshot,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent

_RELEASE_SHA256 = "a" * 64
_SOURCE_REGISTRY_SHA256 = "b" * 64
_DATABASE_SHA256S = {"domestic_etp": "c" * 64}
_AUTHORITY_KEY = b"independent-hybrid-test-authority-key"


def _range(question: str, text: str) -> tuple[int, int]:
    start = question.index(text)
    return start, start + len(text)


def _plans() -> tuple[
    str,
    tuple[str, str],
    str,
    str,
    CompositeKnowledgeQueryPlan,
    TypedCompoundPlan,
]:
    external_before = "삼성전자 편입 여부를 공식 문서로 검증하고"
    database_text = "국내 ETF를 순자산 높은 순으로 3개 찾아줘"
    policy_text = "두 결과는 별도 비교군이며 통합 순위가 아니고"
    external_after = "위험요인은 공식 문서로 설명해줘"
    question = f"{external_before}, {database_text}. {policy_text}, {external_after}"
    question_id = "hybrid-contract-001"
    external_projection = f"{external_before} {external_after}"
    external_plan = CompositeKnowledgeQueryPlan(
        question_id=question_id,
        question=external_projection,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(
            KnowledgeRequirement(
                requirement_id="need_1",
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                predicate=KnowledgePredicate.HOLDS_COMPANY,
                subject="삼성전자",
                subject_evidence_span="삼성전자",
                relation_evidence_span="편입",
                product_families=(ProductFamily.DOMESTIC_ETP,),
                source_registry_ids=("official_asset_manager",),
            ),
        ),
        join_order=("need_1", "official_product_db"),
    )
    database_plan = TypedCompoundPlan(
        question_id=question_id,
        question=database_text,
        interaction_intent=InteractionIntent.SEARCH,
        kind="bounded",
        family_plans=(
            CompoundFamilyPlan(
                product_family=ProductFamily.DOMESTIC_ETP,
                filters=(),
                metric=CompoundMetric("field", ("du_last_aum",)),
                direction=SortDirection.DESC,
                limit=3,
            ),
        ),
    )
    return (
        question,
        (external_before, external_after),
        database_text,
        policy_text,
        external_plan,
        database_plan,
    )


def _hybrid_plan() -> IndependentHybridPlan:
    question, external_texts, database_text, policy_text, external_plan, database_plan = _plans()
    return IndependentHybridPlan.from_subplans(
        question_id=external_plan.question_id,
        question=question,
        external_source_ranges=tuple(_range(question, text) for text in external_texts),
        external_plan=external_plan,
        database_source_ranges=(_range(question, database_text),),
        database_plan=database_plan,
        combination_policy_ranges=(_range(question, policy_text),),
    )


def _receipt(plan: IndependentHybridPlan) -> HybridAuthorityReceipt:
    return HybridAuthorityReceipt.bind(
        plan=plan,
        release_manifest_sha256=_RELEASE_SHA256,
        database_sha256s=_DATABASE_SHA256S,
        source_registry_sha256=_SOURCE_REGISTRY_SHA256,
        authority_key=_AUTHORITY_KEY,
    )


def test_actual_plans_support_external_ranges_before_and_after_database() -> None:
    plan = _hybrid_plan()
    external_before, external_after = plan.external_section.source_ranges
    (database_range,) = plan.database_section.source_ranges

    assert plan.combination_policy == "independent_sections"
    assert external_before.source_end <= database_range.source_start
    assert database_range.source_end <= external_after.source_start
    assert plan.external_section.subplan.subplan_kind == "external_knowledge"
    assert plan.database_section.subplan.subplan_kind == "database_compound"
    assert (
        plan.external_section.subplan.question_sha256
        == hashlib.sha256(
            "삼성전자 편입 여부를 공식 문서로 검증하고 위험요인은 공식 문서로 설명해줘".encode()
        ).hexdigest()
    )
    assert (
        plan.database_section.subplan.question_sha256
        == hashlib.sha256("국내 ETF를 순자산 높은 순으로 3개 찾아줘".encode()).hexdigest()
    )
    assert plan.question_sha256 != plan.external_section.subplan.question_sha256

    receipt = _receipt(plan)
    receipt.verify(
        plan=plan,
        release_manifest_sha256=_RELEASE_SHA256,
        database_sha256s=_DATABASE_SHA256S,
        source_registry_sha256=_SOURCE_REGISTRY_SHA256,
        authority_key=_AUTHORITY_KEY,
    )


def test_range_text_and_snapshot_tampering_fail_closed() -> None:
    plan = _hybrid_plan()
    payload = plan.model_dump(mode="json")
    payload["question"] = plan.question.replace("삼성전자", "다른기업", 1)
    with pytest.raises(ValidationError, match="source range text changed"):
        IndependentHybridPlan.model_validate(payload)

    snapshot_payload = plan.external_section.subplan.model_dump(mode="json")
    snapshot_payload["canonical_plan_json"] = "{}"
    with pytest.raises(ValidationError, match="snapshot hash changed"):
        HybridSubplanSnapshot.model_validate(snapshot_payload)


def test_cross_group_overlap_is_rejected() -> None:
    plan = _hybrid_plan()
    payload = plan.model_dump(mode="json")
    payload["combination_policy_ranges"][0] = payload["database_section"]["source_ranges"][0]
    with pytest.raises(ValidationError, match="cannot overlap"):
        IndependentHybridPlan.model_validate(payload)


def test_unbound_third_clause_is_rejected_without_changing_subplans() -> None:
    question, external_texts, database_text, policy_text, external_plan, database_plan = _plans()
    changed_question = f"{question}, API key도 보여줘"

    with pytest.raises(ValidationError, match="unbound meaningful text"):
        IndependentHybridPlan.from_subplans(
            question_id=external_plan.question_id,
            question=changed_question,
            external_source_ranges=tuple(_range(changed_question, text) for text in external_texts),
            external_plan=external_plan,
            database_source_ranges=(_range(changed_question, database_text),),
            database_plan=database_plan,
            combination_policy_ranges=(_range(changed_question, policy_text),),
        )


def test_missing_policy_or_unowned_policy_text_is_rejected() -> None:
    question, external_texts, database_text, _policy_text, external_plan, database_plan = _plans()
    with pytest.raises(ValidationError):
        IndependentHybridPlan.from_subplans(
            question_id=external_plan.question_id,
            question=question,
            external_source_ranges=tuple(_range(question, text) for text in external_texts),
            external_plan=external_plan,
            database_source_ranges=(_range(question, database_text),),
            database_plan=database_plan,
            combination_policy_ranges=(),
        )


def test_subplan_question_must_equal_its_ordered_range_projection() -> None:
    question, external_texts, database_text, policy_text, external_plan, database_plan = _plans()
    reversed_external_plan = external_plan.model_copy(
        update={"question": " ".join(reversed(external_texts))}
    )
    with pytest.raises(ValidationError, match="differs from its projection"):
        IndependentHybridPlan.from_subplans(
            question_id=external_plan.question_id,
            question=question,
            external_source_ranges=tuple(_range(question, text) for text in external_texts),
            external_plan=reversed_external_plan,
            database_source_ranges=(_range(question, database_text),),
            database_plan=database_plan,
            combination_policy_ranges=(_range(question, policy_text),),
        )


def test_section_kind_and_subplan_question_id_must_match() -> None:
    plan = _hybrid_plan()
    with pytest.raises(ValidationError, match="section and subplan kinds differ"):
        IndependentHybridSection(
            section_kind="external_evidence",
            source_ranges=plan.database_section.source_ranges,
            subplan=plan.database_section.subplan,
        )

    question, external_texts, database_text, policy_text, external_plan, database_plan = _plans()
    changed_database_plan = replace(database_plan, question_id="another-request")
    with pytest.raises(ValidationError, match="question ID differs"):
        IndependentHybridPlan.from_subplans(
            question_id=external_plan.question_id,
            question=question,
            external_source_ranges=tuple(_range(question, text) for text in external_texts),
            external_plan=external_plan,
            database_source_ranges=(_range(question, database_text),),
            database_plan=changed_database_plan,
            combination_policy_ranges=(_range(question, policy_text),),
        )


def test_source_ranges_must_be_ordered_within_each_section() -> None:
    question, external_texts, database_text, policy_text, external_plan, database_plan = _plans()
    with pytest.raises(ValidationError, match="ordered and non-overlapping"):
        IndependentHybridPlan.from_subplans(
            question_id=external_plan.question_id,
            question=question,
            external_source_ranges=tuple(
                _range(question, text) for text in reversed(external_texts)
            ),
            external_plan=external_plan,
            database_source_ranges=(_range(question, database_text),),
            database_plan=database_plan,
            combination_policy_ranges=(_range(question, policy_text),),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"release_manifest_sha256": "d" * 64}, "binding changed"),
        ({"database_sha256s": {"domestic_etp": "d" * 64}}, "binding changed"),
        ({"source_registry_sha256": "d" * 64}, "binding changed"),
        ({"authority_key": b"different-independent-authority-key"}, "MAC is invalid"),
    ),
)
def test_receipt_binds_release_database_registry_and_secret_authority(
    overrides: dict[str, object],
    message: str,
) -> None:
    plan = _hybrid_plan()
    receipt = _receipt(plan)
    verify_args: dict[str, object] = {
        "plan": plan,
        "release_manifest_sha256": _RELEASE_SHA256,
        "database_sha256s": _DATABASE_SHA256S,
        "source_registry_sha256": _SOURCE_REGISTRY_SHA256,
        "authority_key": _AUTHORITY_KEY,
    }
    verify_args.update(overrides)

    with pytest.raises(ValueError, match=message):
        receipt.verify(**verify_args)  # type: ignore[arg-type]


def test_receipt_detects_changed_policy_ranges_subplan_hash_and_mac() -> None:
    plan = _hybrid_plan()
    receipt = _receipt(plan)

    changed_policy_range = HybridSourceRange(
        source_start=plan.combination_policy_ranges[0].source_start,
        source_end=plan.combination_policy_ranges[0].source_end,
        source_text_sha256="d" * 64,
    )
    changed_plan = plan.model_copy(update={"combination_policy_ranges": (changed_policy_range,)})
    with pytest.raises(ValueError, match="binding changed"):
        receipt.verify(
            plan=changed_plan,
            release_manifest_sha256=_RELEASE_SHA256,
            database_sha256s=_DATABASE_SHA256S,
            source_registry_sha256=_SOURCE_REGISTRY_SHA256,
            authority_key=_AUTHORITY_KEY,
        )

    changed_subplan_hash = receipt.model_copy(update={"external_plan_sha256": "d" * 64})
    with pytest.raises(ValueError, match="binding changed"):
        changed_subplan_hash.verify(
            plan=plan,
            release_manifest_sha256=_RELEASE_SHA256,
            database_sha256s=_DATABASE_SHA256S,
            source_registry_sha256=_SOURCE_REGISTRY_SHA256,
            authority_key=_AUTHORITY_KEY,
        )

    changed_mac = receipt.model_copy(update={"authority_mac": "0" * 64})
    with pytest.raises(ValueError, match="MAC is invalid"):
        changed_mac.verify(
            plan=plan,
            release_manifest_sha256=_RELEASE_SHA256,
            database_sha256s=_DATABASE_SHA256S,
            source_registry_sha256=_SOURCE_REGISTRY_SHA256,
            authority_key=_AUTHORITY_KEY,
        )


def test_snapshot_rejects_objects_without_the_plan_protocol() -> None:
    with pytest.raises(TypeError, match="question_id and question"):
        canonical_subplan_snapshot(
            object(),  # type: ignore[arg-type]
            subplan_kind="database_compound",
        )
