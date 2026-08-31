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
from finance_agent_core.agent.numeric_language import requested_result_limit
from finance_agent_core.agent.presentation import semantic_query_surface
from finance_agent_core.agent.product_comparison import (
    ProductComparisonParseError,
    ProductIdentityNotFoundError,
    compile_product_comparison_plan,
    resolve_exact_product_ids,
)
from finance_agent_core.agent.product_identity import (
    is_named_product_mention,
    is_quoted_product_mention,
)
from finance_agent_core.agent.semantic_aliases import (
    explicit_product_name_order,
    ranking_intent_present,
)
from finance_agent_core.agent.semantic_resolution import (
    HardFilterLock,
    ResolutionOperation,
    SemanticResolutionReceipt,
)
from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    Intent,
    NullPlacement,
    ProductFamily,
    SortDirection,
    search_projection,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord


class PlanCompilationBlockedError(ValueError):
    """Raised when a routed draft cannot be compiled without guessing."""


def _normalize_identity_ambiguity_surface(value: object) -> str:
    return " ".join(str(value).strip().strip("\"'“”‘’").casefold().split())


def _mask_bound_product_mentions(question: str, mentions: list[str]) -> str:
    """Hide DB-resolved identities from field linking while preserving the request.

    Product names may legitimately contain schema-looking words such as
    ``혼합자산`` or ``재간접``. Once the exact identity has been bound against
    the approved DB, those words are part of the name rather than additional
    filters. The server still keeps the original question for audit and safety.
    """

    masked = question
    for mention in sorted(mentions, key=len, reverse=True):
        masked = re.sub(re.escape(mention), " ", masked, flags=re.IGNORECASE)
    return masked


def _ranked_identity_requests_single_winner(question: str) -> bool:
    """Return whether an explicit candidate set asks for only its winner."""

    return (
        re.search(
            r"(?:1\s*위|가장|제일|어느\s*(?:것|쪽)|하나|"
            r"더\s*(?:높|낮|크|큰|작|많|적)|"
            r"(?:높은|낮은|큰|작은|긴|짧은|먼저\s*오는)\s*(?:쪽|상품|종목)?)",
            question,
            re.IGNORECASE,
        )
        is not None
    )


def _provided_fund_group_alias(
    value: str,
    records: tuple[ProductIdentityRecord, ...],
) -> str | None:
    """Resolve a named fund group only when the approved DB corroborates it."""

    stripped = re.sub(r"펀드$", "", value.strip(), flags=re.IGNORECASE).strip()
    normalized = "".join(character for character in stripped.casefold() if character.isalnum())
    if len(normalized) < 4:
        return None
    matches = [
        record
        for record in records
        if not record.is_quarantined
        and any(
            normalized
            in "".join(character for character in surface.casefold() if character.isalnum())
            for surface in (record.product_name, record.short_name or "")
        )
    ]
    if not 1 <= len(matches) <= 20:
        return None
    return stripped


@dataclass(frozen=True)
class CompiledFamilySearch:
    """One server-owned plan and its isolated answer-generation question."""

    grounded_question: str
    plan: QueryPlan


