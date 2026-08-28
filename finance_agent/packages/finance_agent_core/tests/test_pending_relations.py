from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
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
                    capabilities=(
                        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    ),
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
            "evidence": (
                relation.evidence[0].model_copy(update={"excerpt": "존재하지 않는 근거"}),
            )
        }
    )
    with pytest.raises(PendingRelationError, match="not verbatim"):
        verify_pending_relation_candidates(root, (forged,))
