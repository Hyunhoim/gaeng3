from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from finance_agent_core.agent.aggregate_parser import (
    AggregatePlanParseError,
    compile_aggregate_plan,
)
from finance_agent_core.agent.fund_comparison_parser import (
    ResolvedFundComparisonPlanProvider,
    RuleFundComparisonDraftProvider,
)
from finance_agent_core.agent.fund_resolver import FundProductResolver
from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.product_comparison import (
    ProductComparisonParseError,
    compile_product_comparison_plan,
    resolve_exact_product_ids,
)
from finance_agent_core.agent.semantic_resolution import (
    HardFilterLock,
    ResolutionOperation,
    SemanticResolutionReceipt,
)
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import Intent, ProductFamily, search_projection
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
)


class PlanCompilationBlockedError(ValueError):
    """Raised when a routed draft cannot be compiled without guessing."""


@dataclass(frozen=True)
class CompiledFamilySearch:
    """One server-owned plan and its isolated answer-generation question."""

    grounded_question: str
    plan: QueryPlan


class ServerQueryPlanCompiler:
    """Compile a minimal route draft into the server-owned QueryPlan contract."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
    ) -> None:
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.record_cache = record_cache or RecordSnapshotCache()
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache()

    def compile(self, decision: RouteDecision) -> QueryPlan:
        if decision.disposition is not RouteDisposition.EXECUTE:
            raise PlanCompilationBlockedError("control routes cannot be compiled")
        if len(decision.draft.product_families) != 1:
            raise PlanCompilationBlockedError(
                "multi-family SEARCH routes require compile_search_plans()"
            )
        if decision.query_plan_intent is Intent.SEARCH:
            return self._compile_search_lowering(decision)
        if decision.query_plan_intent is Intent.COMPARE:
            return self._compile_comparison(decision)
        if decision.query_plan_intent is Intent.AGGREGATE:
            return self._compile_aggregate(decision)
        raise PlanCompilationBlockedError(
            f"no server compiler for QueryPlan intent: {decision.query_plan_intent}"
        )

    def compile_search_plans(self, decision: RouteDecision) -> list[QueryPlan]:
        return [item.plan for item in self.compile_family_searches(decision)]

    def compile_family_searches(
        self,
        decision: RouteDecision,
    ) -> list[CompiledFamilySearch]:
        if (
            decision.disposition is not RouteDisposition.EXECUTE
            or decision.query_plan_intent is not Intent.SEARCH
            or decision.draft.intent is not InteractionIntent.SEARCH
            or len(decision.draft.product_families) < 2
        ):
            raise PlanCompilationBlockedError(
                "compile_search_plans() requires an executable multi-family SEARCH route"
            )
        _validate_cross_family_scope(
            decision.draft.question,
            decision.draft.product_families,
        )
        grounded_questions = _scope_grounded_answer_questions(
            decision.draft.question,
            decision.draft.product_families,
        )
        searches: list[CompiledFamilySearch] = []
        for family in decision.draft.product_families:
            scoped_question = _scope_cross_family_question(
                decision.draft.question,
                family,
            )
            single_draft = decision.draft.model_copy(
                update={
                    "question": scoped_question,
                    "product_families": [family],
                }
            )
            single_decision = decision.model_copy(update={"draft": single_draft})
            searches.append(
                CompiledFamilySearch(
                    grounded_question=grounded_questions[family],
                    plan=self._compile_search_lowering(single_decision),
                )
            )
        return searches

    def _compile_search_lowering(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        if decision.draft.product_mentions and decision.draft.intent not in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            raise PlanCompilationBlockedError(
                "search contains product identities that were not bound to an exact lookup"
            )
        payload = canonicalize_query_plan_payload(
            decision.draft.question,
            {
                "question_id": decision.draft.request_id,
                "product_families": [family.value],
            },
            force_product_family_hint=True,
        )
        if decision.draft.requested_limit is not None:
            payload["limit"] = decision.draft.requested_limit
        if decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            if len(decision.draft.product_mentions) != 1:
                raise PlanCompilationBlockedError(
                    "detail or explain requires one server-linked exact product identity"
                )
            try:
                database_path = self.database_paths[family]
                product_id = resolve_exact_product_ids(
                    family,
                    decision.draft.product_mentions,
                    self.identity_cache.get(database_path).records,
                )[0]
            except (KeyError, ProductComparisonParseError, ValueError) as error:
                raise PlanCompilationBlockedError(str(error)) from error
            payload["constraints"] = [
                constraint
                for constraint in payload["constraints"]
                if constraint["field"] not in {"product_id", "ticker", "isin"}
            ]
            payload["constraints"].append(
                {
                    "field": "product_id",
                    "operator": "eq",
                    "value": product_id,
                    "unit": "code",
                    "strength": "locked",
                }
            )
            payload["limit"] = 1
            payload["ranking"] = []
        plan = QueryPlan.model_validate(payload)
        if plan.product_families != [family]:
            raise PlanCompilationBlockedError("compiler changed the routed product family")
        return plan

    def compile_with_semantic_resolution(
        self,
        decision: RouteDecision,
        *,
        hard_filter_lock: HardFilterLock,
        receipts: tuple[SemanticResolutionReceipt, ...],
    ) -> QueryPlan:
        """Compile only server-admitted semantic receipts on top of locked lexical facts."""

        if type(hard_filter_lock) is not HardFilterLock or any(
            type(receipt) is not SemanticResolutionReceipt for receipt in receipts
        ):
            raise PlanCompilationBlockedError(
                "semantic compiler requires exact server authority contracts"
            )
        try:
            hard_filter_lock = HardFilterLock.model_validate_json(
                hard_filter_lock.model_dump_json()
            )
            receipts = tuple(
                SemanticResolutionReceipt.model_validate_json(receipt.model_dump_json())
                for receipt in receipts
            )
        except ValueError as error:
            raise PlanCompilationBlockedError(
                "semantic authority contract failed revalidation"
            ) from error
        if (
            decision.disposition is not RouteDisposition.EXECUTE
            or decision.query_plan_intent is not Intent.SEARCH
            or len(decision.draft.product_families) != 1
            or not receipts
        ):
            raise PlanCompilationBlockedError(
                "semantic resolution compiler requires one executable SEARCH family"
            )
        family = decision.draft.product_families[0]
        if any(
            receipt.product_family is not family
            or receipt.hard_filter_lock_sha256 != hard_filter_lock.payload_sha256
            for receipt in receipts
        ):
            raise PlanCompilationBlockedError("semantic receipt differs from its hard-filter lock")
        payload = canonicalize_query_plan_payload(
            decision.draft.question,
            {
                "question_id": decision.draft.request_id,
                "product_families": [family.value],
            },
            force_product_family_hint=True,
        )
        admitted_residual_hashes = {receipt.residual_span_sha256 for receipt in receipts}
        payload["ambiguities"] = [
            item
            for item in payload["ambiguities"]
            if hashlib.sha256(item["span"].encode("utf-8")).hexdigest()
            not in admitted_residual_hashes
        ]
        if payload["ambiguities"] or payload["unsupported_conditions"]:
            raise PlanCompilationBlockedError(
                "semantic resolution cannot override another ambiguity or unsupported condition"
            )
        if decision.draft.requested_limit is not None:
            payload["limit"] = decision.draft.requested_limit
        try:
            server_base_plan = QueryPlan.model_validate(payload)
            reconstructed_lock = HardFilterLock.from_plan(
                server_base_plan,
                requested_limit=decision.draft.requested_limit,
                product_mentions=tuple(decision.draft.product_mentions),
            )
        except ValueError as error:
            raise PlanCompilationBlockedError(str(error)) from error
        if reconstructed_lock != hard_filter_lock:
            raise PlanCompilationBlockedError(
                "semantic hard-filter lock differs from the server reconstruction"
            )
        ranking = list(payload["ranking"])
        projection = list(payload["projection"])
        for receipt in receipts:
            if receipt.operation is ResolutionOperation.RANK:
                existing_fields = {item["field"] for item in ranking}
                if existing_fields and receipt.field_id not in existing_fields:
                    raise PlanCompilationBlockedError(
                        "semantic ranking conflicts with an already locked ranking"
                    )
                if receipt.field_id not in existing_fields:
                    ranking.append(
                        {
                            "field": receipt.field_id,
                            "direction": receipt.direction.value,
                            "nulls": "last",
                        }
                    )
                projection = search_projection(family, *projection, receipt.field_id)
            elif receipt.operation is ResolutionOperation.PROJECT:
                projection = search_projection(family, *projection, receipt.field_id)
            else:
                raise PlanCompilationBlockedError(
                    "filter and aggregate semantic operations require explicit server values"
                )
        payload["ranking"] = ranking
        payload["projection"] = projection
        try:
            plan = QueryPlan.model_validate(payload)
            hard_filter_lock.require_preserved(plan)
        except ValueError as error:
            raise PlanCompilationBlockedError(str(error)) from error
        return plan

    def _compile_comparison(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        try:
            database_path = self.database_paths[family]
        except KeyError as error:
            raise PlanCompilationBlockedError(
                f"{family.value} comparison database path is not configured"
            ) from error
        identities = self.identity_cache.get(database_path).records
        if family is ProductFamily.FUND:
            provider = ResolvedFundComparisonPlanProvider(
                RuleFundComparisonDraftProvider(),
                FundProductResolver(identities),
            )
            plan = provider.generate_query_plan(
                decision.draft.question,
                decision.draft.request_id,
            )
            if plan.intent is not Intent.COMPARE:
                raise PlanCompilationBlockedError("fund comparison compiler changed the intent")
            return plan
        try:
            return compile_product_comparison_plan(
                question=decision.draft.question,
                question_id=decision.draft.request_id,
                family=family,
                mentions=decision.draft.product_mentions,
                records=identities,
            )
        except (ProductComparisonParseError, ValueError) as error:
            raise PlanCompilationBlockedError(str(error)) from error

    def _compile_aggregate(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        base_payload = canonicalize_query_plan_payload(
            decision.draft.question,
            {
                "question_id": decision.draft.request_id,
                "product_families": [family.value],
            },
        )
        try:
            return compile_aggregate_plan(
                question=decision.draft.question,
                question_id=decision.draft.request_id,
                family=family,
                base_payload=base_payload,
                requested_limit=decision.draft.requested_limit,
            )
        except (AggregatePlanParseError, ValueError) as error:
            raise PlanCompilationBlockedError(str(error)) from error


_ETP_FAMILY_MENTIONS: tuple[tuple[ProductFamily, re.Pattern[str]], ...] = (
    (
        ProductFamily.DOMESTIC_ETP,
        re.compile(
            r"(?:국내|한국|코스피|코스닥)\s*((?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z]))|"
            r"((?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z]))\s*(?:국내|한국)",
            re.IGNORECASE,
        ),
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"(?:해외|글로벌|미국)\s*((?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z]))|"
            r"((?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z]))\s*해외",
            re.IGNORECASE,
        ),
    ),
)
_FUND_MENTION = re.compile(r"공모\s*펀드|공모펀드", re.IGNORECASE)
_BOND_MENTION = re.compile(
    r"국내\s*채권|국내채권|회사채|국채|국공채|국고채|특수채|금융채|"
    r"지역개발채|도시철도공채|채권\s*상품"
)
_SIMPLE_FAMILY_MENTIONS: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.DOMESTIC_ETP: re.compile(
        r"(?:국내|한국)\s*(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])",
        re.IGNORECASE,
    ),
    ProductFamily.OVERSEAS_ETP: re.compile(
        r"(?:해외|글로벌|미국)\s*(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])",
        re.IGNORECASE,
    ),
    ProductFamily.BOND: _BOND_MENTION,
    ProductFamily.FUND: _FUND_MENTION,
}
_FAMILY_CONNECTOR = re.compile(r"^[\s,·/&]*(?:(?:및|와|과|또는|이랑|하고)[\s,·/&]*)*$")


def _scope_cross_family_question(
    question: str,
    target_family: ProductFamily,
) -> str:
    """Remove other-family labels before deterministic per-family linking."""

    scoped = question
    for family, pattern in _ETP_FAMILY_MENTIONS:
        if family is target_family:
            scoped = pattern.sub(
                lambda match: (match.group(1) or match.group(2)).upper(),
                scoped,
            )
        else:
            scoped = pattern.sub(" ", scoped)
    if target_family is not ProductFamily.FUND:
        scoped = _FUND_MENTION.sub(" ", scoped)
    if target_family is not ProductFamily.BOND:
        scoped = _BOND_MENTION.sub(" ", scoped)
    return " ".join(scoped.split())


def _scope_grounded_answer_questions(
    question: str,
    families: list[ProductFamily],
) -> dict[ProductFamily, str]:
    """Keep only one named family and the shared condition for answer planning."""

    mentions: dict[ProductFamily, re.Match[str]] = {
        family: _SIMPLE_FAMILY_MENTIONS[family].search(question) for family in families
    }
    if any(match is None for match in mentions.values()):
        raise PlanCompilationBlockedError(
            "교차 상품군 grounded answer 질문을 상품군별로 분리할 수 없습니다."
        )
    resolved = {family: match for family, match in mentions.items() if match is not None}
    shared_suffix = question[max(match.end() for match in resolved.values()) :]
    shared_suffix = re.sub(r"\b각각\b", "", shared_suffix)
    return {
        family: " ".join(f"{question[match.start() : match.end()]}{shared_suffix}".split())
        for family, match in resolved.items()
    }


def _validate_cross_family_scope(
    question: str,
    families: list[ProductFamily],
) -> None:
    spans: list[tuple[int, int, ProductFamily]] = []
    for family in families:
        matches = list(_SIMPLE_FAMILY_MENTIONS[family].finditer(question))
        if len(matches) != 1:
            raise PlanCompilationBlockedError(
                "교차 상품군 SEARCH v1은 각 상품군을 한 번씩 단순 명시해야 합니다."
            )
        match = matches[0]
        spans.append((match.start(), match.end(), family))
    spans.sort(key=lambda item: item[0])
    if [family for _, _, family in spans] != families:
        raise PlanCompilationBlockedError("교차 상품군 SEARCH의 상품군 순서를 확정할 수 없습니다.")
    for (_, previous_end, _), (next_start, _, _) in zip(spans, spans[1:], strict=False):
        connector = question[previous_end:next_start]
        if _FAMILY_CONNECTOR.fullmatch(connector) is None:
            raise PlanCompilationBlockedError(
                "상품군별 서로 다른 조건은 아직 지원하지 않습니다. "
                "상품군을 먼저 나열하고 공통 조건을 한 번만 적어주세요."
            )