@dataclass(frozen=True)
class CrossFamilyGlobalRanking:
    """One server-verified metric shared by every family in a global TOP-K."""

    field: str
    direction: SortDirection
    nulls: NullPlacement
    limit: int


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
        scoped_questions = _cross_family_scoped_questions(
            decision.draft.question,
            decision.draft.product_families,
        )
        searches: list[CompiledFamilySearch] = []
        for family in decision.draft.product_families:
            scoped_question = scoped_questions[family]
            # Import locally so the deterministic Router remains independent
            # from the compiler module during package initialization.  Exact
            # identifiers and per-clause limits must be rebound to the isolated
            # family question; retaining the full-request values would allow one
            # clause to leak identities or limits into another family plan.
            from finance_agent_core.agent.router import extract_product_routing_hints

            _, scoped_mentions = extract_product_routing_hints(scoped_question)
            scoped_limit = requested_result_limit(scoped_question)
            if scoped_limit is None and _PER_FAMILY.search(decision.draft.question) is not None:
                scoped_limit = decision.draft.requested_limit
            single_draft = decision.draft.model_copy(
                update={
                    "question": scoped_question,
                    "product_families": [family],
                    "product_mentions": list(scoped_mentions),
                    "requested_limit": scoped_limit,
                }
            )
            single_decision = decision.model_copy(update={"draft": single_draft})
            searches.append(
                CompiledFamilySearch(
                    grounded_question=scoped_question,
                    plan=self._compile_search_lowering(single_decision),
                )
            )
        if (
            _ordinal_family_questions(
                decision.draft.question,
                decision.draft.product_families,
            )
            is None
            and _explicit_family_clause_questions(
                decision.draft.question,
                decision.draft.product_families,
            )
            is None
            and len(
                {
                    (ranking.field, ranking.direction)
                    for search in searches
                    for ranking in search.plan.ranking
                }
            )
            > 1
        ):
            raise PlanCompilationBlockedError(
                "복수 정렬 기준이 어느 상품군에 속하는지 확정할 수 없습니다. "
                "각 상품군에 기준을 명시해 주세요."
            )
        resolve_cross_family_global_ranking(decision.draft.question, searches)
        return searches

    def _compile_search_lowering(self, decision: RouteDecision) -> QueryPlan:
        family = decision.draft.product_families[0]
        exact_identity_lookup = (
            decision.draft.intent is InteractionIntent.SEARCH
            and len(decision.draft.product_mentions) == 1
            and decision.draft.product_mentions[0].casefold() in decision.draft.question.casefold()
        )
        ranked_identity_set = (
            decision.draft.intent is InteractionIntent.SEARCH
            and 2 <= len(decision.draft.product_mentions) <= 20
            and ranking_intent_present(decision.draft.question)
        )
        if (
            decision.draft.product_mentions
            and not exact_identity_lookup
            and not ranked_identity_set
            and decision.draft.intent
            not in {
                InteractionIntent.DETAIL,
                InteractionIntent.EXPLAIN,
            }
        ):
            raise PlanCompilationBlockedError(
                "search contains product identities that were not bound to an exact lookup"
            )
        identity_bound_intent = decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }
        linking_question = (
            _mask_bound_product_mentions(
                decision.draft.question,
                decision.draft.product_mentions,
            )
            if exact_identity_lookup or ranked_identity_set or identity_bound_intent
            else decision.draft.question
        )
        semantic_question = semantic_query_surface(linking_question)
        try:
            payload = canonicalize_query_plan_payload(
                semantic_question,
                {
                    "question_id": decision.draft.request_id,
                    "product_families": [family.value],
                },
                force_product_family_hint=True,
            )
        except ValueError as error:
            # Field hints are inferred from natural-language surfaces before
            # the family-specific registry check.  A heterogeneous request can
            # therefore contain a valid hint for one family that is not a
            # supported field of another family (for example ``채권`` beside
            # ``공모펀드``).  This is a planning ambiguity, never an HTTP 5xx.
            raise PlanCompilationBlockedError(str(error)) from error
        # ``두 상품 중 어느 쪽`` names the comparison candidate set; it is
        # not a request to return two results.  A single-winner comparative
        # therefore outranks the generic Korean counter parser.
        if ranked_identity_set and _ranked_identity_requests_single_winner(decision.draft.question):
            payload["limit"] = 1
        elif decision.draft.requested_limit is not None:
            payload["limit"] = decision.draft.requested_limit
        if exact_identity_lookup or ranked_identity_set:
            identity_name_fallback: str | None = None
            try:
                database_path = self.database_paths[family]
                product_ids = resolve_exact_product_ids(
                    family,
                    decision.draft.product_mentions,
                    self.identity_cache.get(database_path).records,
                )
            except ProductIdentityNotFoundError as error:
                identity_surface = decision.draft.product_mentions[0]
                if not exact_identity_lookup or not (
                    is_named_product_mention(identity_surface)
                    or is_quoted_product_mention(decision.draft.question, identity_surface)
                ):
                    raise PlanCompilationBlockedError(str(error)) from error
                identity_name_fallback = identity_surface
                product_ids = []
            except (KeyError, ProductComparisonParseError, ValueError) as error:
                raise PlanCompilationBlockedError(str(error)) from error
            payload["constraints"] = [
                constraint
                for constraint in payload["constraints"]
                if constraint["field"]
                not in {"product_id", "product_name", "short_name", "ticker", "isin"}
            ]
            if identity_name_fallback is None:
                payload["constraints"].append(
                    {
                        "field": "product_id",
                        "operator": "eq" if exact_identity_lookup else "in",
                        "value": product_ids[0] if exact_identity_lookup else product_ids,
                        "unit": "code",
                        "strength": "locked",
                    }
                )
            else:
                payload["constraints"].append(
                    {
                        "field": "product_name",
                        "operator": "eq",
                        "value": identity_name_fallback,
                        "unit": "none",
                        "strength": "locked",
                    }
                )
            if exact_identity_lookup:
                payload["limit"] = 1
                payload["ranking"] = []
            normalized_mentions = {
                _normalize_identity_ambiguity_surface(mention)
                for mention in decision.draft.product_mentions
            }
            payload["ambiguities"] = [
                ambiguity
                for ambiguity in payload["ambiguities"]
                if _normalize_identity_ambiguity_surface(
                    ambiguity.get("span", "") if isinstance(ambiguity, dict) else ambiguity
                )
                not in normalized_mentions
            ]
        if decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            if len(decision.draft.product_mentions) != 1:
                raise PlanCompilationBlockedError(
                    "detail or explain requires one server-linked exact product identity"
                )
            identity_name_fallback: str | None = None
            identity_surface = decision.draft.product_mentions[0]
            identity_surface_is_name = is_named_product_mention(
                identity_surface
            ) or is_quoted_product_mention(decision.draft.question, identity_surface)
            try:
                database_path = self.database_paths[family]
                identity_records = self.identity_cache.get(database_path).records
                product_id = resolve_exact_product_ids(
                    family,
                    decision.draft.product_mentions,
                    identity_records,
                )[0]
            except ProductIdentityNotFoundError as error:
                mention = identity_surface
                if not identity_surface_is_name:
                    raise PlanCompilationBlockedError(str(error)) from error
                identity_name_fallback = mention
                product_id = None
            except (KeyError, ProductComparisonParseError, ValueError) as error:
                raise PlanCompilationBlockedError(str(error)) from error
            payload["constraints"] = [
                constraint
                for constraint in payload["constraints"]
                if constraint["field"]
                not in {"product_id", "product_name", "short_name", "ticker", "isin"}
            ]
            if product_id is not None:
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
            else:
                assert identity_name_fallback is not None
                fund_group_alias = (
                    _provided_fund_group_alias(identity_surface, identity_records)
                    if family is ProductFamily.FUND
                    else None
                )
                payload["constraints"].append(
                    {
                        "field": "product_name",
                        "operator": "contains" if fund_group_alias is not None else "eq",
                        "value": fund_group_alias or identity_name_fallback,
                        "unit": "none",
                        "strength": "locked",
                    }
                )
                # Only a group surface corroborated by the approved DB may
                # denote several share classes. Other names remain exact.
                payload["limit"] = 20 if fund_group_alias is not None else 1
            if identity_surface_is_name:
                normalized_identity = _normalize_identity_ambiguity_surface(identity_surface)
                payload["ambiguities"] = [
                    ambiguity
                    for ambiguity in payload["ambiguities"]
                    if _normalize_identity_ambiguity_surface(
                        ambiguity.get("span", "") if isinstance(ambiguity, dict) else ambiguity
                    )
                    != normalized_identity
                ]
            payload["ranking"] = []
        plan = QueryPlan.model_validate(payload)
        if plan.product_families != [family]:
            raise PlanCompilationBlockedError("compiler changed the routed product family")
        if (
            decision.draft.intent is InteractionIntent.SEARCH
            and ranking_intent_present(decision.draft.question)
            and not exact_identity_lookup
        ):
            if not plan.ranking:
                raise PlanCompilationBlockedError(
                    "ranking 표현이 있지만 정렬 기준 field를 하나로 확정하지 못했습니다."
                )
            if all(
                item.field in {"product_name", "short_name", "ticker"} for item in plan.ranking
            ) and not explicit_product_name_order(decision.draft.question):
                raise PlanCompilationBlockedError(
                    "ranking 표현을 상품명 정렬로 대체할 수 없습니다."
                )
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
        semantic_question = semantic_query_surface(decision.draft.question)
        payload = canonicalize_query_plan_payload(
            semantic_question,
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
        comparison_question = _strip_presentation_sentences(decision.draft.question)
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
                comparison_question,
                decision.draft.request_id,
            )
            if plan.intent is not Intent.COMPARE:
                raise PlanCompilationBlockedError("fund comparison compiler changed the intent")
            return plan
        try:
            return compile_product_comparison_plan(
                question=comparison_question,
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
    r"국내\s*채권|국내채권|회사채|일반사채|국채|국공채|국고채|특수채|"
    r"특수은행채|금융채|은행채|신용카드채|시설대여채|MBS|"
    r"지역개발채|도시철도공채|채권\s*상품|채권(?!형)",
    re.IGNORECASE,
)
_DOMESTIC_BRANDED_FAMILY_PATTERN = (
    r"\b(?:TIGER|KODEX|ACE|RISE|SOL|HANARO|TIMEFOLIO|ARIRANG|KBSTAR)\b"
    r"(?:\s+[0-9A-Za-z가-힣&·+._/-]+){1,10}?"
    r"(?=\s*(?:,|및|와|과|이랑|하고|의|을|를|은|는|이|가|인|$))"
)
_SIMPLE_FAMILY_MENTIONS: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.DOMESTIC_ETP: re.compile(
        r"(?:국내|한국)\s*(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])|" + _DOMESTIC_BRANDED_FAMILY_PATTERN,
        re.IGNORECASE,
    ),
    ProductFamily.OVERSEAS_ETP: re.compile(
        r"(?:해외|글로벌|미국)\s*(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])",
        re.IGNORECASE,
    ),
    ProductFamily.BOND: _BOND_MENTION,
    ProductFamily.FUND: _FUND_MENTION,
}
_DOMESTIC_BRAND_MENTION = re.compile(
    r"\b(?:TIGER|KODEX|ACE|RISE|SOL|HANARO|TIMEFOLIO|ARIRANG|KBSTAR)\b",
    re.IGNORECASE,
)
_FAMILY_CONNECTOR = re.compile(r"^[\s,·/&]*(?:(?:및|와|과|또는|이랑|하고)[\s,·/&]*)*$")
_FAMILY_TOPIC_PARTICLE = re.compile(r"^\s*(?:은|는)\s*")
_FAMILY_SUFFIX_CONNECTOR = re.compile(r"^\s*(?:(?:및|와|과|이랑|하고)\s+|,\s*)")
_GLOBAL_TOP_K = re.compile(
    r"(?:TOP\s*\d+|상위\s*\d+|하위\s*\d+|통틀어|합쳐서|전체\s*순위)",
    re.IGNORECASE,
)
_GLOBAL_RANKING_SCOPE = re.compile(
    r"통합\s*(?:비교|정렬|순위)|한\s*줄\s*순위|(?:한\s*순위에\s*)?합쳐|"
    r"통합\s*(?:높은|낮은)\s*순|함께\s*(?:비교|정렬|순위)|"
    r"한\s*(?:목록|리스트)(?:으로|에)?|따로\s*나누지\s*말고|"
    r"직접\s*비교|통틀어|전체\s*순위",
    re.IGNORECASE,
)
_PER_FAMILY = re.compile(
    r"각각|상품군\s*(?:마다|별로)\s*(?:따로|별도|독립|나눠)|"
    r"종류\s*별로\s*(?:따로|별도|독립|나눠)|"
    r"(?:서로\s*다른\s*지표|조건과\s*정렬).{0,30}?(?:상품군마다|각각|별도)",
    re.IGNORECASE,
)
_ORDINAL_CLAUSE = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?:첫째|둘째|셋째|넷째)(?:\s*[,.:：])?\s*",
    re.IGNORECASE,
)
_GENERIC_ETP = re.compile(r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])", re.IGNORECASE)
_BOND_SCHEMA_CUE = re.compile(
    r"경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)|이자부단가|"
    r"이자\s*제외\s*평가(?:단가|가격)|평가일단가|민평(?:수익률|금리)|"
    r"표면(?:이자율|이율|금리)|쿠폰(?:금리)?|듀레이션|금리\s*민감도|"
    r"잔존(?:일수|기간)|상환까지\s*남은",
    re.IGNORECASE,
)
_DOMESTIC_ETP_IDENTITY_CUE = re.compile(
    r"(?<![A-Z0-9])KR\d{10}(?![A-Z0-9])|상장\s*지수\s*펀드",
    re.IGNORECASE,
)
_OVERSEAS_ETP_IDENTITY_CUE = re.compile(
    r"(?:RIC\s*)?(?<![A-Z0-9])[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5}(?![A-Z0-9])",
    re.IGNORECASE,
)
_PRESENTATION_SENTENCE = re.compile(
    r"^(?:각\s*(?:조건|목록|상품군)|결과(?:에는|는|를|\s*뒤에)|"
    r"조건과\s*정렬|"
    r"조건에\s*(?:맞는|부합하는)\s*전체|조회된\s*전체|상위\s*목록과|"
    r"순위\s*결과와|순위값의\s*방향|상품군별\s*후보|결측값|0을\s*순위값|"
    r"단위가\s*다른|서로\s*다른|국내채권\s*,|요청한\s*개수|동률이|"
    r"필터에\s*사용한|어떤\s*정렬|상품명만\s*쓰지|"
    r"상품군\s*이름을|조건을\s*통과한)",
    re.IGNORECASE,
)


