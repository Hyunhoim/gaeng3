from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace

import pytest

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDecision,
    ExternalKnowledgeRouteDisposition,
)
from finance_agent_core.agent.external_search_planning import (
    ProvidedProductUniverseBuilder,
    resolve_question_identity_surfaces,
)
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeMatchMode,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import VerifiedLiveRelationExtractor
from finance_agent_core.storage.identity_cache import ProductIdentityRecord


def _draft(*families: ProductFamily) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(families),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )


def _kodex_200_identity() -> ProductIdentityRecord:
    return ProductIdentityRecord(
        product_family="domestic_etp",
        product_id="KR7069500007",
        product_name="삼성 KODEX 200증권상장지수투자신탁[주식]",
        ticker="A069500",
        isin="KR7069500007",
        short_name="KODEX 200",
        public_offering=None,
        is_quarantined=False,
        source_owner="삼성",
    )


@pytest.mark.parametrize(
    "question",
    [
        "KODEX 200을 편입한 국내 ETF를 찾아줘.",
        "KODEX 200이 편입된 해외 ETF를 알려줘.",
        "KODEX 200를 보유한 상장지수상품이 있어?",
    ],
)
def test_provided_product_name_used_as_a_holding_never_binds_the_db_universe(
    question: str,
) -> None:
    records = {ProductFamily.DOMESTIC_ETP: (_kodex_200_identity(),)}

    assert resolve_question_identity_surfaces(records, question) == ()


def test_same_surface_can_be_a_holding_and_an_explicit_product_in_one_question() -> None:
    records = {ProductFamily.DOMESTIC_ETP: (_kodex_200_identity(),)}
    question = "KODEX 200을 담은 ETF와 KODEX 200의 구성종목을 비교해줘."

    assert resolve_question_identity_surfaces(records, question) == ("KODEX 200",)


def test_official_profile_targets_cannot_become_product_identity_aliases() -> None:
    def record(product_id: str, ticker: str, short_name: str) -> ProductIdentityRecord:
        return ProductIdentityRecord(
            product_family="overseas_etp",
            product_id=product_id,
            product_name=f"{short_name} Synthetic ETF",
            ticker=f"{ticker}.O",
            isin=None,
            short_name=short_name,
            public_offering=None,
            is_quarantined=False,
        )

    records = {
        ProductFamily.OVERSEAS_ETP: (
            record("AMX:ALFA.O", "ALFA", "Alpha Source"),
            record("AMX:BETA.O", "BETA", "Beta Source"),
            # This approved-DB alias overlaps punctuation-normalized words in
            # the target holdings list and must not narrow source products.
            record("AMX:CK.O", "CK", "Corp K"),
        )
    }
    question = (
        "ALFA, BETA 두 상품의 공식 상위 10개 편입 종목을 보면 "
        "Mining Corp, Kappa Systems 두 종목이 모두 들어 있는 ETF를 찾아줘."
    )

    assert resolve_question_identity_surfaces(records, question) == ("ALFA", "BETA")


def test_each_official_profile_clause_can_bind_its_own_source_product() -> None:
    def record(product_id: str, ticker: str) -> ProductIdentityRecord:
        return ProductIdentityRecord(
            product_family="overseas_etp",
            product_id=product_id,
            product_name=f"{ticker} Synthetic ETF",
            ticker=f"{ticker}.O",
            isin=None,
            short_name=ticker,
            public_offering=None,
            is_quarantined=False,
        )

    records = {
        ProductFamily.OVERSEAS_ETP: (
            record("AMX:ALFA.O", "ALFA"),
            record("AMX:BETA.O", "BETA"),
            record("AMX:GAMMA.O", "GAMMA"),
        )
    }

    assert resolve_question_identity_surfaces(
        records,
        "ALFA 공식 편입표에서 X를 확인하고 BETA 공식 편입표에서 Y를 확인해줘.",
    ) == ("ALFA", "BETA")
    assert resolve_question_identity_surfaces(
        records,
        "ALFA 공식 편입표에서 X를 확인하고 BETA의 위험요인을 알려줘.",
    ) == ("ALFA", "BETA")
    assert resolve_question_identity_surfaces(
        records,
        "ALFA 공식 편입표에서 BETA, GAMMA 두 종목을 확인해줘.",
    ) == ("ALFA",)
    assert resolve_question_identity_surfaces(
        records,
        "ALFA 공식 편입표에서 BETA를 확인하고 GAMMA 공식 편입표에서 Y를 확인해줘.",
    ) == ("ALFA", "GAMMA")
    assert resolve_question_identity_surfaces(
        records,
        "ALFA 공식 편입표에서 BETA를 확인해줘. GAMMA ETF의 AUM도 알려줘.",
    ) == ("ALFA", "GAMMA")


