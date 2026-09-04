from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeMatchMode,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.retrieval.hangul_romanization import best_phonetic_label
from finance_agent_core.retrieval.temporal_relations import TemporalRelationRecord


class ExternalKnowledgeVerificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialProductFact(ExternalKnowledgeVerificationModel):
    evidence_id: str = Field(min_length=1, max_length=200)
    product_family: ProductFamily
    product_id: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=500)
    canonical_field: str = Field(min_length=1, max_length=100)
    normalized_value: float | int | str
    as_of: date
    source_kind: Literal["provided"] = "provided"
    source_dataset: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=100)
    source_row: int = Field(ge=2)
    source_columns: tuple[str, ...] = Field(min_length=1, max_length=10)
    unit: str = Field(min_length=1, max_length=100)
    quality: QualityStatus = QualityStatus.VALID
    trading_currency: str | None = Field(default=None, min_length=1, max_length=32)
    source_database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalKnowledgeEvidenceBundle(ExternalKnowledgeVerificationModel):
    plan: CompositeKnowledgeQueryPlan
    relations: tuple[TemporalRelationRecord, ...] = Field(min_length=1, max_length=200)
    requirement_relations: dict[str, tuple[str, ...]]
    product_facts: tuple[OfficialProductFact, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def validate_unique_identifiers(self) -> ExternalKnowledgeEvidenceBundle:
        relation_ids = [item.relation_id for item in self.relations]
        fact_ids = [item.evidence_id for item in self.product_facts]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("external evidence relation IDs must be unique")
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("official product fact evidence IDs must be unique")
        return self


class ExternalKnowledgeVerification(ExternalKnowledgeVerificationModel):
    passed: bool
    checks: dict[str, bool]
    violations: tuple[str, ...]


def _normalized(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


_RANK_FIELD_UNITS = {
    "aum": "source_currency_amount",
    "total_expense_ratio_pct": "pct_point",
    "one_day_return_pct": "pct_point",
    "one_month_return_pct": "pct_point",
    "three_month_return_pct": "pct_point",
    "one_year_return_pct": "pct_point",
    "daily_volume": "source_quantity",
    "daily_trading_value": "source_currency_amount",
    "twenty_day_volatility_pct": "pct_point",
}


def _product_key(relation: TemporalRelationRecord) -> tuple[ProductFamily, str] | None:
    if relation.product_family is None or relation.product_id is None:
        return None
    return relation.product_family, relation.product_id


class ExternalKnowledgeEvidenceVerifier:
    """Require a complete external relation chain and official DB product facts."""

    def verify(
        self,
        bundle: ExternalKnowledgeEvidenceBundle,
        *,
        approved_product_ids: set[tuple[ProductFamily, str]],
    ) -> ExternalKnowledgeVerification:
        checks = {
            "requirement_coverage": True,
            "requirement_scope": True,
            "source_scope": True,
            "time_scope": True,
            "official_product_join": True,
            "multi_hop_chain": True,
            "holding_set_operation": True,
            "relation_set_operation": True,
            "dependent_product_scope": True,
            "ranking_source": True,
            "ranking_order": True,
        }
        violations: list[str] = []
        relations_by_id = {item.relation_id: item for item in bundle.relations}
        requirement_by_id = {item.requirement_id: item for item in bundle.plan.requirements}

        if set(bundle.requirement_relations) != set(requirement_by_id):
            checks["requirement_coverage"] = False
            violations.append("requirement evidence map is incomplete or contains unknown IDs")
        for requirement_id, requirement in requirement_by_id.items():
            relation_ids = bundle.requirement_relations.get(requirement_id, ())
            if not relation_ids:
                if not (
                    requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                    and bundle.plan.holding_match_mode is KnowledgeMatchMode.ANY
                ):
                    checks["requirement_coverage"] = False
                    violations.append(f"{requirement_id} has no relation evidence")
                continue
            for relation_id in relation_ids:
                relation = relations_by_id.get(relation_id)
                if relation is None:
                    checks["requirement_coverage"] = False
                    violations.append(f"{requirement_id} refers to an absent relation")
                    continue
                if (
                    relation.capability is not requirement.capability
                    or relation.predicate is not requirement.predicate
                ):
                    checks["requirement_scope"] = False
                    violations.append(f"{requirement_id} relation exceeds its predicate scope")
                labels = {*relation.subject.labels(), *relation.object.labels()}
                allowed_subjects = {
                    _normalized(value)
                    for value in (requirement.subject, *requirement.search_aliases)
                    if _normalized(value)
                }
                if (
                    not requirement.depends_on
                    and not allowed_subjects.intersection({_normalized(label) for label in labels})
                    # A Hangul subject may bind a Latin-labelled relation only
                    # through the same phonetic rule the lookup used.
                    and best_phonetic_label(requirement.subject, labels) is None
                ):
                    checks["requirement_scope"] = False
                    violations.append(f"{requirement_id} relation does not bind its subject")
                if (
                    relation.product_family is not None
                    and requirement.product_families
                    and (relation.product_family not in requirement.product_families)
                ):
                    checks["requirement_scope"] = False
                    violations.append(f"{requirement_id} relation changed product family")
                if not relation.overlaps(requirement.time_range):
                    checks["time_scope"] = False
                    violations.append(f"{requirement_id} relation is outside the time range")
                if relation.observed_at > bundle.plan.evidence_cutoff or any(
                    evidence.as_of > bundle.plan.evidence_cutoff
                    or (
                        evidence.published_at is not None
                        and evidence.published_at > bundle.plan.evidence_cutoff
                    )
                    for evidence in relation.evidence
                ):
                    checks["time_scope"] = False
                    violations.append(f"{requirement_id} evidence exceeds the cutoff")
                if any(
                    evidence.source_registry_id not in requirement.source_registry_ids
                    for evidence in relation.evidence
                ):
                    checks["source_scope"] = False
                    violations.append(f"{requirement_id} used an unauthorized source")
                if (
                    relation.product_id is not None
                    and (
                        relation.product_family,
                        relation.product_id,
                    )
                    not in approved_product_ids
                ):
                    checks["official_product_join"] = False
                    violations.append(f"{requirement_id} product is absent from the official DB")

        fact_product_ids = {(item.product_family, item.product_id) for item in bundle.product_facts}
        if not fact_product_ids <= approved_product_ids:
            checks["official_product_join"] = False
            violations.append("product facts contain a product outside the official DB")
        if any(item.as_of > bundle.plan.evidence_cutoff for item in bundle.product_facts):
            checks["time_scope"] = False
            violations.append("official product fact date exceeds the evidence cutoff")

        holding_requirements = [
            requirement
            for requirement in bundle.plan.requirements
            if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        ]
        holding_sets = [
            {
                key
                for relation_id in bundle.requirement_relations.get(requirement.requirement_id, ())
                if (relation := relations_by_id.get(relation_id)) is not None
                if (key := _product_key(relation)) is not None
            }
            for requirement in holding_requirements
        ]
        if holding_sets:
            if bundle.plan.holding_match_mode is KnowledgeMatchMode.ALL:
                holding_products = set.intersection(*holding_sets)
            else:
                holding_products = set.union(*holding_sets)
        else:
            holding_products = set()
        independent_product_sets: list[set[tuple[ProductFamily, str]]] = []
        if holding_sets:
            independent_product_sets.append(holding_products)
        for requirement in bundle.plan.requirements:
            if (
                requirement.capability
                in {
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    KnowledgeCapability.PRODUCT_RISK_FACTORS,
                }
                and not requirement.depends_on
            ):
                independent_product_sets.append(
                    {
                        key
                        for relation_id in bundle.requirement_relations.get(
                            requirement.requirement_id, ()
                        )
                        if (relation := relations_by_id.get(relation_id)) is not None
                        if (key := _product_key(relation)) is not None
                    }
                )
        relation_products = (
            set.intersection(*independent_product_sets) if independent_product_sets else set()
        )

        subsidiary_objects = {
            relation.object.entity_id
            for relation in bundle.relations
            if relation.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
        }
        holdings = [
            relation
            for relation in bundle.relations
            if relation.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        ]
        if (
            subsidiary_objects
            and holdings
            and not any(
                relation.subject.entity_id in subsidiary_objects
                or relation.object.entity_id in subsidiary_objects
                for relation in holdings
            )
        ):
            checks["multi_hop_chain"] = False
            violations.append("subsidiary and portfolio-holding relations do not join")
        risk_products = {
            (relation.product_family, relation.product_id)
            for relation in bundle.relations
            if relation.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
        }
        all_holding_products = {
            key for relation in holdings if (key := _product_key(relation)) is not None
        }
        if risk_products and all_holding_products and not risk_products <= all_holding_products:
            checks["multi_hop_chain"] = False
            violations.append("risk evidence is not attached to a holding-matched product")

        selection = bundle.plan.product_selection
        selected_product_ids = relation_products
        if selection is not None:
            rank_facts = [
                item
                for item in bundle.product_facts
                if item.canonical_field == selection.rank_field
            ]
            rank_product_ids = {(item.product_family, item.product_id) for item in rank_facts}
            selected_product_ids = rank_product_ids
            if not rank_facts or len(rank_facts) > selection.limit:
                checks["ranking_source"] = False
                violations.append("ranking lacks the required official product facts")
            if len(rank_product_ids) != len(rank_facts):
                checks["ranking_source"] = False
                violations.append("ranking contains duplicate product facts")
            if any(item.source_kind != "provided" for item in rank_facts):
                checks["ranking_source"] = False
                violations.append("ranking used a non-provided product value")
            if holding_sets and not rank_product_ids <= holding_products:
                checks["holding_set_operation"] = False
                violations.append("ranking contains a product outside the holding set operation")
            if independent_product_sets and not rank_product_ids <= relation_products:
                checks["relation_set_operation"] = False
                violations.append("ranking contains a product outside the conjunctive relation set")
            expected_unit = _RANK_FIELD_UNITS[selection.rank_field]
            if any(item.unit != expected_unit for item in rank_facts):
                checks["ranking_source"] = False
                violations.append("ranking facts use the wrong canonical unit")
            if expected_unit == "source_currency_amount":
                currencies = {item.trading_currency for item in rank_facts}
                if None in currencies or len(currencies) != 1:
                    checks["ranking_source"] = False
                    violations.append("currency-valued ranking mixes or omits source currencies")
            values = [item.normalized_value for item in rank_facts]
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or value == 0
                for value in values
            ):
                checks["ranking_order"] = False
                violations.append("ranking values are non-numeric, zero, or non-finite")
            else:
                expected = sorted(
                    values,
                    reverse=selection.direction is SortDirection.DESC,
                )
                if values != expected:
                    checks["ranking_order"] = False
                    violations.append("official product facts are not in requested rank order")

        if independent_product_sets:
            allowed_fact_products = selected_product_ids
            if not fact_product_ids <= allowed_fact_products:
                checks["relation_set_operation"] = False
                violations.append(
                    "official product facts exceed the selected external relation set"
                )

        holding_requirement_ids = {
            requirement.requirement_id for requirement in holding_requirements
        }
        for requirement in bundle.plan.requirements:
            if not (
                requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and holding_requirement_ids.intersection(requirement.depends_on)
            ):
                continue
            scoped_risk_products = {
                key
                for relation_id in bundle.requirement_relations.get(requirement.requirement_id, ())
                if (relation := relations_by_id.get(relation_id)) is not None
                if (key := _product_key(relation)) is not None
            }
            if scoped_risk_products != selected_product_ids:
                checks["dependent_product_scope"] = False
                violations.append(
                    f"{requirement.requirement_id} risk evidence differs from selected products"
                )

        return ExternalKnowledgeVerification(
            passed=all(checks.values()),
            checks=checks,
            violations=tuple(violations),
        )


__all__ = [
    "ExternalKnowledgeEvidenceBundle",
    "ExternalKnowledgeEvidenceVerifier",
    "ExternalKnowledgeVerification",
    "OfficialProductFact",
]