def _strip_presentation_sentences(question: str) -> str:
    """Remove response-format instructions without changing query semantics."""

    return semantic_query_surface(question)


def _clause_has_family_grounding(family: ProductFamily, clause: str) -> bool:
    if family is ProductFamily.BOND:
        return (
            _BOND_MENTION.search(clause) is not None or _BOND_SCHEMA_CUE.search(clause) is not None
        )
    if family is ProductFamily.DOMESTIC_ETP:
        return (
            _GENERIC_ETP.search(clause) is not None
            or _DOMESTIC_ETP_IDENTITY_CUE.search(clause) is not None
        )
    if family is ProductFamily.OVERSEAS_ETP:
        return (
            _GENERIC_ETP.search(clause) is not None
            or _OVERSEAS_ETP_IDENTITY_CUE.search(clause) is not None
        )
    return _FUND_MENTION.search(clause) is not None


def _shared_currency_constraint(searches: list[CompiledFamilySearch]) -> bool:
    values: list[str] = []
    for search in searches:
        matches = [
            constraint
            for constraint in search.plan.constraints
            if constraint.field == "trading_currency"
            and constraint.operator is ConstraintOperator.EQ
            and isinstance(constraint.value, str)
        ]
        if len(matches) != 1:
            return False
        values.append(str(matches[0].value))
    return len(set(values)) == 1


