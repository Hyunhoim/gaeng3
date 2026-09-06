from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeRequirement,
    predicate_for_capability,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_verifier import (
    ExternalKnowledgeEvidenceBundle,
    ExternalKnowledgeEvidenceVerifier,
)
from finance_agent_core.retrieval.document_dense import (
    ApprovedDocumentCacheSource,
    DocumentCacheActivationReceipt,
)
from finance_agent_core.retrieval.external_knowledge_release import (
    _verify_activation_against_corpus,
    _verify_relations_against_corpus,
    build_cache_first_external_knowledge_artifact,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
    TemporalRelationSearchRequest,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_CORPUS_SHA = "1" * 64
_DENSE_SHA = "2" * 64
_KURE_REVISION = "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"


def test_sealed_issuer_risk_scope_is_searchable_without_product_aliasing() -> None:
    registry = load_official_source_registry()
    capability = KnowledgeCapability.PRODUCT_RISK_FACTORS
    relation = TemporalRelationRecord(
        relation_id="woori-bank-bond-risk-test",
        capability=capability,
        predicate=predicate_for_capability(capability),
        subject=TemporalEntity(
            entity_id="product-woori-bond",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name="우리은행26-08이표일(01)1년",
        ),
        object=TemporalEntity(
            entity_id="risk-market-volatility",
            kind=TemporalEntityKind.RISK_CLAIM,
            canonical_name="시장 변동성으로 손실이 발생할 수 있습니다.",
        ),
        product_family=ProductFamily.BOND,
        product_id="KR6000034E90:test",
        observed_at=date(2026, 8, 19),
        evidence=(
            ExternalEvidenceReference(
                evidence_id="woori-bank-risk-evidence",
                source_registry_id="open-dart-disclosures",
                document_id="dart-20260819000123",
                title="우리은행 — 반기보고서 (2026.06)",
                publisher="금융감독원",
                source_uri=(
                    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260819000123"
                ),
                published_at=date(2026, 8, 19),
                as_of=date(2026, 8, 19),
                content_sha256="d" * 64,
                locator="extracted chunk 1",
                excerpt="우리은행은 시장 변동성으로 손실이 발생할 위험이 있습니다.",
            ),
        ),
        scope_entities=(
            TemporalEntity(
                entity_id="company-woori-bank",
                kind=TemporalEntityKind.COMPANY,
                canonical_name="우리은행",
            ),
        ),
    )
    bundle = seal_temporal_relation_bundle(
        (relation,),
        corpus_manifest_sha256=_CORPUS_SHA,
        registry=registry,
    )
    store = TemporalRelationStore(bundle, registry)

    found = store.search(
        TemporalRelationSearchRequest(
            capability=capability,
            subject="우리은행",
            product_families=(ProductFamily.BOND,),
        )
    )

    assert found == (relation,)
    assert "우리은행" not in relation.subject.labels()
    assert not store.search(
        TemporalRelationSearchRequest(
            capability=capability,
            subject="우리카드",
            product_families=(ProductFamily.BOND,),
        )
    )

    question = "우리은행 발행사의 위험요인"
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=capability,
        predicate=predicate_for_capability(capability),
        subject="우리은행",
        subject_evidence_span="우리은행",
        relation_evidence_span="위험요인",
        product_families=(ProductFamily.BOND,),
        source_registry_ids=("open-dart-disclosures",),
    )
    plan = CompositeKnowledgeQueryPlan(
        question_id="issuer-risk-scope-test",
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=registry.evidence_cutoff,
        requirements=(requirement,),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    verification = ExternalKnowledgeEvidenceVerifier().verify(
        ExternalKnowledgeEvidenceBundle(
            plan=plan,
            relations=(relation,),
            requirement_relations={"need_1": (relation.relation_id,)},
        ),
        approved_product_ids={(ProductFamily.BOND, relation.product_id)},
    )
    assert verification.passed


def _activation() -> DocumentCacheActivationReceipt:
    registry = load_official_source_registry()
    return DocumentCacheActivationReceipt(
        source_registry_sha256=registry.canonical_sha256,
        evidence_cutoff=registry.evidence_cutoff,
        approved_corpus_manifest_sha256=_CORPUS_SHA,
        approved_corpus_file_manifest_sha256="3" * 64,
        document_dense_artifact_sha256=_DENSE_SHA,
        document_dense_vector_payload_sha256="4" * 64,
        document_bm25_sha256="5" * 64,
        embedding_snapshot_manifest_sha256="6" * 64,
        provider=EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="kure-v1-test",
            model_id="nlpai-lab/KURE-v1",
            model_revision=_KURE_REVISION,
            license_id="cc-by-nc-4.0",
            dimension=1024,
            pooling="cls",
        ),
        calibration_report_sha256="7" * 64,
        minimum_dense_score=0.5,
        minimum_dense_margin=0.05,
        top_k=5,
        documents=(
            ApprovedDocumentCacheSource(
                document_id="fsc-growth-fund-structure",
                title="국민성장펀드 구조",
                publisher="금융위원회",
                source_registry_id="fsc-policy",
                source_uri="https://www.fsc.go.kr/po010103/86950",
                capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
                content_sha256="8" * 64,
                normalized_text_sha256="9" * 64,
            ),
        ),
    )


