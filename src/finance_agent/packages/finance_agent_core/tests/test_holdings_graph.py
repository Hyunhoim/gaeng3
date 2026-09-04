"""The prebuilt holdings graph must carry live-grade evidence and stay fail-closed.

Official example 5 (``에코프로의 자회사를 편입한 ETF``) and 3 (``캠브리콘이 편입된 중국
반도체 ETF``) need ``company -> products`` lookups that live site search cannot
provide.  The graph is built from the same kind of official resources the live
path joins, so these tests pin: identity binding through the product page, one
relation per security row with both excerpts, cash rows skipped, whole-label
lookup through suffix-stripped aliases, both publishers' parsers, and release
admission rules.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.external_knowledge_release import (
    build_live_external_knowledge_bootstrap,
    live_corpus_manifest_sha256,
)
from finance_agent_core.retrieval.holdings_graph import (
    HoldingsGraphError,
    SavedHoldingsProduct,
    build_holdings_relations,
    company_aliases,
    load_saved_kodex_products,
    load_saved_tiger_products,
    seal_holdings_graph,
    tiger_holdings_url,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationSearchRequest,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_PAGE_URL = "https://www.samsungfund.com/etf/product/view.do?id=2ETFE5"
_API_URL = "https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETFE5.do?gijunYMD=2026.08.22"
_ECOPRO_ETF = ProvidedProductCandidate(
    product_family=ProductFamily.DOMESTIC_ETP,
    product_id="KR7305720005",
    product_name="삼성 KODEX 2차전지산업증권상장지수투자신탁[주식]",
    aliases=("A305720", "KODEX 2차전지산업", "KR7305720005"),
)
_OTHER_ETF = ProvidedProductCandidate(
    product_family=ProductFamily.DOMESTIC_ETP,
    product_id="KR7419430004",
    product_name="삼성 KODEX 차이나2차전지MSCI증권상장지수투자신탁[주식-파생형](합성)",
    aliases=("A419430", "KODEX 차이나2차전지MSCI(합성)", "KR7419430004"),
)
_TIGER_CHINA_SEMI = ProvidedProductCandidate(
    product_family=ProductFamily.DOMESTIC_ETP,
    product_id="KR7396520009",
    product_name="미래에셋 TIGER 차이나반도체FACTSET증권상장지수투자신탁(주식)",
    aliases=("A396520", "KR7396520009", "TIGER 차이나반도체FACTSET"),
)
_TIGER_PAGE_URL = (
    "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do"
    "?ksdFund=KR7396520009"
)


def _universe(*candidates: ProvidedProductCandidate) -> ProvidedProductCandidateUniverse:
    return ProvidedProductCandidateUniverse(
        candidates=tuple(sorted(candidates, key=lambda item: item.product_id)),
        database_sha256s={ProductFamily.DOMESTIC_ETP: "a" * 64},
        candidate_filter_sha256="b" * 64,
    )


def _page(short_name: str = "KODEX 2차전지산업", *, body: str = "") -> bytes:
    return (
        f"<html><head><title>{short_name} ETF | Kodex</title></head><body>"
        f"<h1>{short_name}</h1><p>상장지수펀드 구성종목과 기준가를 안내합니다.</p>{body}"
        "</body></html>"
    ).encode()


def _api(gijun: str = "20260821") -> bytes:
    return json.dumps(
        {
            "pdf": {
                "gijunYMD": gijun,
                "totalCnt": 4,
                "list": [
                    {"secNm": "원화예금", "itmNo": "KRD010010001", "evalA": "7283948"},
                    {"secNm": "에코프로비엠", "itmNo": "247540", "ratio": "12.31"},
                    {"secNm": "Cambricon Technologies Corp Ltd", "itmNo": "688256", "ratio": "3.2"},
                    {"secNm": "설정현금액", "itmNo": "CASH000001"},
                ],
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _saved(page: bytes = _page(), api: bytes = _api()) -> SavedHoldingsProduct:
    return SavedHoldingsProduct(
        publisher="kodex",
        page_id="2ETFE5",
        page_url=_PAGE_URL,
        page_content=page,
        holdings_url=_API_URL,
        holdings_content=api,
    )


_TIGER_TABLE = b"""
<tr data-tot-cnt="3">
  <td>  688825 CH EQUITY</td>
  <td>CXMT Corp</td>
  <td> 4,833</td><td>57,602,617</td><td> 10.28</td>
  <td><div class="color-none"><span class="blind">\xec\x97\x86\xec\x9d\x8c</span>-</div></td>
