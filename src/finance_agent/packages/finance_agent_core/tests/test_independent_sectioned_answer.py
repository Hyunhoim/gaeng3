from __future__ import annotations

import pytest
from pydantic import ValidationError

# The public agent package initializes the existing answering package in its
# supported order before this independently importable helper is loaded.
from finance_agent_core.agent import RoutedFinanceAgent as _RoutedFinanceAgent  # noqa: F401
from finance_agent_core.answering.independent_sections import (
    INDEPENDENT_RANKING_NOTICE,
    IndependentAnswerSection,
    IndependentSectionCitation,
    IndependentSectionId,
    IndependentSectionRanking,
    IndependentSectionStatus,
    SectionedAnswerAtomicityError,
    SectionedAnswerVerificationError,
    SectionedFinalAnswerDraft,
    bind_sectioned_final_answer,
    build_independent_section_bundle,
    namespace_section_reference,
    verify_sectioned_final_answer,
)

EXTERNAL = IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE
DATABASE = IndependentSectionId.PROVIDED_DATABASE_COMPOUND
APPROVED_LEAD = "검증된 상품 데이터와 근거를 바탕으로 조회 결과를 안내합니다."


def _ref(section_id: IndependentSectionId, raw: str) -> str:
    return namespace_section_reference(section_id, raw)


def _external_section(
    *,
    product_ids: tuple[str, ...] = ("overseas_etp:EXT-001",),
    direction: str = "desc",
    status: IndependentSectionStatus = IndependentSectionStatus.SUCCESS,
) -> IndependentAnswerSection:
    complete = status in {
        IndependentSectionStatus.SUCCESS,
        IndependentSectionStatus.NOT_FOUND,
    }
    not_found = status is IndependentSectionStatus.NOT_FOUND
    public_product_ids = () if not_found or not complete else product_ids
    return IndependentAnswerSection(
        section_id=EXTERNAL,
        status=status,
        plan_kind="composite_external_knowledge",
        plan_sha256="a" * 64,
        candidate_count=(0 if not_found else 7) if complete else None,
        product_ids=public_product_ids,
        evidence_refs=(_ref(EXTERNAL, "document:doc-1"),) if complete else (),
        citation_ids=(_ref(EXTERNAL, "official-document:doc-1"),) if complete else (),
        ranking=(
            IndependentSectionRanking(
                field_id="aum",
                direction=direction,
                limit=3,
                ordered_product_ids=public_product_ids,
                value_source="provided_product_database",
            )
            if complete and not not_found
            else None
        ),
        deterministic_answer=(
            "공식 문서와 제공 DB의 교집합 결과입니다."
            if complete and not not_found
            else "공식 문서를 확인했지만 제공 DB와의 교집합은 0건입니다."
            if not_found
            else None
        ),
    )


def _database_section(
    *,
    product_ids: tuple[str, ...] = ("bond:DB-001", "bond:DB-002"),
    status: IndependentSectionStatus = IndependentSectionStatus.SUCCESS,
) -> IndependentAnswerSection:
    complete = status in {
        IndependentSectionStatus.SUCCESS,
        IndependentSectionStatus.NOT_FOUND,
    }
    not_found = status is IndependentSectionStatus.NOT_FOUND
    public_product_ids = () if not_found or not complete else product_ids
    return IndependentAnswerSection(
        section_id=DATABASE,
        status=status,
        plan_kind="typed_compound_two_stage",
        plan_sha256="b" * 64,
        candidate_count=(0 if not_found else 18) if complete else None,
        product_ids=public_product_ids,
        evidence_refs=(_ref(DATABASE, "product-field:yield"),) if complete else (),
        citation_ids=(_ref(DATABASE, "product:yield"),) if complete else (),
        ranking=(
            IndependentSectionRanking(
                field_id="buy_yield_pct",
                direction="desc",
                limit=2,
                ordered_product_ids=public_product_ids,
                value_source="provided_product_database",
            )
            if complete and not not_found
            else None
        ),
        deterministic_answer=(
            "제공 DB에서 독립적으로 계산한 채권 순위입니다."
            if complete and not not_found
            else "제공 DB의 독립 조건 결과는 0건입니다."
            if not_found
            else None
        ),
    )


def _citations() -> tuple[IndependentSectionCitation, ...]:
    return (
        IndependentSectionCitation(
            section_id=EXTERNAL,
            citation_id=_ref(EXTERNAL, "official-document:doc-1"),
            evidence_refs=(_ref(EXTERNAL, "document:doc-1"),),
        ),
        IndependentSectionCitation(
            section_id=DATABASE,
            citation_id=_ref(DATABASE, "product:yield"),
            evidence_refs=(_ref(DATABASE, "product-field:yield"),),
        ),
    )


def _bundle():
    return build_independent_section_bundle(
        external_section=_external_section(),
        database_section=_database_section(),
        citations=_citations(),
    )


def test_two_independent_sections_preserve_public_authority_metadata() -> None:
    bundle = _bundle()

    assert bundle.combination_policy == "independent_sections_no_cross_ranking"
    assert bundle.ranking_notice == INDEPENDENT_RANKING_NOTICE
    assert "통합 순위가 아닙니다" in bundle.deterministic_answer
    assert [item.section_id for item in bundle.sections] == [EXTERNAL, DATABASE]
    assert [item.status for item in bundle.sections] == [
        IndependentSectionStatus.SUCCESS,
        IndependentSectionStatus.SUCCESS,
    ]
    assert [item.candidate_count for item in bundle.sections] == [7, 18]
    assert [item.plan_sha256 for item in bundle.sections] == ["a" * 64, "b" * 64]
    assert bundle.sections[0].product_ids == ("overseas_etp:EXT-001",)
    assert bundle.sections[1].product_ids == ("bond:DB-001", "bond:DB-002")
    assert all(
        citation.citation_id.startswith(f"{citation.section_id.value}::")
        for citation in bundle.citations
    )