def _cache_only_coverage(
    *,
    corpus_sha256: str = _CORPUS_SHA,
    index_sha256: str = _DENSE_SHA,
) -> ApprovedKnowledgeCoverageManifest:
    active = KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="synthetic-cache-only-coverage",
        dataset_release_id="synthetic-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="approved" if capability is active else "not_configured",
                runtime_execution_enabled=capability is active,
                allowed_source_kinds=("provided", "external_approved"),
                corpus_sha256=corpus_sha256 if capability is active else None,
                index_sha256=index_sha256 if capability is active else None,
            )
            for capability in KnowledgeCapability
        ),
    )


def _live_coverage() -> ApprovedKnowledgeCoverageManifest:
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="synthetic-live-coverage",
        dataset_release_id="synthetic-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="approved_live_official",
                runtime_execution_enabled=True,
                allowed_source_kinds=("provided", "external_approved"),
            )
            for capability in KnowledgeCapability
        ),
    )


def _relations(activation: DocumentCacheActivationReceipt):
    return seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=activation.approved_corpus_manifest_sha256,
        registry=load_official_source_registry(),
    )


def _relations_with_unactivated_capability(activation: DocumentCacheActivationReceipt):
    capability = KnowledgeCapability.CORPORATE_SUBSIDIARY
    relation = TemporalRelationRecord(
        relation_id="ecopro-subsidiary-test",
        capability=capability,
        predicate=predicate_for_capability(capability),
        subject=TemporalEntity(
            entity_id="company-ecopro",
            kind=TemporalEntityKind.COMPANY,
            canonical_name="에코프로",
        ),
        object=TemporalEntity(
            entity_id="company-ecopro-bm",
            kind=TemporalEntityKind.COMPANY,
            canonical_name="에코프로비엠",
        ),
        observed_at=date(2026, 8, 24),
        evidence=(
            ExternalEvidenceReference(
                evidence_id="ecopro-subsidiary-evidence",
                source_registry_id="ecopro-corporate",
                document_id="ecopro-family-page",
                title="에코프로 가족사",
                publisher="에코프로",
                source_uri="https://www.ecopro.co.kr/sub010302",
                published_at=None,
                as_of=date(2026, 8, 24),
                content_sha256="a" * 64,
                locator="가족사",
                excerpt="에코프로비엠은 에코프로의 가족사입니다.",
            ),
        ),
    )
    return seal_temporal_relation_bundle(
        (relation,),
        corpus_manifest_sha256=activation.approved_corpus_manifest_sha256,
        registry=load_official_source_registry(),
    )


