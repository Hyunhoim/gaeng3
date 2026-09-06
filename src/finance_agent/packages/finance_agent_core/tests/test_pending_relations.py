from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.corpus import (
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    ExternalCorpusIntakeSpec,
    approved_corpus_manifest_bytes,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusSource,
    collect_pending_corpus,
)
from finance_agent_core.retrieval.pending_relations import (
    PendingRelationError,
    PendingTemporalRelationBundle,
    promote_pending_relations_to_approved_corpus,
    verify_pending_relation_candidates,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
)


class _Transport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        del timeout_seconds, max_bytes
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body="<p>국민성장펀드는 정책금융과 민간자금이 함께 참여합니다.</p>".encode(),
        )


class _NoPdf:
    def extract(self, content: bytes) -> str:
        raise AssertionError(len(content))


def test_pending_relation_requires_verbatim_snapshot_evidence(tmp_path) -> None:
    url = "https://www.fsc.go.kr/po010103/86950"
    root = tmp_path / "pending"
    manifest = collect_pending_corpus(
        root,
        connector=OfficialSourceConnector(_Transport()),
        pdf_text_extractor=_NoPdf(),
        catalog=PendingCorpusCatalog(
            catalog_id="pending-relation-test",
            evidence_cutoff=date(2026, 8, 24),
            documents=(
                PendingCorpusSource(
                    document_id="national-growth-fund",
                    title="국민성장펀드 구조",
                    source_uri=url,
                    capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                    published_at=date(2026, 5, 21),
                    as_of=date(2026, 5, 21),
                    language="ko",
                    purposes=("product_structure",),
                ),
            ),
        ),
    )
    document = manifest.documents[0]
    relation = TemporalRelationRecord(
        relation_id="relation-national-growth-structure",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        predicate="describes_fund_structure_trend",
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
                evidence_id="evidence-national-growth-structure",
                source_registry_id=document.source_registry_id,
                document_id=document.document_id,
                title=document.title,
                publisher=document.publisher,
                source_uri=document.source_uri,
                published_at=document.published_at,
                as_of=document.as_of,
                content_sha256=document.text_sha256,
                locator="paragraph 1",
                excerpt="국민성장펀드는 정책금융과 민간자금이 함께 참여합니다.",
            ),
        ),
    )

    bundle = verify_pending_relation_candidates(root, (relation,))
    assert not bundle.production_enabled
    assert bundle.status == "not_authorized_for_retrieval_or_release"

    forged = relation.model_copy(
        update={
            "evidence": (relation.evidence[0].model_copy(update={"excerpt": "존재하지 않는 근거"}),)
        }
    )
    with pytest.raises(PendingRelationError, match="not verbatim"):
        verify_pending_relation_candidates(root, (forged,))


