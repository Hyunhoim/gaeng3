"""One phonetic rule lets a Hangul question reach a Latin-labelled graph relation.

Official example 3 says ``캠브리콘``; the publisher's holdings table says
``Cambricon Technologies Corp Ltd``.  The relation store lookup, the executor
and the evidence verifier all reuse ``hangul_romanization`` so the same relation
cannot be found by one stage and rejected by the next.  The margin rule keeps
the match unique: two entities that both sound close bind nothing.
"""

from __future__ import annotations

from datetime import date

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import KnowledgePredicate
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.hangul_romanization import (
    best_phonetic_label,
    hangul_romanization_key,
    latin_label_phonetic_score,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
    TemporalRelationSearchRequest,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_AS_OF = date(2026, 8, 21)


def test_phonetic_scores_prefer_the_stripped_spelling_and_ignore_hangul_labels() -> None:
    key = hangul_romanization_key("캠브리콘")

    assert latin_label_phonetic_score(key, "Cambricon") >= 0.64
    assert latin_label_phonetic_score(key, "Cambricon Technologies Corp Ltd") == 0.0  # too long
    assert latin_label_phonetic_score(key, "에코프로비엠") == 0.0
    assert latin_label_phonetic_score(key, "Cambridge") < 0.64
    assert best_phonetic_label("캠브리콘", ["Cambricon Technologies Corp Ltd", "Cambricon"]) == (
        "Cambricon",
        latin_label_phonetic_score(key, "Cambricon"),
    )
    assert best_phonetic_label("삼", ["Sam"]) is None  # one syllable: key too short to be safe
    assert best_phonetic_label("에코프로비엠", ["Cambricon"]) is None


def _relation(
    relation_id: str, product_id: str, company: str, *aliases: str
) -> TemporalRelationRecord:
    return TemporalRelationRecord(
        relation_id=relation_id,
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate=KnowledgePredicate.HOLDS_COMPANY,
        subject=TemporalEntity(
            entity_id=f"product-{product_id.casefold()}",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name=f"미래에셋 TIGER {product_id}",
        ),
        object=TemporalEntity(
            # Same company name -> same entity, as the graph builder derives it.
            entity_id="company-" + "".join(ch for ch in company.casefold() if ch.isalnum()),
            kind=TemporalEntityKind.COMPANY,
            canonical_name=company,
            aliases=tuple(sorted(aliases)),
        ),
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id=product_id,
        valid_from=_AS_OF,
        valid_to=_AS_OF,
        observed_at=_AS_OF,
        evidence=(
            ExternalEvidenceReference(
                evidence_id=f"evidence-{relation_id}",
                source_registry_id="miraeasset-investments",
                document_id=f"doc-{relation_id}",
                title="TIGER official full holdings",
                publisher="미래에셋자산운용",
                source_uri=(
                    "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/"
                    f"pdfListAjax.ajax?ksdFund={product_id}&fixDate=2026.08.21"
                ),
                published_at=None,
                as_of=_AS_OF,
                content_sha256="a" * 64,
                locator="table row 1",
                excerpt=f"688256 C1 EQUITY | {company} | 227 | 49,390,089 | 8.82",
            ),
        ),
    )


def _request(subject: str) -> TemporalRelationSearchRequest:
    return TemporalRelationSearchRequest(
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        subject=subject,
        product_families=(ProductFamily.DOMESTIC_ETP,),
    )


def test_store_reaches_one_latin_entity_from_a_hangul_subject() -> None:
    store = TemporalRelationStore(
        seal_temporal_relation_bundle(
            (
                _relation("rel-1", "KR7396520009", "Cambricon Technologies Corp Ltd", "Cambricon"),
                _relation("rel-2", "KR70047A0007", "Cambricon Technologies Corp Ltd", "Cambricon"),
                _relation("rel-3", "KR7396520009", "NAURA Technology Group Co Ltd", "NAURA"),
            ),
            corpus_manifest_sha256="b" * 64,
        )
    )

    assert store.search(_request("캠브리콘")) == ()
    hits = store.search_cross_script(_request("캠브리콘"))

    assert sorted(relation.product_id for relation in hits) == ["KR70047A0007", "KR7396520009"]
    assert all(relation.object.canonical_name.startswith("Cambricon") for relation in hits)
    assert store.search_cross_script(_request("에코프로비엠")) == ()


def test_store_binds_nothing_when_two_distinct_entities_are_equally_close() -> None:
    store = TemporalRelationStore(
        seal_temporal_relation_bundle(
            (
                _relation("rel-1", "KR7396520009", "Cambricon Technologies Corp Ltd", "Cambricon"),
                _relation("rel-2", "KR70047A0007", "Cambricon Holdings Ltd", "Cambricon"),
            ),
            corpus_manifest_sha256="b" * 64,
        )
    )

    assert store.search_cross_script(_request("캠브리콘")) == ()


def test_store_ignores_a_sub_threshold_runner_up() -> None:
    store = TemporalRelationStore(
        seal_temporal_relation_bundle(
            (
                _relation("rel-1", "KR7396520009", "Cambricon Technologies Corp Ltd", "Cambricon"),
                _relation("rel-2", "KR70047A0007", "Emerson Electric Co", "Emerson"),
            ),
            corpus_manifest_sha256="b" * 64,
        )
    )

    hits = store.search_cross_script(_request("캠브리콘"))

    assert [relation.product_id for relation in hits] == ["KR7396520009"]