def test_bundled_cache_only_authority_rejects_unbound_activation() -> None:
    activation = _activation()

    with pytest.raises(ValueError, match="cache-only capability is not bound"):
        build_cache_first_external_knowledge_artifact(
            activation=activation,
            relation_bundle=_relations(activation),
            retrieval_mode="approved_corpus_cache_only",
        )

    coverage = load_approved_knowledge_coverage_manifest()
    assert {
        entry.capability
        for entry in coverage.capabilities
        if entry.runtime_execution_enabled
    } == {
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    }
    assert all(
        entry.status == ("approved" if entry.runtime_execution_enabled else "not_configured")
        for entry in coverage.capabilities
    )


def test_cache_only_release_binds_capability_corpus_index_and_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation = _activation()
    coverage = _cache_only_coverage()
    monkeypatch.setattr(
        "finance_agent_core.retrieval.external_knowledge_release."
        "load_approved_knowledge_coverage_manifest",
        lambda: coverage,
    )

    artifact = build_cache_first_external_knowledge_artifact(
        activation=activation,
        relation_bundle=_relations(activation),
        retrieval_mode="approved_corpus_cache_only",
    )

    assert artifact.coverage_manifest_sha256 == coverage.canonical_sha256
    assert artifact.corpus_manifest_sha256 == _CORPUS_SHA
    assert artifact.document_dense_sha256 == _DENSE_SHA
    assert artifact.document_bm25_sha256 == activation.document_bm25_sha256
    assert artifact.document_activation_receipt_sha256 == activation.canonical_sha256


@pytest.mark.parametrize(
    ("corpus_sha256", "index_sha256"),
    [("a" * 64, _DENSE_SHA), (_CORPUS_SHA, "b" * 64)],
)
def test_cache_only_release_rejects_capability_hash_substitution(
    monkeypatch: pytest.MonkeyPatch,
    corpus_sha256: str,
    index_sha256: str,
) -> None:
    activation = _activation()
    monkeypatch.setattr(
        "finance_agent_core.retrieval.external_knowledge_release."
        "load_approved_knowledge_coverage_manifest",
        lambda: _cache_only_coverage(
            corpus_sha256=corpus_sha256,
            index_sha256=index_sha256,
        ),
    )

    with pytest.raises(ValueError, match="cache-only capability is not bound"):
        build_cache_first_external_knowledge_artifact(
            activation=activation,
            relation_bundle=_relations(activation),
            retrieval_mode="approved_corpus_cache_only",
        )


def test_cache_only_release_rejects_enabled_capability_absent_from_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation = _activation()
    coverage = _cache_only_coverage()
    missing = KnowledgeCapability.PORTFOLIO_HOLDINGS
    entries = tuple(
        entry.model_copy(
            update={
                "status": "approved",
                "runtime_execution_enabled": True,
                "corpus_sha256": _CORPUS_SHA,
                "index_sha256": _DENSE_SHA,
            }
        )
        if entry.capability is missing
        else entry
        for entry in coverage.capabilities
    )
    coverage = coverage.model_copy(update={"capabilities": entries})
    monkeypatch.setattr(
        "finance_agent_core.retrieval.external_knowledge_release."
        "load_approved_knowledge_coverage_manifest",
        lambda: coverage,
    )

    with pytest.raises(ValueError, match="absent from the activation receipt"):
        build_cache_first_external_knowledge_artifact(
            activation=activation,
            relation_bundle=_relations(activation),
            retrieval_mode="approved_corpus_cache_only",
        )


def test_cache_release_rejects_relation_capability_absent_from_activation() -> None:
    activation = _activation()

    with pytest.raises(ValueError, match="capability absent from the document cache"):
        build_cache_first_external_knowledge_artifact(
            activation=activation,
            relation_bundle=_relations_with_unactivated_capability(activation),
            retrieval_mode="approved_corpus_cache_first",
        )