@pytest.mark.parametrize(
    "question",
    [
        "ALFA의 공식 편입종목은 BETA와 GAMMA야. 관련 상품을 찾아줘.",
        "ALFA 공식 구성종목 목록: BETA, GAMMA. 관련 상품을 찾아줘.",
        "ALFA가 공식적으로 보유한 종목은 BETA와 GAMMA야. 관련 상품을 찾아줘.",
    ],
)
def test_natural_official_holding_lists_do_not_promote_targets_to_source_products(
    question: str,
) -> None:
    def record(product_id: str, ticker: str) -> ProductIdentityRecord:
        return ProductIdentityRecord(
            product_family="overseas_etp",
            product_id=product_id,
            product_name=f"{ticker} Synthetic ETF",
            ticker=f"{ticker}.O",
            isin=None,
            short_name=ticker,
            public_offering=None,
            is_quarantined=False,
        )

    records = {
        ProductFamily.OVERSEAS_ETP: (
            record("AMX:ALFA.O", "ALFA"),
            record("AMX:BETA.O", "BETA"),
            record("AMX:GAMMA.O", "GAMMA"),
        )
    }

    assert resolve_question_identity_surfaces(records, question) == ("ALFA",)


def test_long_product_identity_suppresses_a_contained_shorter_product_name() -> None:
    def record(product_id: str, short_name: str) -> ProductIdentityRecord:
        return ProductIdentityRecord(
            product_family="domestic_etp",
            product_id=product_id,
            product_name=f"한빛 {short_name} 증권상장지수투자신탁",
            ticker=product_id[-6:],
            isin=product_id,
            short_name=short_name,
            public_offering=None,
            is_quarantined=False,
        )

    records = {
        ProductFamily.DOMESTIC_ETP: (
            record("KR7000000101", "ALPHA 성장"),
            record("KR7000000102", "ALPHA 성장TOP10"),
        )
    }

    assert resolve_question_identity_surfaces(
        records,
        "ALPHA 성장TOP10의 공식 구성종목을 확인해줘.",
    ) == ("ALPHA 성장TOP10",)
    assert resolve_question_identity_surfaces(
        records,
        "ALPHA 성장TOP10과 ALPHA 성장의 공식 구성종목을 비교해줘.",
    ) == ("ALPHA 성장TOP10", "ALPHA 성장")


