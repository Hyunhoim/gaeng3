from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

# Import the public Agent package before the live extractor, matching the
# production bootstrap order and avoiding its intentional lazy runtime cycle.
from finance_agent_core.agent import RoutedFinanceAgent as _RoutedFinanceAgent  # noqa: F401
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeDocumentTopNScope,
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import (
    OfficialLiveEvidenceError,
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationDocumentRank,
    TemporalRelationRecord,
    VerifiedTemporalRelationBundle,
    load_verified_temporal_relation_bundle,
)


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _scope(limit: int) -> KnowledgeDocumentTopNScope:
    return KnowledgeDocumentTopNScope(
        limit=limit,
        selection_evidence_span=f"상위 {limit}개 구성종목",
    )


def _requirement(limit: int | None, *, subject: str = "Zythera") -> KnowledgeRequirement:
    return KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate=KnowledgePredicate.HOLDS_COMPANY,
        subject=subject,
        subject_evidence_span=subject,
        relation_evidence_span="구성종목에 포함된",
        product_families=(ProductFamily.OVERSEAS_ETP,),
        source_registry_ids=("kraneshares-product",),
        document_scope=None if limit is None else _scope(limit),
    )


def _universe() -> ProvidedProductCandidateUniverse:
    return ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                product_id="AURR",
                product_name="KraneShares Aurora Solar Index ETF",
                aliases=("AURR",),
            ),
        ),
        database_sha256s={ProductFamily.OVERSEAS_ETP: "a" * 64},
        candidate_filter_sha256="b" * 64,
    )


def _universe_with_sibling() -> ProvidedProductCandidateUniverse:
    base = _universe()
    return ProvidedProductCandidateUniverse(
        candidates=(
            *base.candidates,
            ProvidedProductCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                product_id="SIBL",
                product_name="KraneShares Sibling Technology ETF",
                aliases=("SIBL",),
            ),
        ),
        database_sha256s=base.database_sha256s,
        candidate_filter_sha256=base.candidate_filter_sha256,
    )


def _snapshot(text: str) -> OfficialSourceSnapshot:
    registry = load_official_source_registry()
    url = "https://engage.kraneshares.com/s/aurora-solar-presentation/"
    body = text.encode()
    return OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="Aurora Solar official holdings",
        content_type="text/plain",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=date(2026, 8, 24),
        as_of=date(2026, 8, 24),
    )


def _legacy_relation() -> TemporalRelationRecord:
    registry = load_official_source_registry()
    url = "https://engage.kraneshares.com/s/legacy-holdings/"
    source = registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS)
    return TemporalRelationRecord(
        relation_id="relation:legacy-holding",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate=KnowledgePredicate.HOLDS_COMPANY,
        subject=TemporalEntity(
            entity_id="product:legacy-etf",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name="Legacy ETF",
        ),
        object=TemporalEntity(
            entity_id="company:zythera",
            kind=TemporalEntityKind.COMPANY,
            canonical_name="Zythera",
        ),
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="LEGACY",
        valid_from=date(2026, 8, 24),
        valid_to=date(2026, 8, 24),
        observed_at=date(2026, 8, 24),
        evidence=(
            ExternalEvidenceReference(
                evidence_id="evidence:legacy-holding",
                source_registry_id=source.source_id,
                document_id="document.legacy-holdings",
                title="Legacy official holdings",
                publisher=source.publisher,
                source_uri=url,
                published_at=date(2026, 8, 24),
                as_of=date(2026, 8, 24),
                content_sha256="c" * 64,
                locator="extracted chunk 0",
                excerpt="Legacy ETF 구성종목에 Zythera가 포함되어 있습니다.",
            ),
        ),
    )


def test_document_top_n_scope_is_literal_and_holdings_only() -> None:
    assert _requirement(10).document_scope == _scope(10)
    with pytest.raises(ValidationError, match="limit must occur literally"):
        KnowledgeDocumentTopNScope(limit=3, selection_evidence_span="상위 5개 구성종목")
    invalid = _requirement(3).model_dump(mode="json") | {
        "capability": KnowledgeCapability.PRODUCT_RISK_FACTORS,
        "predicate": KnowledgePredicate.HAS_RISK_FACTOR,
    }
    with pytest.raises(ValidationError, match="only for portfolio holdings"):
        KnowledgeRequirement.model_validate(invalid)