def resolve_cross_family_global_ranking(
    question: str,
    searches: list[CompiledFamilySearch],
) -> CrossFamilyGlobalRanking | None:
    """Authorize global TOP-K only for one identical, unit-compatible numeric field."""

    if len(_ORDINAL_CLAUSE.findall(question)) >= 2:
        return None
    global_scope = _GLOBAL_RANKING_SCOPE.search(question) is not None
    if _PER_FAMILY.search(question) is not None or (
        not global_scope and _GLOBAL_TOP_K.search(question) is None
    ):
        return None
    if len(searches) < 2:
        return None
    rankings = [search.plan.ranking for search in searches]
    if any(len(items) != 1 for items in rankings):
        raise PlanCompilationBlockedError(
            "교차 상품군 전체 순위에는 모든 상품군에서 같은 수치 정렬 기준이 필요합니다."
        )
    first = rankings[0][0]
    if any(items[0] != first for items in rankings[1:]):
        raise PlanCompilationBlockedError(
            "상품군마다 정렬 필드나 방향이 달라 전체 순위를 계산할 수 없습니다."
        )
    limits = {search.plan.limit for search in searches}
    if len(limits) != 1:
        raise PlanCompilationBlockedError("교차 상품군 전체 순위의 결과 개수가 일치하지 않습니다.")

    registry = load_field_registry()
    definitions = [
        registry.require_field(first.field, [search.plan.product_families[0].value])
        for search in searches
    ]
    if any(definition.value_type is not ValueType.NUMBER for definition in definitions):
        raise PlanCompilationBlockedError("교차 상품군 전체 순위는 검증된 수치 필드만 지원합니다.")
    if len({definition.unit for definition in definitions}) != 1:
        raise PlanCompilationBlockedError("상품군 간 단위가 달라 전체 순위를 계산할 수 없습니다.")
    unit = definitions[0].unit
    if unit in {"source_currency_amount", "source_quantity"} and not (
        unit == "source_currency_amount" and _shared_currency_constraint(searches)
    ):
        raise PlanCompilationBlockedError(
            "통화·수량 단위가 다른 상품군의 전체 순위에는 동일 거래통화 조건이 필요합니다."
        )
    return CrossFamilyGlobalRanking(
        field=first.field,
        direction=first.direction,
        nulls=first.nulls,
        limit=next(iter(limits)),
    )


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