def test_indexed_runtime_identity_binding_uses_the_same_holding_boundary(tmp_path) -> None:
    database = tmp_path / "domestic-etp.sqlite3"
    database.write_bytes(b"identity-only")
    snapshot = SimpleNamespace(records=(_kodex_200_identity(),), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.DOMESTIC_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    holding = builder.question_identity_surfaces(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="KODEX 200을 편입한 ETF를 찾아줘.",
    )
    product = builder.question_identity_surfaces(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="KODEX 200의 구성종목과 위험요인을 알려줘.",
    )

    assert holding == ()
    assert product == ("KODEX 200",)


def test_indexed_runtime_recognizes_a_unique_short_overseas_ticker(tmp_path) -> None:
    database = tmp_path / "overseas-etp.sqlite3"
    database.write_bytes(b"identity-only")
    record = ProductIdentityRecord(
        product_family="overseas_etp",
        product_id="AMX:XA",
        product_name="Corgi AI Cybersecurity ETF",
        ticker="XA",
        isin=None,
        short_name=None,
        public_offering=None,
        is_quarantined=False,
    )
    snapshot = SimpleNamespace(records=(record,), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.OVERSEAS_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    detail = builder.question_identity_surfaces(
        families=(ProductFamily.OVERSEAS_ETP,),
        question="XA ETF의 위험요인을 공식 문서로 알려줘.",
    )
    holding = builder.question_identity_surfaces(
        families=(ProductFamily.OVERSEAS_ETP,),
        question="XA를 편입한 해외 ETF를 찾아줘.",
    )

    assert detail == ("XA",)
    assert holding == ()


@pytest.mark.parametrize(
    "question",
    [
        "삼성전자를 편입한 국내 ETF를 찾아줘.",
        "KODEX 200을 편입한 ETF를 찾아줘.",
    ],
)
def test_holding_entity_or_product_brand_cannot_narrow_source_selection(
    tmp_path,
    question: str,
) -> None:
    database = tmp_path / "domestic-etp.sqlite3"
    database.write_bytes(b"identity-only")
    snapshot = SimpleNamespace(records=(_kodex_200_identity(),), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.DOMESTIC_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    signals = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question=question,
    )

    assert signals.db_confirmed_aliases == ()
    assert signals.matched_product_names == ()
    assert signals.matched_source_owners == ()
    assert not signals.explicit_identity_bound


def test_target_product_or_brand_still_selects_its_db_confirmed_source(tmp_path) -> None:
    database = tmp_path / "domestic-etp.sqlite3"
    database.write_bytes(b"identity-only")
    snapshot = SimpleNamespace(records=(_kodex_200_identity(),), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.DOMESTIC_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    exact = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="KODEX 200의 위험요인을 알려줘.",
    )
    brand = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="KODEX ETF 중 우주 테마 연결 이력을 알려줘.",
    )

    assert exact.matched_product_names == (
        "삼성 KODEX 200증권상장지수투자신탁[주식]",
    )
    assert exact.matched_source_owners == ("삼성",)
    assert exact.explicit_identity_bound
    assert "KODEX" in brand.db_confirmed_aliases


def test_source_brand_matching_tolerates_punctuation_without_partial_word_matches(
    tmp_path,
) -> None:
    database = tmp_path / "domestic-etp.sqlite3"
    database.write_bytes(b"identity-only")
    record = ProductIdentityRecord(
        product_family="domestic_etp",
        product_id="KR7000000001",
        product_name="NH-Amundi HANARO 미래산업 ETF",
        ticker="A000001",
        isin="KR7000000001",
        short_name="HANARO 미래산업",
        public_offering=None,
        is_quarantined=False,
        source_owner="NH-Amundi자산운용",
    )
    snapshot = SimpleNamespace(records=(record,), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.DOMESTIC_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    punctuated = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="NH Amundi ETF의 최근 테마 연결 이력을 알려줘.",
    )
    partial = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="NH Amunditech를 편입한 ETF를 알려줘.",
    )

    assert "NH-Amundi" in punctuated.db_confirmed_aliases
    assert "NH-Amundi" not in partial.db_confirmed_aliases