</tr>
<tr data-tot-cnt="3">
  <td>688256 C1 EQUITY</td><td>Cambricon Technologies Corp Ltd</td>
  <td>227</td><td>49,390,089</td><td>8.82</td><td>-</td>
</tr>
<tr data-tot-cnt="3"><td>KRW</td>
<td>\xec\x9b\x90\xed\x99\x94\xed\x98\x84\xea\xb8\x88</td><td>1</td></tr>
"""


def _tiger_saved() -> SavedHoldingsProduct:
    page = (
        "<html><head><title>TIGER 차이나반도체FACTSET | TIGER ETF</title></head><body>"
        "<h2>TIGER 차이나반도체FACTSET</h2><p>중국 반도체 산업에 투자하는 ETF입니다.</p>"
        "<p>관련 상품: TIGER 차이나항셍테크</p></body></html>"
    ).encode()
    return SavedHoldingsProduct(
        publisher="tiger",
        page_id="KR7396520009",
        page_url=_TIGER_PAGE_URL,
        page_content=page,
        holdings_url=tiger_holdings_url("KR7396520009", fix_date=date(2026, 8, 21)),
        holdings_content=_TIGER_TABLE,
    )


def test_graph_binds_the_page_to_one_provided_product_and_keeps_both_excerpts() -> None:
    report = build_holdings_relations((_saved(),), universe=_universe(_ECOPRO_ETF, _OTHER_ETF))

    assert report.bound_page_ids == ("2ETFE5",)
    assert report.unbound_page_ids == ()
    assert report.skipped_rows == 2
    assert sorted(relation.object.canonical_name for relation in report.relations) == [
        "Cambricon Technologies Corp Ltd",
        "에코프로비엠",
    ]
    for relation in report.relations:
        assert relation.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        assert relation.product_id == _ECOPRO_ETF.product_id
        assert relation.subject.canonical_name == _ECOPRO_ETF.product_name
        assert relation.valid_from == relation.valid_to == relation.observed_at == date(2026, 8, 21)
        assert [item.evidence_id for item in relation.evidence] == sorted(
            item.evidence_id for item in relation.evidence
        )
        by_uri = {item.source_uri: item for item in relation.evidence}
        assert set(by_uri) == {_PAGE_URL, _API_URL}
        assert "KODEX 2차전지산업" in by_uri[_PAGE_URL].excerpt
        assert by_uri[_PAGE_URL].as_of == load_official_source_registry().evidence_cutoff
        assert relation.object.canonical_name in by_uri[_API_URL].excerpt
        assert by_uri[_API_URL].as_of == date(2026, 8, 21)
        assert all(
            item.source_registry_id == "samsungfund-kodex-product" for item in relation.evidence
        )


def test_tiger_table_rows_become_relations_dated_by_the_requested_fix_date() -> None:
    report = build_holdings_relations(
        (_tiger_saved(),), universe=_universe(_TIGER_CHINA_SEMI, _ECOPRO_ETF)
    )

    assert report.bound_page_ids == ("KR7396520009",)
    assert report.skipped_rows == 1  # 원화현금
    assert sorted(relation.object.canonical_name for relation in report.relations) == [
        "CXMT Corp",
        "Cambricon Technologies Corp Ltd",
    ]
    cambricon = next(
        relation
        for relation in report.relations
        if relation.object.canonical_name.startswith("Cambricon")
    )
    assert cambricon.product_id == "KR7396520009"
    assert cambricon.observed_at == date(2026, 8, 21)
    by_uri = {item.source_uri: item for item in cambricon.evidence}
    table = by_uri[tiger_holdings_url("KR7396520009", fix_date=date(2026, 8, 21))]
    assert (
        table.excerpt
        == "688256 C1 EQUITY | Cambricon Technologies Corp Ltd | 227 | 49,390,089 | 8.82 | -"
    )
    assert table.locator == "table row 1"
    assert table.source_registry_id == "miraeasset-investments"
    assert "TIGER 차이나반도체FACTSET" in by_uri[_TIGER_PAGE_URL].excerpt


def test_sealed_graph_answers_whole_label_lookups_including_stripped_aliases() -> None:
    report = build_holdings_relations((_saved(),), universe=_universe(_ECOPRO_ETF))
    store = TemporalRelationStore(seal_holdings_graph(report.relations))

    def hits(subject: str) -> tuple[str, ...]:
        return tuple(
            relation.object.canonical_name
            for relation in store.search(
                TemporalRelationSearchRequest(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject=subject,
                    product_families=(ProductFamily.DOMESTIC_ETP,),
                )
            )
        )

    assert hits("에코프로비엠") == ("에코프로비엠",)
    assert hits("Cambricon") == ("Cambricon Technologies Corp Ltd",)
    assert hits("cambricon technologies") == ("Cambricon Technologies Corp Ltd",)
    assert hits("에코프로") == ()
    assert store.bundle.manifest.corpus_manifest_sha256 == live_corpus_manifest_sha256()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "Cambricon Technologies Corp Ltd",
            ("Cambricon", "Cambricon Technologies", "Cambricon Technologies Corp"),
        ),
        ("에코프로비엠", ()),
        ("삼성전자우", ()),
        ("NVIDIA Corp", ("NVIDIA",)),
        (
            "Taiwan Semiconductor Manufacturing Co Ltd",
            (
                "Taiwan",
                "Taiwan Semiconductor Manufacturing",
                "Taiwan Semiconductor Manufacturing Co",
            ),
        ),
    ],
)
def test_company_aliases_only_strip_suffixes_or_take_the_leading_word(
    name: str, expected: tuple[str, ...]
) -> None:
    assert company_aliases(name) == expected


def test_page_header_decides_when_the_body_mentions_sibling_products() -> None:
    sibling_body = "<p>함께 보는 상품: KODEX 차이나2차전지MSCI(합성)</p>"
    report = build_holdings_relations(
        (_saved(page=_page(body=sibling_body)),),
        universe=_universe(_ECOPRO_ETF, _OTHER_ETF),
    )

    assert report.bound_page_ids == ("2ETFE5",)
    assert {relation.product_id for relation in report.relations} == {_ECOPRO_ETF.product_id}


def test_header_prefers_the_longest_identity_over_a_sibling_substring() -> None:
    long_name = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR70008S0004",
        product_name=(
            "미래에셋 TIGER 미국배당다우존스타겟데일리커버드콜증권상장지수투자신탁(주식)"
        ),
        aliases=("A0008S0", "KR70008S0004", "TIGER 미국배당다우존스타겟데일리커버드콜"),
    )
    short_name = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7458730009",
        product_name="미래에셋 TIGER 미국배당다우존스증권상장지수투자신탁(주식)",
        aliases=("A458730", "KR7458730009", "TIGER 미국배당다우존스"),
    )
    page = (
        "<html><head><title>TIGER 미국배당다우존스타겟데일리커버드콜 | ETF 상품 | "
        "미래에셋 TIGER ETF</title></head><body><h2>TIGER 미국배당다우존스타겟데일리커버드콜</h2>"
        "<p>TIGER 미국배당다우존스 시리즈의 커버드콜 상품입니다.</p></body></html>"
    ).encode()
    saved = SavedHoldingsProduct(
        publisher="tiger",
        page_id="KR70008S0004",
        page_url=_TIGER_PAGE_URL.replace("KR7396520009", "KR70008S0004"),
        page_content=page,
        holdings_url=tiger_holdings_url("KR70008S0004", fix_date=date(2026, 8, 21)),
        holdings_content=_TIGER_TABLE,
    )

    report = build_holdings_relations((saved,), universe=_universe(long_name, short_name))

    assert report.bound_page_ids == ("KR70008S0004",)
    assert {relation.product_id for relation in report.relations} == {"KR70008S0004"}


def test_header_matches_identities_regardless_of_publisher_spacing() -> None:
    covered_call = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7166400002",
        product_name="미래에셋 TIGER 200커버드콜OTM증권상장지수투자신탁(주식)",
        aliases=("A166400", "KR7166400002", "TIGER 200커버드콜OTM"),
    )
    kospi200 = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7102110004",
        product_name="미래에셋 TIGER 200증권상장지수투자신탁(주식)",
        aliases=("A102110", "KR7102110004", "TIGER 200"),
    )
    page = (
        "<html><head><title>TIGER 200 커버드콜OTM | ETF 상품 | 미래에셋 TIGER ETF</title></head>"
        "<body><h2>TIGER 200 커버드콜OTM</h2><p>TIGER 200 지수에 콜옵션을 매도합니다.</p></body>"
        "</html>"
    ).encode()
    saved = SavedHoldingsProduct(
        publisher="tiger",
        page_id="KR7166400002",
        page_url=_TIGER_PAGE_URL.replace("KR7396520009", "KR7166400002"),
        page_content=page,
        holdings_url=tiger_holdings_url("KR7166400002", fix_date=date(2026, 8, 21)),
        holdings_content=_TIGER_TABLE,
    )

    report = build_holdings_relations((saved,), universe=_universe(covered_call, kospi200))

    assert report.bound_page_ids == ("KR7166400002",)
    assert {relation.product_id for relation in report.relations} == {"KR7166400002"}


def test_ambiguous_or_unbound_pages_produce_no_relation() -> None:
    both = _page("KODEX 2차전지산업 KODEX 차이나2차전지MSCI(합성)")
    report = build_holdings_relations(
        (_saved(page=both), _saved(page=_page("KODEX 200"))),
        universe=_universe(_ECOPRO_ETF, _OTHER_ETF),
    )

    assert report.relations == ()
    assert report.unbound_page_ids == ("2ETFE5", "2ETFE5")


def test_holdings_dated_after_the_cutoff_fail_closed() -> None:
    with pytest.raises(HoldingsGraphError, match="after the evidence cutoff"):
        build_holdings_relations((_saved(api=_api("20260901")),), universe=_universe(_ECOPRO_ETF))


def test_loaders_pair_saved_files_and_date_the_request(tmp_path) -> None:
    (tmp_path / "2ETFE5.pdf.json").write_bytes(_api())
    (tmp_path / "2ETFE5.page.html").write_bytes(_page())
    (tmp_path / "2ETFB1.pdf.json").write_bytes(_api())  # no page: cannot bind, skipped
    (tmp_path / "KR7396520009.pdf.html").write_bytes(_TIGER_TABLE)
    (tmp_path / "KR7396520009.page.html").write_bytes(_tiger_saved().page_content)

    kodex = load_saved_kodex_products(tmp_path, requested_as_of=date(2026, 8, 22))
    mobile = load_saved_kodex_products(
        tmp_path, requested_as_of=date(2026, 8, 22), page_host="m.samsungfund.com"
    )
    tiger = load_saved_tiger_products(tmp_path, fix_date=date(2026, 8, 21))

    assert [item.page_id for item in kodex] == ["2ETFE5"]
    assert kodex[0].holdings_url == _API_URL
    assert kodex[0].page_url == _PAGE_URL
    assert mobile[0].page_url == "https://m.samsungfund.com/etf/product/view.do?id=2ETFE5"
    assert [item.page_id for item in tiger] == ["KR7396520009"]
    assert tiger[0].holdings_url.endswith("&fixDate=2026.08.21")
    assert tiger[0].page_url == _TIGER_PAGE_URL


def test_mobile_kodex_page_binds_through_its_approved_source() -> None:
    saved = SavedHoldingsProduct(
        publisher="kodex",
        page_id="2ETFE5",
        page_url="https://m.samsungfund.com/etf/product/view.do?id=2ETFE5",
        page_content=_page(),
        holdings_url=_API_URL,
        holdings_content=_api(),
    )
    report = build_holdings_relations((saved,), universe=_universe(_ECOPRO_ETF))

    assert report.bound_page_ids == ("2ETFE5",)
    page_evidence = next(
        item
        for item in report.relations[0].evidence
        if item.source_uri.startswith("https://m.samsungfund.com/")
    )
    assert page_evidence.source_registry_id == "samsungfund-kodex-product-mobile"


def test_live_bootstrap_admits_a_graph_only_inside_its_trust_boundary() -> None:
    report = build_holdings_relations((_saved(),), universe=_universe(_ECOPRO_ETF))
    graph = seal_holdings_graph(report.relations)

    bootstrap = build_live_external_knowledge_bootstrap(graph)

    assert bootstrap.artifact.retrieval_mode == "verified_live_official_with_relation_graph"
    assert bootstrap.artifact.temporal_relation_sha256 == graph.manifest.relation_set_sha256
    assert bootstrap.artifact.document_dense_sha256 is None
    assert bootstrap.relation_bundle is graph
    assert build_live_external_knowledge_bootstrap().artifact.retrieval_mode == (
        "verified_live_official"
    )

    foreign_corpus = seal_temporal_relation_bundle(
        report.relations, corpus_manifest_sha256="f" * 64
    )
    with pytest.raises(ValueError, match="trust boundary"):
        build_live_external_knowledge_bootstrap(foreign_corpus)
    with pytest.raises(HoldingsGraphError):
        seal_holdings_graph(())
