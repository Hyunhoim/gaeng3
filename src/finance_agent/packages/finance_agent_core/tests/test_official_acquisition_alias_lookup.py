"""The relation cache must be reachable through a requirement's verified search aliases.

Official example 3 names ``캠브리콘`` while the publisher's holdings table says
``Cambricon Technologies Corp Ltd``.  ``TemporalRelationStore`` matches whole
labels, so without alias lookup a prebuilt holdings graph would never be hit
and the request would fall through to live search that cannot answer it.
"""

from __future__ import annotations

import json

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.holdings_graph import (
    SavedHoldingsProduct,
    build_holdings_relations,
    seal_holdings_graph,
)
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
    OfficialEvidenceAcquisitionStatus,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.temporal_relations import TemporalRelationStore

_ETF = ProvidedProductCandidate(
    product_family=ProductFamily.DOMESTIC_ETP,
    product_id="KR70162L0003",
    product_name="삼성 KODEX 차이나AI반도체TOP10증권상장지수투자신탁[주식]",
    aliases=("A0162L0", "KODEX 차이나AI반도체TOP10", "KR70162L0003"),
)


class _UnusedTransport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        raise AssertionError((url, timeout_seconds, max_bytes))


class _UnusedDiscovery:
    def discover(self, **kwargs):
        raise AssertionError(kwargs)


def _store() -> TemporalRelationStore:
    page = (
        "<html><head><title>KODEX 차이나AI반도체TOP10 ETF | Kodex</title></head>"
        "<body><h1>KODEX 차이나AI반도체TOP10</h1></body></html>"
    ).encode()
    api = json.dumps(
        {
            "pdf": {
                "gijunYMD": "20260821",
                "list": [{"secNm": "Cambricon Technologies Corp Ltd", "itmNo": "688256"}],
            }
        }
    ).encode()
    saved = SavedHoldingsProduct(
        publisher="kodex",
        page_id="2ETFZ9",
        page_url="https://www.samsungfund.com/etf/product/view.do?id=2ETFZ9",
        page_content=page,
        holdings_url=(
            "https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETFZ9.do?gijunYMD=2026.08.22"
        ),
        holdings_content=api,
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(_ETF,),
        database_sha256s={ProductFamily.DOMESTIC_ETP: "a" * 64},
        candidate_filter_sha256="b" * 64,
    )
    report = build_holdings_relations((saved,), universe=universe)
    return TemporalRelationStore(seal_holdings_graph(report.relations))


def _requirement(subject: str, *aliases: str) -> KnowledgeRequirement:
    return KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate=KnowledgePredicate.HOLDS_COMPANY,
        subject=subject,
        subject_evidence_span=subject,
        relation_evidence_span="편입된",
        product_families=(ProductFamily.DOMESTIC_ETP,),
        source_registry_ids=("samsungfund-kodex-product",),
        search_aliases=tuple(aliases),
    )


def test_cache_lookup_uses_search_aliases_and_deduplicates() -> None:
    store = _store()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=_UnusedDiscovery(),
        connector=OfficialSourceConnector(_UnusedTransport(), store.registry),
    )

    hit = coordinator.acquire(_requirement("캠브리콘", "Cambricon", "Cambricon Technologies"))

    assert hit.status is OfficialEvidenceAcquisitionStatus.CACHE_HIT
    assert [relation.product_id for relation in hit.relations] == ["KR70162L0003"]
    assert hit.relations[0].object.canonical_name == "Cambricon Technologies Corp Ltd"


def test_cache_lookup_without_a_matching_label_still_misses() -> None:
    store = _store()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=_UnusedDiscovery(),
        connector=OfficialSourceConnector(_UnusedTransport(), store.registry),
    )

    class _EmptyDiscovery:
        def discover(self, **kwargs):
            return ()

    coordinator.discovery_provider = _EmptyDiscovery()
    miss = coordinator.acquire(_requirement("에코프로비엠", "Ecopro BM"))

    assert miss.status is not OfficialEvidenceAcquisitionStatus.CACHE_HIT


def test_hangul_subject_reaches_a_latin_graph_label_by_phonetic_key() -> None:
    store = _store()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=_UnusedDiscovery(),
        connector=OfficialSourceConnector(_UnusedTransport(), store.registry),
    )

    # HCLX proposed a wrong alias ("Cambridge"); the phonetic key still finds Cambricon.
    hit = coordinator.acquire(_requirement("캠브리콘", "Cambridge"))

    assert hit.status is OfficialEvidenceAcquisitionStatus.CACHE_HIT
    assert hit.relations[0].object.canonical_name == "Cambricon Technologies Corp Ltd"