def _ordinal_family_questions(
    question: str,
    families: list[ProductFamily],
) -> dict[ProductFamily, str] | None:
    """Split an explicit first/second/... request into isolated family clauses.

    This grammar deliberately requires the number and order of clauses to match
    the server-routed family order.  It supports independent family searches
    without letting a model decide which filter belongs to which family.
    """

    markers = list(_ORDINAL_CLAUSE.finditer(question))
    if not markers:
        return None
    if len(markers) != len(families):
        raise PlanCompilationBlockedError(
            "순서형 교차 조회의 조건 절 수와 상품군 수가 일치하지 않습니다."
        )
    clauses = []
    for index, marker in enumerate(markers):
        clause_end = markers[index + 1].start() if index + 1 < len(markers) else len(question)
        raw_clause = question[marker.end() : clause_end].strip(" \t\r\n,.;:：")
        clauses.append(_strip_presentation_sentences(raw_clause))
    scoped: dict[ProductFamily, str] = {}
    for family, clause in zip(families, clauses, strict=True):
        if not clause:
            raise PlanCompilationBlockedError("순서형 교차 조회에 비어 있는 조건 절이 있습니다.")
        explicitly_named = {
            candidate
            for candidate, pattern in _SIMPLE_FAMILY_MENTIONS.items()
            if pattern.search(clause) is not None
        }
        if explicitly_named and family not in explicitly_named:
            raise PlanCompilationBlockedError(
                "순서형 교차 조회의 상품군 순서와 조건 절이 일치하지 않습니다."
            )
        if not explicitly_named and not _clause_has_family_grounding(family, clause):
            raise PlanCompilationBlockedError(
                "순서형 교차 조회의 각 조건 절에 상품군 또는 전용 식별자·필드를 명시해 주세요."
            )
        scoped[family] = " ".join(clause.split())
    return scoped


