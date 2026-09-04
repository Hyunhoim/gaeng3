"""Dependent external steps spend their budget on evidence-supported entities.

Official example 5 (``에코프로의 자회사를 편입한 ETF …``) extracted 207 subsidiary
relations from a half-year filing, including noise such as ``2024년``, ``업무위탁``,
``에코프로_대표이사`` and ``에코프로머티리얼즈로`` (a real name plus a particle).  Twenty
targets each received one search query and the holdings step could not verify
anything.  Targets are now ranked by supporting relation count, cleaned and capped.
"""

from __future__ import annotations

from types import SimpleNamespace

import finance_agent_core.agent  # noqa: F401 - resolves the retrieval import cycle
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntime,
    _plausible_dependency_entity,
    _relation_survives_universe,
)


def _entity(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=f"company:{name}", canonical_name=name, aliases=(), kind="company"
    )


def _subsidiary(child: str) -> SimpleNamespace:
    return SimpleNamespace(
        capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
        subject=_entity("에코프로"),
        object=_entity(child),
        product_family=None,
        product_id=None,
    )


def _holdings_requirement() -> SimpleNamespace:
    return SimpleNamespace(
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS, depends_on=("need_1",)
    )


def test_targets_are_ranked_by_support_cleaned_and_capped() -> None:
    relations = (
        [_subsidiary("에코프로비엠")] * 12
        + [_subsidiary("에코프로머티리얼즈")] * 9
        + [_subsidiary("에코프로에이치엔")] * 7
        + [_subsidiary("에코프로머티리얼즈로")] * 3
        + [_subsidiary("2024년")] * 20
        + [_subsidiary("업무위탁")] * 5
        + [_subsidiary("에코프로_대표이사")] * 2
        + [_subsidiary("에코프로글로벌")] * 1
    )

    targets = ExternalKnowledgeRuntime._dependency_targets(
        _holdings_requirement(),
        {"need_1": tuple(relations)},
        max_targets=3,
    )

    assert [name for name, _, _ in targets] == [
        "에코프로비엠",
        "에코프로머티리얼즈",
        "에코프로에이치엔",
    ]


def test_uncapped_targets_keep_every_plausible_entity_in_support_order() -> None:
    relations = [_subsidiary("에코프로글로벌")] + [_subsidiary("에코프로비엠")] * 2

    targets = ExternalKnowledgeRuntime._dependency_targets(
        _holdings_requirement(),
        {"need_1": tuple(relations)},
    )

    assert [name for name, _, _ in targets] == ["에코프로비엠", "에코프로글로벌"]


def test_implausible_entities_are_rejected() -> None:
    assert not _plausible_dependency_entity("2024년")
    assert not _plausible_dependency_entity("에코프로_대표이사")
    assert not _plausible_dependency_entity("업무위탁")
    assert _plausible_dependency_entity("에코프로비엠")
    assert _plausible_dependency_entity("Cambricon")


def test_subsidiary_relations_survive_the_universe_filter() -> None:
    approved = frozenset({("domestic_etp", "KR7305720005")})
    subsidiary = _subsidiary("에코프로비엠")
    holding_in = SimpleNamespace(
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        product_family="domestic_etp",
        product_id="KR7305720005",
    )
    holding_out = SimpleNamespace(
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        product_family="domestic_etp",
        product_id="KR0000000000",
    )

    assert _relation_survives_universe(subsidiary, approved)
    assert _relation_survives_universe(holding_in, approved)
    assert not _relation_survives_universe(holding_out, approved)
