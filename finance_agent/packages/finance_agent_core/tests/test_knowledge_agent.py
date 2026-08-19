from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from finance_agent_core.agent.knowledge_cli import main as knowledge_cli_main
from finance_agent_core.agent.knowledge_service import (
    KnowledgeAgent,
    KnowledgeServiceError,
)
from finance_agent_core.answering.claims import (
    KnowledgeAnswerContext,
    KnowledgeAnswerDraft,
    expected_knowledge_answer_draft,
)
from finance_agent_core.contracts.knowledge import (
    DocumentKnowledgeOperation,
    KnowledgePlanAuthorityError,
    KnowledgePlanAuthorityGate,
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.release import (
    DocumentRetrievalArtifactRelease,
    KnowledgeRetrievalRelease,
    RelationRetrievalArtifactRelease,
)
from finance_agent_core.retrieval import (
    DocumentInput,
    DocumentSourceKind,
    RelationIndexError,
    RelationType,
    SQLiteDocumentIndex,
    SQLiteRelationIndex,
    VerifiedProductDatabase,
    build_provided_relation_index,
)
from finance_agent_core.storage.approval import sha256_file
from finance_agent_core.storage.identity_cache import load_product_identities


class SyntheticDatabaseVerifier:
    def __init__(self, approval_manifest_sha256: str = "f" * 64) -> None:
        self._approval_manifest_sha256 = approval_manifest_sha256

    @property
    def approval_manifest_sha256(self) -> str:
        return self._approval_manifest_sha256

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase:
        resolved = Path(path).resolve(strict=True)
        manifest, identities = load_product_identities(resolved)
        if manifest.dataset != product_family.value:
            raise RelationIndexError("synthetic verifier family mismatch")
        return VerifiedProductDatabase(
            product_family=product_family,
            path=resolved,
            manifest=manifest,
            database_sha256=sha256_file(resolved),
            identities=identities,
        )


class FakeClaimProvider:
    provider_name = "fake-structured-claims"
    model_name = "fake-model-v1"

    def __init__(self, behavior: Literal["valid", "product", "excerpt", "error"] = "valid"):
        self.behavior = behavior
        self.calls = 0

    def generate_claims(self, context: KnowledgeAnswerContext) -> KnowledgeAnswerDraft:
        self.calls += 1
        if self.behavior == "error":
            raise TimeoutError("synthetic provider timeout")
        expected = expected_knowledge_answer_draft(context)
        payload = expected.model_dump(mode="python")
        if self.behavior == "product":
            payload["claims"][0]["product_id"] = "INVENTED-PRODUCT"
        elif self.behavior == "excerpt":
            payload["claims"][0]["excerpt"] = "문서에 없는 수익률 99% 주장"
        return KnowledgeAnswerDraft.model_validate(payload)


def _relation_plan(
    *,
    query: str = "테스트운용",
    top_k: int = 3,
) -> KnowledgeQueryPlan:
    return KnowledgeQueryPlan(
        question_id="relation-q1",
        question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        operation=RelationKnowledgeOperation(
            query=query,
            relation_types=(RelationType.MANAGED_BY,),
            product_families=(ProductFamily.DOMESTIC_ETP,),
            top_k=top_k,
        ),
    )


def _document_plan(query: str = "위험등급 손실 가능성") -> KnowledgeQueryPlan:
    return KnowledgeQueryPlan(
        question_id="document-q1",
        question="금융상품 위험등급이 무엇인지 설명해줘",
        operation=DocumentKnowledgeOperation(
            query=query,
            source_kinds=(DocumentSourceKind.PROVIDED,),
            top_k=2,
        ),
    )


@pytest.fixture
def relation_agent_factory(tmp_path: Path, domestic_sample_database):
    product_database, _, _ = domestic_sample_database
    relation_index = tmp_path / "relations.sqlite3"
    verifier = SyntheticDatabaseVerifier()
    build_provided_relation_index(
        {ProductFamily.DOMESTIC_ETP: product_database},
        relation_index,
        verifier=verifier,
    )
    manifest = SQLiteRelationIndex(relation_index).manifest()
    release = KnowledgeRetrievalRelease(
        relation=RelationRetrievalArtifactRelease(
            index_sha256=sha256_file(relation_index),
            approval_manifest_sha256=manifest.approval_manifest_sha256,
            relation_set_sha256=manifest.relation_set_sha256,
        )
    )

    def create(provider: FakeClaimProvider | None = None) -> KnowledgeAgent:
        return KnowledgeAgent(
            release=release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=verifier,
            claim_provider=provider,
        )

    return create, relation_index, release, product_database


@pytest.fixture
def document_agent_factory(tmp_path: Path):
    document_index = tmp_path / "documents.sqlite3"
    index = SQLiteDocumentIndex(document_index)
    index.initialize()
    index.ingest(
        DocumentInput(
            document_id="provided-risk-terms",
            title="금융상품 위험등급 용어",
            text="위험등급은 금융상품의 손실 가능성을 비교하기 위한 분류입니다.",
            source_uri="approved://provided-risk-terms",
            source_kind=DocumentSourceKind.PROVIDED,
            as_of=date(2026, 7, 11),
            metadata={"category": "glossary"},
        )
    )
    os.chmod(document_index, 0o444)
    release = KnowledgeRetrievalRelease(
        document=DocumentRetrievalArtifactRelease(
            index_sha256=sha256_file(document_index),
            corpus_manifest_sha256="a" * 64,
            file_manifest_sha256="b" * 64,
        )
    )

    def create(provider: FakeClaimProvider | None = None) -> KnowledgeAgent:
        return KnowledgeAgent(
            release=release,
            document_index_path=document_index,
            claim_provider=provider,
        )

    return create, document_index, release


def test_typed_relation_plan_rejects_fund_and_incompatible_relations() -> None:
    with pytest.raises(ValidationError, match="fund relation search is disabled"):
        RelationKnowledgeOperation(
            query="운용사",
            relation_types=(RelationType.MANAGED_BY,),
            product_families=(ProductFamily.FUND,),
        )
    with pytest.raises(ValidationError, match="issued_by is unavailable"):
        RelationKnowledgeOperation(
            query="한국전력공사",
            relation_types=(RelationType.ISSUED_BY,),
            product_families=(ProductFamily.DOMESTIC_ETP,),
        )


def test_typed_plan_requires_canonical_unique_filters() -> None:
    with pytest.raises(ValidationError, match="canonical sorted order"):
        RelationKnowledgeOperation(
            query="미국",
            relation_types=(RelationType.INVESTS_IN_REGION,),
            product_families=(
                ProductFamily.OVERSEAS_ETP,
                ProductFamily.DOMESTIC_ETP,
            ),
        )
    with pytest.raises(ValidationError, match="source_kinds must not contain duplicates"):
        DocumentKnowledgeOperation(
            query="위험등급",
            source_kinds=(DocumentSourceKind.PROVIDED, DocumentSourceKind.PROVIDED),
        )


def test_relation_plan_allows_only_one_predicate_per_request() -> None:
    with pytest.raises(ValidationError, match="at most 1 item"):
        RelationKnowledgeOperation(
            query="미국",
            relation_types=(
                RelationType.CLASSIFIED_AS_ASSET,
                RelationType.INVESTS_IN_REGION,
            ),
            product_families=(ProductFamily.DOMESTIC_ETP,),
        )


def test_knowledge_plan_forbids_untyped_extra_operations() -> None:
    payload = _relation_plan().model_dump(mode="python")
    payload["operation"]["aggregation"] = {"field": "aum", "function": "avg"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeQueryPlan.model_validate(payload)


def test_exact_plan_gate_rejects_any_model_change() -> None:
    server = _relation_plan()
    proposal = _relation_plan(top_k=4)

    with pytest.raises(KnowledgePlanAuthorityError, match="differs"):
        KnowledgePlanAuthorityGate().authorize(
            server,
            proposal,
            proposal_provider_name="hyperclova",
            proposal_model_name="HCX-007",
        )


def test_exact_plan_gate_records_matching_provider_proposal() -> None:
    plan = _relation_plan()
    validated = KnowledgePlanAuthorityGate().authorize(
        plan,
        KnowledgeQueryPlan.model_validate_json(plan.model_dump_json()),
        proposal_provider_name="hyperclova",
        proposal_model_name="HCX-007",
    )

    assert validated.plan == plan
    assert validated.receipt.status == "authorized_exact_match"
    assert validated.receipt.proposal_provider_name == "hyperclova"


def test_relation_agent_runs_exact_plan_and_verified_structured_claims(
    relation_agent_factory,
) -> None:
    provider = FakeClaimProvider()
    agent = relation_agent_factory[0](provider)

    result = agent.execute(_relation_plan())

    assert result.status == "found"
    assert result.candidate_count == 3
    assert result.answer.mode == "structured_grounded"
    assert result.answer.verification.passed
    assert provider.calls == 1
    assert [item.product_name for item in result.relation_response.evidence] == [
        "국내 테스트 A000002",
        "국내 테스트 A000003",
        "국내 테스트 A000004",
    ]
    assert "evidence relation:" in result.answer.answer
    assert "투자 추천이나 인과관계를 뜻하지 않습니다" in result.answer.answer


def test_relation_claim_hallucination_uses_full_deterministic_fallback(
    relation_agent_factory,
) -> None:
    agent = relation_agent_factory[0](FakeClaimProvider("product"))

    result = agent.execute(_relation_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert not result.answer.verification.passed
    assert result.answer.draft.claims[0].product_id == "INVENTED-PRODUCT"
    assert "국내 테스트 A000002" in result.answer.answer
    assert "INVENTED-PRODUCT" not in result.answer.answer


def test_relation_provider_failure_uses_deterministic_fallback(
    relation_agent_factory,
) -> None:
    result = relation_agent_factory[0](FakeClaimProvider("error")).execute(_relation_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert result.answer.draft is None
    assert result.answer.verification.violations[0].startswith("TimeoutError")


def test_relation_not_found_never_calls_claim_provider(relation_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = relation_agent_factory[0](provider).execute(_relation_plan(query="존재하지않는운용사"))

    assert result.status == "not_found"
    assert result.candidate_count == 0
    assert result.answer.mode == "deterministic"
    assert provider.calls == 0


def test_relation_release_rejects_writable_or_mismatched_index(
    relation_agent_factory,
) -> None:
    create, relation_index, release, product_database = relation_agent_factory
    os.chmod(relation_index, 0o644)
    with pytest.raises(KnowledgeServiceError, match="read-only"):
        create().execute(_relation_plan())

    os.chmod(relation_index, 0o444)
    bad_release = release.model_copy(
        update={
            "relation": release.relation.model_copy(update={"approval_manifest_sha256": "e" * 64})
        }
    )
    with pytest.raises(KnowledgeServiceError, match="manifest differs"):
        KnowledgeAgent(
            release=bad_release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=SyntheticDatabaseVerifier(),
        ).execute(_relation_plan())


def test_document_agent_returns_approved_exact_excerpts(document_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = document_agent_factory[0](provider).execute(_document_plan())

    assert result.status == "found"
    assert result.candidate_count == 1
    assert result.answer.mode == "structured_grounded"
    assert result.document_response.evidence[0].source_kind is DocumentSourceKind.PROVIDED
    assert "손실 가능성을 비교하기 위한 분류" in result.answer.answer
    assert provider.calls == 1


def test_document_claim_not_in_source_uses_fallback(document_agent_factory) -> None:
    provider = FakeClaimProvider("excerpt")
    result = document_agent_factory[0](provider).execute(_document_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert not result.answer.verification.passed
    assert "99%" not in result.answer.answer
    assert "손실 가능성" in result.answer.answer


def test_document_not_found_never_calls_claim_provider(document_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = document_agent_factory[0](provider).execute(_document_plan("존재하지않는용어"))

    assert result.status == "not_found"
    assert result.answer.mode == "deterministic"
    assert provider.calls == 0


def test_document_release_rejects_index_permission_drift(document_agent_factory) -> None:
    create, document_index, _ = document_agent_factory
    os.chmod(document_index, 0o644)

    with pytest.raises(KnowledgeServiceError, match="read-only"):
        create().execute(_document_plan())


def test_knowledge_release_requires_a_pinned_artifact() -> None:
    with pytest.raises(ValidationError, match="at least one artifact"):
        KnowledgeRetrievalRelease()


def test_claim_schema_has_no_free_form_summary_or_numeric_claim_field() -> None:
    schema = KnowledgeAnswerDraft.model_json_schema()
    serialized = str(schema)

    assert "summary" not in serialized
    assert "answer" not in serialized
    assert "numeric_value" not in serialized


@pytest.mark.parametrize("kind", ["plan", "release", "result", "answer-draft"])
def test_knowledge_cli_exports_strict_schemas(kind: str, capsys) -> None:
    assert knowledge_cli_main(["schema", "--kind", kind]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert payload["additionalProperties"] is False


def test_knowledge_cli_rejects_ambiguous_duplicate_json_keys(tmp_path: Path) -> None:
    plan_path = tmp_path / "duplicate-plan.json"
    plan_path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")

    with pytest.raises(SystemExit, match="duplicate JSON key"):
        knowledge_cli_main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--release",
                str(plan_path),
            ]
        )