def _explicit_family_clause_questions(
    question: str,
    families: list[ProductFamily],
) -> dict[ProductFamily, str] | None:
    """Split only grammar-bound family clauses without model inference.

    Two deterministic forms are accepted:

    * ``국내 ETF는 ... , 해외 ETF는 ...`` (family topic clauses)
    * ``... 해외 ETF와 ... 국내채권을 각각 ...`` (a connector directly
      attached to the preceding family label)

    Every resulting clause must contain exactly one routed family. Ambiguous
    prose continues through the older shared-condition validator and fails
    closed there.
    """

    matches: list[tuple[int, int, ProductFamily]] = []
    for family in families:
        found = list(_SIMPLE_FAMILY_MENTIONS[family].finditer(question))
        if len(found) != 1:
            return None
        match = found[0]
        matches.append((match.start(), match.end(), family))
    matches.sort(key=lambda item: item[0])
    if [family for _, _, family in matches] != families:
        return None

    def validate(raw_clauses: list[tuple[ProductFamily, str]]) -> dict[ProductFamily, str] | None:
        if len(raw_clauses) != len(families):
            return None
        scoped: dict[ProductFamily, str] = {}
        for family, raw_clause in raw_clauses:
            clause = _strip_presentation_sentences(raw_clause.strip(" \t\r\n,.;:：·/&"))
            named = {
                candidate
                for candidate, pattern in _SIMPLE_FAMILY_MENTIONS.items()
                if pattern.search(clause) is not None
            }
            if named != {family} or not clause:
                return None
            scoped[family] = " ".join(clause.split())
        return scoped

    # Topic form: no condition may float before the first family label.
    if not question[: matches[0][0]].strip(" \t\r\n,.;:：") and all(
        _FAMILY_TOPIC_PARTICLE.match(question[end:]) is not None for _, end, _ in matches
    ):

        def topic_clause_end(index: int) -> int:
            return matches[index + 1][0] if index + 1 < len(matches) else len(question)

        topic_clauses = [
            (
                family,
                question[start : topic_clause_end(index)],
            )
            for index, (start, _, family) in enumerate(matches)
        ]
        validated = validate(topic_clauses)
        if validated is not None:
            return validated

    # Suffix form: a connector must start immediately after every family label
    # except the last. This makes ownership of the preceding condition explicit.
    boundaries: list[tuple[int, int]] = []
    for _, end, _ in matches[:-1]:
        connector = _FAMILY_SUFFIX_CONNECTOR.match(question[end:])
        if connector is None:
            boundaries = []
            break
        boundaries.append((end, end + connector.end()))
    has_owned_prefixes = (
        len(boundaries) == len(matches) - 1
        and bool(question[: matches[0][0]].strip())
        and all(
            question[connector_end:next_start].strip()
            or (
                next_family is ProductFamily.DOMESTIC_ETP
                and _DOMESTIC_BRAND_MENTION.match(question[next_start:]) is not None
            )
            for (_, connector_end), (next_start, _, next_family) in zip(
                boundaries,
                matches[1:],
                strict=True,
            )
        )
    )
    if boundaries and has_owned_prefixes:
        clause_starts = [0, *(connector_end for _, connector_end in boundaries)]
        clause_ends = [*(family_end for family_end, _ in boundaries), len(question)]
        suffix_clauses = [
            (family, question[start:end])
            for family, start, end in zip(families, clause_starts, clause_ends, strict=True)
        ]
        return validate(suffix_clauses)
    return None


def _cross_family_scoped_questions(
    question: str,
    families: list[ProductFamily],
) -> dict[ProductFamily, str]:
    ordinal = _ordinal_family_questions(question, families)
    if ordinal is not None:
        return ordinal
    explicit = _explicit_family_clause_questions(question, families)
    if explicit is not None:
        return explicit
    semantic_question = _strip_presentation_sentences(question)
    _validate_cross_family_scope(semantic_question, families)
    grounded = _scope_grounded_answer_questions(semantic_question, families)
    return {family: _scope_cross_family_question(grounded[family], family) for family in families}


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
    shared_prefix = question[: min(match.start() for match in resolved.values())]
    shared_suffix = question[max(match.end() for match in resolved.values()) :]
    shared_suffix = re.sub(r"\b각각\b", "", shared_suffix)
    return {
        family: " ".join(
            f"{shared_prefix}{question[match.start() : match.end()]}{shared_suffix}".split()
        )
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
