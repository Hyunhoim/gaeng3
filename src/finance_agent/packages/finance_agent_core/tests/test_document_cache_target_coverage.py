from __future__ import annotations

import threading
from datetime import date
from types import SimpleNamespace

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.document_dense import DocumentSearchHit
from finance_agent_core.retrieval.document_discovery import CacheFirstOfficialDiscoveryProvider
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate
from finance_agent_core.retrieval.official_discovery import OfficialDiscoveryReport

_CAPABILITY = KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
_CUTOFF = date(2026, 8, 24)


def _hit(document_id: str, title: str, score: float, rank: int, channel: str):
    return DocumentSearchHit(
        chunk_id=f"{document_id}:0000",
        document_id=document_id,
        title=title,
        text=title,
        score=score,
        rank=rank,
        channel=channel,
    )


class _TargetAwareIndex:
    def __init__(self, *, include_second: bool) -> None:
        self.include_second = include_second

    def dense_search(self, query: str, *, top_k: int):
        assert top_k >= 2
        target = (
            _hit("product-a", "새봄 알파 펀드 공식자료", 0.20, 2, "dense")
            if "새봄 알파" in query
            else (
                _hit("product-b", "푸른 베타 펀드 공식자료", 0.20, 2, "dense")
                if "푸른 베타" in query and self.include_second
                else None
            )
        )
        values = [_hit("noise", "무관한 공식자료", 0.99, 1, "dense")]
        if target is not None:
            values.append(target)
        return tuple(values)

    def bm25_search(self, _query: str, *, top_k: int):
        assert top_k >= 2
        return ()


class _LiveFallback:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def discover_report(self, **kwargs) -> OfficialDiscoveryReport:
        self.calls.append(kwargs)
        return OfficialDiscoveryReport(
            candidates=(
                OfficialDiscoveryCandidate(
                    url="https://www.fsc.go.kr/po010102/87009",
                    title="푸른 베타 펀드 공식 검색 결과",
                    published_at=date(2026, 5, 29),
                    as_of=date(2026, 5, 29),
                ),
            ),
            queried_source_registry_ids=("fsc-policy",),
            search_query_count=1,
            exhaustive=False,
        )


def _provider(*, include_second: bool):
    provider = object.__new__(CacheFirstOfficialDiscoveryProvider)
    provider.index = _TargetAwareIndex(include_second=include_second)
    provider.registry = load_official_source_registry()
    provider.minimum_dense_score = 0.95
    provider.minimum_dense_margin = 0.50
    provider.cache_top_k = 20
    provider._documents = {
        "noise": SimpleNamespace(
            title="무관한 공식자료",
            source_uri="https://www.fsc.go.kr/po010103/86950",
            source_registry_id="fsc-policy",
            capabilities=(_CAPABILITY,),
            published_at=date(2026, 5, 21),
            as_of=date(2026, 5, 21),
        ),
        "product-a": SimpleNamespace(
            title="새봄 알파 펀드 공식자료",
            source_uri="https://www.fsc.go.kr/po010103/86950",
            source_registry_id="fsc-policy",
            capabilities=(_CAPABILITY,),
            published_at=date(2026, 5, 21),
            as_of=date(2026, 5, 21),
        ),
        "product-b": SimpleNamespace(
            title="푸른 베타 펀드 공식자료",
            source_uri="https://www.fsc.go.kr/po010102/87009",
            source_registry_id="fsc-policy",
            capabilities=(_CAPABILITY,),
            published_at=date(2026, 5, 29),
            as_of=date(2026, 5, 29),
        ),
    }
    provider._document_text = {
        "noise": "다른 정책 자료",
        "product-a": "새봄 알파 펀드의 구조와 운용 동향",
        "product-b": "푸른 베타 펀드의 구조와 운용 동향",
    }
    provider.fallback = _LiveFallback()
    provider._stats_lock = threading.Lock()
    provider.cache_queries = 0
    provider.cache_hits = 0
    provider.cache_evidence_hits = 0
    provider.live_fallback_queries = 0
    return provider


def _discover(provider: CacheFirstOfficialDiscoveryProvider) -> OfficialDiscoveryReport:
    return provider.discover_report(
        query="두 펀드 공식 자료를 비교해줘",
        capability=_CAPABILITY,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=_CUTOFF,
        top_k=2,
        max_queries=4,
        product_terms=("새봄 알파 펀드", "푸른 베타 펀드"),
        product_term_groups=(
            ("새봄 알파 펀드", "SAEBOM-A"),
            ("푸른 베타 펀드", "PUREUN-B"),
        ),
        relation_terms=("공식 자료",),
    )


def test_exact_typed_targets_can_use_dense_cache_without_bm25_or_noise() -> None:
    provider = _provider(include_second=True)

    report = _discover(provider)

    assert [item.title for item in report.candidates] == [
        "새봄 알파 펀드 공식자료",
        "푸른 베타 펀드 공식자료",
    ]
    assert provider.fallback.calls == []
    assert provider.cache_hits == 1
    assert provider.live_fallback_queries == 0


def test_partial_cache_hit_searches_only_the_uncovered_typed_target() -> None:
    provider = _provider(include_second=False)

    report = _discover(provider)

    assert [item.title for item in report.candidates] == [
        "새봄 알파 펀드 공식자료",
        "푸른 베타 펀드 공식 검색 결과",
    ]
    assert len(provider.fallback.calls) == 1
    assert provider.fallback.calls[0]["product_terms"] == ("푸른 베타 펀드", "PUREUN-B")
    assert "새봄 알파" not in provider.fallback.calls[0]["query"]
    assert provider.cache_hits == 1
    assert provider.live_fallback_queries == 1


def test_short_product_identity_does_not_claim_a_longer_sibling_document() -> None:
    """Literal coverage must preserve an identity boundary, not a substring."""

    provider = _provider(include_second=True)
    provider._documents["product-a"].title = "새봄 알파플러스 펀드 공식자료"
    provider._document_text["product-a"] = "새봄 알파플러스 펀드의 구조와 운용 동향"

    assert not provider._document_matches_target(
        "product-a",
        ("새봄 알파", "SAEBOM-A"),
    )