def test_verified_not_found_is_a_complete_independent_section() -> None:
    external = _external_section(status=IndependentSectionStatus.NOT_FOUND)
    citations = (
        IndependentSectionCitation(
            section_id=EXTERNAL,
            citation_id=_ref(EXTERNAL, "official-document:doc-1"),
            evidence_refs=(_ref(EXTERNAL, "document:doc-1"),),
        ),
        _citations()[1],
    )

    bundle = build_independent_section_bundle(
        external_section=external,
        database_section=_database_section(),
        citations=citations,
    )

    assert bundle.sections[0].status is IndependentSectionStatus.NOT_FOUND
    assert bundle.sections[0].candidate_count == 0
    assert bundle.sections[0].product_ids == ()


def test_verified_not_found_can_have_no_public_identity_excerpt() -> None:
    external = IndependentAnswerSection.model_validate(
        {
            **_external_section(
                status=IndependentSectionStatus.NOT_FOUND
            ).model_dump(mode="python"),
            "evidence_refs": (),
            "citation_ids": (),
        }
    )
    bundle = build_independent_section_bundle(
        external_section=external,
        database_section=_database_section(),
        citations=(_citations()[1],),
    )

    assert bundle.sections[0].status is IndependentSectionStatus.NOT_FOUND
    assert bundle.sections[0].evidence_refs == ()
    assert bundle.sections[0].citation_ids == ()


@pytest.mark.parametrize(
    ("external_status", "database_status"),
    [
        (IndependentSectionStatus.CANNOT_VERIFY, IndependentSectionStatus.SUCCESS),
        (IndependentSectionStatus.SUCCESS, IndependentSectionStatus.ERROR),
        (IndependentSectionStatus.CLARIFICATION, IndependentSectionStatus.SUCCESS),
    ],
)
def test_one_failed_section_forbids_partial_answer(
    external_status: IndependentSectionStatus,
    database_status: IndependentSectionStatus,
) -> None:
    with pytest.raises(SectionedAnswerAtomicityError, match="partial public answer"):
        build_independent_section_bundle(
            external_section=_external_section(status=external_status),
            database_section=_database_section(status=database_status),
            citations=(),
        )


def test_citation_and_evidence_references_cannot_cross_section_namespace() -> None:
    with pytest.raises(ValidationError, match="outside its namespace"):
        IndependentSectionCitation(
            section_id=EXTERNAL,
            citation_id=_ref(EXTERNAL, "official-document:doc-1"),
            evidence_refs=(_ref(DATABASE, "product-field:yield"),),
        )

    with pytest.raises(ValueError, match="cannot move between namespaces"):
        namespace_section_reference(EXTERNAL, _ref(DATABASE, "product-field:yield"))


def test_hclx_can_add_only_an_approved_lead_to_the_frozen_sections() -> None:
    bundle = _bundle()
    context = bind_sectioned_final_answer(bundle, allowed_leads=(APPROVED_LEAD,))
    draft = SectionedFinalAnswerDraft(
        lead=APPROVED_LEAD,
        section_manifest_sha256=context.section_manifest_sha256,
        answer_body_sha256=context.answer_body_sha256,
    )

    answer = verify_sectioned_final_answer(context, draft)

    assert answer == f"{APPROVED_LEAD}\n{bundle.deterministic_answer}"


@pytest.mark.parametrize("changed_hash", ["section", "answer"])
def test_hclx_hash_change_is_rejected(changed_hash: str) -> None:
    context = bind_sectioned_final_answer(_bundle(), allowed_leads=(APPROVED_LEAD,))
    payload = {
        "lead": APPROVED_LEAD,
        "section_manifest_sha256": context.section_manifest_sha256,
        "answer_body_sha256": context.answer_body_sha256,
    }
    hash_field = (
        f"{changed_hash}_manifest_sha256" if changed_hash == "section" else "answer_body_sha256"
    )
    payload[hash_field] = "f" * 64

    with pytest.raises(SectionedAnswerVerificationError, match="HCX changed"):
        verify_sectioned_final_answer(context, SectionedFinalAnswerDraft(**payload))


@pytest.mark.parametrize("mutation", ["product_order", "ranking_direction"])
def test_product_or_ranking_mutation_changes_section_manifest(mutation: str) -> None:
    original = _bundle()
    external = _external_section()
    database = _database_section()
    if mutation == "product_order":
        database = _database_section(product_ids=("bond:DB-002", "bond:DB-001"))
    else:
        external = _external_section(direction="asc")
    changed = build_independent_section_bundle(
        external_section=external,
        database_section=database,
        citations=_citations(),
    )

    assert changed.answer_body_sha256 == original.answer_body_sha256
    assert changed.section_manifest_sha256 != original.section_manifest_sha256
    changed_context = bind_sectioned_final_answer(changed, allowed_leads=(APPROVED_LEAD,))
    stale_draft = SectionedFinalAnswerDraft(
        lead=APPROVED_LEAD,
        section_manifest_sha256=original.section_manifest_sha256,
        answer_body_sha256=original.answer_body_sha256,
    )
    with pytest.raises(SectionedAnswerVerificationError, match="product, ranking"):
        verify_sectioned_final_answer(changed_context, stale_draft)