def _pending_and_approved_relation_fixture(tmp_path):
    url = "https://www.fsc.go.kr/po010103/86950"
    root = tmp_path / "promotion"
    pending_manifest = collect_pending_corpus(
        root,
        connector=OfficialSourceConnector(_Transport()),
        pdf_text_extractor=_NoPdf(),
        catalog=PendingCorpusCatalog(
            catalog_id="pending-relation-promotion-test",
            evidence_cutoff=date(2026, 8, 24),
            documents=(
                PendingCorpusSource(
                    document_id="national-growth-fund",
                    title="국민성장펀드 구조",
                    source_uri=url,
                    capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                    published_at=date(2026, 5, 21),
                    as_of=date(2026, 5, 21),
                    language="ko",
                    purposes=("product_structure",),
                ),
            ),
        ),
    )
    document = pending_manifest.documents[0]
    relation = TemporalRelationRecord(
        relation_id="relation-national-growth-promotion",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        predicate="describes_fund_structure_trend",
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
                evidence_id="evidence-national-growth-promotion",
                source_registry_id=document.source_registry_id,
                document_id=document.document_id,
                title=document.title,
                publisher=document.publisher,
                source_uri=document.source_uri,
                published_at=document.published_at,
                as_of=document.as_of,
                content_sha256=document.text_sha256,
                locator="paragraph 1",
                excerpt="국민성장펀드는 정책금융과 민간자금이 함께 참여합니다.",
            ),
        ),
    )
    pending = verify_pending_relation_candidates(root, (relation,))
    reviewed_at = document.collected_at_utc + timedelta(seconds=1)
    approved_manifest = seal_approved_corpus_manifest(
        ExternalCorpusIntakeSpec(
            corpus_id="approved-relation-promotion-test",
            reviews=(
                CorpusReview(
                    reviewer_role="data_rights",
                    reviewer_id="rights-reviewer",
                    reviewed_at_utc=reviewed_at,
                    note="합성 공식 문서 저장 및 검색 권리 검토",
                ),
                CorpusReview(
                    reviewer_role="finance_domain",
                    reviewer_id="finance-reviewer",
                    reviewed_at_utc=reviewed_at,
                    note="합성 관계 근거와 기준일 검토",
                ),
            ),
            documents=(
                CorpusDocumentSpec(
                    document_id=document.document_id,
                    relative_path=document.text_relative_path,
                    title=document.title,
                    publisher=document.publisher,
                    source_registry_id=document.source_registry_id,
                    source_uri=document.source_uri,
                    capabilities=document.capabilities,
                    collected_at_utc=document.collected_at_utc,
                    published_at=document.published_at,
                    as_of=document.as_of,
                    language=document.language,
                    media_type="text/plain",
                    purposes=document.purposes,
                    license=CorpusLicenseApproval(
                        license_id="synthetic-test-approval",
                        license_uri="https://www.fsc.go.kr/",
                        storage_allowed=True,
                        retrieval_allowed=True,
                        competition_use_allowed=True,
                        deployment_bundle_allowed=True,
                        attribution_text="합성 테스트용 승인",
                    ),
                ),
            ),
        ),
        root,
    )
    approved_manifest_path = root / "approved-manifest.json"
    approved_manifest_path.write_bytes(approved_corpus_manifest_bytes(approved_manifest))
    approved = verify_approved_corpus(approved_manifest_path, root)
    return pending, approved


def _replace_pending_evidence(
    pending: PendingTemporalRelationBundle,
    **updates,
) -> PendingTemporalRelationBundle:
    evidence = pending.relations[0].evidence[0].model_copy(update=updates)
    relation = pending.relations[0].model_copy(update={"evidence": (evidence,)})
    payload = [relation.model_dump(mode="json")]
    digest = hashlib.sha256(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    return PendingTemporalRelationBundle(
        corpus_manifest_sha256=pending.corpus_manifest_sha256,
        relation_set_sha256=digest,
        relation_count=1,
        relations=(relation,),
    )


def test_pending_relation_promotion_rebinds_raw_text_file_to_normalized_text(
    tmp_path,
) -> None:
    pending, approved = _pending_and_approved_relation_fixture(tmp_path)
    source = approved.manifest.documents[0]

    # The generic pending collector intentionally stores one trailing newline.
    # Promotion must verify that exact input, then prevent its raw-file SHA from
    # becoming the approved evidence SHA.
    assert pending.relations[0].evidence[0].content_sha256 == source.content_sha256
    assert source.content_sha256 != source.normalized_text_sha256

    promoted = promote_pending_relations_to_approved_corpus(
        pending,
        approved,
        registry=load_official_source_registry(),
    )

    evidence = promoted.relations[0].evidence[0]
    assert promoted.manifest.corpus_manifest_sha256 == approved.manifest_sha256
    assert evidence.document_id == source.document_id
    assert evidence.content_sha256 == source.normalized_text_sha256
    assert evidence.content_sha256 != source.content_sha256


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"document_id": "different-approved-document"}, "absent from the approved corpus"),
        (
            {"source_uri": "https://www.fsc.go.kr/po010103/86951"},
            "metadata differs",
        ),
        ({"title": "변조된 공식 자료 제목"}, "metadata differs"),
        ({"content_sha256": "0" * 64}, "content SHA-256 differs"),
        ({"excerpt": "승인 문서에 존재하지 않는 관계 주장"}, "excerpt is absent"),
    ),
)
def test_pending_relation_promotion_rejects_evidence_substitution(
    tmp_path,
    updates,
    message,
) -> None:
    pending, approved = _pending_and_approved_relation_fixture(tmp_path)
    forged = _replace_pending_evidence(pending, **updates)

    with pytest.raises(PendingRelationError, match=message):
        promote_pending_relations_to_approved_corpus(
            forged,
            approved,
            registry=load_official_source_registry(),
        )