def test_bootstrap_guard_rejects_corpus_and_receipt_with_crafted_publisher() -> None:
    activation = _activation()
    forged_source = activation.documents[0].model_copy(
        update={"publisher": "금융위원회 사칭 출처"},
    )
    forged_activation = activation.model_copy(update={"documents": (forged_source,)})
    forged_corpus_document = SimpleNamespace(
        approval=SimpleNamespace(
            document_id=forged_source.document_id,
            title=forged_source.title,
            publisher=forged_source.publisher,
            source_registry_id=forged_source.source_registry_id,
            source_uri=forged_source.source_uri,
            capabilities=forged_source.capabilities,
            published_at=forged_source.published_at,
            as_of=forged_source.as_of,
            content_sha256=forged_source.content_sha256,
            normalized_text_sha256=forged_source.normalized_text_sha256,
        )
    )

    with pytest.raises(ValueError, match="Source Registry identity differs"):
        _verify_activation_against_corpus(
            activation=forged_activation,
            corpus=SimpleNamespace(documents=(forged_corpus_document,)),  # type: ignore[arg-type]
            registry=load_official_source_registry(),
        )


def test_bootstrap_guard_binds_relation_evidence_to_exact_approved_text() -> None:
    source = _activation().documents[0]
    excerpt = "국민성장펀드는 정책금융과 민간자금이 함께 참여합니다."
    corpus_document = SimpleNamespace(
        approval=SimpleNamespace(
            document_id=source.document_id,
            title=source.title,
            publisher=source.publisher,
            source_registry_id=source.source_registry_id,
            source_uri=source.source_uri,
            capabilities=source.capabilities,
            published_at=source.published_at,
            as_of=source.as_of,
            normalized_text_sha256=source.normalized_text_sha256,
        ),
        text=f"공식 자료\n{excerpt}\n",
    )
    capability = KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    relation = TemporalRelationRecord(
        relation_id="national-growth-fund-structure",
        capability=capability,
        predicate=predicate_for_capability(capability),
        subject=TemporalEntity(
            entity_id="fund-national-growth",
            kind=TemporalEntityKind.FUND,
            canonical_name="국민성장펀드",
        ),
        object=TemporalEntity(
            entity_id="event-public-private-capital",
            kind=TemporalEntityKind.POLICY_EVENT,
            canonical_name="정책금융과 민간자금 참여",
        ),
        observed_at=date(2026, 5, 21),
        evidence=(
            ExternalEvidenceReference(
                evidence_id="national-growth-fund-evidence",
                source_registry_id=source.source_registry_id,
                document_id=source.document_id,
                title=source.title,
                publisher=source.publisher,
                source_uri=source.source_uri,
                published_at=source.published_at,
                as_of=source.as_of,
                content_sha256=source.normalized_text_sha256,
                locator="paragraph 1",
                excerpt=excerpt,
            ),
        ),
    )
    bundle = seal_temporal_relation_bundle(
        (relation,),
        corpus_manifest_sha256=_CORPUS_SHA,
        registry=load_official_source_registry(),
    )
    corpus = SimpleNamespace(documents=(corpus_document,))

    _verify_relations_against_corpus(
        relation_bundle=bundle,
        corpus=corpus,  # type: ignore[arg-type]
    )

    forged = bundle.model_copy(
        update={
            "relations": (
                relation.model_copy(
                    update={
                        "evidence": (
                            relation.evidence[0].model_copy(
                                update={"excerpt": "승인 문서에 없는 관계 주장"}
                            ),
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="excerpt is absent"):
        _verify_relations_against_corpus(
            relation_bundle=forged,
            corpus=corpus,  # type: ignore[arg-type]
        )


def test_live_cache_first_release_keeps_distinct_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation = _activation()
    coverage = _live_coverage()
    monkeypatch.setattr(
        "finance_agent_core.retrieval.external_knowledge_release."
        "load_approved_knowledge_coverage_manifest",
        lambda: coverage,
    )

    artifact = build_cache_first_external_knowledge_artifact(
        activation=activation,
        relation_bundle=_relations(activation),
        retrieval_mode="approved_corpus_cache_first",
    )

    assert artifact.retrieval_mode == "approved_corpus_cache_first"
    assert artifact.coverage_manifest_sha256 == coverage.canonical_sha256
