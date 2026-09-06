from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_evidence_semantics import requirement_relation_signal
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeMatchMode,
    KnowledgeRequirement,
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
    "net_asset_total": "source_currency_amount",
    "total_expense_ratio_pct": "pct_point",
    "tracking_error_pct": "pct_point",
    "one_day_return_pct": "pct_point",
    "one_month_return_pct": "pct_point",
    "three_month_return_pct": "pct_point",
    "one_year_return_pct": "pct_point",
    "daily_volume": "source_quantity",
    "daily_trading_value": "source_currency_amount",
    "twenty_day_volatility_pct": "pct_point",
}

_RANK_FIELD_FAMILIES = {
    "net_asset_total": frozenset((ProductFamily.DOMESTIC_ETP,)),
    "tracking_error_pct": frozenset((ProductFamily.DOMESTIC_ETP,)),
}


def _product_key(relation: TemporalRelationRecord) -> tuple[ProductFamily, str] | None:
    if relation.product_family is None or relation.product_id is None:
        return None
    return relation.product_family, relation.product_id


def relation_satisfies_document_binding(
    requirement: KnowledgeRequirement,
    relation: TemporalRelationRecord,
) -> bool:
    """Verify one relation against its signed product-local document scope."""

    if not requirement.document_bindings:
        return True
    product_key = _product_key(relation)
    binding = next(
        (
            item
            for item in requirement.document_bindings
            if (item.product_family, item.product_id) == product_key
        ),
        None,
    )
    if binding is None or not relation.overlaps(binding.time_range):
        return False
    return any(
        evidence.source_registry_id in binding.source_registry_ids
        and evidence.as_of <= binding.time_range.end
        and (
            binding.time_range.start is None
            or evidence.as_of >= binding.time_range.start
        )
        for evidence in relation.evidence
    )


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
            "document_scope": True,
            "source_scope": True,
            "time_scope": True,
            "official_product_join": True,
            "multi_hop_chain": True,
            "holding_set_operation": True,
            "relation_set_operation": True,
            "dependent_product_scope": True,
            "ranking_source": True,
            "ranking_order": True,
            "evidence_date_source": True,
            "evidence_date_order": True,
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
                if not relation.satisfies_document_scope(requirement.document_scope):
                    checks["document_scope"] = False
                    violations.append(
                        f"{requirement_id} relation lacks the requested document Top-N proof"
                    )
                if not relation_satisfies_document_binding(requirement, relation):
                    checks["document_scope"] = False
                    violations.append(
                        f"{requirement_id} relation differs from its product document binding"
                    )
                if (
                    requirement.capability
                    is KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
                    and not any(
                        requirement_relation_signal(requirement).search(evidence.excerpt)
                        is not None
                        for evidence in relation.evidence
                    )
                ):
                    checks["requirement_scope"] = False
                    violations.append(
                        f"{requirement_id} evidence differs from its requested fund dimension"
                    )
                labels = {
                    *relation.subject.labels(),
                    *relation.object.labels(),
                    *(
                        label
                        for entity in relation.scope_entities
                        for label in entity.labels()
                    ),
                }
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
            allowed_rank_families = _RANK_FIELD_FAMILIES.get(selection.rank_field)
            plan_rank_families = {
                family
                for requirement in bundle.plan.requirements
                for family in requirement.product_families
            }
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
            if allowed_rank_families is not None and (
                not plan_rank_families <= allowed_rank_families
                or any(item.product_family not in allowed_rank_families for item in rank_facts)
            ):
                checks["ranking_source"] = False
                violations.append("ranking field is not valid for a selected product family")
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

        evidence_selection = bundle.plan.evidence_selection
        if evidence_selection is not None:
            source_requirement = requirement_by_id[evidence_selection.source_requirement_id]
            dates_by_product: dict[tuple[ProductFamily, str], list[date]] = {}
            for relation_id in bundle.requirement_relations.get(
                source_requirement.requirement_id,
                (),
            ):
                relation = relations_by_id.get(relation_id)
                if relation is None:
                    continue
                product_key = _product_key(relation)
                if product_key is None or product_key not in relation_products:
                    continue
                for evidence in relation.evidence:
                    selected_date = (
                        evidence.published_at
                        if evidence_selection.order_by == "published_at"
                        else evidence.as_of
                    )
                    if selected_date is None:
                        continue
                    if selected_date > bundle.plan.evidence_cutoff:
                        checks["evidence_date_source"] = False
                        checks["time_scope"] = False
                        violations.append("evidence ordering date exceeds the cutoff")
                        continue
                    if source_requirement.time_range is not None and (
                        selected_date > source_requirement.time_range.end
                        or (
                            source_requirement.time_range.start is not None
                            and selected_date < source_requirement.time_range.start
                        )
                    ):
                        continue
                    dates_by_product.setdefault(product_key, []).append(selected_date)
            if not dates_by_product:
                checks["evidence_date_source"] = False
                violations.append("evidence ordering lacks its requested date field")
            representative_dates = {
                key: (
                    max(values)
                    if evidence_selection.direction is SortDirection.DESC
                    else min(values)
                )
                for key, values in dates_by_product.items()
            }
            expected_product_order = sorted(
                representative_dates,
                key=lambda item: (item[0].value, item[1]),
            )
            expected_product_order.sort(
                key=lambda item: representative_dates[item],
                reverse=evidence_selection.direction is SortDirection.DESC,
            )
            expected_product_order = expected_product_order[: evidence_selection.limit]
            actual_product_order = list(
                dict.fromkeys(
                    (item.product_family, item.product_id)
                    for item in bundle.product_facts
                )
            )
            selected_product_ids = set(actual_product_order)
            if actual_product_order != expected_product_order:
                checks["evidence_date_order"] = False
                violations.append(
                    "official product facts are not in verified evidence-date order"
                )
            if any(key not in dates_by_product for key in actual_product_order):
                checks["evidence_date_source"] = False
                violations.append("selected product lacks the requested evidence date")

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
    "relation_satisfies_document_binding",
]