def test_existing_rankless_relation_bundle_remains_loadable(tmp_path) -> None:
    registry = load_official_source_registry()
    relation_payload = _legacy_relation().model_dump(mode="json")
    relation_payload.pop("document_rank")
    relation_set_sha256 = hashlib.sha256(_canonical_bytes([relation_payload])).hexdigest()
    payload = {
        "manifest": {
            "schema_version": "1.0",
            "manifest_kind": "approved_temporal_relations",
            "evidence_cutoff": registry.evidence_cutoff.isoformat(),
            "source_registry_sha256": registry.canonical_sha256,
            "relation_count": 1,
            "relation_set_sha256": relation_set_sha256,
            "corpus_manifest_sha256": "d" * 64,
        },
        "relations": [relation_payload],
    }
    data = _canonical_bytes(payload)
    path = tmp_path / "legacy-relations.json"
    path.write_bytes(data)

    bundle = load_verified_temporal_relation_bundle(
        path,
        expected_file_sha256=hashlib.sha256(data).hexdigest(),
        registry=registry,
    )

    assert isinstance(bundle, VerifiedTemporalRelationBundle)
    assert bundle.relations[0].document_rank is None
    assert not bundle.relations[0].satisfies_document_scope(_scope(10))


def test_rank_metadata_requires_matching_source_evidence() -> None:
    relation = _legacy_relation()
    with pytest.raises(ValidationError, match="must name relation evidence"):
        TemporalRelationRecord.model_validate(
            relation.model_dump(mode="json")
            | {
                "document_rank": TemporalRelationDocumentRank(
                    position=3,
                    basis="explicit_document_rank",
                    evidence_id="evidence:missing-rank",
                ).model_dump(mode="json")
            }
        )
    with pytest.raises(ValidationError, match="explicit document rank is absent"):
        TemporalRelationRecord.model_validate(
            relation.model_dump(mode="json")
            | {
                "document_rank": TemporalRelationDocumentRank(
                    position=3,
                    basis="explicit_document_rank",
                    evidence_id="evidence:legacy-holding",
                ).model_dump(mode="json")
            }
        )
    unrelated_rank = relation.model_dump(mode="json")
    unrelated_rank["evidence"][0]["excerpt"] = (
        "Top 3 Holdings\n3. Alpha Corp\nZythera is also held."
    )
    unrelated_rank["document_rank"] = TemporalRelationDocumentRank(
        position=3,
        basis="explicit_document_rank",
        evidence_id="evidence:legacy-holding",
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="explicit document rank is absent"):
        TemporalRelationRecord.model_validate(unrelated_rank)


def test_generic_live_extraction_enforces_evidenced_top_n() -> None:
    text = """KraneShares Aurora Solar Index ETF
Top 3 Holdings
1. Alpha Corp
2. Beta Corp
3. Zythera
"""
    extractor = VerifiedLiveRelationExtractor()

    assert extractor.extract_many((_snapshot(text),), _requirement(2), _universe()) == ()

    relations = extractor.extract_many((_snapshot(text),), _requirement(3), _universe())
    assert len(relations) == 1
    assert relations[0].document_rank == TemporalRelationDocumentRank(
        position=3,
        basis="explicit_document_rank",
        evidence_id=relations[0].evidence[0].evidence_id,
    )


def test_unranked_holding_language_cannot_satisfy_top_n_scope() -> None:
    text = """KraneShares Aurora Solar Index ETF
Top Holdings
Zythera is included in the official portfolio holdings.
"""

    assert (
        VerifiedLiveRelationExtractor().extract_many(
            (_snapshot(text),),
            _requirement(10),
            _universe(),
        )
        == ()
    )


_UNNUMBERED_TOP_TEN = """KraneShares Aurora Solar Index ETF
Ticker AURR
TOP 10 HOLDINGS (%) Holdings Subject to Change
Alpha Systems Inc 9.90%
Beta Robotics Corp 8.80%
Gamma Cloud Ltd 7.70%
Zythera Inc 6.60%
Epsilon Networks Plc 5.50%
Zeta Software Inc 4.40%
Eta Security Corp 3.30%
Theta Data Ltd 2.20%
Iota Automation Inc 1.10%
Kappa Semiconductors Corp 0.90%
PERFORMANCE (%)
Past performance does not guarantee future results.
"""