@pytest.mark.parametrize(
    ("question", "families", "expected"),
    [
        (
            "아틀라스성장펀드가 어떤 구조로 조성됐고 운용 전략은 무엇인지 알려줘.",
            (ProductFamily.FUND,),
            (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
        ),
        (
            "아틀라스성장펀드의 모집 현황과 출시 일정을 공식 자료로 정리해줘.",
            (ProductFamily.FUND,),
            (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
        ),
        (
            "아틀라스성장펀드 판매 완료 뒤 향후 계획과 진행 상황을 찾아줘.",
            (ProductFamily.FUND,),
            (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
        ),
        (
            "아틀라스성장펀드의 조성 방식과 투자전략 동향을 설명해줘.",
            (ProductFamily.FUND,),
            (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
        ),
        (
            "네뷸라테크를 편입한 국내 ETF를 찾아줘.",
            (ProductFamily.DOMESTIC_ETP,),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "네뷸라테크를 보유 중인 해외 ETF를 알려줘.",
            (ProductFamily.OVERSEAS_ETP,),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "구성종목에 네뷸라테크가 들어 있는 ETF를 정리해줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "포트폴리오에 네뷸라테크를 담고 있는 ETF가 있어?",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 ETF를 정리해줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "6개월 이내 공식 문서에서 우주항공과 연관된 국내 상장 ETF를 찾아줘.",
            (ProductFamily.DOMESTIC_ETP,),
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "지난 180일 사이 방위산업 섹터와 연관된 국내 ETF를 보여줘.",
            (ProductFamily.DOMESTIC_ETP,),
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "2026년 5~7월 AI 산업에 노출된 해외 ETF를 찾아줘.",
            (ProductFamily.OVERSEAS_ETP,),
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "올해 1월 이후 친환경 테마 관련 ETF를 확인해줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "오로라그룹의 자회사를 편입한 ETF 중 AUM이 가장 큰 상품의 위험요인을 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        ),
        (
            "오로라그룹 자회사인 네뷸라소재를 담은 국내 ETF의 위험요인을 알려줘.",
            (ProductFamily.DOMESTIC_ETP,),
            (
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        ),
        (
            "오로라그룹 계열사 중 네뷸라소재를 보유한 ETF를 총보수 낮은 순으로 3개 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.CORPORATE_SUBSIDIARY, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        ),
        (
            "오로라홀딩스 산하 기업을 보유하고 로봇 테마와 연결된 ETF의 위험요인을 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.SECTOR_THEME_HISTORY,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        ),
        (
            "네뷸라테크나 오로라반도체 중 하나 이상을 담은 ETF를 찾아줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "네뷸라테크와 오로라반도체를 둘 다 보유한 ETF를 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "위성기업을 편입하고 우주 산업과 연관된 ETF를 검색해줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS, KnowledgeCapability.SECTOR_THEME_HISTORY),
        ),
        (
            "로봇 산업 관련 ETF 중 투자위험 요인을 확인할 수 있는 상품을 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.SECTOR_THEME_HISTORY, KnowledgeCapability.PRODUCT_RISK_FACTORS),
        ),
        (
            "네뷸라테크가 포함된 ETF를 거래량 많은 순으로 다섯 개 보여줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        ),
        (
            "네뷸라테크 편입 ETF 가운데 순자산이 큰 두 상품의 위험요인을 비교해줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS, KnowledgeCapability.PRODUCT_RISK_FACTORS),
        ),
        (
            "최근 반년 우주 산업 관련 ETF 중 위성기업을 구성종목으로 둔 상품을 알려줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            (KnowledgeCapability.PORTFOLIO_HOLDINGS, KnowledgeCapability.SECTOR_THEME_HISTORY),
        ),
        (
            "아틀라스성장펀드의 구조, 모집 일정, 운용 기조와 향후 계획을 알려줘.",
            (ProductFamily.FUND,),
            (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
        ),
    ],
)
def test_unseen_external_relation_language_compiles_without_entity_rules(
    question: str,
    families: tuple[ProductFamily, ...],
    expected: tuple[KnowledgeCapability, ...],
) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(question, _draft(*families))
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-unseen-variant",
        sanitized,
    )

    assert tuple(dict.fromkeys(item.capability for item in plan.requirements)) == expected
    assert all(item.subject_evidence_span in question for item in plan.requirements)
    assert all(item.relation_evidence_span in question for item in plan.requirements)


@pytest.mark.parametrize(
    ("question", "expected_mode"),
    [
        (
            "네뷸라테크나 오로라반도체 중 하나 이상을 담은 ETF를 찾아줘.",
            KnowledgeMatchMode.ANY,
        ),
        (
            "네뷸라테크와 오로라반도체를 둘 다 보유한 ETF를 알려줘.",
            KnowledgeMatchMode.ALL,
        ),
    ],
)
def test_unseen_holding_set_operation_is_explicit(
    question: str,
    expected_mode: KnowledgeMatchMode,
) -> None:
    draft = _draft(ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-holding-set-variant",
        sanitize_server_authorized_knowledge_draft(question, draft),
    )

    assert plan.holding_match_mode is expected_mode


@pytest.mark.parametrize(
    ("question", "expected_start"),
    [
        (
            "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 ETF를 정리해줘.",
            date(2026, 2, 24),
        ),
        (
            "지난 180일 사이 방위산업 섹터와 연관된 국내 ETF를 보여줘.",
            date(2026, 2, 25),
        ),
        (
            "올해 1월 이후 친환경 테마 관련 ETF를 확인해줘.",
            date(2026, 1, 1),
        ),
    ],
)
def test_unseen_temporal_surface_is_cutoff_bounded(
    question: str,
    expected_start: date,
) -> None:
    draft = _draft(ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-time-variant",
        sanitize_server_authorized_knowledge_draft(question, draft),
    )

    assert all(item.time_range is not None for item in plan.requirements)
    assert all(
        item.time_range.start == expected_start
        for item in plan.requirements
        if item.time_range
    )
    assert all(
        item.time_range.end == date(2026, 8, 24)
        for item in plan.requirements
        if item.time_range
    )


@pytest.mark.parametrize(
    ("requested_relation", "official_text", "expected_count"),
    [
        (
            "구조",
            "아틀라스성장펀드는 정책금융과 민간자금이 함께 출자하여 조성되는 투자기구입니다.",
            1,
        ),
        (
            "투자 전략",
            "아틀라스성장펀드의 자산 배분과 운용 전략은 성장기업 장기투자입니다.",
            1,
        ),
        (
            "판매 일정",
            "아틀라스성장펀드의 공모와 판매는 8월 20일 완료될 예정입니다.",
            1,
        ),
        (
            "운용 동향",
            "아틀라스성장펀드의 최근 운용 현황과 향후 계획을 공지합니다.",
            1,
        ),
        (
            "판매 일정",
            "아틀라스성장펀드는 정책금융과 민간자금이 함께 참여하는 투자 구조입니다.",
            0,
        ),
        (
            "투자 전략",
            "아틀라스성장펀드의 판매와 모집 일정은 8월 말 완료될 예정입니다.",
            0,
        ),
    ],
)
def test_fund_evidence_cannot_substitute_a_different_requested_dimension(
    requested_relation: str,
    official_text: str,
    expected_count: int,
) -> None:
    question = f"아틀라스성장펀드의 {requested_relation}을 알려줘."
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "fund-dimension-evidence",
        sanitize_server_authorized_knowledge_draft(
            question,
            _draft(ProductFamily.FUND),
        ),
    )
    requirement = plan.requirements[0]
    registry = load_official_source_registry()
    source_uri = "https://www.fsc.go.kr/po010103/86950"
    content = official_text.encode()
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(
            source_uri,
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        ),
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_uri=source_uri,
        title="아틀라스성장펀드 공식 자료",
        content_type="text/plain",
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        published_at=date(2026, 8, 20),
        as_of=date(2026, 8, 20),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.FUND,
                product_id="FUND-ATLAS-A",
                product_name="아틀라스성장펀드증권투자신탁(주식)A",
            ),
        ),
        database_sha256s={ProductFamily.FUND: "a" * 64},
        candidate_filter_sha256="b" * 64,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert len(relations) == expected_count


@pytest.mark.parametrize(
    "question",
    [
        "별빛전자를 실제 구성종목으로 담은 국내 ETF를 찾아 주세요.",
        "포트폴리오에 네뷸라소재를 담은 해외 ETF를 공식 자료로 확인해 주세요.",
        "최근 넉 달 동안 심해로봇 테마와 연관된 ETF를 정리해 주세요.",
        "오로라그룹 산하 회사를 편입한 국내 ETF를 알려 주세요.",
        "은하전자를 보유한 ETF 가운데 순자산이 큰 상품의 위험요인을 알려 주세요.",
        "새봄성장펀드가 어떤 구조로 조성됐는지 설명해 주세요.",
        "푸른정책펀드의 모집 현황과 최근 운용 동향을 찾아 주세요.",
        "위성기업을 구성종목으로 두고 우주산업과 연결된 ETF를 확인해 주세요.",
    ],
)
def test_valid_external_question_without_evidence_runtime_is_cannot_verify_not_unsupported(
    sample_database,
    domestic_sample_database,
    question: str,
) -> None:
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="external-runtime-absent", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.products == []
    assert result.response.query_plan is not None


def test_theme_to_product_to_risk_with_evidence_order_reaches_typed_external_route(
    sample_database,
    domestic_sample_database,
) -> None:
    question = (
        "2026년 2월 24일부터 2026년 8월 24일까지 운용사가 공개한 공식 자료에서 "
        "AI 반도체 테마로 소개된 국내 ETF를 찾아주세요. "
        "테마 연결 근거의 발행일이 최신인 순서로 최대 5개를 보여주고, "
        "각 상품의 최신 공식 문서에 적힌 핵심 위험요인도 함께 요약해 주세요."
    )
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="theme-risk-evidence-order", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.query_plan is not None
    assert result.response.query_plan.evidence_selection is not None
    assert result.response.query_plan.evidence_selection.order_by == "published_at"
    assert [
        requirement.capability
        for requirement in result.response.query_plan.requirements
    ] == [
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]


def test_public_route_keeps_two_holding_entities_without_projection_noise(
    sample_database,
    domestic_sample_database,
) -> None:
    question = (
        "가장 최근의 공식 구성종목 자료를 기준으로 삼성전자와 SK하이닉스를 "
        "둘 다 담고 있는 국내 ETF만 찾아 순자산이 큰 순서로 5개 보여주세요. "
        "두 종목의 편입 비중과 구성종목 기준일도 알려주세요."
    )
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="two-holding-entities", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.query_plan is not None
    holdings = [
        requirement
        for requirement in result.response.query_plan.requirements
        if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    assert [requirement.subject for requirement in holdings] == [
        "삼성전자",
        "SK하이닉스",
    ]
    assert result.response.query_plan.holding_match_mode is KnowledgeMatchMode.ALL


def test_absent_external_runtime_preserves_typed_knowledge_search_route(
    sample_database,
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = agent._answer_public_atomically(
        "오로라그룹 계열사를 편입한 국내 ETF의 위험요인을 확인해 주세요.",
        "typed-knowledge-search-without-runtime",
    )

    assert type(result) is ExternalKnowledgeRouteDecision
    assert result.disposition is ExternalKnowledgeRouteDisposition.KNOWLEDGE_SEARCH
    assert result.plan is not None
    assert not result.plan.execution_authorized


@pytest.mark.parametrize(
    "question",
    [
        "제공 DB에 없는 금융상품 코드 AB-MISSING-17의 상세를 조회해 주세요.",
        "허구 상품 식별자 XY-NOT-LISTED-204를 공식 데이터에서 확인해 주세요.",
        "존재하지 않는 종목 번호 ZZ-ABSENT-390의 정보를 알려 주세요.",
    ],
)
def test_plain_unknown_labeled_identity_is_not_found_after_all_db_scopes_are_checked(
    sample_database,
    domestic_sample_database,
    question: str,
) -> None:
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="unknown-labeled-identity", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.NOT_FOUND
    assert result.response.intent is InteractionIntent.DETAIL
    assert result.response.candidate_count == 0
    assert result.response.products == []


def test_plain_labeled_identity_that_exists_still_uses_normal_detail_execution(
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]}
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="known-labeled-identity",
            question="상품 코드 KR7000000003의 상세 정보를 조회해 주세요.",
        ),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.SUCCESS
    assert result.response.intent is InteractionIntent.DETAIL
    assert [item.product_id for item in result.response.products] == ["KR7000000003"]


def test_source_selection_without_a_named_family_inspects_every_approved_database(
    tmp_path,
) -> None:
    # "2026년 9월 1일 기준 SPY에서 엔비디아 편입 비중" names a product but no
    # family. Blind set A question 35 crashed the request with HTTP 500 here.
    database = tmp_path / "domestic-etp.sqlite3"
    database.write_bytes(b"identity-only")
    snapshot = SimpleNamespace(records=(_kodex_200_identity(),), version="v1")

    class FixedIdentityCache:
        def get(self, path):
            assert path == database
            return snapshot

    builder = ProvidedProductUniverseBuilder(
        {ProductFamily.DOMESTIC_ETP: database},
        identity_cache=FixedIdentityCache(),  # type: ignore[arg-type]
        verify_approved_databases=False,
    )

    signals = builder.source_selection_signals(
        families=(),
        question="KODEX 200의 위험요인을 알려줘.",
    )
    named = builder.source_selection_signals(
        families=(ProductFamily.DOMESTIC_ETP,),
        question="KODEX 200의 위험요인을 알려줘.",
    )

    # The selector treats an empty family tuple as "unconstrained", so only the
    # identity-derived fields must agree with the family-named call.
    assert signals.product_families == ()
    assert signals.db_confirmed_aliases == named.db_confirmed_aliases
    assert signals.matched_product_names == named.matched_product_names
    assert signals.matched_source_owners == named.matched_source_owners
    assert signals.explicit_identity_bound is named.explicit_identity_bound
    assert signals.matched_product_names != ()
