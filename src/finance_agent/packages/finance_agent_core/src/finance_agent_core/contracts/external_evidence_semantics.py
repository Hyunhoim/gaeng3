from __future__ import annotations

import re

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import KnowledgeRequirement

_RELATION_SIGNAL = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"편입|보유|구성\s*종목|holding", re.IGNORECASE
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|가족사|지주회사|분할\s*설립|family|subsidiar", re.IGNORECASE
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"테마|섹터|산업|우주|항공|theme|sector", re.IGNORECASE
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"구조|전략|운용|판매|일정|structure|strategy", re.IGNORECASE
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험|리스크|손실|변동|risk", re.IGNORECASE
    ),
}
_FUND_RELATION_DIMENSIONS: tuple[
    tuple[re.Pattern[str], re.Pattern[str]],
    ...,
] = (
    (
        re.compile(
            r"구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
            r"자금\s*(?:조달|배분)\s*구조|형태|structure|framework",
            re.IGNORECASE,
        ),
        re.compile(
            r"구조|조성|재원|출자|모자형|재간접|투자\s*기구|"
            r"structure|framework|funding\s+vehicle",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"(?:투자|운용)\s*(?:전략|기조|방침)|strategy", re.IGNORECASE),
        re.compile(
            r"(?:투자|운용)\s*(?:전략|기조|방침|방식)|자산\s*배분|"
            r"strategy|investment\s+approach|asset\s+allocation",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"판매|모집|출시|공모|subscription|sale", re.IGNORECASE),
        re.compile(
            r"판매|모집|출시|공모|청약|가입|납입|"
            r"subscription|offering|sale|launch",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"동향|현황|진행\s*(?:상황|현황)|향후|앞으로|계획|일정|"
            r"운용\s*(?:방향|계획|현황)|trend|status",
            re.IGNORECASE,
        ),
        re.compile(
            r"동향|현황|진행\s*상황|향후|계획|일정|완료|예정|"
            r"trend|status|schedule|completed|planned",
            re.IGNORECASE,
        ),
    ),
)


def requirement_relation_signal(requirement: KnowledgeRequirement) -> re.Pattern[str]:
    """Bind each requested relation dimension to matching official evidence text."""

    base = _RELATION_SIGNAL[requirement.capability]
    if requirement.capability is not KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND:
        return base
    patterns = tuple(
        evidence
        for request, evidence in _FUND_RELATION_DIMENSIONS
        if request.search(requirement.relation_evidence_span) is not None
    )
    if not patterns:
        return base
    unique_patterns = tuple(dict.fromkeys(item.pattern for item in patterns))
    request_surface = requirement.relation_evidence_span
    sales_requested = _FUND_RELATION_DIMENSIONS[2][0].search(request_surface) is not None
    status_requested = _FUND_RELATION_DIMENSIONS[3][0].search(request_surface) is not None
    if sales_requested and status_requested:
        required_patterns = (
            _FUND_RELATION_DIMENSIONS[2][1].pattern,
            _FUND_RELATION_DIMENSIONS[3][1].pattern,
        )
        return re.compile(
            "".join(f"(?=.*(?:{pattern}))" for pattern in required_patterns),
            re.IGNORECASE | re.DOTALL,
        )
    return re.compile("|".join(f"(?:{pattern})" for pattern in unique_patterns), re.IGNORECASE)


__all__ = ["requirement_relation_signal"]