def test_complete_unnumbered_top_ten_is_conservative_membership_proof() -> None:
    extractor = VerifiedLiveRelationExtractor()

    assert (
        extractor.extract_many(
            (_snapshot(_UNNUMBERED_TOP_TEN),),
            _requirement(5),
            _universe(),
        )
        == ()
    )

    for requested_limit in (10, 20):
        relations = extractor.extract_many(
            (_snapshot(_UNNUMBERED_TOP_TEN),),
            _requirement(requested_limit),
            _universe(),
        )
        assert len(relations) == 1
        assert relations[0].product_id == "AURR"
        rank_evidence = next(
            item
            for item in relations[0].evidence
            if item.evidence_id == relations[0].document_rank.evidence_id
        )
        assert relations[0].document_rank == TemporalRelationDocumentRank(
            position=10,
            basis="bounded_top_n_membership",
            evidence_id=rank_evidence.evidence_id,
        )
        assert "Zythera Inc 6.60%" in rank_evidence.excerpt


def test_snapshot_intake_relation_requires_approved_document_binding() -> None:
    live_snapshot = _snapshot(_UNNUMBERED_TOP_TEN)
    intake_snapshot = replace(
        live_snapshot,
        source=live_snapshot.source.model_copy(
            update={"status": "approved_for_snapshot_intake"}
        ),
    )
    extractor = VerifiedLiveRelationExtractor()

    with pytest.raises(OfficialLiveEvidenceError, match="approved document"):
        extractor.extract(intake_snapshot, _requirement(10), _universe())

    approved_snapshot = replace(
        intake_snapshot,
        approved_document_id="approved-document-aurora-holdings",
    )
    relations = extractor.extract(approved_snapshot, _requirement(10), _universe())

    assert [item.product_id for item in relations] == ["AURR"]


def test_snapshot_intake_subject_evidence_requires_approved_document_binding() -> None:
    live_snapshot = _snapshot(_UNNUMBERED_TOP_TEN)
    intake_snapshot = replace(
        live_snapshot,
        source=live_snapshot.source.model_copy(
            update={"status": "approved_for_snapshot_intake"}
        ),
    )
    extractor = VerifiedLiveRelationExtractor()

    assert extractor.extract_subject_evidence(
        (intake_snapshot,), _requirement(None)
    ) == ()

    approved_snapshot = replace(
        intake_snapshot,
        approved_document_id="approved-document-aurora-holdings",
    )
    evidence = extractor.extract_subject_evidence(
        (approved_snapshot,), _requirement(None)
    )

    assert len(evidence) == 1
    assert evidence[0].document_id == "approved-document-aurora-holdings"


@pytest.mark.parametrize(
    "broken_table",
    (
        _UNNUMBERED_TOP_TEN.replace("Kappa Semiconductors Corp 0.90%\n", ""),
        _UNNUMBERED_TOP_TEN.replace("Zythera Inc 6.60%", "Zythera Inc weight unavailable"),
        _UNNUMBERED_TOP_TEN.replace(
            "PERFORMANCE (%)",
            "Lambda Materials Inc 0.80%\nPERFORMANCE (%)",
        ),
    ),
    ids=("incomplete", "malformed-row", "more-than-heading-limit"),
)
def test_incomplete_or_malformed_unnumbered_table_fails_closed(
    broken_table: str,
) -> None:
    assert (
        VerifiedLiveRelationExtractor().extract_many(
            (_snapshot(broken_table),),
            _requirement(10),
            _universe(),
        )
        == ()
    )


def test_top_n_table_never_leaks_to_sibling_or_unproven_owner() -> None:
    extractor = VerifiedLiveRelationExtractor()
    relations = extractor.extract_many(
        (_snapshot(_UNNUMBERED_TOP_TEN),),
        _requirement(10),
        _universe_with_sibling(),
    )
    assert {item.product_id for item in relations} == {"AURR"}
    assert (
        extractor.extract_many(
            (_snapshot(_UNNUMBERED_TOP_TEN),),
            _requirement(10, subject="Zyth"),
            _universe_with_sibling(),
        )
        == ()
    )

    unproven_owner = _UNNUMBERED_TOP_TEN.replace(
        "KraneShares Aurora Solar Index ETF\nTicker AURR",
        "An Official but Unlisted Fund",
    )
    assert (
        extractor.extract_many(
            (_snapshot(unproven_owner),),
            _requirement(10),
            _universe_with_sibling(),
        )
        == ()
    )
