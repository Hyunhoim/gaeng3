from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finance_agent_core.agent.field_language import registry_field_mentions
from finance_agent_core.agent.product_identity import (
    DatabaseProductIdentityMatch,
    DatabaseProductIdentityMatcher,
    normalize_database_identity,
    unresolved_identity_like_spans,
)
from finance_agent_core.agent.semantic_aliases import filter_alias_matches, rank_alias_matches
from finance_agent_core.config import (
    AsOfBasis,
    FieldDefinition,
    QualityStatus,
    ValueType,
    load_field_registry,
)
from finance_agent_core.config.registry import SourceTransform
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Aggregation,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    NullPlacement,
    ProductFamily,
    Ranking,
    SortDirection,
    Unit,
    search_projection,
)
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import FieldEvidence, ProductEvidence
from finance_agent_core.storage import RecordSnapshotCache
from finance_agent_core.storage.sqlite import connect_read_only


@dataclass(frozen=True, slots=True)
class CompoundFilter:
    source_field: str
    operator: str
    value: str | int | float | bool | tuple[str | int | float | bool, ...]


@dataclass(frozen=True, slots=True)
class CompoundMetric:
    kind: str
    source_fields: tuple[str, ...]
    scale: Decimal = Decimal("1")


@dataclass(frozen=True, slots=True)
class CompoundAggregation:
    function: AggregateFunction
    source_field: str


@dataclass(frozen=True, slots=True)
class CompoundAggregateThreshold:
    """One server-owned aggregate threshold applied to its own base cohort."""

    metric: CompoundMetric
    function: AggregateFunction
    operator: str


@dataclass(frozen=True, slots=True)
class CompoundAggregateThresholdEvidence:
    """Deterministic proof of the values used to derive one threshold."""

    metric: CompoundMetric
    function: AggregateFunction
    operator: str
    value: Decimal
    valid_count: int
    source_id: str
    as_of_start: date
    as_of_end: date
    input_sha256: str


@dataclass(frozen=True, slots=True)
class CompoundAggregateValue:
    function: AggregateFunction
    source_field: str
    value: Decimal | int | None
    valid_count: int
    missing_count: int


@dataclass(frozen=True, slots=True)
class CompoundGroupResult:
    group_value: str | int | bool | tuple[str | int | bool, ...]
    aggregates: tuple[CompoundAggregateValue, ...]
    source_id: str
    as_of: date
    row_count: int


@dataclass(frozen=True, slots=True)
class CompoundFamilyPlan:
    product_family: ProductFamily
    filters: tuple[CompoundFilter, ...]
    metric: CompoundMetric
    direction: SortDirection
    limit: int
    shortlist_metric: CompoundMetric | None = None
    shortlist_limit: int | None = None
    shortlist_direction: SortDirection = SortDirection.DESC
    exclude_product_ids: tuple[str, ...] = ()
    relative_filters: tuple[tuple[CompoundMetric, str, Decimal], ...] = ()
    projection_fields: tuple[str, ...] = ()
    cohort_label: str | None = None
    include_product_ids: tuple[str, ...] = ()
    identity_scope_active: bool = False
    group_by_source_field: str | None = None
    # ``group_by_source_field`` remains the backwards-compatible single-axis
    # view.  New aggregate plans seal the complete one-or-two-axis tuple here;
    # grouped product TOP-K intentionally remains single-axis.
    group_by_source_fields: tuple[str, ...] = ()
    aggregations: tuple[CompoundAggregation, ...] = ()
    aggregate_threshold: CompoundAggregateThreshold | None = None
    group_min_count: int | None = None
    # ``group_min_count`` is a HAVING condition over the number of usable
    # values for this exact field, not over every row in the group.  Keeping
    # the owner in the signed plan prevents a missing/zero metric from being
    # counted merely because the product row itself exists.
    group_min_count_source_field: str | None = None
    group_limit: int | None = None
    group_order_by_count: bool = False
    # A grouped TOP-K is ordered by one explicitly requested aggregate
    # output (for example AVG(total_expense_ratio_pct)), never by an implicit
    # row count.  ``None`` means deterministic group-label presentation.
    group_order_aggregation: CompoundAggregation | None = None
    # For ``각 그룹 상위 N개`` searches, ``limit`` is the public per-group
    # result bound and this duplicate, signed field proves that the bound was
    # not accidentally interpreted as a global TOP-K.
    per_group_limit: int | None = None
    field_comparisons: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CompoundFamilyExclusion:
    """One explicitly requested family omitted from a global comparison.

    An exclusion is permitted only when the shared canonical metric does not
    exist in that family's approved Field Registry.  Keeping it inside the
    sealed plan makes the omission reproducible and visible in the answer;
    it can never be used to hide a parser failure or a missing value in a
    family that *does* support the metric.
    """

    product_family: ProductFamily
    canonical_field: str
    reason_code: str = "field_unavailable_in_provided_schema"


@dataclass(frozen=True, slots=True)
class TypedCompoundPlan:
    question_id: str
    question: str
    interaction_intent: InteractionIntent
    kind: str
    family_plans: tuple[CompoundFamilyPlan, ...]
    excluded_families: tuple[CompoundFamilyExclusion, ...] = ()
    hclx_structure_spans: tuple[str, ...] = ()
    # Optional deterministic comparison over two already verified cohort
    # means. It never changes either cohort or its representative TOP-K.
    cohort_mean_direction: SortDirection | None = None
    cohort_mean_difference: bool = False

    @property
    def product_families(self) -> tuple[ProductFamily, ...]:
        return tuple(dict.fromkeys(item.product_family for item in self.family_plans))

    @property
    def requested_product_families(self) -> tuple[ProductFamily, ...]:
        """Families named by the user, including schema-ineligible families."""

        included = [item.product_family for item in self.family_plans]
        excluded = [item.product_family for item in self.excluded_families]
        represented = {*included, *excluded}
        explicit = tuple(
            dict.fromkeys(
                item.product_family
                for item in _compound_family_matches(self.question)
                if item.product_family in represented
            )
        )
        # A named-pair request does not need a generic family word such as
        # ``해외 ETF``: each exact identifier has already been resolved
        # uniquely against the approved databases by ``_compile_named_pair``.
        # Treat that DB-bound identity proof as the requested family scope so
        # authorization does not reject otherwise complete comparisons merely
        # because the user wrote two product names/tickers only.
        if self.kind in {"anchor_relative", "cross_family_named_comparison"}:
            return self.product_families
        if not explicit and self.kind == "named_pair":
            return self.product_families
        return explicit


@dataclass(frozen=True, slots=True)
class CompoundFamilyExecution:
    plan: CompoundFamilyPlan
    query_plan: QueryPlan
    candidate_count: int
    products: tuple[ProductEvidence, ...]
    product_records: tuple[object, ...]
    shortlist_ids: tuple[str, ...]
    metric_values: tuple[Decimal | str, ...]
    aggregate_summary: tuple[int, Decimal, Decimal, Decimal] | None = None
    # Canonical value required by a Registry comparison scope.  At present
    # this carries the one verified trading currency shared by every value in
    # a cohort; it is independently compared across cohorts before rendering.
    comparison_scope_value: str | None = None
    grouped_aggregates: tuple[CompoundGroupResult, ...] = ()
    # One value per returned product, aligned with ``products`` and
    # ``metric_values``.  Qualified counts are recorded before per-group
    # slicing so HAVING and TOP-K can be independently reverified.
    grouped_product_values: tuple[str | int | bool, ...] = ()
    grouped_product_valid_counts: tuple[tuple[str | int | bool, int], ...] = ()
    aggregate_threshold_evidence: CompoundAggregateThresholdEvidence | None = None


@dataclass(frozen=True, slots=True)
class CompoundExecution:
    plan: TypedCompoundPlan
    families: tuple[CompoundFamilyExecution, ...]
    answer: str
    # ``families`` is grouped for provenance, whereas a verified global TOP-K
    # is interleaved across those groups.  Preserve that exact public order so
    # adapters never reconstruct it by concatenating family buckets.
    ordered_products: tuple[ProductEvidence, ...] = ()


class NamedComparisonIncompleteError(RuntimeError):
    """A named comparison lost at least one explicitly requested product.

    Exact-identity comparisons are atomic: every named product must have a
    usable value for the requested metric.  Returning the remaining products
    would silently answer a different question, so the public service converts
    this execution stop into CLARIFY without exposing a partial result.
    """

    def __init__(
        self,
        *,
        product_family: ProductFamily,
        unavailable_product_ids: tuple[str, ...],
        metric_source_fields: tuple[str, ...],
    ) -> None:
        self.product_family = product_family
        self.unavailable_product_ids = unavailable_product_ids
        self.metric_source_fields = metric_source_fields
        super().__init__("named comparison requires a usable metric for every requested product")


class AggregateThresholdUnavailableError(RuntimeError):
    """The approved base cohort has no usable non-zero value for its threshold."""

    def __init__(self, *, product_family: ProductFamily, source_field: str) -> None:
        self.product_family = product_family
        self.source_field = source_field
        super().__init__("aggregate threshold requires at least one usable source value")


class CohortComparisonUnavailableError(RuntimeError):
    """A requested cohort winner/difference cannot be computed atomically."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("cohort comparison requires complete, directly comparable values")


@dataclass(frozen=True, slots=True)
class CompoundAuthorizationReceipt:
    """Server-owned proof that one frozen compound plan may execute.

    The receipt is intentionally not a general replacement for
    :class:`PlanAuthorityGate`.  It exists only for compound calculations that
    the normal single-plan QueryPlan contract cannot represent yet.  A caller
    cannot construct useful authority by changing a RouteDecision: the exact
    question, request, plan and previously trusted route reason are bound into
    the digest and rechecked immediately before execution.
    """

    request_id: str
    question_sha256: str
    plan_sha256: str
    prior_reason_code: str
    covered_unresolved_spans: tuple[str, ...]
    canonical_clause_ids: tuple[str, ...]
    validated_field_ids: tuple[str, ...]
    authorized_intent: str
    authorized_families: tuple[str, ...]
    authorized_query_plan_intent: str
    authority_mac: str


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    product_id: str
    product_name: str
    ticker: str | None
    source_id: str
    source_row: int
    dynamic_as_of: date
    source_values: dict[str, str | int | bool | None]
    source_key: dict[str, str] = dataclass_field(default_factory=dict)
    source_snapshot_date: date | None = None
    static_as_of: date | None = None
    remaining_days: int | None = None
    field_quality: dict[str, QualityStatus] = dataclass_field(default_factory=dict)
    field_quality_reasons: dict[str, str | None] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _FamilyScope:
    """One explicitly named product-family clause and its locked base filters."""

    start: int
    end: int
    product_family: ProductFamily
    filters: tuple[CompoundFilter, ...]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class _DatabaseRelativeAnchor:
    """One DB-proven reference product and the attributes tied to it.

    ``lookup_product_id`` retains the physical row identity when the caller
    supplied it (for example a bond venue/snapshot key).  ``public_product_id``
    is the logical ticker/ID used only to corroborate alternate name/ticker
    surfaces.  Relative execution excludes the exact physical reference row;
    it must not erase a distinct venue row that the official DB treats as a
    separate product observation.
    """

    surface: str
    lookup_product_id: str
    public_product_id: str
    product_family: ProductFamily
    identity_spans: tuple[tuple[int, int], ...]
    equal_source_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AnchorRelativeClauseCoverage:
    """Canonical proof that every executable anchor clause was preserved.

    This ledger is intentionally derived from DB-bound identity and typed
    fields, not from a Router reason code.  It is used only for the explicit
    ``reference product -> relative thresholds -> ranked other products``
    grammar.  A missing identity, cohort filter, relative condition, ranking
    field/direction/count, or anchor exclusion prevents the ledger from being
    created and therefore cannot gain execution authority.
    """

    product_family: ProductFamily
    anchor_lookup_product_id: str
    anchor_public_product_id: str
    filters: tuple[CompoundFilter, ...]
    relative_clauses: tuple[tuple[CompoundMetric, str], ...]
    ranking_metric: CompoundMetric
    ranking_direction: SortDirection
    ranking_limit: int
    projection_fields: tuple[str, ...]

    @property
    def clause_ids(self) -> tuple[str, ...]:
        def metric_id(metric: CompoundMetric) -> str:
            return ":".join(
                (metric.kind, *metric.source_fields, format(metric.scale, "f"))
            )

        filter_ids = tuple(
            "filter:"
            + ":".join(
                (
                    item.source_field,
                    item.operator,
                    json.dumps(
                        item.value,
                        default=str,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            for item in self.filters
        )
        relative_ids = tuple(
            f"relative:{metric_id(metric)}:{operator}"
            for metric, operator in self.relative_clauses
        )
        return (
            f"anchor:{self.product_family.value}:{self.anchor_lookup_product_id}",
            *filter_ids,
            *relative_ids,
            f"ranking:{metric_id(self.ranking_metric)}",
            f"direction:{self.ranking_direction.value}",
            f"limit:{self.ranking_limit}",
            *(f"projection:{source}" for source in self.projection_fields),
            f"exclude_anchor:{self.anchor_lookup_product_id}",
        )


@dataclass(frozen=True, slots=True)
class _NamedSetClauseCoverage:
    """Canonical proof for a closed, exact provided-DB comparison set.

    Product names can contain words that resemble a family, metric or result
    count (for example ``채권혼합`` or ``TOP10``).  Once every listed identity
    is matched exactly to the frozen ID set, those name-internal tokens must
    not override the explicit ranking clause.  Conversely, a partial or open
    product list never creates this ledger and therefore remains fail-closed.
    """

    product_family: ProductFamily
    product_ids: tuple[str, ...]
    ranking_metric: CompoundMetric
    ranking_direction: SortDirection
    ranking_limit: int
    projection_fields: tuple[str, ...]

    @property
    def clause_ids(self) -> tuple[str, ...]:
        def metric_id(metric: CompoundMetric) -> str:
            return ":".join(
                (metric.kind, *metric.source_fields, format(metric.scale, "f"))
            )

        return (
            *(
                f"named_product:{self.product_family.value}:{product_id}"
                for product_id in self.product_ids
            ),
            f"ranking:{metric_id(self.ranking_metric)}",
            f"direction:{self.ranking_direction.value}",
            f"limit:{self.ranking_limit}",
            *(f"projection:{source}" for source in self.projection_fields),
            "identity_scope:closed",
        )


@dataclass(frozen=True, slots=True)
class _CanonicalFamilyClauseCoverage:
    """One family-local clause reproduced from the frozen compound plan.

    HCX can conservatively return a complete staged or parallel clause as one
    ``UNRESOLVED`` span even though the deterministic compiler has preserved
    every filter, shortlist and final ranking.  This record is created only
    after independently reproducing that exact family plan from its own text
    segment.  It therefore closes a representation gap without letting one
    family's meaning authorize another family's clause.
    """

    product_family: ProductFamily
    segment_start: int
    segment_end: int
    plan: CompoundFamilyPlan


_TABLE_BY_FAMILY = {
    ProductFamily.BOND: "bond_products",
    ProductFamily.DOMESTIC_ETP: "domestic_etp_products",
    ProductFamily.OVERSEAS_ETP: "overseas_etp_products",
    ProductFamily.FUND: "fund_products",
}

_SOURCE_ID_BY_FAMILY = {
    ProductFamily.BOND: "PRBD01N001",
    ProductFamily.DOMESTIC_ETP: "PREF01N001",
    ProductFamily.OVERSEAS_ETP: "PREF02N001",
    ProductFamily.FUND: "PRFD01N001",
}

_DEFAULT_ORDER_FIELD_BY_FAMILY = {
    ProductFamily.BOND: "pd_no",
    ProductFamily.DOMESTIC_ETP: "pd_itm_no",
    ProductFamily.OVERSEAS_ETP: "pd_itm_no",
    ProductFamily.FUND: "itm_no",
}

_RISK_GROUP_SOURCE_BY_FAMILY = {
    ProductFamily.BOND: "pd_risk_nm",
    ProductFamily.DOMESTIC_ETP: "pd_risk_nm",
    ProductFamily.FUND: "zrin_fd_ivst_risk_gcd",
}


_FAMILY_PATTERNS: tuple[
    tuple[ProductFamily, re.Pattern[str], str | None],
    ...,
] = (
    (
        ProductFamily.BOND,
        # A parenthesized list after the group axis names allowed group
        # values, not three independent bond cohorts.
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권\s*대분류\s*"
            r"\([^)]{1,120}\)\s*(?:별(?:로)?|마다)"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])(?P<value>회사채|특수채|국공채|개인투자용국채|"
            r"외화채권-회사채|외화채권-금융채)(?![0-9A-Za-z가-힣])"
        ),
        "std_pd_mcls_nm",
    ),
    (
        ProductFamily.BOND,
        re.compile(r"채권종류가\s*(?P<value>[^,]+?)인\s*장내\s*국내채권"),
        "bd_knd",
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"장내\s*(?P<value>[0-9A-Za-z가-힣&·._-]{1,40}?채)"
            r"(?:을|를|에서|중에서|중|가운데|대상)?(?=\s|[,.;:]|$)"
        ),
        "bd_knd",
    ),
    (
        ProductFamily.BOND,
        re.compile(r"장내\s*채권"),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(r"장내\s*국내채권"),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(r"(?<![0-9A-Za-z가-힣])국내채권"),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(r"(?<![0-9A-Za-z가-힣])원화\s*채권"),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣&·._-])"
            r"(?P<value>[0-9A-Za-z가-힣&·._-]{1,40}?채)"
            r"(?=\s*(?:에서|에서는|은|는|중|가운데))"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권(?=\s*(?:에서|에서는|은|는|중|쪽))"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권(?=\s*(?:대분류|분류|시장|별))"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권(?=\s*(?:\d+|한|두|세|셋|네|넷|다섯|"
            r"여섯|일곱|여덟|아홉|열)\s*(?:개|종목|상품))"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        # Group-HAVING grammar: ``유효 채권이 100개 이상인 ...``.
        # The subject particle is accepted only immediately before the
        # explicit numeric sample bound, so a product name containing 채권
        # cannot manufacture a family scope.
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권(?=\s*(?:이|가)\s*\d+\s*"
            r"(?:개|건|종목)?\s*이상)"
        ),
        None,
    ),
    (
        ProductFamily.BOND,
        # A bare family noun is safe only when Korean coordination or an
        # object particle proves that it names one member of a compound
        # request.  Do not accept arbitrary occurrences inside product names.
        re.compile(
            r"(?<![0-9A-Za-z가-힣])채권(?=\s*(?:[,;·ㆍ/]|"
            r"(?:및|와|과|을|를)(?:\s|$)))"
        ),
        None,
    ),
    (
        ProductFamily.FUND,
        re.compile(r"제로인\s*대유형이\s*(?P<value>[^,]+?)인\s*공모펀드"),
        "zrin_btyp_nm",
    ),
    (
        ProductFamily.FUND,
        re.compile(r"공모\s*(?:형\s*)?펀드"),
        None,
    ),
    (
        ProductFamily.FUND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])펀드(?=\s*(?:운용\s*속성|"
            r"국내\s*[·ㆍ/,와과및]+\s*해외|해외\s*펀드\s*구분))"
        ),
        None,
    ),
    (
        ProductFamily.FUND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])펀드(?=\s*(?:에서|에서는|은|는|중|쪽))"
        ),
        None,
    ),
    (
        ProductFamily.FUND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])펀드(?=\s*(?:[,;·ㆍ/]|"
            r"(?:및|와|과|을|를|의)(?:\s|$)))"
        ),
        None,
    ),
    (
        ProductFamily.FUND,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])펀드(?=\s*(?:이|가)\s*\d+\s*"
            r"(?:개|건|종목)?\s*이상)"
        ),
        None,
    ),
    (
        ProductFamily.DOMESTIC_ETP,
        re.compile(
            r"(?<![0-9A-Za-z가-힣&·._-])"
            r"(?P<value>[0-9A-Za-z가-힣&·._-]{1,30})\s*운용\s*국내\s*"
            r"(?:ETF|ETN|ETP|상장지수상품)",
            re.IGNORECASE,
        ),
        "cu_fund_mgmt_co",
    ),
    (
        ProductFamily.DOMESTIC_ETP,
        re.compile(
            r"운용사가\s*(?P<value>[^,;]{1,60}?)인\s*국내\s*ETF",
            re.IGNORECASE,
        ),
        "cu_fund_mgmt_co",
    ),
    (
        ProductFamily.DOMESTIC_ETP,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])국내(?=\s*(?:[·ㆍ/]|및|와|과)\s*해외\s*(?:ETF|ETN|ETP))",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        ProductFamily.DOMESTIC_ETP,
        # A row-level asset modifier may sit between the market and the
        # product-family noun (``국내 채권형 ETF``).  The family matcher owns
        # only the market/family shell; the Linker must still bind the
        # modifier to ``asset_type`` so it cannot disappear during execution.
        re.compile(
            r"국내\s+(?P<value>[0-9A-Za-z가-힣&·._/-]{1,30})\s*"
            r"(?:ETF|ETN|ETP|상장지수상품)",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        ProductFamily.DOMESTIC_ETP,
        re.compile(r"국내\s*(?:ETF|ETN|ETP|상장지수상품)", re.IGNORECASE),
        None,
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"(?<![0-9A-Za-z가-힣])해외(?=\s*거래소(?:\s*코드)?\s*(?:별|마다))",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"(?:거래소|나스닥)?\s*(?:코드)?\s*(?P<value>[A-Z0-9]{3})\s*"
            r"해외\s*(?:ETF|ETN|ETP|상장지수상품)",
            re.IGNORECASE,
        ),
        "pd_exg_mkt_cd",
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"투자자산군이\s*(?P<value>[^,;]+?)"
            r"(?:인|로\s*등록된)\s*해외\s*ETF"
        ),
        "wu_inv_ast_type",
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"투자지역이\s*(?P<value>[^,;]+?)"
            r"(?:인|로\s*등록된)\s*해외\s*ETF"
        ),
        "wu_inv_rgn",
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(
            r"해외\s+(?P<value>[0-9A-Za-z가-힣&·._/-]{1,30})\s*"
            r"(?:ETF|ETN|ETP|상장지수상품)",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        ProductFamily.OVERSEAS_ETP,
        re.compile(r"해외\s*(?:ETF|ETN|ETP|상장지수상품)", re.IGNORECASE),
        None,
    ),
)


_ALL_FAMILY_PATTERN = re.compile(
    r"(?:네|4\s*개|전|모든)\s*상품군(?:\s*전체)?|"
    r"전체\s*(?:네|4\s*개)개?\s*상품군"
)


_SOURCE_TO_CANONICAL: dict[tuple[ProductFamily, str], str] = {
    (ProductFamily.BOND, "pd_exg_mkt"): "bond_market",
    (ProductFamily.BOND, "bd_knd"): "bond_type",
    (ProductFamily.BOND, "std_pd_mcls_nm"): "bond_major_class",
    (ProductFamily.BOND, "std_pd_scls_nm"): "bond_subclass",
    (ProductFamily.BOND, "pd_pbcm"): "issuer",
    (ProductFamily.BOND, "pd_no"): "product_id",
    (ProductFamily.BOND, "pd_nm"): "product_name",
    (ProductFamily.BOND, "curr_cd"): "trading_currency",
    (ProductFamily.BOND, "applied_yield"): "applied_yield_pct",
    (ProductFamily.BOND, "buy_yield"): "buy_yield_pct",
    (ProductFamily.BOND, "after_tax_yield"): "after_tax_yield_pct",
    (ProductFamily.BOND, "bd_tisu_a"): "total_issue_amount",
    (ProductFamily.BOND, "cov"): "convexity",
    (ProductFamily.BOND, "crd_grd"): "credit_rating",
    (ProductFamily.BOND, "dirty"): "dirty_price",
    (ProductFamily.BOND, "dur"): "duration_years",
    (ProductFamily.BOND, "eval_price"): "evaluation_price",
    (ProductFamily.BOND, "isu_bal_amt"): "issue_amount",
    (ProductFamily.BOND, "pd_risk_nm"): "risk_level",
    (ProductFamily.BOND, "remaining_days"): "remaining_days",
    (ProductFamily.BOND, "mat_dt"): "maturity_date",
    (ProductFamily.BOND, "srfc_irt"): "coupon_rate_pct",
    (ProductFamily.BOND, "bd_inrt_tcd"): "interest_rate_type",
    (ProductFamily.BOND, "bd_intp_tcd"): "interest_payment_type",
    (ProductFamily.BOND, "__constant_currently_buyable"): "currently_buyable",
    (ProductFamily.DOMESTIC_ETP, "cu_fund_mgmt_co"): "manager",
    (ProductFamily.DOMESTIC_ETP, "pd_exg_mkt_cd"): "exchange_code",
    (ProductFamily.DOMESTIC_ETP, "du_chas_errt"): "tracking_error_pct",
    (ProductFamily.DOMESTIC_ETP, "du_clpr"): "close_price",
    (ProductFamily.DOMESTIC_ETP, "du_diff_rt"): "discrepancy_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_1d"): "one_day_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_1m"): "one_month_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_1y"): "one_year_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_3m"): "three_month_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_6m"): "six_month_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_er_ytd"): "ytd_return_pct",
    (ProductFamily.DOMESTIC_ETP, "du_last_aum"): "aum",
    (ProductFamily.DOMESTIC_ETP, "du_val_1d"): "daily_trading_value",
    (ProductFamily.DOMESTIC_ETP, "du_val_1m"): "one_month_average_trading_value",
    (ProductFamily.DOMESTIC_ETP, "du_vol_1d"): "daily_volume",
    (ProductFamily.DOMESTIC_ETP, "du_vlty_1m"): "twenty_day_volatility_pct",
    (ProductFamily.DOMESTIC_ETP, "du_vlty_3m"): "sixty_day_volatility_pct",
    (
        ProductFamily.DOMESTIC_ETP,
        "du_vlty_1y",
    ): "two_hundred_fifty_two_day_volatility_pct",
    (ProductFamily.DOMESTIC_ETP, "pd_grp_no"): "product_type",
    (ProductFamily.DOMESTIC_ETP, "pd_sale_yn"): "sellable",
    (ProductFamily.DOMESTIC_ETP, "pd_tr_yn"): "trading_suspended",
    (ProductFamily.DOMESTIC_ETP, "cu_lev_fector"): "leverage_factor",
    (ProductFamily.DOMESTIC_ETP, "pd_itm_no"): "product_id",
    (ProductFamily.DOMESTIC_ETP, "pd_nm"): "product_name",
    (ProductFamily.DOMESTIC_ETP, "pd_curr_cd"): "trading_currency",
    (ProductFamily.DOMESTIC_ETP, "pd_net_tamt"): "net_asset_total",
    (ProductFamily.DOMESTIC_ETP, "pd_risk_nm"): "risk_level",
    (ProductFamily.DOMESTIC_ETP, "cu_charge_rt"): "total_expense_ratio_pct",
    (ProductFamily.DOMESTIC_ETP, "pd_dvid_yield"): "distribution_yield_pct",
    (ProductFamily.DOMESTIC_ETP, "wu_inv_ast_type"): "asset_type",
    (ProductFamily.DOMESTIC_ETP, "wu_inv_rgn"): "investment_region",
    (ProductFamily.OVERSEAS_ETP, "du_diff_rt"): "discrepancy_pct",
    (ProductFamily.OVERSEAS_ETP, "cu_fund_mgmt_co"): "manager",
    (ProductFamily.FUND, "fd_mm1_ern_r"): "one_month_return_pct",
    (ProductFamily.FUND, "fd_mm3_ern_r"): "three_month_return_pct",
    (ProductFamily.FUND, "fd_mm6_ern_r"): "six_month_return_pct",
    (ProductFamily.FUND, "fd_nast_suma"): "aum",
    (ProductFamily.FUND, "fd_yr1_ern_r"): "one_year_return_pct",
    (ProductFamily.FUND, "fd_yr3_ern_r"): "three_year_return_pct",
    (ProductFamily.FUND, "han_clas_fee_type"): "fee_type",
    (ProductFamily.FUND, "han_clas_sales_channel"): "sales_channel",
    (ProductFamily.FUND, "itm_no"): "product_id",
    (ProductFamily.FUND, "itm_nm"): "product_name",
    (ProductFamily.FUND, "curr_cd"): "trading_currency",
    (ProductFamily.FUND, "or_co_rwrd_r"): "management_company_fee_pct",
    (ProductFamily.FUND, "ofwk_trus_rwrd_r"): "office_administration_fee_pct",
    (ProductFamily.FUND, "prvo_pbff_desc"): "public_offering",
    (ProductFamily.FUND, "sale_co_rwrd_r"): "sale_company_fee_pct",
    (ProductFamily.FUND, "sale_yn"): "sellable",
    (ProductFamily.FUND, "thco_sale_yn"): "company_sellable",
    (ProductFamily.FUND, "trusc_rwrd_r"): "trustee_fee_pct",
    (ProductFamily.FUND, "zrin_btyp_nm"): "fund_major_type",
    (ProductFamily.FUND, "or_attr_desc"): "fund_management_attribute",
    (ProductFamily.FUND, "ovrs_fd_desc"): "fund_geography_scope",
    (ProductFamily.FUND, "zrin_fd_ivst_risk_gcd"): "risk_level",
    (ProductFamily.FUND, "fd_ivst_rgn_desc"): "investment_region",
    (ProductFamily.FUND, "zrin_dmst_stk_cmst_rt"): "domestic_stock_ratio_pct",
    (ProductFamily.FUND, "zrin_dmst_bd_cmst_rt"): "domestic_bond_ratio_pct",
    (ProductFamily.FUND, "zrin_ovrs_stk_cmst_rt"): "overseas_stock_ratio_pct",
    (ProductFamily.FUND, "zrin_ovrs_bd_cmst_rt"): "overseas_bond_ratio_pct",
    (ProductFamily.FUND, "fd_last_dstb_r"): "latest_distribution_pct",
    (ProductFamily.FUND, "fd_prsv_r"): "preservation_ratio_pct",
    (ProductFamily.OVERSEAS_ETP, "cu_charge_rt"): "total_expense_ratio_pct",
    (ProductFamily.OVERSEAS_ETP, "pd_exg_mkt_cd"): "exchange_code",
    (ProductFamily.OVERSEAS_ETP, "du_clpr"): "close_price",
    (ProductFamily.OVERSEAS_ETP, "du_hpr"): "high_price",
    (ProductFamily.OVERSEAS_ETP, "du_lpr"): "low_price",
    (ProductFamily.OVERSEAS_ETP, "du_opr"): "open_price",
    (ProductFamily.OVERSEAS_ETP, "du_er_1d"): "one_day_return_pct",
    (ProductFamily.OVERSEAS_ETP, "du_last_aum"): "aum",
    (ProductFamily.OVERSEAS_ETP, "du_val_1d"): "daily_trading_value",
    (ProductFamily.OVERSEAS_ETP, "du_vol_1d"): "daily_volume",
    (ProductFamily.OVERSEAS_ETP, "pd_grp_no"): "product_type",
    (ProductFamily.OVERSEAS_ETP, "pd_sale_yn"): "sellable",
    (ProductFamily.OVERSEAS_ETP, "pd_tr_yn"): "trading_suspended",
    (ProductFamily.OVERSEAS_ETP, "cu_lev_fector"): "leverage_factor",
    (ProductFamily.OVERSEAS_ETP, "pd_itm_no"): "ticker",
    (ProductFamily.OVERSEAS_ETP, "pd_nm"): "product_name",
    (ProductFamily.OVERSEAS_ETP, "pd_trd_ccy"): "trading_currency",
    (ProductFamily.OVERSEAS_ETP, "wu_inv_ast_type"): "asset_type",
    (ProductFamily.OVERSEAS_ETP, "wu_inv_rgn"): "investment_region",
}

_OVERSEAS_QUALITY_COLUMNS: dict[str, tuple[str, str | None]] = {
    "total_expense_ratio_pct": (
        "total_expense_ratio_quality",
        "total_expense_ratio_quality_reason",
    ),
    "aum": ("aum_quality", "aum_quality_reason"),
    "one_day_return_pct": (
        "one_day_return_quality",
        "one_day_return_quality_reason",
    ),
    "close_price": ("close_price_quality", "close_price_quality_reason"),
    "daily_trading_value": (
        "daily_trading_value_quality",
        "daily_trading_value_quality_reason",
    ),
    "daily_volume": ("daily_volume_quality", "daily_volume_quality_reason"),
    "discrepancy_pct": ("discrepancy_quality", "discrepancy_quality_reason"),
}

_CANONICAL_TO_SOURCE: dict[tuple[ProductFamily, str], str] = {
    (family, canonical): source
    for (family, source), canonical in _SOURCE_TO_CANONICAL.items()
}

_GROUP_SOURCE_BY_CANONICAL: dict[str, dict[ProductFamily, str]] = {
    canonical: {
        family: source
        for (family, source), mapped in _SOURCE_TO_CANONICAL.items()
        if mapped == canonical
    }
    for canonical in {
        "asset_type",
        "bond_major_class",
        "bond_subclass",
        "bond_market",
        "exchange_code",
        "fund_geography_scope",
        "fund_management_attribute",
        "investment_region",
        "issuer",
        "manager",
        "product_type",
        "risk_level",
        "sellable",
        "trading_currency",
    }
}

_GROUP_FIELD_SURFACES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"판매\s*(?:가능\s*)?여부\s*(?:별|마다)"), "sellable"),
    (re.compile(r"거래\s*통화\s*(?:별|마다)|통화\s*(?:별|마다)"), "trading_currency"),
    (
        re.compile(
            r"투자\s*지역\s*(?:별(?:로)?|마다|"
            r"만(?=\s*(?:대상|남겨|골라|추려|선별)))|"
            r"(?<!투자)지역\s*(?:별(?:로)?|마다|"
            r"만(?=\s*(?:대상|남기|남겨|골라|추려|선별)))"
        ),
        "investment_region",
    ),
    (
        re.compile(
            r"자산\s*(?:유형|군)\s*(?:별(?:로)?|마다|"
            r"만(?=\s*(?:대상|남기|남겨|골라|추려|선별)))"
        ),
        "asset_type",
    ),
    (
        re.compile(
            r"상품\s*(?:유형|종류)\s*(?:별|마다)|"
            r"ETF\s*(?:[/·ㆍ]|와|과|및)\s*ETN\s*"
            r"(?:(?:조합\s*)?(?:별|마다)|조합으로\s*(?:나눠|나누어|구분))"
        ),
        "product_type",
    ),
    (
        re.compile(
            r"운용사\s*(?:별(?:로)?|마다|"
            r"만(?=\s*(?:대상|남기|남겨|골라|추려|선별)))"
        ),
        "manager",
    ),
    (
        re.compile(
            r"운용\s*속성\s*(?:별(?:로)?|마다|"
            r"만(?=\s*(?:대상|남기|남겨|골라|추려|선별)))|속성별"
        ),
        "fund_management_attribute",
    ),
    (
        re.compile(
            r"(?:해외\s*펀드\s*)?구분\s*(?:별|마다)|"
            r"국내\s*[·ㆍ/,와과및]+\s*해외\s*[·ㆍ/,와과및]+\s*"
            r"국내외\s*혼합\s*구분\s*(?:별|마다)"
        ),
        "fund_geography_scope",
    ),
    (re.compile(r"발행(?:기관|사|자)\s*(?:별|마다)"), "issuer"),
    (
        re.compile(
            r"채권\s*대분류(?:\s*\([^)]{1,120}\))?\s*(?:별|마다)|"
            r"대분류(?:\s*\([^)]{1,120}\))?\s*(?:별|마다)"
        ),
        "bond_major_class",
    ),
    (
        re.compile(
            r"(?:채권\s*)?세부\s*분류\s*"
            r"(?:별(?:로)?|마다|만(?=\s*(?:대상|남기|남겨|골라|추려|선별)))"
        ),
        "bond_subclass",
    ),
    (
        re.compile(
            r"(?:채권\s*)?시장(?:명)?\s*(?:별|마다)|"
            r"(?:장내\s*(?:와|과|및|[·ㆍ/,])\s*장외|"
            r"장외\s*(?:와|과|및|[·ㆍ/,])\s*장내)"
            r"(?:\s*채권)?(?:으?로)?\s*(?:나눠|나누|구분)|"
            r"장내\s*(?:와|과|및)\s*장외\s*채권\s*(?:별|마다)"
        ),
        "bond_market",
    ),
    (re.compile(r"거래소(?:\s*코드)?\s*(?:별|마다)"), "exchange_code"),
)

_COORDINATED_GROUP_LABELS: dict[str, tuple[str, ...]] = {
    "asset_type": ("투자 자산 유형", "자산 유형", "자산유형", "자산군"),
    "investment_region": ("투자 지역", "투자지역"),
    "risk_level": ("위험 등급", "위험등급"),
    "manager": ("운용사",),
    "product_type": ("상품 유형", "상품유형"),
    "fund_management_attribute": ("운용 속성", "운용속성"),
    "fund_geography_scope": ("국내외 구분", "국내외구분"),
    "bond_major_class": ("채권 대분류", "채권대분류", "대분류"),
    "bond_subclass": ("채권 세부 분류", "채권세부분류", "세부분류"),
    "bond_market": ("채권 시장", "채권시장"),
    "issuer": ("발행 기관", "발행기관", "발행사"),
    "exchange_code": ("거래소 코드", "거래소코드", "거래소"),
    "trading_currency": ("거래 통화", "거래통화"),
    "sellable": ("판매 여부", "판매여부"),
}


def _explicit_group_sources(question: str, family: ProductFamily) -> tuple[str, ...]:
    """Resolve one or two explicit schema-backed aggregate group axes."""

    candidates: list[tuple[int, str]] = []
    for pattern, canonical in _GROUP_FIELD_SURFACES:
        match = pattern.search(question)
        source = _GROUP_SOURCE_BY_CANONICAL.get(canonical, {}).get(family)
        if match is not None and source is not None:
            candidates.append((match.start(), source))
    if risk_match := re.search(r"위험\s*등급\s*(?:마다|별(?:로)?)", question):
        source = _RISK_GROUP_SOURCE_BY_FAMILY.get(family)
        if source is not None:
            candidates.append((risk_match.start(), source))

    # Korean can share the grouping suffix across two labels:
    # ``투자지역과 위험등급별``.  Only explicit registry field mentions and a
    # coordinator-only bridge can open this two-axis contract.
    mentions: list[tuple[str, int, int]] = [
        (item.field_id, item.start, item.end)
        for item in registry_field_mentions(
            question,
            family.value,
            "queryable",
            value_types=frozenset({ValueType.STRING, ValueType.ENUM, ValueType.BOOLEAN}),
        )
        if item.field_id not in {"product_id", "product_name", "short_name", "ticker", "isin"}
        and _GROUP_SOURCE_BY_CANONICAL.get(item.field_id, {}).get(family) is not None
    ]
    for canonical, phrases in _COORDINATED_GROUP_LABELS.items():
        if _GROUP_SOURCE_BY_CANONICAL.get(canonical, {}).get(family) is None:
            continue
        for phrase in phrases:
            pattern = re.escape(phrase).replace(r"\ ", r"\s*")
            mentions.extend(
                (canonical, match.start(), match.end())
                for match in re.finditer(pattern, question, re.IGNORECASE)
            )
    mentions = [
        item
        for item in sorted(set(mentions), key=lambda value: (value[1], -(value[2] - value[1])))
        if not any(
            other[0] != item[0]
            and other[1] <= item[1]
            and item[2] <= other[2]
            and (other[2] - other[1]) > (item[2] - item[1])
            for other in mentions
        )
    ]
    for left, right in zip(mentions, mentions[1:], strict=False):
        if left[0] == right[0]:
            continue
        if re.fullmatch(
            r"\s*(?:와|과|및|하고|이랑|랑|[·ㆍ,/&+])\s*",
            question[left[2] : right[1]],
            flags=re.IGNORECASE,
        ) is None:
            continue
        if re.match(
            r"\s*(?:별(?:로)?|마다)(?:\s*(?:나눠|나누|구분|묶))?",
            question[right[2] :],
            flags=re.IGNORECASE,
        ) is None:
            continue
        for mention in (left, right):
            source = _GROUP_SOURCE_BY_CANONICAL[mention[0]][family]
            candidates.append((mention[1], source))
        break
    sources = tuple(dict.fromkeys(source for _start, source in sorted(candidates)))
    return sources if 1 <= len(sources) <= 2 else ()


def _explicit_group_source(question: str, family: ProductFamily) -> str | None:
    """Resolve the legacy single-axis group shape."""

    sources = _explicit_group_sources(question, family)
    return sources[0] if len(sources) == 1 else None


def _plan_group_sources(plan: CompoundFamilyPlan) -> tuple[str, ...]:
    """Return the complete sealed group tuple with legacy compatibility."""

    if plan.group_by_source_fields:
        return plan.group_by_source_fields
    return () if plan.group_by_source_field is None else (plan.group_by_source_field,)


def _shared_product_name_contains(question: str) -> str | None:
    """Return one explicit name substring that scopes every named family.

    This handles coordinated requests such as ``상품명에 '인도'가 들어간
    국내 ETP와 공모펀드``.  The literal is copied from the user question;
    it is never expanded through model knowledge.  Multiple or empty literals
    remain unresolved so a weaker family-only query cannot execute.
    """

    matches = list(
        re.finditer(
            r"(?:상품|종목|펀드)\s*(?:명|이름)\s*(?:에|이)?\s*"
            r"[\"'“”‘’]?\s*(?P<literal>[0-9A-Za-z가-힣&+._/-]{1,40})\s*"
            r"[\"'“”‘’]?(?:이|가|을|를)?\s*"
            r"(?:들어간|포함(?:된|한|하는)|있는)",
            question,
            re.IGNORECASE,
        )
    )
    literals = tuple(
        dict.fromkeys(match.group("literal").strip() for match in matches)
    )
    return literals[0] if len(literals) == 1 else None


def _explicit_bond_issuer_cohort(question: str) -> str | None:
    """Extract an issuer only from an explicit issuer-owned cohort clause."""

    matches = list(
        re.finditer(
            r"(?<![0-9A-Za-z가-힣])"
            r"(?P<issuer>[0-9A-Za-z가-힣㈜()·&._-]{2,60})\s+채권\s*중\s*"
            r"(?:그|해당)\s*발행사\s*(?:의\s*)?"
            r"(?:평균|중앙\s*값|중위수|메디안)",
            question,
            re.IGNORECASE,
        )
    )
    issuers = tuple(dict.fromkeys(match.group("issuer").strip() for match in matches))
    return issuers[0] if len(issuers) == 1 else None

_TEXT_SORT_SOURCE_FIELDS = frozenset(
    source
    for (_family, source), canonical in _SOURCE_TO_CANONICAL.items()
    if canonical == "product_name"
)


_METRIC_ALIASES: tuple[tuple[re.Pattern[str], dict[ProductFamily, str]], ...] = (
    (
        re.compile(r"상품\s*명|종목\s*명|펀드\s*명|정식\s*명칭|이름"),
        {
            ProductFamily.BOND: "pd_nm",
            ProductFamily.DOMESTIC_ETP: "pd_nm",
            ProductFamily.OVERSEAS_ETP: "pd_nm",
            ProductFamily.FUND: "itm_nm",
        },
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])AUM(?![A-Za-z0-9])|순\s*자산(?!\s*총액)|"
            r"운용\s*자산|자산\s*규모",
            re.IGNORECASE,
        ),
        {
            ProductFamily.DOMESTIC_ETP: "du_last_aum",
            ProductFamily.OVERSEAS_ETP: "du_last_aum",
            ProductFamily.FUND: "fd_nast_suma",
        },
    ),
    (
        re.compile(r"20(?:거래)?일(?:\s*연환산)?\s*변동성"),
        {ProductFamily.DOMESTIC_ETP: "du_vlty_1m"},
    ),
    (
        re.compile(r"(?:60(?:거래)?일|3\s*개월)(?:\s*연환산)?\s*변동성"),
        {ProductFamily.DOMESTIC_ETP: "du_vlty_3m"},
    ),
    (
        re.compile(r"252거래일(?:\s*연환산)?\s*변동성"),
        {ProductFamily.DOMESTIC_ETP: "du_vlty_1y"},
    ),
    (
        re.compile(r"1개월\s*평균\s*거래대금"),
        {ProductFamily.DOMESTIC_ETP: "du_val_1m"},
    ),
    (
        # Keep this after the longer monthly-average form. ``_metric_from_label``
        # resolves the nearest/longest surface, so an explicit daily trading
        # value in a clause cannot disappear and a numeric fee threshold can
        # never be promoted to the clause's ranking field.
        re.compile(
            r"(?<!평균\s)(?:(?:일|하루)\s*)?거래대금|"
            r"자금\s*거래(?:가|이)?\s*(?:큰|많은|활발)"
        ),
        {
            ProductFamily.DOMESTIC_ETP: "du_val_1d",
            ProductFamily.OVERSEAS_ETP: "du_val_1d",
        },
    ),
    (
        re.compile(r"일간\s*순자산총액"),
        {ProductFamily.OVERSEAS_ETP: "du_last_aum"},
    ),
    (
        re.compile(r"순자산총액"),
        {
            ProductFamily.DOMESTIC_ETP: "pd_net_tamt",
            ProductFamily.OVERSEAS_ETP: "du_last_aum",
            ProductFamily.FUND: "fd_nast_suma",
        },
    ),
    (
        re.compile(r"집합투자업자보수|운용사보수"),
        {ProductFamily.FUND: "or_co_rwrd_r"},
    ),
    (
        re.compile(r"판매\s*회사\s*보수|판매\s*보수"),
        {ProductFamily.FUND: "sale_co_rwrd_r"},
    ),
    (
        re.compile(
            r"국내\s*주식\s*(?:구성\s*비(?:율)?|편입\s*비(?:율)?|비중)"
        ),
        {ProductFamily.FUND: "zrin_dmst_stk_cmst_rt"},
    ),
    (
        re.compile(
            r"국내\s*채권\s*(?:구성\s*비(?:율)?|편입\s*비(?:율)?|비중)"
        ),
        {ProductFamily.FUND: "zrin_dmst_bd_cmst_rt"},
    ),
    (
        re.compile(
            r"해외\s*주식\s*(?:구성\s*비(?:율)?|편입\s*비(?:율)?|비중)"
        ),
        {ProductFamily.FUND: "zrin_ovrs_stk_cmst_rt"},
    ),
    (
        re.compile(
            r"해외\s*채권\s*(?:구성\s*비(?:율)?|편입\s*비(?:율)?|비중)"
        ),
        {ProductFamily.FUND: "zrin_ovrs_bd_cmst_rt"},
    ),
    (
        re.compile(r"원본\s*보존\s*비율|원본(?:을)?\s*남겨\s*둔\s*비율|보전\s*율|보전\s*비율"),
        {ProductFamily.FUND: "fd_prsv_r"},
    ),
    (
        re.compile(r"최근\s*분배\s*율|최신\s*분배\s*율|최근\s*분배\s*수익률"),
        {ProductFamily.FUND: "fd_last_dstb_r"},
    ),
    (
        re.compile(
            r"평가(?:일)?\s*(?:단가|가격)|클린\s*프라이스|"
            r"이자\s*제외\s*평가(?:\s*단가|가격)"
        ),
        {ProductFamily.BOND: "eval_price"},
    ),
    (
        re.compile(r"이자부단가"),
        {ProductFamily.BOND: "dirty"},
    ),
    (
        re.compile(r"총\s*발행금액"),
        {ProductFamily.BOND: "bd_tisu_a"},
    ),
    (
        # The official Field Registry defines the otherwise terse
        # ``발행금액`` as outstanding issue amount (isu_bal_amt).  Keep it
        # distinct from the explicitly qualified ``총발행금액`` above.
        re.compile(r"(?<!총)(?<!총\s)발행\s*금액|발행\s*잔액"),
        {ProductFamily.BOND: "isu_bal_amt"},
    ),
    (
        re.compile(
            r"매수\s*수익률|매수수익률|"
            r"(?:현재|지금)\s*(?:가격|매수가격).{0,20}?세전\s*(?:수익률|보상)"
        ),
        {ProductFamily.BOND: "buy_yield"},
    ),
    (
        re.compile(
            r"세후\s*수익률|세후수익률|"
            r"(?:세금\s*(?:공제|차감)|세금을?\s*떼고).{0,20}?(?:수익률|수익|보상)"
        ),
        {ProductFamily.BOND: "after_tax_yield"},
    ),
    (
        re.compile(r"민평\s*(?:금리|수익률)|적용\s*수익률|평가기관\s*수익률"),
        {ProductFamily.BOND: "applied_yield"},
    ),
    (
        re.compile(
            r"표면이자율|표면이율|표면금리|쿠폰\s*금리|"
            r"(?:액면|표면).{0,12}?(?:이자|이율)"
        ),
        {ProductFamily.BOND: "srfc_irt"},
    ),
    (
        re.compile(r"컨벡시티"),
        {ProductFamily.BOND: "cov"},
    ),
    (
        re.compile(r"듀레이션"),
        {ProductFamily.BOND: "dur"},
    ),
    (
        re.compile(
            r"잔존일수|잔존\s*기간|남은\s*(?:기간|일수)|"
            r"(?:만기|상환)\s*(?:시점|일)?"
        ),
        {ProductFamily.BOND: "remaining_days"},
    ),
    (
        re.compile(
            r"연간\s*보수(?:율)?|총\s*보수(?:율)?|보수율|"
            r"(?:총\s*)?운용\s*보수(?:율)?|보유\s*관리비"
        ),
        {
            ProductFamily.OVERSEAS_ETP: "cu_charge_rt",
            ProductFamily.DOMESTIC_ETP: "cu_charge_rt",
        },
    ),
    (
        re.compile(r"추적\s*오차(?:율)?"),
        {ProductFamily.DOMESTIC_ETP: "du_chas_errt"},
    ),
    (
        re.compile(r"괴리율"),
        {
            ProductFamily.DOMESTIC_ETP: "du_diff_rt",
            ProductFamily.OVERSEAS_ETP: "du_diff_rt",
        },
    ),
    (
        re.compile(r"(?:연초\s*(?:이후|대비)|YTD)\s*(?:수익률|수익|성과|성적)", re.I),
        {ProductFamily.DOMESTIC_ETP: "du_er_ytd"},
    ),
    (
        re.compile(r"(?:연환산\s*)?분배\s*수익률|분배\s*율"),
        {ProductFamily.DOMESTIC_ETP: "pd_dvid_yield"},
    ),
    (
        re.compile(
            r"(?:1\s*년|일\s*년|12\s*개월|열두\s*달|연간)\s*(?:누적\s*)?"
            r"(?:수익률|수익|성과|성적)"
        ),
        {
            ProductFamily.DOMESTIC_ETP: "du_er_1y",
            ProductFamily.FUND: "fd_yr1_ern_r",
        },
    ),
    (
        re.compile(
            r"(?:6\s*개월|반년|여섯\s*달)\s*"
            r"(?:수익률|수익|성과|성적)"
        ),
        {
            ProductFamily.DOMESTIC_ETP: "du_er_6m",
            ProductFamily.FUND: "fd_mm6_ern_r",
        },
    ),
    (
        re.compile(r"3년\s*(?:누적\s*)?(?:수익률|수익|성과|성적)"),
        {ProductFamily.FUND: "fd_yr3_ern_r"},
    ),
    (
        re.compile(
            r"(?:3\s*개월|한\s*분기|분기|석\s*달)\s*"
            r"(?:수익률|수익|성과|성적)"
        ),
        {
            ProductFamily.DOMESTIC_ETP: "du_er_3m",
            ProductFamily.FUND: "fd_mm3_ern_r",
        },
    ),
    (
        re.compile(r"(?:1개월|한\s*달)\s*(?:수익률|수익|성과|성적)"),
        {
            ProductFamily.DOMESTIC_ETP: "du_er_1m",
            ProductFamily.FUND: "fd_mm1_ern_r",
        },
    ),
    (
        re.compile(r"(?:1\s*일|일간|하루|당일)\s*(?:수익률|수익|성과|성적)"),
        {
            ProductFamily.DOMESTIC_ETP: "du_er_1d",
            ProductFamily.OVERSEAS_ETP: "du_er_1d",
        },
    ),
    (
        re.compile(r"일거래대금"),
        {
            ProductFamily.DOMESTIC_ETP: "du_val_1d",
            ProductFamily.OVERSEAS_ETP: "du_val_1d",
        },
    ),
    (
        re.compile(r"(?:일|하루)?\s*거래량|하루\s*거래\s*(?:주식|종목)?\s*수"),
        {
            ProductFamily.DOMESTIC_ETP: "du_vol_1d",
            ProductFamily.OVERSEAS_ETP: "du_vol_1d",
        },
    ),
    (
        re.compile(r"순자산"),
        {
            ProductFamily.DOMESTIC_ETP: "du_last_aum",
            ProductFamily.OVERSEAS_ETP: "du_last_aum",
            ProductFamily.FUND: "fd_nast_suma",
        },
    ),
    (
        re.compile(r"종가"),
        {
            ProductFamily.DOMESTIC_ETP: "du_clpr",
            ProductFamily.OVERSEAS_ETP: "du_clpr",
        },
    ),
)


# Attributes that may be copied from one exact DB-bound reference product into
# a candidate cohort.  This is deliberately separate from ``_METRIC_ALIASES``:
# these fields are equality predicates, never numeric ranking metrics.  Every
# source field is backed by the same canonical Field Registry used by ordinary
# QueryPlan validation.
_ANCHOR_EQUAL_FIELD_ALIASES: tuple[
    tuple[re.Pattern[str], dict[ProductFamily, str]], ...
] = (
    (
        re.compile(r"발행\s*(?:기관|사|자)"),
        {ProductFamily.BOND: "pd_pbcm"},
    ),
    (
        re.compile(r"(?:채권\s*)?세부\s*분류|세부분류"),
        {ProductFamily.BOND: "std_pd_scls_nm"},
    ),
    (
        re.compile(r"운용\s*(?:회사|사)"),
        {
            ProductFamily.DOMESTIC_ETP: "cu_fund_mgmt_co",
            ProductFamily.OVERSEAS_ETP: "cu_fund_mgmt_co",
        },
    ),
    (
        re.compile(r"투자\s*지역"),
        {
            ProductFamily.DOMESTIC_ETP: "wu_inv_rgn",
            ProductFamily.OVERSEAS_ETP: "wu_inv_rgn",
            ProductFamily.FUND: "fd_ivst_rgn_desc",
        },
    ),
    (
        re.compile(r"위험\s*(?:등급|도)"),
        {
            ProductFamily.BOND: "pd_risk_nm",
            ProductFamily.DOMESTIC_ETP: "pd_risk_nm",
            ProductFamily.FUND: "zrin_fd_ivst_risk_gcd",
        },
    ),
    (
        re.compile(r"(?:투자\s*)?자산\s*(?:군|유형)"),
        {
            ProductFamily.DOMESTIC_ETP: "wu_inv_ast_type",
            ProductFamily.OVERSEAS_ETP: "wu_inv_ast_type",
        },
    ),
    (
        re.compile(r"운용\s*속성|펀드\s*(?:유형|운용\s*유형)"),
        {ProductFamily.FUND: "or_attr_desc"},
    ),
)

_ANCHOR_MARKER = "<ANCHOR>"
_ANCHOR_MARKER_WITH_WRAPPERS = (
    r"[\s\"'`‘’“”「」『』()\[\]{},，:：]*"
    + re.escape(_ANCHOR_MARKER)
    + r"[\s\"'`‘’“”「」『』()\[\]{},，:：]*"
)
_ANCHOR_EQUAL_RELATION = r"(?:같(?:은|고|되|으면서|으며)?|동일(?:한|하고|하며|하면서)?)"
_ANCHOR_DECLARATION_SUFFIX = (
    r"(?:을|를)?\s*(?:(?:비교|검색)\s*)?"
    r"기준(?:점|\s*상품|\s*종목)?(?:으로)?\s*"
    r"(?:삼(?:아|고)|잡(?:아|고)|정(?:해|하고)|두(?:고|어)|"
    r"설정(?:해|하고)?|보(?:고|면)|비교|$)"
)
_ANAPHORIC_ANCHOR_OWNER = (
    r"(?:이|그|해당)\s*(?:기준\s*)?"
    r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)"
)


def _anaphoric_anchor_relative_specs(
    text: str,
    family: ProductFamily,
) -> tuple[tuple[CompoundMetric, str], ...] | None:
    """Canonicalize every explicit anaphoric reference threshold.

    The function recognizes a grammatical relation owned by an already
    declared reference product, for example ``이 상품 이상인 3개월
    수익률과 이 상품 이하인 보수``.  It returns ``None`` as soon as one
    conjunct names an unsupported metric; retaining only the recognized half
    would widen the requested cohort.
    """

    next_clause = (
        rf"(?:과|와|및|[,;·ㆍ/])\s*{_ANAPHORIC_ANCHOR_OWNER}\s*"
        r"(?:이상|이하|초과|미만)인"
    )
    terminal = r"(?:을|를)?\s*(?:동시에|모두|함께)\s*(?:만족|충족)"
    pattern = re.compile(
        rf"{_ANAPHORIC_ANCHOR_OWNER}\s*"
        r"(?P<operator>이상|이하|초과|미만)인\s*"
        rf"(?P<label>.{{1,80}}?)(?=\s*(?:{next_clause}|{terminal}))",
        re.IGNORECASE,
    )
    result: list[tuple[CompoundMetric, str]] = []
    for match in pattern.finditer(text):
        metric = _metric_from_label(match.group("label"), family)
        if metric is None:
            return None
        clause = (
            metric,
            {
                "이상": "gte",
                "이하": "lte",
                "초과": "gt",
                "미만": "lt",
            }[match.group("operator")],
        )
        if clause not in result:
            result.append(clause)

    # Field-first Korean is equally explicit: ``순자산이 이 상품 이상인``
    # and ``종가가 이 상품보다 큰``. Resolve only Registry-owned metric
    # surfaces immediately followed by the anaphoric owner and comparator;
    # a free adjective or an unknown field cannot create a clause.
    owner_suffix = re.compile(
        rf"\s*(?:이|가|은|는)?\s*{_ANAPHORIC_ANCHOR_OWNER}\s*"
        r"(?:(?P<bound>이상|이하|초과|미만)인|"
        r"(?:보다|대비)\s*(?:더\s*)?"
        r"(?P<relative>큰|작은|높은|낮은|많은|적은|긴|짧은))",
        re.IGNORECASE,
    )
    for _start, end, source_field in _ordered_metric_mentions(text, family):
        matched = owner_suffix.match(text[end:])
        if matched is None:
            continue
        if matched.group("bound") is not None:
            operator = {
                "이상": "gte",
                "이하": "lte",
                "초과": "gt",
                "미만": "lt",
            }[matched.group("bound")]
        else:
            operator = (
                "gt"
                if matched.group("relative") in {"큰", "높은", "많은", "긴"}
                else "lt"
            )
        clause = (CompoundMetric("field", (source_field,)), operator)
        if clause not in result:
            result.append(clause)

    # Also support the owner-first comparative form ``이 상품보다 종가가
    # 큰``. The bounded label is independently resolved and an unknown label
    # rejects the whole canonical proof.
    owner_first = re.compile(
        rf"{_ANAPHORIC_ANCHOR_OWNER}\s*(?:보다|대비)\s*",
        re.IGNORECASE,
    )
    owner_first_relation = re.compile(
        r"\s*(?:이|가|은|는)?\s*(?:더\s*)?"
        r"(?P<relative>큰|작은|높은|낮은|많은|적은|긴|짧은)",
        re.IGNORECASE,
    )
    for owner in owner_first.finditer(text):
        tail = text[owner.end() :]
        mentions = _ordered_metric_mentions(tail, family)
        if not mentions:
            continue
        start, end, source_field = mentions[0]
        # The metric must immediately own the post-anchor clause. This avoids
        # consuming ``큰 다른 상품을 <ranking field> 작은 순`` as a second
        # reference threshold.
        if tail[:start].strip():
            continue
        relation = owner_first_relation.match(tail[end:])
        if relation is None:
            continue
        operator = (
            "gt"
            if relation.group("relative") in {"큰", "높은", "많은", "긴"}
            else "lt"
        )
        clause = (CompoundMetric("field", (source_field,)), operator)
        if clause not in result:
            result.append(clause)
    return tuple(result)


def _anchor_equal_source_fields(
    question: str,
    family: ProductFamily,
    identity_spans: tuple[tuple[int, int], ...],
) -> tuple[str, ...]:
    """Resolve only equality attributes syntactically tied to one anchor.

    All corroborating name/ticker/ID surfaces for the same product are replaced
    by one marker.  The three accepted Korean shapes cover ``field가 A와
    같다``, ``A와 field가 같다`` and ``A와 같은 field`` without treating a
    field word elsewhere in the request as an equality predicate.
    """

    if not identity_spans:
        return ()
    start = min(item[0] for item in identity_spans)
    end = max(item[1] for item in identity_spans)
    marked = question[:start] + _ANCHOR_MARKER + question[end:]
    sources: list[str] = []
    connector = r"(?:와|과|랑|이랑|하고|및)"
    for field_pattern, mapping in _ANCHOR_EQUAL_FIELD_ALIASES:
        source = mapping.get(family)
        if source is None:
            continue
        field = rf"(?:{field_pattern.pattern})"
        shapes = (
            rf"{field}(?:이|가|은|는)?\s*.{{0,12}}?"
            rf"{_ANCHOR_MARKER_WITH_WRAPPERS}(?:{connector})?\s*"
            rf"{_ANCHOR_EQUAL_RELATION}",
            rf"{_ANCHOR_MARKER_WITH_WRAPPERS}(?:{connector})\s*"
            rf"{field}(?:이|가|은|는)?\s*{_ANCHOR_EQUAL_RELATION}",
            rf"{_ANCHOR_MARKER_WITH_WRAPPERS}(?:{connector})\s*"
            rf"{_ANCHOR_EQUAL_RELATION}\s*{field}",
        )
        if any(re.search(shape, marked, re.IGNORECASE) for shape in shapes):
            sources.append(source)
    return tuple(dict.fromkeys(sources))


def _mask_anchor_identity_surfaces(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
) -> str:
    """Hide DB-verified identity text and its non-semantic annotation shell.

    Official examples commonly print a reference as ``name (ticker, ID id)``
    or ``name (venue, ID physical-id)``.  A venue inside that parenthesis is
    metadata that identifies the *reference row*; it is not a requested cohort
    filter.  Expand the mask to the complete parenthesis only when, after all
    independently DB-verified identity spans are removed, the remainder is a
    closed vocabulary of identity labels and punctuation.  Arbitrary text in a
    parenthesis is therefore never discarded.
    """

    masked = list(question)
    for start, end in identity_spans:
        masked[start:end] = " " * (end - start)
    for wrapper in re.finditer(r"\([^()]{0,120}\)", question):
        if not any(
            start < wrapper.end() and wrapper.start() < end
            for start, end in identity_spans
        ):
            continue
        remainder = "".join(masked[wrapper.start() : wrapper.end()])
        if re.fullmatch(
            r"\(\s*(?:(?:상품\s*)?ID|식별자|티커|종목\s*코드|"
            r"장내|장외|[,，:/;\-]|\s)*\)",
            remainder,
            re.IGNORECASE,
        ):
            masked[wrapper.start() : wrapper.end()] = " " * (
                wrapper.end() - wrapper.start()
            )
    return "".join(masked)


_DERIVED_METRICS: tuple[tuple[re.Pattern[str], str, tuple[str, ...], Decimal], ...] = (
    (
        re.compile(r"총발행금액\s*대비\s*발행잔액\s*비율"),
        "ratio",
        ("isu_bal_amt", "bd_tisu_a"),
        Decimal("100"),
    ),
    (
        re.compile(
            r"(?:민평금리에서\s*표면이자율을|민평금리와\s*표면이자율의\s*차이|"
            r"민평금리\s*[-−]\s*표면이자율)(?:\s*뺀|\s*차이|\s*격차)?"
        ),
        "subtract",
        ("applied_yield", "srfc_irt"),
        Decimal("1"),
    ),
    (
        re.compile(
            r"(?:이자부단가에서\s*평가일단가를|이자부단가와\s*평가일단가의\s*차이|"
            r"이자부단가\s*[-−]\s*평가일단가)(?:\s*뺀|\s*차이|\s*격차)?"
        ),
        "subtract",
        ("dirty", "eval_price"),
        Decimal("1"),
    ),
    (
        re.compile(
            r"1년\s*(?:수익률|수익|성과|성적)에서\s*"
            r"3개월\s*(?:수익률|수익|성과|성적)(?:을|를)\s*뺀"
        ),
        "subtract",
        ("fd_yr1_ern_r", "fd_mm3_ern_r"),
        Decimal("1"),
    ),
    (
        re.compile(
            r"1년\s*(?:수익률|수익|성과|성적)에서\s*"
            r"3개월\s*(?:수익률|수익|성과|성적)(?:을|를)\s*뺀"
        ),
        "subtract_domestic",
        ("du_er_1y", "du_er_3m"),
        Decimal("1"),
    ),
    (
        re.compile(r"20거래일\s*변동성에서\s*252거래일\s*변동성을\s*뺀"),
        "subtract",
        ("du_vlty_1m", "du_vlty_1y"),
        Decimal("1"),
    ),
    (
        re.compile(r"판매회사보수에서\s*집합투자업자보수를\s*뺀"),
        "subtract",
        ("sale_co_rwrd_r", "or_co_rwrd_r"),
        Decimal("1"),
    ),
    (
        re.compile(
            r"판매(?:회사)?\s*(?:[·ㆍ,/]|\s*(?:와|과|\+|\band\b)\s*)\s*"
            r"(?:운용|집합투자업자)\s*(?:[·ㆍ,/]|\s*(?:와|과|\+|\band\b)\s*)\s*"
            r"신탁\s*(?:[·ㆍ,/]|\s*(?:와|과|\+|\band\b)\s*)\s*"
            r"일반사무관리\s*보수"
            r"(?:\s*네\s*항목의\s*단순)?\s*합계",
            re.IGNORECASE,
        ),
        "add",
        ("sale_co_rwrd_r", "or_co_rwrd_r", "trusc_rwrd_r", "ofwk_trus_rwrd_r"),
        Decimal("1"),
    ),
    (
        re.compile(
            r"일거래대금(?:을)?\s*(?:1개월\s*평균\s*거래대금으로\s*나눈|대비\s*"
            r"1개월\s*평균\s*거래대금\s*(?:비율|배수))"
        ),
        "ratio",
        ("du_val_1d", "du_val_1m"),
        Decimal("1"),
    ),
    (
        re.compile(r"일간\s*순자산총액\s*대비\s*일거래대금\s*비율"),
        "ratio",
        ("du_val_1d", "du_last_aum"),
        Decimal("100"),
    ),
    (
        re.compile(
            r"시가\s*대비\s*종가\s*변화율"
            r"(?:\s*\(\s*\(?\s*종가\s*[-\u2212]\s*시가\s*\)?\s*/\s*"
            r"시가\s*(?:[\u00d7xX*]\s*100)?\s*\))?"
        ),
        "relative_change",
        ("du_clpr", "du_opr"),
        Decimal("100"),
    ),
    (
        re.compile(r"고가와\s*저가의\s*차이"),
        "subtract",
        ("du_hpr", "du_lpr"),
        Decimal("1"),
    ),
)


def _public_product_id(record: object, family: ProductFamily) -> str:
    if family in {ProductFamily.BOND, ProductFamily.OVERSEAS_ETP}:
        return str(record.ticker)
    return str(record.product_id)


def _execution_product_id(
    record: object,
    family: ProductFamily,
    compound_kind: str,
) -> str:
    """Return the identity grain explicitly owned by an execution shape."""

    if compound_kind in {"anchor_relative", "cross_family_named_comparison"}:
        return str(record.product_id)
    return _public_product_id(record, family)


def _decimal(value: object, *, allow_zero: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return None if parsed == 0 and not allow_zero else parsed


def _normalized_evidence_value(raw: object, definition: FieldDefinition) -> object:
    """Apply the same registry transform used by normalized domain records.

    Compound execution reads a narrow raw projection directly from SQLite for
    latency and memory reasons. Evidence must nevertheless expose the canonical
    value (for example ``0`` -> ``False`` and ``공모`` -> ``True``) while
    retaining the untouched source cell separately in ``raw_values``.
    """

    source = definition.source
    value = source.constant_value if source.transform is SourceTransform.CONSTANT else raw
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if source.transform in {SourceTransform.BOOLEAN_CODE, SourceTransform.CODE_MAP}:
        value = source.value_map.get(str(value), value)
    if definition.value_type is ValueType.BOOLEAN:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        return None
    if definition.value_type is ValueType.NUMBER:
        parsed = _decimal(value, allow_zero=True)
        return None if parsed is None else format(parsed, "f")
    return str(value).strip()


def _canonical_constraint_value(raw: object, definition: FieldDefinition) -> object:
    """Convert a raw SQLite predicate value to the public QueryPlan value.

    Compound SQL deliberately uses source codes (for example public-fund risk
    code ``2``), while the public QueryPlan contract exposes the canonical
    Registry enum (``높은위험(2등급)``).  Converting only booleans allowed a
    valid SQL predicate to become an invalid public DTO after the DB had
    already executed.  Keep the two representations explicit and validate the
    public one before an authorization receipt is issued.
    """

    value = raw
    if definition.source.transform in {
        SourceTransform.BOOLEAN_CODE,
        SourceTransform.CODE_MAP,
    }:
        value = definition.source.value_map.get(str(raw), raw)
    if definition.value_type is ValueType.BOOLEAN:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        raise ValueError("compound boolean filter has an unknown source code")
    if definition.value_type is ValueType.NUMBER:
        if isinstance(value, bool):
            raise ValueError("compound numeric filter cannot be boolean")
        parsed = _decimal(value, allow_zero=True)
        if parsed is None:
            raise ValueError("compound numeric filter is not numeric")
        return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)
    if definition.value_type in {ValueType.STRING, ValueType.DATE, ValueType.ENUM}:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("compound text filter cannot be empty")
        if (
            definition.value_type is ValueType.ENUM
            and normalized not in definition.enum_values
            and not definition.unknown_enum_as_empty_set
        ):
            raise ValueError("compound enum filter has an unknown canonical value")
        return normalized
    raise ValueError("compound filter has an unsupported public value type")


def _raw_value(record: object, source_field: str) -> object:
    if source_field == "remaining_days":
        explicit = getattr(record, "remaining_days", None)
        if explicit is not None:
            return explicit
    return record.source_values.get(source_field)


def _compound_plan_payload(plan: TypedCompoundPlan) -> dict[str, object]:
    return {
        "question_id": plan.question_id,
        "question": plan.question,
        "interaction_intent": plan.interaction_intent.value,
        "kind": plan.kind,
        "cohort_mean_direction": (
            None
            if plan.cohort_mean_direction is None
            else plan.cohort_mean_direction.value
        ),
        "cohort_mean_difference": plan.cohort_mean_difference,
        "hclx_structure_spans": list(plan.hclx_structure_spans),
        "excluded_families": [
            {
                "product_family": item.product_family.value,
                "canonical_field": item.canonical_field,
                "reason_code": item.reason_code,
            }
            for item in plan.excluded_families
        ],
        "families": [
            {
                "product_family": family.product_family.value,
                "filters": [
                    {
                        "source_field": item.source_field,
                        "operator": item.operator,
                        "value": item.value,
                    }
                    for item in family.filters
                ],
                "metric": {
                    "kind": family.metric.kind,
                    "source_fields": list(family.metric.source_fields),
                    "scale": str(family.metric.scale),
                },
                "direction": family.direction.value,
                "limit": family.limit,
                "shortlist_metric": (
                    None
                    if family.shortlist_metric is None
                    else {
                        "kind": family.shortlist_metric.kind,
                        "source_fields": list(family.shortlist_metric.source_fields),
                        "scale": str(family.shortlist_metric.scale),
                    }
                ),
                "shortlist_limit": family.shortlist_limit,
                "shortlist_direction": family.shortlist_direction.value,
                "exclude_product_ids": list(family.exclude_product_ids),
                "relative_filters": [
                    {
                        "metric": {
                            "kind": metric.kind,
                            "source_fields": list(metric.source_fields),
                            "scale": str(metric.scale),
                        },
                        "operator": operator,
                        "threshold": str(threshold),
                    }
                    for metric, operator, threshold in family.relative_filters
                ],
                "projection_fields": list(family.projection_fields),
                "cohort_label": family.cohort_label,
                "include_product_ids": list(family.include_product_ids),
                "identity_scope_active": family.identity_scope_active,
                "group_by_source_field": family.group_by_source_field,
                "group_by_source_fields": list(_plan_group_sources(family)),
                "aggregations": [
                    {
                        "function": item.function.value,
                        "source_field": item.source_field,
                    }
                    for item in family.aggregations
                ],
                "aggregate_threshold": (
                    None
                    if family.aggregate_threshold is None
                    else {
                        "metric": {
                            "kind": family.aggregate_threshold.metric.kind,
                            "source_fields": list(
                                family.aggregate_threshold.metric.source_fields
                            ),
                            "scale": str(family.aggregate_threshold.metric.scale),
                        },
                        "function": family.aggregate_threshold.function.value,
                        "operator": family.aggregate_threshold.operator,
                    }
                ),
                "group_min_count": family.group_min_count,
                "group_min_count_source_field": family.group_min_count_source_field,
                "group_limit": family.group_limit,
                "group_order_by_count": family.group_order_by_count,
                "group_order_aggregation": (
                    None
                    if family.group_order_aggregation is None
                    else {
                        "function": family.group_order_aggregation.function.value,
                        "source_field": family.group_order_aggregation.source_field,
                    }
                ),
                "per_group_limit": family.per_group_limit,
                "field_comparisons": [
                    {"left": left, "operator": operator, "right": right}
                    for left, operator, right in family.field_comparisons
                ],
            }
            for family in plan.family_plans
        ],
    }


def _compound_plan_sha256(plan: TypedCompoundPlan) -> str:
    encoded = json.dumps(
        _compound_plan_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metric_value(record: object, metric: CompoundMetric) -> Decimal | str | None:
    if metric.kind == "identity_order":
        raw = _raw_value(record, metric.source_fields[0])
        return raw.strip() if isinstance(raw, str) and raw.strip() else None
    if metric.kind == "field":
        raw = _raw_value(record, metric.source_fields[0])
        if metric.source_fields[0] in _TEXT_SORT_SOURCE_FIELDS:
            return raw.strip() if isinstance(raw, str) and raw.strip() else None
        # Organizer clarification (2026-08-26): numeric zero and missing
        # values are not valid bases for filtering/ranking.  Apply the same
        # availability rule to remaining days; zero must not outrank a real
        # one-day remaining value merely because its source cell is present.
        return _decimal(raw, allow_zero=False)
    values = tuple(_decimal(_raw_value(record, field)) for field in metric.source_fields)
    if any(value is None for value in values):
        return None
    concrete = tuple(value for value in values if value is not None)
    if metric.kind.startswith("subtract"):
        return concrete[0] - concrete[1]
    if metric.kind == "add":
        return sum(concrete, Decimal("0"))
    if metric.kind == "ratio":
        if concrete[1] == 0:
            return None
        return concrete[0] / concrete[1] * metric.scale
    if metric.kind == "relative_change":
        if concrete[1] == 0:
            return None
        return (concrete[0] - concrete[1]) / concrete[1] * metric.scale
    raise ValueError(f"unsupported compound metric: {metric.kind}")


def _aggregate_threshold_evidence(
    records: tuple[_SourceRecord, ...],
    threshold: CompoundAggregateThreshold,
    family: ProductFamily,
) -> CompoundAggregateThresholdEvidence:
    """Recompute AVG/MEDIAN from exact non-zero, non-missing source values."""

    inputs: list[tuple[_SourceRecord, Decimal]] = []
    seen_product_ids: set[str] = set()
    for record in records:
        # Organizer policy: numeric zero and missing values have no usable
        # basis for a numeric aggregate. Aggregate thresholds intentionally
        # apply this rule even to fields whose ordinary row-level semantics
        # can otherwise treat zero as meaningful.
        value = _decimal(
            _raw_value(record, threshold.metric.source_fields[0]),
            allow_zero=False,
        )
        if value is None:
            continue
        # Bond and overseas-ETP source tables may contain more than one source
        # row for the same public product identity.  The aggregate is defined
        # over products, not workbook rows.  Mark an identity as consumed only
        # after finding a usable value so an earlier missing/zero duplicate
        # cannot hide a later valid source row.
        # Aggregate thresholds follow the approved database product identity,
        # not the shorter display ticker.  A bond can have separate market or
        # snapshot-key rows and an overseas ticker can be venue-qualified;
        # collapsing those rows changes the official cohort mean/median.
        normalized_id = record.product_id
        if normalized_id in seen_product_ids:
            continue
        seen_product_ids.add(normalized_id)
        inputs.append((record, value))
    if not inputs:
        raise AggregateThresholdUnavailableError(
            product_family=family,
            source_field=threshold.metric.source_fields[0],
        )
    values = sorted(value for _record, value in inputs)
    if threshold.function is AggregateFunction.AVG:
        result = sum(values, Decimal("0")) / Decimal(len(values))
    elif threshold.function is AggregateFunction.MEDIAN:
        midpoint = len(values) // 2
        result = (
            values[midpoint]
            if len(values) % 2
            else (values[midpoint - 1] + values[midpoint]) / Decimal("2")
        )
    else:
        raise ValueError("aggregate threshold function is not server-approved")
    input_payload = [
        {
            "product_id": record.product_id,
            "source_row": record.source_row,
            "value": str(value),
        }
        for record, value in sorted(
            inputs,
            key=lambda item: (
                item[0].product_id,
                item[0].source_row,
            ),
        )
    ]
    input_sha256 = hashlib.sha256(
        json.dumps(
            input_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CompoundAggregateThresholdEvidence(
        metric=threshold.metric,
        function=threshold.function,
        operator=threshold.operator,
        value=result,
        valid_count=len(inputs),
        source_id=_SOURCE_ID_BY_FAMILY[family],
        as_of_start=min(record.dynamic_as_of for record, _value in inputs),
        as_of_end=max(record.dynamic_as_of for record, _value in inputs),
        input_sha256=input_sha256,
    )


def _base_family_filters(family: ProductFamily) -> tuple[CompoundFilter, ...]:
    if family is ProductFamily.BOND:
        return ()
    if family is ProductFamily.FUND:
        return (CompoundFilter("prvo_pbff_desc", "eq", "공모"),)
    return (CompoundFilter("pd_grp_no", "eq", "ETF"),)


def _family_matches(question: str) -> list[_FamilyScope]:
    """Return the longest non-overlapping explicit scope for each clause.

    Broad family expressions (for example ``국내 ETF``) intentionally coexist
    with field-bearing expressions (``운용사가 X인 국내 ETF``).  Selecting the
    longest overlapping expression preserves the captured hard filter while
    still allowing an unseen literal value.  No value is inferred from a
    neighbouring family clause.
    """

    candidates: list[tuple[int, int, int, _FamilyScope]] = []
    for priority, (family, pattern, filter_field) in enumerate(_FAMILY_PATTERNS):
        for match in pattern.finditer(question):
            captured = match.groupdict().get("value")
            value = None if captured is None else captured.strip()
            if (
                filter_field is None
                and value is not None
                and family
                in {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
                and re.search(r"(?:ETF|ETN|ETP|상장지수상품)", value, re.IGNORECASE)
                is not None
            ):
                # The optional row modifier in ``국내 채권형 ETF`` must not
                # greedily consume the next member of a compact family list
                # (``국내 ETF·해외 ETF`` or ``국내 ETF와해외 ETF``).  Such a
                # capture is a coordination boundary, never an asset label.
                continue
            filters = list(_base_family_filters(family))
            if (
                family in {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
                and re.search(r"ETP|상장지수상품", match.group(0), re.IGNORECASE)
                and re.search(r"ETF|ETN", match.group(0), re.IGNORECASE) is None
            ):
                # ETP is the table-level family and includes both ETF and ETN.
                # Only an explicit ETF/ETN surface may narrow product_type.
                filters = [item for item in filters if item.source_field != "pd_grp_no"]
            if family is ProductFamily.BOND and "장내" in match.group(0):
                filters.append(CompoundFilter("pd_exg_mkt", "eq", "장내"))
            if filter_field is not None and value:
                filters.append(CompoundFilter(filter_field, "eq", value))
            candidates.append(
                (
                    match.start(),
                    match.end(),
                    priority,
                    _FamilyScope(
                        start=match.start(),
                        end=match.end(),
                        product_family=family,
                        filters=tuple(filters),
                        label=value,
                    ),
                )
            )

    selected: list[_FamilyScope] = []
    for _start, _end, _priority, scope in sorted(
        candidates,
        key=lambda item: (item[0], -(item[1] - item[0]), item[2]),
    ):
        if any(scope.start < kept.end and kept.start < scope.end for kept in selected):
            continue
        selected.append(scope)
    selected.sort(key=lambda item: item.start)
    deduplicated: list[_FamilyScope] = []
    for scope in selected:
        if any(
            item.product_family is scope.product_family
            and item.filters == scope.filters
            for item in deduplicated
        ):
            # Repeating the same fully bound cohort as the Korean result noun
            # (``회사채 평균보다 ... 회사채를``) does not introduce a second
            # subquery.  Distinct filters remain separate and fail closed in
            # compilers that cannot represent two same-family cohorts.
            continue
        if (
            any(item.product_family is scope.product_family for item in deduplicated)
            and scope.filters == _base_family_filters(scope.product_family)
            and re.match(
                r"\s*(?:을|를)?\s*(?:\d+|한|두|세|셋|네|넷|다섯|여섯|"
                r"일곱|여덟|아홉|열)\s*(?:개|종목|상품)",
                question[scope.end :],
            )
        ):
            # ``만기가 가까운 채권을 6개`` uses the second family word
            # as the result noun, not as a new cohort.
            continue
        deduplicated.append(scope)
    return deduplicated


def _compound_family_matches(question: str) -> list[_FamilyScope]:
    """Return explicit families, expanding only a proven all-family surface.

    The ordinary router can represent one family word at a time.  Compound
    aggregation/search also needs a deterministic representation for phrases
    such as ``네 상품군``.  Expansion is confined to that exact table-level
    phrase and therefore cannot manufacture a family from an otherwise
    family-less question.
    """

    explicit = _family_matches(question)
    if explicit:
        return explicit
    match = _ALL_FAMILY_PATTERN.search(question)
    if match is None:
        return []
    return [
        _FamilyScope(
            start=match.start(),
            end=match.end(),
            product_family=family,
            filters=(
                ()
                if family in {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
                else _base_family_filters(family)
            ),
            label=family.value,
        )
        for family in ProductFamily
    ]


def _metric_from_label(label: str, family: ProductFamily) -> CompoundMetric | None:
    # Manual aliases cover stable colloquial language, while the Field
    # Registry owns every exact schema label.  Prefer a Registry-owned field
    # on an equal span so a broad alias (``순자산``/AUM) can never capture a
    # longer official field (``순자산총액``), and newly approved sortable
    # fields automatically become available to the structural compiler.
    matches: list[tuple[int, int, int, str]] = []
    for pattern, mapping in _METRIC_ALIASES:
        match = pattern.search(label)
        if match is not None and family in mapping:
            matches.append((match.start(), len(match.group(0)), 0, mapping[family]))
    for mention in registry_field_mentions(label, family.value, "sortable"):
        source_field = _CANONICAL_TO_SOURCE.get((family, mention.field_id))
        if source_field is None:
            continue
        matches.append(
            (
                mention.start,
                mention.end - mention.start,
                1,
                source_field,
            )
        )
    if not matches:
        return None
    _, _, _, source_field = max(matches, key=lambda item: (item[0], item[1], item[2]))
    return CompoundMetric("field", (source_field,))


def _unique_metric_from_label(label: str, family: ProductFamily) -> CompoundMetric | None:
    """Resolve one and only one metric meaning from a structural clause.

    This helper is used only when Korean word order places the result count
    before a shared ranking cue.  Requiring a single source field prevents a
    numeric filter and a different ranking metric from being silently merged.
    """

    source_fields = {
        mapping[family]
        for pattern, mapping in _METRIC_ALIASES
        if family in mapping and pattern.search(label) is not None
    }
    source_fields.update(
        source_field
        for mention in registry_field_mentions(label, family.value, "sortable")
        if (
            source_field := _CANONICAL_TO_SOURCE.get((family, mention.field_id))
        )
        is not None
    )
    if len(source_fields) != 1:
        return None
    return CompoundMetric("field", (next(iter(source_fields)),))


def _unique_numeric_metric_from_label(
    label: str,
    family: ProductFamily,
) -> CompoundMetric | None:
    """Resolve exactly one numeric metric while ignoring text group fields."""

    registry = load_field_registry()
    source_fields: set[str] = set()
    for pattern, mapping in _METRIC_ALIASES:
        source = mapping.get(family)
        canonical = None if source is None else _SOURCE_TO_CANONICAL.get((family, source))
        if (
            source is not None
            and canonical is not None
            and pattern.search(label) is not None
            and registry.require_field(canonical, [family.value]).value_type
            is ValueType.NUMBER
        ):
            source_fields.add(source)
    source_fields.update(
        source
        for mention in registry_field_mentions(
            label,
            family.value,
            "sortable",
            value_types=frozenset({ValueType.NUMBER}),
        )
        if (source := _CANONICAL_TO_SOURCE.get((family, mention.field_id))) is not None
    )
    if len(source_fields) != 1:
        return None
    return CompoundMetric("field", (next(iter(source_fields)),))


def _derived_metric(text: str, family: ProductFamily) -> CompoundMetric | None:
    for pattern, kind, fields, scale in _DERIVED_METRICS:
        if pattern.search(text) is None:
            continue
        if kind == "subtract_domestic" and family is not ProductFamily.DOMESTIC_ETP:
            continue
        if kind == "subtract" and fields == ("fd_yr1_ern_r", "fd_mm3_ern_r"):
            if family is not ProductFamily.FUND:
                continue
        if any((family, field) not in _SOURCE_TO_CANONICAL for field in fields):
            continue
        return CompoundMetric("subtract" if kind == "subtract_domestic" else kind, fields, scale)
    return None


_COORDINATED_RETURN_METRICS = re.compile(
    r"(?P<periods>"
    r"(?:1\s*일|하루|1\s*개월|한\s*달|3\s*개월|한\s*분기|분기|석\s*달|"
    r"6\s*개월|반년|1\s*년|12\s*개월|열두\s*달|3\s*년)"
    r"(?:\s*(?:와|과|및|[·ㆍ,/&+])\s*"
    r"(?:1\s*일|하루|1\s*개월|한\s*달|3\s*개월|한\s*분기|분기|석\s*달|"
    r"6\s*개월|반년|1\s*년|12\s*개월|열두\s*달|3\s*년))+"
    r")\s*(?:수익률|수익|성과|성적)",
    re.IGNORECASE,
)
_RETURN_PERIOD_TOKEN = re.compile(
    r"1\s*일|하루|1\s*개월|한\s*달|3\s*개월|한\s*분기|분기|석\s*달|"
    r"6\s*개월|반년|1\s*년|12\s*개월|열두\s*달|3\s*년",
    re.IGNORECASE,
)


def _ordered_metric_mentions(
    text: str,
    family: ProductFamily,
) -> tuple[tuple[int, int, str], ...]:
    """Return longest owned numeric/sortable metric mentions in text order."""

    found: list[tuple[int, int, int, str]] = []
    for pattern, mapping in _METRIC_ALIASES:
        source = mapping.get(family)
        if source is None:
            continue
        for match in pattern.finditer(text):
            found.append((match.start(), match.end(), 0, source))
    for mention in registry_field_mentions(text, family.value, "sortable"):
        source = _CANONICAL_TO_SOURCE.get((family, mention.field_id))
        if source is not None:
            found.append((mention.start, mention.end, 1, source))
    # Korean coordination can share the trailing measure noun: ``1개월·1년
    # 수익률``.  Resolve each explicitly written period; no omitted period is
    # inferred.  This feeds both exact cross-family projection comparisons and
    # ordinary structural parsing through the same Registry-backed aliases.
    for coordinated in _COORDINATED_RETURN_METRICS.finditer(text):
        periods = coordinated.group("periods")
        for token in _RETURN_PERIOD_TOKEN.finditer(periods):
            metric = _metric_from_label(f"{token.group(0)} 수익률", family)
            if metric is None or metric.kind != "field":
                continue
            start = coordinated.start("periods") + token.start()
            end = coordinated.start("periods") + token.end()
            found.append((start, end, 2, metric.source_fields[0]))
    owned: list[tuple[int, int, int, str]] = []
    for item in found:
        start, end, _priority, source = item
        if any(
            other_source != source
            and other_start <= start
            and end <= other_end
            and (other_end - other_start) > (end - start)
            for other_start, other_end, _other_priority, other_source in found
        ):
            continue
        owned.append(item)
    best: dict[tuple[int, int, str], tuple[int, int, int, str]] = {}
    for item in owned:
        key = (item[0], item[1], item[3])
        previous = best.get(key)
        if previous is None or item[2] > previous[2]:
            best[key] = item
    return tuple(
        (start, end, source)
        for start, end, _priority, source in sorted(
            best.values(), key=lambda item: (item[0], -(item[1] - item[0]), item[3])
        )
    )


def _coordinated_metric_sources_before_function(
    text: str,
    family: ProductFamily,
    function_match: re.Match[str],
    *,
    lower_bound: int,
) -> tuple[str, ...]:
    """Return explicit numeric nouns sharing one trailing aggregate function."""

    raw_mentions = [
        (start, end, source)
        for start, end, source in _ordered_metric_mentions(
            text[lower_bound : function_match.start()],
            family,
        )
        if _numeric_source_field(family, source)
    ]
    mentions: list[tuple[int, int, str]] = []
    for start, end, source in raw_mentions:
        absolute = (lower_bound + start, lower_bound + end, source)
        if mentions and source == mentions[-1][2] and absolute[0] <= mentions[-1][1]:
            if absolute[1] - absolute[0] > mentions[-1][1] - mentions[-1][0]:
                mentions[-1] = absolute
            continue
        mentions.append(absolute)
    if len({source for _start, _end, source in mentions}) < 2:
        return ()
    for left, right in zip(mentions, mentions[1:], strict=False):
        if re.fullmatch(
            r"\s*(?:은|는|이|가|을|를)?\s*"
            r"(?:와|과|및|하고|이랑|랑|[·ㆍ,/&+])\s*",
            text[left[1] : right[0]],
            flags=re.IGNORECASE,
        ) is None:
            return ()
    if re.fullmatch(
        r"\s*(?:의|에\s*대한)?\s*",
        text[mentions[-1][1] : function_match.start()],
        flags=re.IGNORECASE,
    ) is None:
        return ()
    return tuple(dict.fromkeys(source for _start, _end, source in mentions))


_GROUP_MIN_COUNT_PATTERN = re.compile(
    r"(?:유효(?:한)?(?:\s*값)?|표본(?:\s*수)?)"
    r"(?P<cohort>(?:\s*[0-9A-Za-z가-힣·._/-]+){0,6}?)"
    r"\s*(?P<count>\d+)\s*(?:개|건|종목)?\s*이상(?:인)?",
    re.IGNORECASE,
)


def _numeric_source_field(family: ProductFamily, source_field: str) -> bool:
    canonical = _SOURCE_TO_CANONICAL.get((family, source_field))
    return bool(
        canonical is not None
        and load_field_registry()
        .require_field(canonical, [family.value])
        .value_type
        is ValueType.NUMBER
    )


def _group_min_count_spec(
    text: str,
    family: ProductFamily,
) -> tuple[int, str, re.Match[str]] | None:
    """Bind a HAVING count to the nearest explicit numeric field.

    ``표본 20개`` and ``총보수 유효 ETP가 5개`` both count usable
    values of the named metric.  The count never falls back to total product
    rows, and two separate sample bounds remain ambiguous.
    """

    matches = tuple(_GROUP_MIN_COUNT_PATTERN.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    prior_mentions = [
        (start, end, source)
        for start, end, source in _ordered_metric_mentions(text[: match.start()], family)
        if _numeric_source_field(family, source)
    ]
    if not prior_mentions:
        return None
    nearest_end = max(end for _start, end, _source in prior_mentions)
    nearest_sources = {
        source
        for _start, end, source in prior_mentions
        if end == nearest_end and match.start() - end <= 80
    }
    if len(nearest_sources) != 1:
        return None
    count = int(match.group("count"))
    if not 1 <= count <= 100000:
        return None
    return count, next(iter(nearest_sources)), match


_GROUP_RESULT_COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s*(?:개(?:\s*(?:분류|지역|운용사|그룹|항목))?|곳|분류|지역|운용사|그룹|항목)",
    re.IGNORECASE,
)

_GROUP_DIRECTION_PATTERN = re.compile(
    r"상위|하위|높(?:은|게)?|낮(?:은|게)?|큰|작은|많은|적은",
    re.IGNORECASE,
)


def _group_order_spec(
    text: str,
    family: ProductFamily,
    aggregations: tuple[CompoundAggregation, ...],
    *,
    after: int = 0,
) -> tuple[CompoundAggregation, SortDirection, int, bool] | None:
    """Resolve one explicit aggregate-output ordering and group limit.

    The ranking cue must occur after a result-size token and identify exactly
    one requested aggregate.  When a question contains multiple measures,
    a bare ``상위`` cannot silently choose one of them.
    """

    result_counts = [
        match
        for match in _GROUP_RESULT_COUNT_PATTERN.finditer(text)
        if match.start() >= after
    ]
    if not result_counts:
        return None
    result_match = result_counts[-1]
    directions = list(
        _GROUP_DIRECTION_PATTERN.finditer(text[after : result_match.start()])
    )
    if not directions:
        return None
    direction_match = directions[-1]
    direction_surface = direction_match.group(0)
    direction = (
        SortDirection.ASC
        if re.search(r"하위|낮|작은|적은", direction_surface)
        else SortDirection.DESC
    )
    direction_start = after + direction_match.start()
    prefix = text[after:direction_start]
    boundaries = list(
        re.finditer(
            r"[,;]|(?:구하고|구해|내고|산출하고|계산하고)",
            prefix,
        )
    )
    local_start = boundaries[-1].end() if boundaries else max(0, len(prefix) - 80)
    context = prefix[local_start:]

    count_aggregations = tuple(
        item for item in aggregations if item.function is AggregateFunction.COUNT
    )
    non_count = tuple(
        item for item in aggregations if item.function is not AggregateFunction.COUNT
    )
    if re.search(r"(?:상품|종목|펀드)\s*(?:수|개수|건수)", context):
        candidates = count_aggregations
    else:
        function: AggregateFunction | None = None
        for pattern, candidate in (
            (r"평균", AggregateFunction.AVG),
            (r"중앙\s*값|중위수|메디안", AggregateFunction.MEDIAN),
            (r"합계|총합", AggregateFunction.SUM),
            (r"최솟값|최소값", AggregateFunction.MIN),
            (r"최햓값|최대값|최고값", AggregateFunction.MAX),
        ):
            if re.search(pattern, context, re.IGNORECASE):
                if function is not None and function is not candidate:
                    return None
                function = candidate
        candidates = tuple(
            item for item in non_count if function is None or item.function is function
        )
        metric_mentions = [
            source
            for _start, _end, source in _ordered_metric_mentions(context, family)
            if _numeric_source_field(family, source)
        ]
        if metric_mentions:
            owned_source = metric_mentions[-1]
            candidates = tuple(
                item for item in candidates if item.source_field == owned_source
            )
    candidates = tuple(dict.fromkeys(candidates))
    if len(candidates) != 1:
        return None
    limit = int(result_match.group("count"))
    if not 1 <= limit <= 100:
        return None
    selected = candidates[0]
    return selected, direction, limit, selected.function is AggregateFunction.COUNT


_KOREAN_SMALL_COUNTS = {
    "한": 1,
    "두": 2,
    "세": 3,
    "셋": 3,
    "네": 4,
    "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}


def _closed_named_set_count(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
    *,
    logical_product_count: int,
) -> int | None:
    """Return the explicitly closed identity-set size, never an open cohort.

    Two safe surfaces are supported: an explicit ``후보를 이 세 상품으로만
    한정`` declaration, and the ordinary ``A와 B 중`` comparison grammar.
    Identity text is masked first so a number inside an official product name
    cannot become the set cardinality.
    """

    if not identity_spans:
        return None
    masked = _mask_anchor_identity_surfaces(question, identity_spans)
    count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    explicit = re.search(
        rf"(?:후보|대상)(?:를|은|는)?\s*(?:이|다음|아래|위)?\s*"
        rf"{count}\s*(?:개\s*)?(?:상품|종목|펀드|ETF|ETN|ETP|채권)"
        r"(?:으?로)?만\s*(?:한정|제한)",
        masked,
        re.IGNORECASE,
    )
    if explicit is not None:
        raw = explicit.group("count")
        return int(raw) if raw.isdigit() else _KOREAN_SMALL_COUNTS[raw]

    first_start = min(start for start, _end in identity_spans)
    last_end = max(end for _start, end in identity_spans)
    if re.match(r"\s*(?:중|끼리)(?![가-힣])", question[last_end:]) is not None:
        return logical_product_count
    # ``A하고 B를 비교`` is a closed set when the DB matcher resolved
    # exactly two logical products, only a coordination particle remains
    # between their identity groups, and the second identity is followed
    # directly by a compare verb. Open-set markers such as ``등`` or
    # ``포함`` deliberately do not match this grammar.
    if logical_product_count == 2:
        between = masked[first_start:last_end]
        compact_between = re.sub(
            r"[\s()\[\]{},.:;/'\"`‘’“”「」『』_\-]+",
            "",
            between,
        )
        comparison = re.match(
            r"[\s()\[\]{},.:;/'\"`‘’“”「」『』_\-]*"
            r"(?:을|를|은|는)?\s*(?:서로\s*)?(?:비교|대조)",
            question[last_end:],
            re.IGNORECASE,
        )
        if compact_between in {"와", "과", "하고", "및", "랑", "이랑"} and (
            comparison is not None
        ):
            trailing_clause = question[last_end + comparison.end() :]
            open_extension = re.search(
                r"(?:등(?:도|을|이|과|와)?(?:\s|$)|포함(?:해|하|해서|하고|한)?|그\s*외|"
                r"다른\s*(?:상품|종목|펀드|ETF|ETN|ETP|채권)(?:도|을|를|과|와)?|"
                r"추가(?:로|적인)?\s*(?:상품|종목|펀드|ETF|ETN|ETP|채권)?)",
                trailing_clause,
                re.IGNORECASE,
            )
            if open_extension is None:
                return 2
    closed_suffix = re.match(
        rf"\s*(?:{count}\s*(?:개\s*)?"
        r"(?:상품|종목|펀드|ETF|ETN|ETP|채권))?\s*만\s*"
        r"(?:놓고|대상(?:으로)?|비교|대조|한정|제한)",
        question[last_end:],
        re.IGNORECASE,
    )
    if closed_suffix is not None:
        raw = closed_suffix.group("count")
        if raw is not None:
            return int(raw) if raw.isdigit() else _KOREAN_SMALL_COUNTS[raw]
        declared = re.search(
            rf"(?:다음|이|아래|위)?\s*{count}\s*(?:개\s*)?"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)[^.;:!?]{0,40}$",
            question[:first_start],
            re.IGNORECASE,
        )
        if declared is not None:
            declared_raw = declared.group("count")
            return (
                int(declared_raw)
                if declared_raw.isdigit()
                else _KOREAN_SMALL_COUNTS[declared_raw]
            )
        return logical_product_count
    return None


def _has_grouped_product_topk_surface(text: str) -> bool:
    """Prove that a TOP-K is requested *inside every group*.

    A plain ``운용사별 평균 상위 5개`` orders aggregate rows and must stay
    on the grouped-aggregate compiler.  Product words, ordinal ranks, ``각``
    or the distributive suffix ``씩`` are therefore required here.
    """

    return bool(
        re.search(r"(?:개|상품|종목)\s*씩", text)
        or re.search(
            r"각\s*[^,;]{0,40}?(?:상위|하위|최고|최저|"
            r"높은|낮은|큰|작은|\d+(?:\s*[·ㆍ,/]\s*\d+)*\s*위)",
            text,
        )
        or re.search(
            r"(?:별(?:로)?|마다).{0,100}?"
            r"(?:상위|하위|최고|최저|높은|낮은|큰|작은).{0,24}?"
            r"(?:상품|종목)",
            text,
        )
        or re.search(
            r"(?:별(?:로)?|마다).{0,100}?"
            r"\d+(?:\s*[·ㆍ,/]\s*\d+)*\s*위",
            text,
        )
    )


def _group_product_rank_spec(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    """Resolve a metric and per-group TOP-K without inventing either.

    The ordinary ranking grammar covers common ``상위 두 상품`` forms.
    This extension only adds explicit 최고/최저 and ordinal surfaces such as
    ``1·2위``.  Ordinals must start at 1 and be contiguous.
    """

    ordinary = _rank_spec(text, family)
    if ordinary is not None:
        return ordinary
    count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    extrema = list(
        re.finditer(
            rf"(?P<label>.{{1,120}}?)(?P<cue>최고|최저)\s*{count}\s*"
            r"(?:개|종목|상품)?(?:씩)?",
            text,
            re.IGNORECASE,
        )
    )
    if extrema:
        match = extrema[-1]
        metric = _metric_from_label(match.group("label"), family)
        raw_count = match.group("count")
        limit = int(raw_count) if raw_count.isdigit() else _KOREAN_SMALL_COUNTS[raw_count]
        if metric is None or not 1 <= limit <= 100:
            return None
        direction = (
            SortDirection.ASC if match.group("cue") == "최저" else SortDirection.DESC
        )
        return metric, direction, limit

    ordinal_matches = list(
        re.finditer(
            r"(?P<label>.{1,140}?)(?P<ranks>\d+(?:\s*[·ㆍ,/]\s*\d+)*)\s*위",
            text,
            re.IGNORECASE,
        )
    )
    if not ordinal_matches:
        return None
    match = ordinal_matches[-1]
    ranks = tuple(int(value) for value in re.findall(r"\d+", match.group("ranks")))
    if not ranks or tuple(dict.fromkeys(ranks)) != tuple(range(1, max(ranks) + 1)):
        return None
    metric = _metric_from_label(match.group("label"), family)
    if metric is None or max(ranks) > 100:
        return None
    return metric, SortDirection.DESC, max(ranks)


def _explicit_group_values(
    text: str,
    family: ProductFamily,
    source_field: str,
) -> tuple[str, ...]:
    """Return two or more literal enum buckets explicitly listed by user."""

    canonical = _SOURCE_TO_CANONICAL.get((family, source_field))
    if canonical is None:
        return ()
    definition = load_field_registry().require_field(canonical, [family.value])
    if not definition.enum_values:
        return ()
    found: list[tuple[int, str]] = []
    for value in sorted(definition.enum_values, key=len, reverse=True):
        match = re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(value)}(?![0-9A-Za-z가-힣])",
            text,
            re.IGNORECASE,
        )
        if match is not None:
            found.append((match.start(), value))
    values = tuple(dict.fromkeys(value for _start, value in sorted(found)))
    return values if len(values) >= 2 else ()


def _field_comparison_spec(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, tuple[str, str, str], SortDirection, int] | None:
    """Parse one row-field comparison followed by a deterministic gap rank.

    The compared fields must both appear verbatim, the relational word must
    sit between the second field and the family/result noun, and the user must
    explicitly request the size and direction of the *difference*.  No field
    or subtraction orientation is inferred from a generic ``좋은`` cue.
    """

    mentions = _ordered_metric_mentions(text, family)
    # Keep the first occurrence of each source field.  Repeated references in
    # the final ranking phrase do not create a third operand.
    distinct: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for mention in mentions:
        if mention[2] not in seen:
            seen.add(mention[2])
            distinct.append(mention)
    if len(distinct) != 2:
        return None
    left, right = distinct
    relation = re.match(
        r"\s*(?:보다|에\s*비해)\s*"
        r"(?P<cue>높은|낮은|큰|작은|많은|적은|긴|짧은)",
        text[right[1] :],
    )
    if relation is None:
        return None
    operator = (
        "gt"
        if relation.group("cue") in {"높은", "큰", "많은", "긴"}
        else "lt"
    )
    rank = re.search(
        r"(?:그\s*)?(?:두\s*)?(?:수익률|성과|수치|값|지표)?\s*"
        r"(?:차이|격차)(?:가|이)?\s*"
        r"(?P<cue>큰|작은|높은|낮은)\s*순(?:으로)?\s*"
        r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
        r"(?:개|종목|상품)",
        text,
    )
    if rank is None:
        return None
    counts = {
        "한": 1,
        "두": 2,
        "세": 3,
        "셋": 3,
        "네": 4,
        "넷": 4,
        "다섯": 5,
        "여섯": 6,
        "일곱": 7,
        "여덟": 8,
        "아홉": 9,
        "열": 10,
    }
    raw_count = rank.group("count")
    limit = int(raw_count) if raw_count.isdigit() else counts[raw_count]
    direction = (
        SortDirection.DESC
        if rank.group("cue") in {"큰", "높은"}
        else SortDirection.ASC
    )
    # Always expose a positive magnitude for the asserted relation.  If
    # left < right the gap is right-left; if left > right it is left-right.
    gap_sources = (left[2], right[2]) if operator == "gt" else (right[2], left[2])
    return (
        CompoundMetric("subtract", gap_sources),
        (left[2], operator, right[2]),
        direction,
        limit,
    )


def _direction(text: str, *, default: SortDirection = SortDirection.DESC) -> SortDirection:
    matches = list(
        re.finditer(
            r"(?:(큰|높은|많은|빠른|가까운|작은|낮은|적은|늦은|먼)"
            r"\s*순(?:서)?(?:대로|으로)?|"
            r"(내림차순|오름차순))",
            text,
        )
    )
    if not matches:
        return default
    cue = matches[-1].group(1) or matches[-1].group(2)
    return (
        SortDirection.ASC
        if cue in {"작은", "낮은", "적은", "빠른", "가까운", "오름차순"}
        else SortDirection.DESC
    )


def _final_limit(text: str) -> int | None:
    count = r"(\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    patterns = (
        rf"순(?:서)?(?:대로|으로)?\s*(?:최대\s*)?{count}\s*개(?:씩)?",
        rf"(?:큰|높은|작은|낮은)\s*(?:값|것|상품)?부터\s*(?:최대\s*)?{count}\s*개(?:씩)?",
        rf"최대\s*{count}\s*개(?:씩)?",
        rf"{count}\s*개(?:씩|만|를|를\s*별도|를\s*따로|를\s*비교표|를\s*골라|를\s*뽑|를\s*남겨)",
        rf"{count}\s*(?:개(?!월)|종목|상품(?!군))"
        r"(?=\s*(?:씩|만|은|는|이|가|을|를|으로|에는|보여|알려|찾아|"
        r"골라|선정|제시|나열|조회|정리|$))",
    )
    count_values = {
        "한": 1,
        "두": 2,
        "세": 3,
        "셋": 3,
        "네": 4,
        "넷": 4,
        "다섯": 5,
        "여섯": 6,
        "일곱": 7,
        "여덟": 8,
        "아홉": 9,
        "열": 10,
    }
    values: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw = match.group(1)
            values.append((match.start(), int(raw) if raw.isdigit() else count_values[raw]))
    return max(values, key=lambda item: item[0])[1] if values else None


def _numeric_filters(text: str, family: ProductFamily) -> tuple[CompoundFilter, ...]:
    """Extract explicit field/value bounds without guessing a missing field.

    The field label must be one of the frozen metric aliases and the numeric
    operator must immediately follow it.  This intentionally does not accept a
    free-floating number, so a presentation sentence cannot become a filter.
    """

    operators = {"이상": "gte", "이하": "lte", "초과": "gt", "미만": "lt"}
    prefix_operators = {"최소": "gte", "적어도": "gte", "최대": "lte", "많아야": "lte"}
    found: list[tuple[int, CompoundFilter]] = []
    for pattern, mapping in _METRIC_ALIASES:
        source_field = mapping.get(family)
        if source_field is None:
            continue
        for alias in pattern.finditer(text):
            suffix = text[alias.end() :]
            bound = re.match(
                r"\s*(?:이|가)?\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*%?\s*"
                r"(이상|이하|초과|미만)",
                suffix,
            )
            prefix_bound = re.match(
                r"\s*(?:이|가)?\s*(최소|최대|적어도|많아야)\s*"
                r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%?",
                suffix,
            )
            if bound is None and prefix_bound is None:
                continue
            if bound is not None:
                raw_value = bound.group(1).replace(",", "")
                operator = operators[bound.group(2)]
            else:
                assert prefix_bound is not None
                raw_value = prefix_bound.group(2).replace(",", "")
                operator = prefix_operators[prefix_bound.group(1)]
            value: int | float = float(raw_value) if "." in raw_value else int(raw_value)
            found.append(
                (
                    alias.start(),
                    CompoundFilter(source_field, operator, value),
                )
            )
    found.sort(key=lambda item: item[0])
    unique: list[CompoundFilter] = []
    seen: set[tuple[str, str, str]] = set()
    for _, item in found:
        key = (item.source_field, item.operator, str(item.value))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def _linked_clause_filters(
    text: str,
    family: ProductFamily,
    *,
    extra_represented_source_fields: tuple[str, ...] = (),
    allow_owned_anchor_comparison: bool = False,
) -> tuple[CompoundFilter, ...] | None:
    """Reuse the single-family Linker instead of maintaining a second grammar.

    A cross-family clause must preserve every deterministic hard filter that
    the ordinary QueryPlan compiler would lock.  ``None`` means the clause
    contains a known unsupported/ambiguous condition and therefore cannot be
    safely reduced to a Compound plan.
    """

    from finance_agent_core.agent.linker import build_lexical_hints

    # Output ordering is presentation metadata, not a row predicate.  In a
    # coordinated request such as ``ETF 다음 펀드 순`` the bare ``ETF`` used
    # to be re-read as ``product_type=ETF`` inside the fund clause and made an
    # otherwise complete plan fail.  Remove only a fully explicit family
    # sequence; metric ranking phrases remain untouched.
    semantic_text = re.sub(
        r"(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드)\s*"
        r"(?:다음|먼저)\s*(?:국내\s*|해외\s*)?"
        r"(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드)\s*순(?:서)?(?:대로|으로)?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    hints = build_lexical_hints(
        semantic_text,
        family.value,
        force_product_family_hint=True,
    )
    if hints["unsupported_spans"]:
        # The common single-plan linker deliberately rejects comparisons
        # between two row fields and reference-product comparisons because it
        # cannot encode either shape.  The dedicated anchor compiler *can*
        # encode the latter, but it may close that representation gap only
        # after it has positively parsed every mentioned metric into the
        # frozen relative/ranking fields supplied by its caller.
        represented = set(extra_represented_source_fields)
        for span in hints["unsupported_spans"]:
            mentioned = {
                mapping[family]
                for pattern, mapping in _METRIC_ALIASES
                if family in mapping and pattern.search(span) is not None
            }
            mentioned.update(
                mapping[family]
                for pattern, mapping in _ANCHOR_EQUAL_FIELD_ALIASES
                if family in mapping and pattern.search(span) is not None
            )
            comparison_cue = re.search(
                r"(?:보다|대비|웃돌|넘는|초과|밑돌|밑도|미만|이상|이하|"
                r"더\s*(?:높|낮|크|작|많|적|짧|길)|"
                r"(?:높|낮|크|작|많|적|짧|길)(?:고|은|는)|같|동일)",
                span,
            )
            if (
                not allow_owned_anchor_comparison
                or not mentioned
                or not mentioned.issubset(represented)
                or (comparison_cue is None and len(mentioned) < 2)
            ):
                return None
    represented_fields = {
        str(item["field"])
        for item in (
            *hints["required_constraints"],
            *hints["required_rankings"],
        )
    }
    represented_fields.update(
        canonical
        for source_field in extra_represented_source_fields
        if (canonical := _SOURCE_TO_CANONICAL.get((family, source_field))) is not None
    )
    # Detect an explicit Registry field used as a filter but unavailable (or
    # left unparsed) in this particular family clause.  This prevents a valid
    # filter in the first subquery from falsely "covering" the same words in a
    # second family, for example domestic risk_level followed by an overseas
    # risk_level clause where that field does not exist.
    registry = load_field_registry()
    represented_source_columns: set[tuple[str, ...]] = set()
    for field_id in represented_fields:
        definition = registry.fields.get(field_id)
        if definition is not None and family.value in definition.datasets:
            represented_source_columns.add(
                tuple(definition.resolve(family.value).source.columns)
            )
    ignored_identity_fields = {
        "product_id",
        "product_name",
        "product_family",
        "ticker",
        "short_name",
        "isin",
    }
    filter_suffix = re.compile(
        r"^\s*(?:이|가|은|는|을|를)?\s*(?:"
        r"[-+]?\d[\d,.]*\s*%?\s*(?:이상|이하|초과|미만)|"
        r"[^,.;]{1,32}?(?:인|아닌)\s*(?:상품|종목|펀드|채권|ETF|ETN)|"
        r"(?:가능|불가|불가능|중지|정지)|"
        r"[^,.;]{0,24}?(?:제외|빼고|말고|포함))"
    )
    for field_id, definition in registry.fields.items():
        if field_id in ignored_identity_fields:
            continue
        surfaces = (definition.label, *definition.aliases)
        for surface in surfaces:
            compact = "".join(surface.split())
            if len(compact) < 3:
                continue
            pattern = re.compile(
                r"\s*".join(re.escape(part) for part in surface.split()),
                re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                if filter_suffix.search(text[match.end() :]) is None:
                    continue
                field_is_represented = field_id in represented_fields
                if family.value in definition.datasets and not field_is_represented:
                    field_is_represented = (
                        tuple(definition.resolve(family.value).source.columns)
                        in represented_source_columns
                    )
                if family.value not in definition.datasets or not field_is_represented:
                    return None
                break
    # Ambiguous semantic spans are deliberately not discarded here.  This
    # helper only preserves deterministic filters.  The authorization bridge
    # receives the full SemanticCoverageGate residual list and refuses a
    # receipt unless every span is positively represented by the frozen plan.
    filters: list[CompoundFilter] = []
    for item in hints["required_constraints"]:
        canonical = str(item["field"])
        # A quoted/labeled product-name literal owns its content.  A token
        # such as ``미국`` or ``글로벌`` inside that literal must not also
        # become an investment-region filter merely because it is a known
        # ontology alias.
        owned_name = re.search(
            r"(?:상품\s*이름|상품명|종목명|펀드명|ETF\s*명|ETN\s*명|"
            r"채권명|이름)(?:에|에는|에서)\s*['‘“\"]?"
            r"(?P<value>[^'’”\"\n]{1,80}?)['’”\"]?\s*(?:이|가)?\s*"
            r"(?:포함|들어|적힌)",
            text,
            re.IGNORECASE,
        )
        if (
            canonical != "product_name"
            and owned_name is not None
            and any(
                alias.field_id == canonical
                and alias.value == item.get("value")
                and owned_name.start() <= alias.start
                and alias.end <= owned_name.end()
                for alias in filter_alias_matches(text, family)
            )
        ):
            continue
        source = _CANONICAL_TO_SOURCE.get((family, canonical))
        definition = registry.require_field(canonical, [family.value])
        if source is None:
            if len(definition.source.columns) == 1:
                candidate = definition.source.columns[0]
                if (family, candidate) in _SOURCE_TO_CANONICAL:
                    source = candidate
        if source is None:
            return None
        operator = str(item["operator"])
        if operator not in {
            "eq",
            "neq",
            "gt",
            "gte",
            "lt",
            "lte",
            "in",
            "between",
            "contains",
        }:
            return None
        value = item["value"]
        if isinstance(value, list):
            value = tuple(value)
        if definition.source.value_map:
            def raw_codes(
                canonical_value: object,
                field_definition: FieldDefinition = definition,
            ) -> tuple[str, ...]:
                return tuple(
                    raw
                    for raw, normalized in field_definition.source.value_map.items()
                    if normalized == canonical_value
                )

            if isinstance(value, tuple):
                groups = tuple(raw_codes(item) for item in value)
                if any(not item for item in groups):
                    return None
                value = tuple(dict.fromkeys(raw for group in groups for raw in group))
                operator = "in"
            else:
                converted = raw_codes(value)
                if not converted:
                    return None
                if len(converted) == 1:
                    value = converted[0]
                elif operator == "eq":
                    value = converted
                    operator = "in"
                else:
                    return None
        filters.append(CompoundFilter(source, operator, value))
    return tuple(filters)


def _merge_clause_filters(
    scope_filters: tuple[CompoundFilter, ...],
    linked_filters: tuple[CompoundFilter, ...],
    *,
    text: str,
    family: ProductFamily,
) -> tuple[CompoundFilter, ...] | None:
    range_operators = {"gt", "gte", "lt", "lte"}
    filters = list(scope_filters)
    if family in {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP} and re.search(
        r"(?:ETF\s*(?:와|과|및|[·/&])\s*ETN|ETN\s*(?:와|과|및|[·/&])\s*ETF)",
        text,
        re.IGNORECASE,
    ):
        filters = [item for item in filters if item.source_field != "pd_grp_no"]
        filters.append(CompoundFilter("pd_grp_no", "in", ("ETF", "ETN")))
    for linked in linked_filters:
        same_field = [item for item in filters if item.source_field == linked.source_field]
        if (
            family is ProductFamily.FUND
            and linked.source_field == "prvo_pbff_desc"
            and linked.operator == "eq"
            and linked.value is True
            and any(item.operator == "eq" and item.value == "공모" for item in same_field)
        ):
            # The family scope stores the approved raw workbook code while the
            # common Linker exposes its canonical boolean.  They are the same
            # locked predicate and must not be treated as a conflict.
            continue
        if same_field and all(item != linked for item in same_field):
            # A precise Linker result replaces only the generic family base
            # restriction. Independent numeric bounds on one field are also
            # safe to intersect (for example ``0.2 이상`` plus the complement
            # of ``0.8 초과분 제외``). Any other conflict is an unsafe clause.
            if linked.source_field == "pd_grp_no" and all(
                item.operator == "eq" and item.value == "ETF" for item in same_field
            ):
                filters = [item for item in filters if item.source_field != "pd_grp_no"]
            elif (
                linked.source_field == "cu_fund_mgmt_co"
                and linked.operator == "contains"
                and isinstance(linked.value, str)
                and all(
                    item.operator == "eq"
                    and isinstance(item.value, str)
                    and item.value.strip().casefold()
                    == linked.value.strip().casefold()
                    for item in same_field
                )
            ):
                # ``X 운용 국내 ETF`` explicitly labels X as the manager
                # value.  Keep that exact family-scope predicate.  Replacing
                # it with CONTAINS also admits malformed manager cells that
                # merely begin with the brand and then contain the whole
                # product name, changing both the candidate universe and the
                # ranking.  Free-form ``운용사에 X가 포함`` wording remains
                # owned by the Linker and may still compile to CONTAINS.
                continue
            elif linked.operator in range_operators and all(
                item.operator in range_operators for item in same_field
            ):
                pass
            else:
                return None
        if linked not in filters:
            filters.append(linked)
    # Exclusion predicates describe the complement of the mentioned set.
    # Linker hints intentionally preserve lexical mentions, so compound
    # execution performs this last, clause-local polarity check before
    # granting authority.  This is generic grammar: no product or fixture
    # identity is encoded here.
    exclusion_cue = r"(?:제외(?:하고|한|해)?|빼고|걷어내고|배제(?:하고|한|해)?)"
    if family in {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}:
        if re.search(rf"레버리지(?:형|\s*상품|\s*종목)?.{{0,32}}?{exclusion_cue}", text):
            filters = [item for item in filters if item.source_field != "cu_lev_fector"]
            filters.append(CompoundFilter("cu_lev_fector", "lte", 1))
        if re.search(
            rf"거래\s*(?:정지|중지)(?:\s*상품|\s*종목)?.{{0,32}}?{exclusion_cue}",
            text,
        ):
            filters = [item for item in filters if item.source_field != "pd_tr_yn"]
            filters.append(CompoundFilter("pd_tr_yn", "eq", "0"))

    inverse_operator = {"gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt"}
    for pattern, mapping in _METRIC_ALIASES:
        source_field = mapping.get(family)
        if source_field is None:
            continue
        for alias in pattern.finditer(text):
            excluded = re.match(
                rf"\s*(?:이|가|은|는)?\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*%?\s*"
                rf"(이상|이하|초과|미만)(?:분|값|상품|종목)?(?:은|는|을|를)?\s*{exclusion_cue}",
                text[alias.end() :],
            )
            if excluded is None:
                continue
            raw = excluded.group(1).replace(",", "")
            value: int | float = float(raw) if "." in raw else int(raw)
            source_operator = {
                "이상": "gte",
                "이하": "lte",
                "초과": "gt",
                "미만": "lt",
            }[excluded.group(2)]
            # Remove only the positive lexical predicate that described the
            # excluded set. Removing every bound for this field would silently
            # widen a request that also supplied an independent lower/upper
            # bound in the same clause.
            excluded_filter = CompoundFilter(source_field, source_operator, value)
            filters = [item for item in filters if item != excluded_filter]
            complement = CompoundFilter(
                source_field,
                inverse_operator[source_operator],
                value,
            )
            if complement not in filters:
                filters.append(complement)
    return tuple(filters)


def _compiled_clause_filters(
    scope: _FamilyScope,
    text: str,
    *,
    extra_represented_source_fields: tuple[str, ...] = (),
    allow_owned_anchor_comparison: bool = False,
) -> tuple[CompoundFilter, ...] | None:
    linked = _linked_clause_filters(
        text,
        scope.product_family,
        extra_represented_source_fields=extra_represented_source_fields,
        allow_owned_anchor_comparison=allow_owned_anchor_comparison,
    )
    if linked is None:
        return None
    merged = _merge_clause_filters(
        scope.filters,
        linked,
        text=text,
        family=scope.product_family,
    )
    if merged is None or scope.product_family is not ProductFamily.BOND:
        return merged

    # Rating agencies expose +/- sub-grades while users also refer to the
    # parent band (``AA 이상``).  Treat the parent band as the whole AA
    # category instead of passing the non-existent literal ``AA`` to SQL.
    # Explicit sub-grades such as ``AA- 이상`` remain owned by the common
    # Linker.  The registry order is the sole source of stronger/weaker order.
    parent_bound = re.search(
        r"(?:신용\s*등급(?:이|가|은|는)?\s*)?"
        r"(AAA|AA|A|BBB|BB|B)(?![+0-])\s*(이상|이하|초과|미만)",
        text,
        re.IGNORECASE,
    )
    if parent_bound is None:
        return merged
    registry = load_field_registry()
    ratings = list(registry.require_field("credit_rating", ["bond"]).enum_values)
    group = parent_bound.group(1).upper()
    group_indexes = [
        index
        for index, rating in enumerate(ratings)
        if rating == group or re.fullmatch(rf"{re.escape(group)}[+0-]", rating)
    ]
    if not group_indexes:
        return None
    first, last = min(group_indexes), max(group_indexes)
    boundary = parent_bound.group(2)
    if boundary == "이상":
        selected = ratings[: last + 1]
    elif boundary == "초과":
        selected = ratings[:first]
    elif boundary == "이하":
        selected = ratings[first:]
    else:
        selected = ratings[last + 1 :]
    if not selected:
        return None
    without_credit = tuple(item for item in merged if item.source_field != "crd_grd")
    return (*without_credit, CompoundFilter("crd_grd", "in", tuple(selected)))


def _shortlist_spec(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    parsed = _shortlist_spec_with_span(text, family)
    return None if parsed is None else parsed[:3]


_COMPLETE_CALCULATION_INPUTS = re.compile(
    r"(?:"
    r"계산\s*입력값|"
    r"계산(?:에)?\s*(?:쓸|사용할|필요한)\s*값|"
    r"산출(?:에)?\s*(?:쓸|사용할|필요한)?\s*(?:입력)?값"
    r")\s*(?:이|가)?\s*(?:모두|전부)\s*"
    r"(?:있는|존재하는|갖춘|갖춰진)"
)


def _with_complete_calculation_inputs(
    filters: tuple[CompoundFilter, ...],
    text: str,
    *metrics: CompoundMetric,
) -> tuple[CompoundFilter, ...]:
    """Seal an explicit all-inputs-present request into the executable plan.

    Missing and numeric zero are unavailable under the organizer's data rule.
    When the user explicitly asks for products with every calculation input,
    those values must therefore be filtered before the first shortlist.  The
    signed ``neq 0`` filters also let the independent verifier reproduce that
    ordering without interpreting prose again.
    """

    if _COMPLETE_CALCULATION_INPUTS.search(text) is None:
        return filters
    result = list(filters)
    for field in dict.fromkeys(
        source_field for metric in metrics for source_field in metric.source_fields
    ):
        availability = CompoundFilter(field, "neq", 0)
        if availability not in result:
            result.append(availability)
    return tuple(result)


_STAGED_COUNT_SURFACE = re.compile(
    r"(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
    # ``3개월 수익률`` is a period, not a three-product shortlist.
    # The missing lexical boundary used to pair that prefix with the final
    # ``상위 3개`` and force an ordinary filter+rank query into the
    # two-stage compiler, producing a false CLARIFY.
    r"(?:개(?!월|년)|종목|상품)"
)

# Only syntax which the canonical two-stage parser may consume is removable
# from an HCX ``UNRESOLVED`` span.  Field/filter literals are subtracted
# separately below; any remaining content word is therefore an unowned
# condition and keeps the request fail-closed.
_TWO_STAGE_STRUCTURAL_SURFACE = re.compile(
    r"(?:상위|하위|최상단|최하단|높은|낮은|큰|작은|많은|적은|짧은|긴|"
    r"가까운|먼(?!저)|빠른|늦은)(?:\s*순(?:서)?(?:대로|으로)?)?|"
    r"(?:내림차순|오름차순)(?:으로)?|"
    r"(?:먼저|우선|1\s*(?:차|단계)|예선|후보(?:로|를)?)|"
    r"(?:추린|추리고|추려|고른|고르고|골라|뽑은|뽑고|뽑아|"
    r"선별한|선별하고|남긴|남기고|남겨|거른|거르고|걸러|"
    r"좁힌|좁히고|한정한|한정하고|줄인|줄이고|만든|만들고)|"
    r"(?:그\s*(?:중|안|집합)|이\s*(?:안|후보|집합)|"
    r"가운데서|안에서|중에서|중)|"
    r"(?:다음|뒤|후)(?:에)?|"
    r"(?:고르되|골라내되|뽑되|선택하되|선정하되|선별하되|정리하되)|"
    r"(?:다시\s*)?(?:골라|고르|뽑아|뽑|선정|선별|추려|추리|"
    r"남겨|남기|보여|알려|찾아|제시|나열|정리|조회|선택)"
    r"(?:\s*(?:줘|주세요|주십시오|달라|세요|해\s*주세요))?|"
    r"(?:상품|종목|결과|목록|리스트)|"
    r"(?:각각|상품군별로|서로\s*다른\s*계산)|"
    r"(?:첫째|둘째|셋째|넷째)",
    re.IGNORECASE,
)
_TWO_STAGE_GRAMMAR_ONLY_TOKEN = re.compile(
    r"(?:이|가|은|는|을|를|의|에|에서|에는|으로|로|와|과|및|하고|"
    r"이고|이며|인|인데|것|만|씩|도|부터|까지|해|주세요|주십시오|"
    r"수행|수행해|하지|마세요)",
    re.IGNORECASE,
)
_STAGED_TRANSITION_SURFACE = re.compile(
    r"(?:만\s*)?(?:먼저|우선|1\s*(?:차|단계)|예선|후보(?:로|를)?|"
    r"(?:뽑은|뽑고|뽑아|추린|추리고|추려|고른|고르고|골라|"
    r"선별한|선별하고|남긴|남기고|남겨|거른|거르고|걸러|"
    r"좁힌|좁히고|한정한|한정하고|줄인|줄이고|만든|만들고)|"
    r"그\s*(?:중|안|집합)|이\s*(?:안|후보|집합)|안에서|가운데서|중(?:에서)?)"
)
_SHORTLIST_ORDER_SURFACE = r"순(?:서(?:대로|로)?|으로)?"


def requires_typed_two_stage_plan(question: str) -> bool:
    """Return whether two explicit result sizes form one staged request.

    The service and compiler share this predicate.  Consequently, a syntax
    miss cannot fall back to a one-stage SEARCH that silently discards either
    the first shortlist or the final ranking.
    """

    def has_staged_counts(text: str) -> bool:
        counts = list(_STAGED_COUNT_SURFACE.finditer(text))
        if len(counts) < 2:
            return False
        between = text[counts[0].end() : counts[-1].start()]
        prefix = text[: counts[0].start()]
        return (
            _STAGED_TRANSITION_SURFACE.search(between) is not None
            or (
                _STAGED_TRANSITION_SURFACE.search(prefix) is not None
                and re.search(
                    r"(?:후보|먼저|우선|1\s*(?:차|단계))",
                    between,
                )
                is not None
            )
        )

    family_scopes = _compound_family_matches(question)
    if len({item.product_family for item in family_scopes}) <= 1:
        return has_staged_counts(question)

    # In a multi-family request, counts in different one-stage clauses must
    # never be paired into an invented shortlist.  Conversely, one or more
    # clauses may explicitly contain their own ``top N -> top K`` stages.  In
    # that case force the atomic two-stage compiler: it either preserves a
    # complete stage for every family or rejects the whole request, preventing
    # the generic parallel path from silently dropping a shortlist.
    return any(has_staged_counts(segment) for _scope, segment in _plan_segments(question))


def _shortlist_spec_with_span(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection, int, int] | None:
    count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    transition = re.compile(
        r"^.{0,72}?(?:먼저|우선|1\s*(?:차|단계)|예선|후보(?:로|를)|"
        r"(?:추린|고른|선별한|남긴|거른|좁힌|한정한|줄인|만든)|"
        r"(?:추리|고르|선별|남기|거르|좁히|한정|줄이|만들|뽑)(?:고|고서|어|어서)?|"
        r"그\s*(?:집합|중|안)|이\s*(?:집합|후보)|안에서|가운데서|"
        r"중(?:에서|에|에는)?)"
    )
    count_values = {
        "한": 1,
        "두": 2,
        "세": 3,
        "셋": 3,
        "네": 4,
        "넷": 4,
        "다섯": 5,
        "여섯": 6,
        "일곱": 7,
        "여덟": 8,
        "아홉": 9,
        "열": 10,
    }
    patterns = (
        re.compile(
            rf"(?P<label>.{{1,140}}?)(?P<order>상위|하위|최상단|최하단)\s*"
            rf"{count}\s*(?:개|종목|상품)"
        ),
        # A bare ``metric 기준 N개`` does not say whether the shortlist is
        # the high or low tail.  Older code silently defaulted to DESC, which
        # changed an underspecified request into an executable plan.  Only
        # patterns carrying an explicit order/cue are admitted here.
        re.compile(
            rf"(?P<label>.{{1,140}}?)(?:이|가|은|는)?\s*"
            r"(?P<cue>큰|작은|높은|낮은|많은|적은|짧은|긴|빠른|늦은)\s*"
            rf"(?:{_SHORTLIST_ORDER_SURFACE}\s*)?"
            r"(?:장내\s*)?(?:국내\s*채권|원화\s*채권|채권|국내\s*ETF|"
            r"국내\s*(?:ETN|ETP)|해외\s*(?:ETF|ETN|ETP)|"
            rf"공모\s*펀드|펀드)\s*{count}\s*"
            r"(?:개|종목|상품)"
        ),
        # The family noun can precede the first metric (``국내 ETF 중
        # 순자산이 큰 열 개를 먼저 ...``).  Direction remains mandatory;
        # ``거래대금 기준 20개 후보`` is deliberately not admitted.
        re.compile(
            rf"(?P<label>.{{1,140}}?)(?:이|가|은|는)?\s*"
            r"(?P<cue>큰|작은|높은|낮은|많은|적은|짧은|긴|빠른|늦은)\s*"
            rf"(?:{_SHORTLIST_ORDER_SURFACE}\s*)?"
            rf"{count}\s*(?:개|종목|상품)"
        ),
    )
    candidates: list[tuple[int, CompoundMetric, SortDirection, int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if transition.search(text[match.end() :]) is None:
                continue
            metric = _metric_from_label(match.group("label"), family)
            if metric is None:
                continue
            raw_count = match.group("count")
            limit = int(raw_count) if raw_count.isdigit() else count_values[raw_count]
            order = match.groupdict().get("order")
            cue = match.groupdict().get("cue")
            direction = (
                SortDirection.ASC
                if order in {"하위", "최하단"}
                or cue in {"작은", "낮은", "적은", "짧은", "빠른"}
                else SortDirection.DESC
            )
            candidates.append((match.start(), metric, direction, limit, match.end()))
    if not candidates:
        return None
    # The first staged ranking owns the shortlist; the final ranking is parsed
    # independently by ``_rank_spec`` from the last ranking expression.
    _start, metric, direction, limit, end = min(candidates, key=lambda item: item[0])
    return metric, direction, limit, end


def _rank_spec(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    patterns = (
        re.compile(
            r"(.{1,100}?(?:상품\s*명|종목\s*명|펀드\s*명|정식\s*명칭|이름))\s*"
            rf"(가나다순|알파벳순|순)\s*(?:으로)?\s*{count}\s*"
            r"(?:개|종목|상품|을|를)?(?:씩)?"
        ),
        re.compile(
            r"(.{1,100}?)(?:이|가)?\s*(?:(?:가장|제일)\s*)?"
            r"(높은|낮은|큰|작은|많은|적은|짧은|긴|가까운|먼|빠른|늦은)\s*"
            rf"순(?:서)?(?:대로|으로|서로)?\s*(?:최대\s*)?{count}\s*"
            r"(?:개|을|를)?(?:씩)?"
        ),
        re.compile(rf"(.{{1,100}}?)\s*(상위|하위)\s*{count}\s*(?:개|을|를)?"),
        re.compile(rf"(.{{1,100}}?)\s*(내림차순|오름차순)(?:으로)?\s*{count}\s*(?:개|을|를)?"),
        re.compile(
            r"(.{1,100}?)(?:이|가)?\s*"
            r"(높은|낮은|큰|작은|많은|적은|짧은|긴|가까운|먼|빠른|늦은)\s*"
            rf"(?:값|것|상품)?부터\s*(?:최대\s*)?{count}\s*(?:개|을|를)?"
        ),
        re.compile(
            r"(.{1,100}?)(?:이|가|은|는)?\s*(?:(?:가장|제일)\s*)?"
            r"(높은|낮은|큰|작은|많은|적은|짧은|긴|가까운|먼|빠른|늦은)\s*"
            rf"(?:값|것|상품|종목)?(?:을|를|이|가|은|는)?\s*(?:최대\s*)?{count}\s*"
            r"(?:개|종목|상품|을|를)?"
        ),
        re.compile(
            r"(.{1,100}?)(?:이|가|은|는)?\s*(?:(?:가장|제일)\s*)?"
            r"(높은|낮은|큰|작은|많은|적은|짧은|긴)\s*"
            rf"(?:전체\s*)?(?:TOP|톱)\s*{count}\s*(?:개|종목|상품)?",
            re.IGNORECASE,
        ),
        re.compile(
            r"(.{1,100}?)(?:이|가|은|는)?\s*(?:(?:가장|제일)\s*)?"
            r"(높은|낮은|큰|작은|많은|적은|짧은|긴|가까운|먼|빠른|늦은)\s*"
            r"(?:장내\s*)?(?:국내\s*채권|채권|국내\s*ETF|해외\s*ETF|"
            rf"공모\s*펀드|펀드)(?:을|를)?\s*{count}\s*(?:개|종목|상품)?(?:만)?",
            re.IGNORECASE,
        ),
    )
    matches: list[tuple[int, re.Match[str]]] = []
    for pattern in patterns:
        matches.extend((item.start(), item) for item in pattern.finditer(text))
    if not matches:
        return None
    _, match = max(matches, key=lambda item: item[0])
    metric = _metric_from_label(match.group(1), family)
    if metric is None:
        return None
    cue = match.group(2)
    strictly_calendar_cues = {"가까운", "먼", "빠른", "늦은"}
    duration_sources = {("remaining_days",), ("dur",)}
    if cue in strictly_calendar_cues and metric.source_fields != ("remaining_days",):
        return None
    if cue in {"짧은", "긴"} and metric.source_fields not in duration_sources:
        return None
    direction = (
        SortDirection.ASC
        if cue
        in {
            "낮은",
            "작은",
            "적은",
            "짧은",
            "가까운",
            "빠른",
            "하위",
            "오름차순",
            "가나다순",
            "알파벳순",
            "순",
        }
        else SortDirection.DESC
    )
    count_values = {
        "한": 1,
        "두": 2,
        "세": 3,
        "셋": 3,
        "네": 4,
        "넷": 4,
        "다섯": 5,
        "여섯": 6,
        "일곱": 7,
        "여덟": 8,
        "아홉": 9,
        "열": 10,
    }
    raw_count = match.group("count")
    return metric, direction, int(raw_count) if raw_count.isdigit() else count_values[raw_count]


def _named_set_rank_spec(
    question: str,
    family: ProductFamily,
    *,
    identity_count: int,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    """Bind one explicit rank clause to a closed exact-identity set."""

    ranking = _rank_spec(question, family)
    if ranking is None:
        winner = re.search(
            r"(.{1,120}?)(?:이|가)?\s*(?:더\s*)?(높은|낮은|큰|작은)\s*"
            r"(?:상품|종목|펀드|ETF|채권|쪽|것)(?:이|은|가)?",
            question,
            flags=re.IGNORECASE,
        )
        if winner is not None:
            metric = _metric_from_label(winner.group(1), family)
            if metric is not None:
                ranking = (
                    metric,
                    (
                        SortDirection.ASC
                        if winner.group(2) in {"낮은", "작은"}
                        else SortDirection.DESC
                    ),
                    identity_count,
                )
    if ranking is None:
        pair_order = re.search(
            r"(.{1,100}?)(?:이|가)?\s*(큰|작은|높은|낮은)\s*"
            r"(?:쪽|것|상품)?부터\s*둘\s*다",
            question,
        )
        if pair_order is not None:
            metric = _metric_from_label(pair_order.group(1), family)
            if metric is not None:
                ranking = (
                    metric,
                    (
                        SortDirection.ASC
                        if pair_order.group(2) in {"작은", "낮은"}
                        else SortDirection.DESC
                    ),
                    2,
                )
    if ranking is None:
        return None
    metric, direction, parsed_limit = ranking
    return metric, direction, min(parsed_limit, identity_count)


def _named_set_projection_fields(
    question: str,
    family: ProductFamily,
) -> tuple[str, ...] | None:
    """Return the explicitly requested companion field, or ``None`` on loss."""

    projection_match = re.search(r"각\s*상품의\s*(.+?)(?:도|을|를)\s*", question)
    if projection_match is None:
        return ()
    projection_metric = _metric_from_label(projection_match.group(1), family)
    if projection_metric is None:
        return None
    return projection_metric.source_fields


def _aggregate_threshold_spec(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundAggregateThreshold, int] | None:
    """Parse one explicit aggregate-derived bound without inventing a field.

    Only AVG and MEDIAN over one approved numeric field are admitted.  The
    aggregate is always calculated over the same base filters held by the
    resulting family plan; an LLM never supplies the threshold or SQL.
    """

    aggregate_pattern = re.compile(
        r"산술\s*평균|평균(?:값|치)?|중앙\s*값|중위수|메디안"
    )
    aggregate_matches = list(aggregate_pattern.finditer(text))
    if len(aggregate_matches) != 1:
        return None
    aggregate_match = aggregate_matches[0]
    tail = text[aggregate_match.end() :]
    relation_patterns = (
        re.compile(
            r"^\s*(?P<label>.{0,64}?)\s*보다\s*(?:더\s*)?"
            r"(?P<restatement>.{0,32}?)(?P<relation>큰|높은|많은|작은|"
            r"낮은|적은|짧은|긴|가까운|먼|싼|"
            r"초과하는|웃도는|넘는|미만인|밑도는)"
        ),
        re.compile(
            r"^\s*(?P<label>.{0,64}?)(?:이|가|은|는|을|를)?\s*"
            r"(?P<relation>이상|이하|초과|미만)(?:인|의)?"
        ),
        re.compile(
            r"^\s*(?P<label>.{0,64}?)(?:을|를)?\s*"
            r"(?P<relation>넘는|웃도는|밑도는)"
        ),
    )
    relation_match: re.Match[str] | None = None
    for pattern in relation_patterns:
        relation_match = pattern.search(tail)
        if relation_match is not None:
            break
    if relation_match is None:
        return None
    suffix_label = " ".join(
        value
        for value in (
            relation_match.groupdict().get("label", ""),
            relation_match.groupdict().get("restatement", ""),
        )
        if value
    )
    candidates = [
        item
        for item in (
            _metric_from_label(text[: aggregate_match.start()], family),
            _metric_from_label(suffix_label, family),
        )
        if item is not None and item.kind == "field"
    ]
    unique_fields = {item.source_fields for item in candidates}
    if len(unique_fields) != 1:
        return None
    metric = CompoundMetric("field", next(iter(unique_fields)))
    relation = relation_match.group("relation")
    if relation in {"짧은", "긴", "가까운", "먼"} and metric.source_fields not in {
        ("dur",),
        ("remaining_days",),
    }:
        return None
    if relation in {"가까운", "먼"} and metric.source_fields != ("remaining_days",):
        return None
    if relation == "싼" and metric.source_fields[0] not in {
        "cu_charge_rt",
        "sale_co_rwrd_r",
        "or_co_rwrd_r",
        "trusc_rwrd_r",
        "ofwk_trus_rwrd_r",
        "du_clpr",
        "eval_price",
        "dirty",
    }:
        return None
    function_surface = re.sub(r"\s+", "", aggregate_match.group(0))
    function = (
        AggregateFunction.MEDIAN
        if function_surface in {"중앙값", "중위수", "메디안"}
        else AggregateFunction.AVG
    )
    operator = {
        "큰": "gt",
        "높은": "gt",
        "많은": "gt",
        "초과하는": "gt",
        "웃도는": "gt",
        "넘는": "gt",
        "작은": "lt",
        "낮은": "lt",
        "적은": "lt",
        "짧은": "lt",
        "긴": "gt",
        "미만인": "lt",
        "밑도는": "lt",
        "가까운": "lt",
        "먼": "gt",
        "싼": "lt",
        "이상": "gte",
        "이하": "lte",
        "초과": "gt",
        "미만": "lt",
    }[relation]
    return (
        CompoundAggregateThreshold(metric, function, operator),
        aggregate_match.end() + relation_match.end(),
    )


def _aggregate_final_rank_spec(
    text: str,
    family: ProductFamily,
    *,
    threshold_end: int,
    threshold: CompoundAggregateThreshold,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    """Parse the independent final ordering after an aggregate threshold.

    Korean requests frequently place the result count before the final order
    (``3개를 만기 빠른 순으로``).  The ordinary ranking grammar requires the
    count after the order, so this helper binds the globally explicit count to
    a ranking surface that occurs strictly after the aggregate predicate.
    """

    parsed = _rank_spec(text[threshold_end:], family)
    if parsed is not None:
        return parsed
    limit = _final_limit(text)
    if limit is None:
        return None
    tail = text[threshold_end:]
    explicit = re.compile(
        # Keep the grammatical particle inside the label.  Making ``가`` an
        # optional suffix truncates lexical fields ending in the same
        # syllable (most importantly ``종가`` -> ``종``).
        r"(?P<label>.{1,100}?)\s*"
        r"(?P<cue>높은|낮은|큰|작은|많은|적은|짧은|긴|"
        r"가까운|먼|빠른|늦은|싼)\s*순(?:서)?(?:대로|으로|서로)?"
    )
    bare = re.compile(
        r"(?P<label>.{1,100}?)(?:을|를|이|가|은|는)?\s*"
        r"(?:기준(?:으로)?\s*)?순(?:서)?(?:대로|으로|서로)?"
    )
    candidates: list[tuple[int, int, re.Match[str], str | None]] = []
    candidates.extend(
        (1, match.start(), match, match.group("cue"))
        for match in explicit.finditer(tail)
    )
    candidates.extend(
        (0, match.start(), match, None) for match in bare.finditer(tail)
    )
    # A lexical order cue (``낮은 순``) is more authoritative than a later
    # substring that merely ends in bare ``순``.  Otherwise the bare matcher
    # can consume the cue as part of its label and silently restore DESC.
    for _priority, _start, match, cue in sorted(
        candidates,
        key=lambda item: (item[0], item[1]),
        reverse=True,
    ):
        metric = _metric_from_label(match.group("label"), family)
        if (
            metric is None
            and cue == "싼"
            and threshold.metric.source_fields[0]
            in {
                "cu_charge_rt",
                "sale_co_rwrd_r",
                "or_co_rwrd_r",
                "trusc_rwrd_r",
                "ofwk_trus_rwrd_r",
                "du_clpr",
                "eval_price",
                "dirty",
            }
        ):
            metric = threshold.metric
        if metric is None:
            continue
        if cue in {"가까운", "먼", "빠른", "늦은"} and metric.source_fields != (
            "remaining_days",
        ):
            continue
        if cue in {"짧은", "긴"} and metric.source_fields not in {
            ("remaining_days",),
            ("dur",),
        }:
            continue
        direction = (
            SortDirection.ASC
            if cue
            in {
                "낮은",
                "작은",
                "적은",
                "짧은",
                "가까운",
                "빠른",
                "싼",
            }
            else SortDirection.DESC
        )
        return metric, direction, limit
    # Reuse the aggregate predicate's field/direction only when the user also
    # supplies a bounded count and an explicit response verb.  This keeps the
    # intentionally incomplete ``... 상품 3개`` form fail-closed.
    if re.search(r"(?:찾아|찾줘|찾\s*줘|보여|알려|나열|정리)", tail):
        direction = (
            SortDirection.DESC
            if threshold.operator in {"gt", "gte"}
            else SortDirection.ASC
        )
        return threshold.metric, direction, limit
    return None


def _rank_direction_without_local_limit(
    text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection] | None:
    """Parse a family-local metric when one coordinated count follows later.

    Korean coordinated requests often state each metric next to its family but
    put the count only once at the end (for example ``1개월 수익률 높은 국내
    ETF와 AUM 큰 해외 ETF를 각각 2개``).  ``_rank_spec`` intentionally
    requires a local count, so use this narrower parser only from the parallel
    compiler after a shared final count has been proven.  Every family must
    still supply its own metric cue; an incomplete second clause is rejected.
    """

    family_label = {
        ProductFamily.BOND: r"(?:장내\s*)?(?:국내\s*)?채권",
        ProductFamily.DOMESTIC_ETP: r"국내\s*(?:ETF|ETN|ETP)",
        ProductFamily.OVERSEAS_ETP: r"해외\s*(?:ETF|ETN|ETP)",
        ProductFamily.FUND: r"(?:공모\s*)?펀드",
    }[family]
    pattern = re.compile(
        r"(?P<label>.{1,100}?)(?:이|가|은|는)?\s*"
        r"(?P<cue>높은|낮은|큰|작은|많은|적은|짧은|긴|가까운|먼|빠른|늦은)\s*"
        rf"(?:순(?:서)?(?:대로|으로|서로)?\s*)?{family_label}",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    match = matches[-1] if matches else None
    metric = None if match is None else _metric_from_label(match.group("label"), family)
    cue = None if match is None else match.group("cue")
    if metric is None:
        # Keep compound parsing aligned with the shared Typed Alias Registry
        # instead of duplicating every colloquial period/metric expression in
        # this structural compiler.  Only one unambiguous registry meaning may
        # cross this boundary.
        semantic_matches = rank_alias_matches(text, family)
        semantic_meanings = {
            (item.field_id, item.direction) for item in semantic_matches
        }
        if len(semantic_meanings) != 1:
            return None
        field_id, direction = next(iter(semantic_meanings))
        source_fields = tuple(
            source_field
            for (source_family, source_field), canonical in _SOURCE_TO_CANONICAL.items()
            if source_family is family and canonical == field_id
        )
        if len(source_fields) != 1:
            return None
        return CompoundMetric("field", source_fields), direction
    assert cue is not None
    if cue in {"가까운", "먼", "빠른", "늦은"} and metric.source_fields != (
        "remaining_days",
    ):
        return None
    if cue in {"짧은", "긴"} and metric.source_fields not in {
        ("remaining_days",),
        ("dur",),
    }:
        return None
    direction = (
        SortDirection.ASC
        if cue in {"낮은", "작은", "적은", "짧은", "가까운", "빠른"}
        else SortDirection.DESC
    )
    return metric, direction


def _explicit_rank_direction(text: str) -> SortDirection | None:
    """Return a direction only when the user stated an ordering cue."""

    matches = list(
        re.finditer(
            r"(?P<asc>낮은|작은|적은|짧은|가까운|빠른|하위|오름차순)|"
            r"(?P<desc>높은|큰|많은|늦은|상위|내림차순|"
            r"(?<![가-힣])(?:긴|먼)(?![가-힣]))",
            text,
        )
    )
    if not matches:
        return None
    return SortDirection.ASC if matches[-1].group("asc") else SortDirection.DESC


_SHARED_LOCAL_METRIC_REFERENCE = re.compile(
    r"(?:"
    r"(?:각각(?:의)?|상품군별(?:로)?|종류별(?:로)?|"
    r"각\s*(?:상품군|시장)(?:에서|의)?)"
    r".{0,24}?(?:각자(?:의)?|자기(?:의)?|해당)?\s*(?:기준\s*)?"
    r"|(?:각자(?:의)?|자기(?:의)?)\s*(?:기준\s*)?"
    r")(?P<placeholder>수치|값|지표)",
    re.IGNORECASE,
)


def _shared_local_metric_reference_owns_span(question: str, span: str) -> bool:
    """Return whether ``span`` is the literal placeholder in a shared anaphor.

    The caller must still prove that every family has exactly one preceding
    numeric field and that the frozen plan ranks by that same field.  Keeping
    the literal ownership check here prevents an unrelated ``수치`` elsewhere
    in the question from borrowing authority from a later shared suffix.
    """

    start = question.find(span)
    if start < 0 or question.find(span, start + 1) >= 0:
        return False
    end = start + len(span)
    return any(
        match.start("placeholder") <= start
        and end <= match.end("placeholder")
        for match in _SHARED_LOCAL_METRIC_REFERENCE.finditer(question)
    )


def _shared_parallel_order(question: str) -> tuple[SortDirection, int] | None:
    """Parse one explicitly shared direction/count for clause-local metrics."""

    if (
        re.search(
            r"(?:각각|각자|상품군별(?:로)?|종류별(?:로)?|각\s*(?:상품군|시장))",
            question,
        )
        is None
        and _SHARED_LOCAL_METRIC_REFERENCE.search(question) is None
    ):
        return None
    limit = _final_limit(question)
    if limit is None:
        return None
    shared_tail = re.search(
        r"(?:각각|각자|상품군별(?:로)?|종류별(?:로)?|각\s*(?:상품군|시장)).{0,36}?"
        r"(?P<order>상위|하위|높은|낮은|큰|작은|많은|적은|"
        r"내림차순|오름차순)",
        question,
    )
    if shared_tail is None:
        # Coordinated Korean often places the common direction immediately
        # before ``각각 N개씩`` (``거래량 높은 순으로 각각 3개씩``).
        # ``각각`` makes only the direction/count shared; metrics remain
        # owned by their respective family clauses.
        shared_tail = re.search(
            r"(?P<order>상위|하위|높은|낮은|큰|작은|많은|적은|"
            r"내림차순|오름차순)(?:\s*순(?:서)?(?:대로|으로)?)?\s*"
            r"각각\s*(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)",
            question,
        )
    if shared_tail is None:
        return None
    return (
        SortDirection.ASC
        if shared_tail.group("order") in {"하위", "낮은", "작은", "적은", "오름차순"}
        else SortDirection.DESC,
        limit,
    )


def _detached_count_rank_spec(
    local_text: str,
    shared_direction_text: str,
    family: ProductFamily,
) -> tuple[CompoundMetric, SortDirection, int] | None:
    """Parse ``family N items ... metric order`` without inventing a slot.

    Korean often places the per-family count beside the family noun and the
    metric/direction later.  We require exactly one metric in the local clause,
    an explicit local count, and an explicit local or shared direction.
    """

    metric = _unique_metric_from_label(local_text, family)
    limit = _final_limit(local_text)
    direction = _explicit_rank_direction(local_text)
    if direction is None:
        direction = _explicit_rank_direction(shared_direction_text)
    if metric is None or limit is None or direction is None:
        return None
    return metric, direction, limit


def _plan_segments(question: str) -> list[tuple[_FamilyScope, str]]:
    matches = _compound_family_matches(question)
    if not matches:
        return []
    if (
        len(matches) == len(ProductFamily)
        and len({(item.start, item.end) for item in matches}) == 1
        and _ALL_FAMILY_PATTERN.search(question) is not None
    ):
        # One all-family phrase owns the same common constraints for every
        # subquery.  Returning the full question avoids inventing artificial
        # clause boundaries between four synthetic scopes.
        return [(scope, question) for scope in matches]
    clause_starts: list[int] = []
    boundary = re.compile(
        # A comma between digits is a thousands separator, not a clause
        # boundary (``536,305,919``).  Splitting there silently drops the
        # first family's numeric predicate and is therefore fail-open.
        r"(?<!\d),(?!\d)|(?<!\d)\.(?!\d)|[;!?。]|"
        r"(?:^|\s)(?:그리고|이어서|다음으로|반면|한편|또한|그에\s*비해)\s*",
        re.IGNORECASE,
    )
    for index, scope in enumerate(matches):
        if index == 0:
            # A pre-nominal modifier belongs to the first family only. It must
            # not be copied into every later subquery as a pseudo-global
            # prefix (for example ``미국 주식형 국내 ETF, ... 공모펀드``).
            clause_starts.append(0)
            continue
        previous = matches[index - 1]
        between = question[previous.end : scope.start]
        separators = list(boundary.finditer(between))
        # Include modifiers immediately before the family noun (``판매중인
        # 공모펀드``, ``위험등급 2등급인 국내 ETF``) in that family's clause.
        # Without an explicit clause delimiter, retain the old family-noun
        # boundary so shared grammar such as ``국내 ETF와 해외 ETF`` is not
        # spuriously assigned to one side.
        if separators:
            clause_starts.append(previous.end + separators[-1].end())
            continue
        conjunctions = list(re.finditer(r"(?:와|과|및)\s+", between))
        if conjunctions:
            modifier_start = previous.end + conjunctions[-1].end()
            if question[modifier_start : scope.start].strip():
                clause_starts.append(modifier_start)
                continue
        clause_starts.append(scope.start)
    segments: list[tuple[_FamilyScope, str]] = []
    for index, scope in enumerate(matches):
        next_start = clause_starts[index + 1] if index + 1 < len(matches) else len(question)
        segments.append((scope, question[clause_starts[index] : next_start]))
    return segments


def _shared_exclusion_prefix(
    question: str,
    segments: list[tuple[_FamilyScope, str]],
) -> str:
    """Return a clause-initial exclusion that explicitly scopes all families.

    Korean users commonly put one exclusion before a coordinated family list
    (``레버리지와 거래정지는 빼고 국내 ETF와 해외 ETF ...``).  The prefix
    belongs to every compatible subquery; ordinary positive pre-modifiers remain
    owned only by the first family so they cannot silently widen another family.
    """

    if len(segments) < 2:
        return ""
    prefix = question[: segments[0][0].start]
    if re.search(
        r"(?:제외(?:하고|한|해)?|빼고|말고|걷어내고|"
        r"배제(?:하고|한|해)?)",
        prefix,
    ) is None:
        return ""
    return prefix.strip()


def _shared_compound_text(
    question: str,
    scope: _FamilyScope,
    segment: str,
    *,
    last_family_end: int,
) -> str:
    """Attach only the common suffix to one family-local clause.

    In Korean a coordinated family list normally puts shared filters and the
    ranking after the final family noun (``국내 ETP와 펀드 중 1년
    수익률 ...``).  Keeping that suffix on every subquery preserves the user's
    common constraints while pre-nominal modifiers remain local to their own
    family segment.
    """

    if segment == question:
        return segment
    # A suffix is shared only when every explicit family name forms one
    # contiguous coordination list.  This grammatical proof is important:
    # blindly appending the last clause to earlier families leaks filters from
    # requests such as ``국내 ETF는 ... , 해외 ETF는 ...``.  Conversely,
    # ``국내 ETF와 해외 ETF 중 ...`` owns one common suffix and every
    # compatible family must receive it.
    family_scopes = _compound_family_matches(question)
    if len(family_scopes) < 2:
        return segment
    for left, right in zip(family_scopes, family_scopes[1:], strict=False):
        coordinator = question[left.end : right.start]
        if re.fullmatch(
            r"\s*(?:(?:와|과|및|또는|혹은)|[,;·ㆍ/&+])\s*",
            coordinator,
            flags=re.IGNORECASE,
        ) is None:
            return segment
    common_tail = question[last_family_end:].strip()
    if not common_tail or common_tail in segment:
        return segment
    local = re.sub(r"\s*(?:와|과|및|[,;·ㆍ/])\s*$", "", segment)
    return f"{local} {common_tail}"


def _without_dataset_label_filter(
    scope: _FamilyScope,
    filters: tuple[CompoundFilter, ...],
    text: str,
) -> tuple[CompoundFilter, ...]:
    """Remove table-label defaults unless the row attribute is explicit."""

    if scope.product_family is ProductFamily.FUND and re.search(
        r"(?:모집\s*구분|공모\s*여부).{0,12}(?:공모|공개\s*모집)|"
        r"(?:공모형|공개\s*모집)\s*(?:인|인\s*것만|상품만|펀드만)",
        text,
    ) is None:
        filters = tuple(item for item in filters if item.source_field != "prvo_pbff_desc")
    return filters


def _with_approved_fund_scope(
    family: ProductFamily,
    filters: tuple[CompoundFilter, ...],
) -> tuple[CompoundFilter, ...]:
    """Seal the executable public-fund universe into aggregate subplans."""

    if family is not ProductFamily.FUND:
        return filters
    approved = CompoundFilter("prvo_pbff_desc", "eq", "공모")
    if approved in filters:
        return filters
    # Preserve a conflicting explicit private predicate.  The public QueryPlan
    # validation then sees two independent constraints and fails closed rather
    # than rewriting the user's request into the approved universe.
    return (*filters, approved)


class TypedCompoundQueryEngine:
    """Compile only explicit multi-step numeric grammar into deterministic Python.

    This executor is intentionally narrower than Text-to-SQL. Every filter,
    metric, direction and limit must be present in the question and must map to
    a frozen source field. Unknown text therefore remains on the established
    CLARIFY path instead of acquiring execution authority.
    """

    def __init__(
        self,
        database_paths: dict[ProductFamily, Path],
        *,
        record_cache: RecordSnapshotCache,
    ) -> None:
        self.database_paths = database_paths
        self.record_cache = record_cache
        self.database_identity_matcher = DatabaseProductIdentityMatcher(database_paths)
        # Compound execution authority is process-local.  A public dataclass
        # that merely contains predictable hashes is forgeable by another
        # caller in the same Python process; bind every receipt to an
        # unpredictable engine key instead.
        self._authorization_key = secrets.token_bytes(32)

    def compile(
        self,
        question: str,
        request_id: str,
        *,
        hclx_structure: tuple[tuple[str, tuple[ProductFamily, ...], str], ...] = (),
    ) -> TypedCompoundPlan | None:
        plan: TypedCompoundPlan | None = None
        roles = {role for _span, _families, role in hclx_structure}
        ranking_owners = {
            family
            for _span, families, role in hclx_structure
            if role == "ranking"
            for family in families
        }
        # A procedural phrase such as ``먼저 추린 뒤`` is not, by itself, a
        # two-stage shortlist.  It often only separates explicit hard filters
        # from the final ranking.  Requiring two bounded result sizes plus a
        # transition prevents that ordinary filter+rank query from being
        # diverted into the shortlist compiler and rejected.
        two_stage_cue = requires_typed_two_stage_plan(question)
        anchor_relative_cue = self._database_relative_anchor(question) is not None or (
            (
                re.search(r"\([A-Z0-9.]{2,20}(?:,\s*장내)?\)", question)
                and re.search(r"기준(?:점|\s*상품)?|이\s*상품(?:보다|\s*(?:이상|이하))", question)
                and re.search(r"다른\s*상품|나머지\s*상품|제외한\s*상품", question)
            )
            or re.search(
                r"(?<![A-Z0-9])(?:[A-Z][A-Z0-9.]{1,19}|KR[A-Z0-9]{10})"
                r"(?:\)|\s)*(?:보다|대비)\s*.{1,60}?"
                r"(?:큰|작은|높은|낮은|많은|적은)",
                question,
                re.IGNORECASE,
            )
            or re.search(
                r"(?<![A-Z0-9])(?:[A-Z][A-Z0-9.]{1,19}|KR[A-Z0-9]{10})의\s*"
                r".{1,60}?(?:을|를)?\s*(?:넘는|웃도는|웃돌(?:고|며|는)|"
                r"초과하는|밑도는|미만인)",
                question,
                re.IGNORECASE,
            )
            or re.search(
                r"(?<![A-Z0-9])(?:[A-Z][A-Z0-9.]{1,19}|KR[A-Z0-9]{10})\s*"
                r"(?:을|를)?\s*기준(?:으로)?\s*.{1,100}?"
                r"(?:이상|이하|초과|미만)",
                question,
                re.IGNORECASE,
            )
        )
        exact_identity_count = len(
            re.findall(r"KR[A-Z0-9]{10}|(?<=\()[A-Z][A-Z0-9.]{1,12}(?=\))", question)
        )
        named_comparison_cue = re.search(
            r"(?:중|비교).{0,100}?"
            r"(?:더\s*)?(?:높은|낮은|큰|작은)\s*(?:상품|종목|펀드|ETF|채권|쪽|것)",
            question,
        )
        family_scopes = _compound_family_matches(question)
        family_summary_cue = (
            len(family_scopes) >= 2
            and re.search(r"분포", question) is None
            and re.search(r"상품군\s*별|각\s*상품군|각각", question) is not None
            and re.search(
                r"평균|최(?:대|고)(?:값)?|"
                r"(?:몇|전체)\s*(?:개|건|종목|상품)|"
                r"(?:상품|종목|펀드)\s*(?:수|개수|건수)",
                question,
            )
            is not None
        )
        global_union_surface = (
            re.search(
                r"(?:함께\s*(?:놓|모|보)|하나로\s*(?:모|합)|통합|통틀어|"
                r"(?:한|하나의)\s*목록(?:에|으로)\s*(?:놓|모|합)|"
                r"한\s*(?:목록|리스트|표)(?:으로|에서|에)?|"
                r"한데\s*(?:모아|모으|모여)|전체\s*(?:상품군|후보)|"
                r"합쳐서|합산\s*순위|직접\s*(?:비교|정렬|순위)|"
                r"같이\s*(?:찾|검색)|함께\s*(?:찾|검색)|"
                r"(?:두|둘|세|네)\s*상품군\s*중|"
                r"상품군\s*전체에서|전체에서)",
                question,
            )
            is not None
            or (
                family_scopes
                and re.search(
                    r"^\s*(?:중|가운데).{0,120}(?:순|상위|하위|찾|보여)",
                    question[max(item.end for item in family_scopes) :],
                )
                is not None
            )
        )
        global_ranking_cue = (
            len(family_scopes) >= 2
            and global_union_surface
            and re.search(
                r"(?:각각|상품군별|각\s*(?:상품군|시장)|따로|구분해서|"
                r"합치지\s*말고|섞지\s*말고)",
                question,
            )
            is None
        )
        grouped_aggregate_cue = (
            len(family_scopes) == 1
            and (
                bool(_explicit_group_sources(question, family_scopes[0].product_family))
                or re.search(
                    r"위험\s*등급\s*(?:마다|별(?:로)?)", question
                )
                is not None
            )
            and re.search(
                r"평균|중앙\s*값|중위수|메디안|합계|총합|최솟값|최햓값|"
                r"최소값|최대값|상품\s*수|종목\s*수|펀드\s*수|개수|건수",
                question,
                re.IGNORECASE,
            )
            is not None
        )
        grouped_product_topk_cue = bool(
            family_scopes
            and _has_grouped_product_topk_surface(question)
            and all(
                _explicit_group_source(question, scope.product_family) is not None
                for scope in family_scopes
            )
        )
        cross_family_named_comparison = (
            self._compile_cross_family_named_comparison(question, request_id)
            if re.search(r"비교|대조", question, re.IGNORECASE) is not None
            else None
        )
        # Explicit server grammar is more authoritative than HCX's advisory
        # clause roles.  A reference-product screen may contain both a first
        # filter and a final ranking, which HCX can reasonably label
        # ``shortlist`` + ``ranking``.  That semantic description must not
        # pre-empt the unambiguous ``기준 상품 이상/이하`` compiler.
        if anchor_relative_cue:
            plan = self._compile_anchor_relative(question, request_id)
        elif cross_family_named_comparison is not None:
            plan = cross_family_named_comparison
        elif exact_identity_count >= 2 and named_comparison_cue is not None:
            plan = self._compile_named_pair(question, request_id)
        elif grouped_product_topk_cue:
            # Partitioned product ranking is stronger structure than a
            # generic two-stage cue and is not an aggregate-row TOP-K.
            plan = self._compile_grouped_topk(question, request_id)
        elif grouped_aggregate_cue:
            # A HAVING bound also contains two sizes and often ``남겨``.
            # The explicit group axis plus aggregate function is stronger
            # structure than the generic two-stage shortlist cue.
            plan = self._compile_grouped_aggregate(question, request_id)
        elif two_stage_cue:
            plan = self._compile_two_stage(question, request_id)
        elif len(family_scopes) == 1 and re.search(
            r"(?:보다|에\s*비해).{0,32}(?:높은|낮은|큰|작은|많은|적은|긴|짧은)"
            r".{0,80}(?:차이|격차)",
            question,
        ):
            plan = self._compile_field_comparison(question, request_id)
        elif (
            len(family_scopes) >= 2
            and re.search(r"분포", question) is None
            and re.search(
                r"(?:상품|종목|펀드)\s*(?:수|개수|건수)|"
                r"(?:몇|각각).{0,10}(?:개|건|종목|상품)",
                question,
            )
            is not None
            and re.search(r"집계|세어|계산", question) is not None
        ):
            plan = self._compile_family_count_aggregate(question, request_id)
        elif family_summary_cue:
            plan = self._compile_family_summary_aggregate(question, request_id)
        elif global_ranking_cue:
            plan = self._compile_global_ranking(question, request_id)
        elif (
            len(family_scopes) == 1
            and (
                bool(_explicit_group_sources(question, family_scopes[0].product_family))
                or re.search(
                    r"위험\s*등급\s*(?:마다|별(?:로)?)", question
                )
                is not None
            )
            and re.search(
                r"평균|중앙\s*값|중위수|메디안|합계|총합|최솟값|최댓값|"
                r"최소값|최대값|상품\s*수|종목\s*수|펀드\s*수|개수|건수",
                question,
                re.IGNORECASE,
            )
            is not None
        ):
            plan = self._compile_grouped_aggregate(question, request_id)
        elif len(family_scopes) >= 2 and re.search(
            r"(?:(?:상품|종목|펀드)\s*(?:수|개수|건수).{0,24}상품군별|"
            r"상품군별.{0,24}(?:상품|종목|펀드)\s*(?:수|개수|건수))",
            question,
        ):
            plan = self._compile_family_count_aggregate(question, request_id)
        elif re.search(r"분포.{0,30}(?:비교|차이)", question) and re.search(
            r"평균|중앙값|최솟값|최댓값|최대값|최소값", question
        ):
            plan = self._compile_cohort(question, request_id)
        elif len(family_scopes) == 1 and re.search(
            r"(?:평균(?:값|치)?|중앙\s*값|중위수|메디안).{0,80}?"
            r"(?:(?:보다|이상|이하|초과|미만)|(?:을|를)?\s*"
            r"(?:넘는|웃도는|밑도는))",
            question,
        ):
            plan = self._compile_aggregate_threshold(question, request_id)
        elif (
            re.search(
            r"(?:각각|두\s*묶음(?:으로)?|각\s*(?:상품군|지표)(?:에서|가)?|따로|나란히|나눠|"
                r"상품군별(?:로)?|종류별(?:로)?|구분해서|합치지\s*말고|섞지\s*말고|"
                r"합쳐.{0,16}?(?:정렬|줄\s*세우|비교).{0,8}?말(?:아|고))",
                question,
            )
            and len(family_scopes) >= 2
        ):
            plan = self._compile_parallel(question, request_id)
        elif exact_identity_count >= 2 and re.search(r"순|상위|하위|나열|정렬", question):
            plan = self._compile_named_pair(question, request_id)
        elif re.search(
            r"(?:이상|이하|초과|미만)인?\s*(?:것|상품|후보)\s*(?:중|가운데|에서)",
            question,
        ):
            plan = self._compile_bounded(question, request_id)
        elif len(family_scopes) == 1 and re.search(
            r"(?:제외(?:하고|한|해)?|빼고|걷어내고|배제(?:하고|한|해)?)",
            question,
        ):
            # Exclusion polarity is easy to lose in a generic lexical plan
            # (``0.8% 초과분을 배제`` means <= 0.8).  Route such a
            # bounded single-family request through the same typed complement
            # compiler used by multi-family clauses.
            plan = self._compile_bounded(question, request_id)
        elif ("후보를 이" in question and "상품으로만 한정" in question) or (
            re.search(r"비교|대조|차이", question) is not None
            and re.search(r"(?:쪽|것|상품)?부터\s*둘\s*다", question) is not None
        ):
            plan = self._compile_named_pair(question, request_id)
        elif len(ranking_owners) >= 2:
            # Family ownership proposed by HCX is advisory. Exact identifiers
            # resolved against one provided-DB family above are stronger than
            # a model family label inferred from words inside a product name
            # (for example ``[채권]`` in the name of a public fund).
            plan = self._compile_parallel(question, request_id)
        elif len(family_scopes) >= 2 and all(
            _rank_spec(segment, scope.product_family) is not None
            or _detached_count_rank_spec(
                segment,
                question[max(item.end for item in family_scopes) :],
                scope.product_family,
            )
            is not None
            for scope, segment in _plan_segments(question)
        ):
            # A coordinated request does not need the literal word ``각각``
            # when every family clause independently supplies its own metric,
            # direction and limit.  The parallel compiler rechecks every
            # clause atomically; one unsupported filter or incomplete ranking
            # rejects the whole plan rather than widening another clause.
            plan = self._compile_parallel(question, request_id)
        elif {"shortlist", "ranking"}.issubset(roles):
            # HCX clause roles are advisory and may describe a complete named
            # comparison or cohort request as shortlist + ranking.  They may
            # open the narrow two-stage parser only after every explicit,
            # server-owned grammar above had a chance to compile; otherwise a
            # semantically reasonable segmentation could veto a safer and more
            # complete deterministic plan.
            plan = self._compile_two_stage(question, request_id)
        elif len(family_scopes) == 1 and _derived_metric(
            question,
            family_scopes[0].product_family,
        ) is not None:
            plan = self._compile_derived(question, request_id)
        if plan is None or not hclx_structure:
            return plan
        plan_families = set(plan.requested_product_families)
        admitted: list[str] = []
        for span, families, _role in hclx_structure:
            # HCX proposes clause ownership only. The server admits exact
            # question spans whose family scope cannot expand the independently
            # compiled plan; all filters and arithmetic still come from the
            # deterministic parser above.
            if span in question and set(families).issubset(plan_families):
                admitted.append(span)
        return replace(plan, hclx_structure_spans=tuple(dict.fromkeys(admitted)))

    def _compile_global_ranking(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile one comparable metric into a single cross-family TOP-K.

        A requested family may be omitted only when the approved Field
        Registry proves that the shared metric does not exist for that family.
        Parser misses and row-level missing values are never exclusions.
        """

        segments = _plan_segments(question)
        if len(segments) < 2:
            return None
        shared_exclusion = _shared_exclusion_prefix(question, segments)
        last_family_end = max(scope.end for scope, _segment in segments)
        common_tail = question[last_family_end:]
        plans: list[CompoundFamilyPlan] = []
        exclusions: list[CompoundFamilyExclusion] = []
        registry = load_field_registry()
        parsed_rankings = [
            (
                scope,
                segment,
                _rank_spec(common_tail, scope.product_family)
                or _rank_spec(question, scope.product_family),
            )
            for scope, segment in segments
        ]
        supported: list[tuple[str, SortDirection, int]] = []
        for scope, _segment, ranking in parsed_rankings:
            if ranking is None or ranking[0].kind != "field":
                continue
            metric, direction, limit = ranking
            canonical = _SOURCE_TO_CANONICAL.get(
                (scope.product_family, metric.source_fields[0])
            )
            if canonical is not None:
                supported.append((canonical, direction, limit))
        if not supported:
            return None
        canonical_metrics = {canonical for canonical, _direction, _limit in supported}
        requested_directions = {direction for _canonical, direction, _limit in supported}
        requested_limits = {limit for _canonical, _direction, limit in supported}
        if (
            len(canonical_metrics) != 1
            or len(requested_directions) != 1
            or len(requested_limits) != 1
        ):
            return None
        canonical_metric = next(iter(canonical_metrics))
        requested_direction = next(iter(requested_directions))
        requested_limit = next(iter(requested_limits))
        global_definition = registry.fields.get(canonical_metric)
        if global_definition is None:
            return None
        for index, (scope, segment, ranking) in enumerate(parsed_rankings):
            family = scope.product_family
            if ranking is None:
                if (
                    family.value in global_definition.datasets
                    or (family, canonical_metric) in _CANONICAL_TO_SOURCE
                ):
                    # The metric exists for this family, so failure to parse it
                    # is an ambiguity—not a basis for silently dropping data.
                    return None
                exclusions.append(
                    CompoundFamilyExclusion(
                        product_family=family,
                        canonical_field=canonical_metric,
                    )
                )
                continue
            local_text = _shared_compound_text(
                question,
                scope,
                segment,
                last_family_end=last_family_end,
            )
            if index > 0 and shared_exclusion:
                local_text = f"{shared_exclusion} {local_text}"
            filters = _compiled_clause_filters(
                scope,
                local_text,
                extra_represented_source_fields=(
                    () if ranking is None else ranking[0].source_fields
                ),
            )
            if filters is None:
                return None
            filters = _without_dataset_label_filter(scope, filters, local_text)
            metric, direction, limit = ranking
            if metric.kind != "field":
                return None
            canonical = _SOURCE_TO_CANONICAL.get((family, metric.source_fields[0]))
            if canonical != canonical_metric:
                return None
            definition = registry.require_field(canonical, [family.value])
            # Numeric currency amounts cannot be globally ranked without an
            # approved FX conversion and common as-of timestamp.  A textual
            # product-name ordering is safe because every family maps to the
            # same canonical Unicode string field; other textual fields remain
            # outside this narrow contract.
            numeric_comparable = (
                definition.value_type is ValueType.NUMBER
                and definition.unit
                not in {"source_currency_amount", "source_quantity"}
            )
            text_comparable = (
                definition.value_type is ValueType.STRING
                and canonical_metric == "product_name"
            )
            if not (numeric_comparable or text_comparable):
                return None
            if limit != requested_limit or direction is not requested_direction:
                return None
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                )
            )
        if len(plans) < 2:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="global_ranking",
            family_plans=tuple(plans),
            excluded_families=tuple(exclusions),
        )

    def _compile_two_stage(self, question: str, request_id: str) -> TypedCompoundPlan | None:
        plans: list[CompoundFamilyPlan] = []
        segments = _plan_segments(question)
        shared_exclusion = _shared_exclusion_prefix(question, segments)
        for index, (scope, segment) in enumerate(segments):
            family = scope.product_family
            local_text = (
                f"{shared_exclusion} {segment}"
                if index > 0 and shared_exclusion
                else segment
            )
            filters = _compiled_clause_filters(scope, local_text)
            if filters is None:
                return None
            shortlist = _shortlist_spec_with_span(segment, family)
            if shortlist is None:
                return None
            shortlist_metric, shortlist_direction, shortlist_limit, shortlist_end = shortlist
            # Parse the final stage only from text after the first-stage count.
            # Reusing the first ranking as the final ranking silently discards
            # requests such as AUM top-100 -> fee bottom-10.
            final_text = segment[shortlist_end:]
            metric = _derived_metric(final_text, family)
            final_ranking = _rank_spec(final_text, family)
            if metric is None and final_ranking is not None:
                metric, direction, limit = final_ranking
            else:
                direction = _direction(final_text)
                limit = _final_limit(final_text)
            if shortlist_metric is None or metric is None or limit is None:
                return None
            if shortlist_limit < limit:
                return None
            filters = _with_complete_calculation_inputs(
                filters,
                segment,
                shortlist_metric,
                metric,
            )
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                    shortlist_metric=shortlist_metric,
                    shortlist_limit=shortlist_limit,
                    shortlist_direction=shortlist_direction,
                )
            )
        if not plans:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="two_stage",
            family_plans=tuple(plans),
        )

    def _compile_parallel(self, question: str, request_id: str) -> TypedCompoundPlan | None:
        plans: list[CompoundFamilyPlan] = []
        segments = _plan_segments(question)
        shared_name_literal = _shared_product_name_contains(question)
        shared_exclusion = _shared_exclusion_prefix(question, segments)
        shared_limit = _final_limit(question)
        shared_order = _shared_parallel_order(question)
        shared_marker = re.search(
            r"(?:각각|상품군별(?:로)?|각\s*(?:상품군|시장))",
            question,
        )
        shared_metric_tail = "" if shared_marker is None else question[shared_marker.end() :]
        shared_metric_declared = bool(
            shared_metric_tail
            and any(
                _unique_metric_from_label(shared_metric_tail, scope.product_family)
                is not None
                for scope, _segment in segments
            )
        )
        common_tail = (
            ""
            if not segments
            else question[max(scope.end for scope, _segment in segments) :]
        )
        last_family_end = max(
            (scope.end for scope, _segment in segments),
            default=0,
        )
        # A canonical metric can be stated once in the suffix owned by a
        # coordinated family list, before the word ``상품군별``.  The older
        # check looked only *after* that marker, so ``1년 수익률 ...
        # 상품군별 수익률 상위`` was visible to the last family but absent
        # from every earlier one.  Admit the suffix only when every family
        # independently resolves it to the same canonical Field Registry id.
        suffix_metrics: list[str] = []
        shared_suffix_proven = bool(segments) and (
            segments[0][1] == question
            or _shared_compound_text(
                question,
                segments[0][0],
                segments[0][1],
                last_family_end=last_family_end,
            )
            != segments[0][1]
        )
        for suffix_scope, _suffix_segment in segments:
            if not shared_suffix_proven:
                break
            suffix_metric = _unique_metric_from_label(
                common_tail,
                suffix_scope.product_family,
            )
            if suffix_metric is None or suffix_metric.kind != "field":
                suffix_metrics = []
                break
            canonical = _SOURCE_TO_CANONICAL.get(
                (suffix_scope.product_family, suffix_metric.source_fields[0])
            )
            if canonical is None:
                suffix_metrics = []
                break
            suffix_metrics.append(canonical)
        shared_metric_declared = shared_metric_declared or (
            len(suffix_metrics) == len(segments)
            and len(set(suffix_metrics)) == 1
        )
        for index, (scope, segment) in enumerate(segments):
            family = scope.product_family
            local_text = _shared_compound_text(
                question,
                scope,
                segment,
                last_family_end=last_family_end,
            )
            if index > 0 and shared_exclusion:
                local_text = f"{shared_exclusion} {local_text}"
            filters = _compiled_clause_filters(scope, local_text)
            if filters is None:
                return None
            if shared_name_literal is not None:
                name_source = _CANONICAL_TO_SOURCE.get((family, "product_name"))
                if name_source is None:
                    return None
                name_filter = CompoundFilter(
                    source_field=name_source,
                    operator="contains",
                    value=shared_name_literal,
                )
                filters = tuple(
                    dict.fromkeys(
                        (
                            *(
                                item
                                for item in filters
                                if item.source_field != name_source
                                or item.operator != "contains"
                            ),
                            name_filter,
                        )
                    )
                )
            numeric_filters = tuple(
                item
                for item in filters
                if item.operator in {"gt", "gte", "lt", "lte", "between"}
            )
            owned_metric = (
                None
                if shared_order is None or shared_metric_declared
                else _unique_metric_from_label(segment, family)
            )
            overlapping_return_periods = (
                shared_order is not None
                and not shared_metric_declared
                and re.search(r"수익률|수익|성과|성적", segment) is not None
                and len(
                    {
                        re.sub(r"\s+", "", match.group(0)).casefold()
                        for match in re.finditer(
                            r"1\s*일|하루|1\s*개월|한\s*달|3\s*개월|석\s*달|"
                            r"6\s*개월|반년|1\s*년|12\s*개월|열두\s*달|"
                            r"3\s*년|5\s*년|연초|YTD",
                            segment,
                            re.IGNORECASE,
                        )
                    }
                )
                > 1
            )
            if overlapping_return_periods:
                return None
            if shared_order is not None and not shared_metric_declared and owned_metric is None:
                # At least one clause omitted or overlapped its local metric.
                # A later family's metric must never fill that gap.
                return None
            ranking = _rank_spec(segment, family)
            if ranking is not None and owned_metric is not None and ranking[0] != owned_metric:
                return None
            if ranking is None:
                ranking = _detached_count_rank_spec(
                    segment,
                    common_tail,
                    family,
                )
            if ranking is None and shared_order is not None:
                # Each clause owns exactly one metric, while the coordinated
                # suffix owns only direction and count.  Never parse the whole
                # question for a family here: that leaked the last clause's
                # metric into every preceding family.
                if owned_metric is not None:
                    direction, limit = shared_order
                    ranking = owned_metric, direction, limit
            if ranking is None:
                # Prefer a metric stated inside this family's own clause over
                # a metric in the coordinated prefix/tail.  Otherwise a first
                # clause such as ``순자산 상위 공모펀드`` can leak AUM into the
                # following, independently labelled ``하루 거래대금 상위 국내
                # ETF`` clause merely because the request ends with ``따로``.
                ranking = self._shared_parallel_ranking(
                    question,
                    family,
                    numeric_filters,
                )
            if ranking is None and shared_limit is not None:
                local_ranking = _rank_direction_without_local_limit(segment, family)
                if local_ranking is not None:
                    metric, direction = local_ranking
                    ranking = metric, direction, shared_limit
            if ranking is None:
                explicitly_independent = re.search(
                    r"(?:각각|두\s*묶음(?:으로)?|각\s*(?:시장|상품군)|따로|구분해서|상품군별|종류별|"
                    r"합치지\s*말고|섞지\s*말고|"
                    r"합쳐.{0,16}?(?:정렬|줄\s*세우|비교).{0,8}?말(?:아|고))",
                    question,
                )
                unresolved_ranking = re.search(
                    r"(?:높은|낮은|큰|작은|많은|적은|짧은|긴|빠른|늦은|"
                    r"가까운|먼)\s*(?:순|상품|종목|것)|상위|하위|내림차순|"
                    r"오름차순|(?:TOP|톱)\s*\d+",
                    segment,
                    re.IGNORECASE,
                )
                any_explicit_ranking = re.search(
                    r"(?:높은|낮은|큰|작은|많은|적은|짧은|긴|빠른|느은|"
                    r"가까운|먼)\s*(?:순|상품|종목|것)|상위|하위|내림차순|"
                    r"오름차순|(?:TOP|톱)\s*\d+",
                    question,
                    re.IGNORECASE,
                )
                if (
                    explicitly_independent is None
                    or unresolved_ranking is not None
                    or any_explicit_ranking is not None
                ):
                    return None
                # A plain ``각각 N개`` request is already represented by the
                # established cross-family QueryPlan compiler and its atomic
                # authority gate.  Do not divert it to the compound executor:
                # doing so would duplicate semantics, lose shared filters and
                # bypass the existing concurrent execution contract.
                return None
            metric, direction, limit = ranking
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                )
            )
        if len(plans) < 2:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="parallel",
            family_plans=tuple(plans),
        )

    @staticmethod
    def _shared_parallel_ranking(
        question: str,
        family: ProductFamily,
        numeric_filters: tuple[CompoundFilter, ...],
    ) -> tuple[CompoundMetric, SortDirection, int] | None:
        """Bind a final shared ranking without leaking one family's threshold.

        A shared metric phrase (``각 상품군에서 1년 수익률 ...``) is
        parsed against each family's metric registry. ``각 지표`` is accepted
        only when that family clause contains exactly one explicit numeric
        field, making the intended metric deterministic.
        """

        shared_scope = re.search(
            r"(?:각각|각자|각\s*(?:상품군|시장|지표)(?:에서|가)?|상품군별(?:로)?|"
            r"종류별(?:로)?|따로(?:\s*(?:계산|정렬|보여|추출))?|구분해서|나눠|"
            r"각각.{0,24}?(?:각자|자기)\s*(?:기준|지표|수치))",
            question,
        )
        if shared_scope is None and _SHARED_LOCAL_METRIC_REFERENCE.search(question) is None:
            return None
        self_metric = _SHARED_LOCAL_METRIC_REFERENCE.search(question) or re.search(
            r"각\s*지표가?",
            question,
        )
        if self_metric is not None:
            unique_fields = tuple(dict.fromkeys(item.source_field for item in numeric_filters))
            limit = _final_limit(question)
            if len(unique_fields) != 1 or limit is None:
                return None
            if re.search(
                r"(?:높은|낮은|큰|작은)\s*순|내림차순|오름차순", question
            ) is None:
                return None
            return CompoundMetric("field", (unique_fields[0],)), _direction(question), limit
        family_scopes = _family_matches(question)
        if not family_scopes:
            return None
        # A shared ranking must appear after the final coordinated family.
        # Parsing the whole question could otherwise copy the first family's
        # local metric into a later family whose own clause is incomplete.
        common_tail = question[max(scope.end for scope in family_scopes) :]
        ranking = _rank_spec(common_tail, family)
        if ranking is not None:
            return ranking
        limit = _final_limit(question)
        shared_surface = f"{question[: family_scopes[0].start]} {common_tail}"
        # Only syntax outside every family-owned clause can be shared.  Looking
        # across the whole question copied the first clause's AUM into a later
        # clause with an unsupported ``샤프지수`` ranking.
        shared_metric = _unique_metric_from_label(shared_surface, family)
        shared_direction = _explicit_rank_direction(shared_surface)
        if shared_metric is not None and shared_direction is not None and limit is not None:
            return shared_metric, shared_direction, limit
        # In shared wording the family split can sit between the ordering cue
        # and the result count (``수익률이 높은 상품을 종류별로 5개씩``).
        # Bind only an explicit metric + direction and reuse the one explicit
        # global count; no field or direction is inferred from the family.
        if limit is None:
            return None
        shared_order = re.search(
            r"(?P<label>.{1,100}?)(?:이|가|은|는)?\s*"
            r"(?P<cue>높은|낮은|큰|작은|많은|적은|짧은|긴)\s*"
            r"(?:상품|종목|것)(?:을|를|이|가|은|는)?\s*"
            r"(?:종류별(?:로)?|상품군별(?:로)?|각각|따로)",
            common_tail,
        )
        if shared_order is None:
            return None
        metric = _metric_from_label(shared_order.group("label"), family)
        if metric is None:
            return None
        cue = shared_order.group("cue")
        if cue in {"짧은", "긴"} and metric.source_fields not in {
            ("remaining_days",),
            ("dur",),
        }:
            return None
        direction = (
            SortDirection.ASC
            if cue in {"낮은", "작은", "적은", "짧은"}
            else SortDirection.DESC
        )
        return metric, direction, limit

    def _compile_derived(self, question: str, request_id: str) -> TypedCompoundPlan | None:
        segments = _plan_segments(question)
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        filters = _compiled_clause_filters(scope, segment)
        if filters is None:
            return None
        metric = _derived_metric(segment, family)
        limit = _final_limit(segment)
        if metric is None or limit is None:
            return None
        if re.search(
            r"(?:높은|낮은|큰|작은)\s*순|내림차순|오름차순|"
            r"상위\s*(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)|"
            r"하위\s*(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)",
            segment,
        ) is None:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="derived_ranking",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=_direction(segment),
                    limit=limit,
                ),
            ),
        )

    def _compile_field_comparison(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile row-wise field comparison plus an explicit gap ranking."""

        segments = _plan_segments(question)
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        parsed = _field_comparison_spec(segment, family)
        if parsed is None:
            return None
        metric, comparison, direction, limit = parsed
        filters = _compiled_clause_filters(
            scope,
            segment,
            extra_represented_source_fields=tuple(metric.source_fields),
            allow_owned_anchor_comparison=True,
        )
        if filters is None:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="field_relative_ranking",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                    field_comparisons=(comparison,),
                    projection_fields=tuple(metric.source_fields),
                ),
            ),
        )

    def _compile_aggregate_threshold(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile one single-family aggregate bound followed by a final rank."""

        segments = _plan_segments(question)
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        parsed_threshold = _aggregate_threshold_spec(segment, family)
        if parsed_threshold is None:
            return None
        aggregate_threshold, threshold_end = parsed_threshold
        ranking = _aggregate_final_rank_spec(
            segment,
            family,
            threshold_end=threshold_end,
            threshold=aggregate_threshold,
        )
        filters = _compiled_clause_filters(
            scope,
            segment,
            extra_represented_source_fields=(
                *aggregate_threshold.metric.source_fields,
                *(
                    ("pd_pbcm",)
                    if family is ProductFamily.BOND
                    and _explicit_bond_issuer_cohort(segment) is not None
                    else ()
                ),
            ),
        )
        if filters is None or ranking is None:
            return None
        issuer = (
            _explicit_bond_issuer_cohort(segment)
            if family is ProductFamily.BOND
            else None
        )
        if issuer is not None:
            issuer_filter = CompoundFilter("pd_pbcm", "eq", issuer)
            existing_issuers = tuple(
                item
                for item in filters
                if item.source_field == "pd_pbcm" and item.operator == "eq"
            )
            if existing_issuers and any(item != issuer_filter for item in existing_issuers):
                return None
            if issuer_filter not in filters:
                filters = (*filters, issuer_filter)
        metric, direction, limit = ranking
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="aggregate_threshold",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                    aggregate_threshold=aggregate_threshold,
                    projection_fields=aggregate_threshold.metric.source_fields,
                ),
            ),
        )

    def _compile_bounded(self, question: str, request_id: str) -> TypedCompoundPlan | None:
        segments = _plan_segments(question)
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        filters = _compiled_clause_filters(scope, segment)
        ranking = _rank_spec(segment, family)
        if filters is None or not filters or ranking is None:
            return None
        metric, direction, limit = ranking
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="bounded",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=direction,
                    limit=limit,
                ),
            ),
        )

    def _database_relative_anchor(
        self,
        question: str,
        *,
        family: ProductFamily | None = None,
    ) -> tuple[str, str, ProductFamily] | None:
        """Return one DB identity that owns a complete relative clause."""

        anchor = self._database_relative_anchor_context(question, family=family)
        if anchor is None:
            return None
        return (
            anchor.surface,
            anchor.public_product_id,
            anchor.product_family,
        )

    def _database_relative_anchor_context(
        self,
        question: str,
        *,
        family: ProductFamily | None = None,
    ) -> _DatabaseRelativeAnchor | None:
        """Bind corroborating name/ticker/ID surfaces to one reference row.

        A product printed as ``name (ticker, ID product_id)`` creates several
        exact matcher hits.  They are corroboration for one product, not
        several anchors.  Conversely, two different canonical products or an
        ambiguous physical row are rejected before any threshold is read.
        """

        matches = self.database_identity_matcher.find(question)
        if not matches:
            return None
        logical_keys: set[tuple[ProductFamily, str]] = set()
        for match in matches:
            if not match.is_unique:
                return None
            candidate = match.candidates[0]
            if family is None or candidate.product_family is family:
                logical_keys.add(
                    (candidate.product_family, candidate.canonical_literal)
                )
        if len(logical_keys) != 1:
            return None
        anchor_family, public_product_id = next(iter(logical_keys))
        related = tuple(
            match
            for match in matches
            if any(
                candidate.product_family is anchor_family
                and candidate.canonical_literal == public_product_id
                for candidate in match.candidates
            )
        )
        if not related:
            return None
        identity_spans = tuple((match.start, match.end) for match in related)
        equal_source_fields = _anchor_equal_source_fields(
            question,
            anchor_family,
            identity_spans,
        )
        directly_owned = tuple(
            match
            for match in related
            if re.match(
                r"\s*(?:(?:,\s*(?:장내|장외))?\)|\]|[’”'\"])?\s*(?:"
                + _ANCHOR_DECLARATION_SUFFIX
                + r"|보다|대비|의\s*.{1,60}?(?:을|를)?\s*"
                r"(?:넘는|웃도는|웃돌(?:고|며|는)|초과하는|밑도는|미만인))",
                question[match.end :],
                re.IGNORECASE,
            )
            is not None
        )
        if not directly_owned and not equal_source_fields:
            return None

        # Prefer an explicitly supplied physical product_id so bond venue and
        # overseas exchange rows read the exact reference value.  A unique
        # single-row name remains valid; a name spanning multiple physical
        # observations without a disambiguating ID fails closed.
        explicit_rows = {
            candidate.product_id
            for match in related
            for candidate in match.candidates
            if candidate.product_family is anchor_family
            and candidate.canonical_literal == public_product_id
            and candidate.alias_kind == "product_id"
            and normalize_database_identity(match.surface)
            == normalize_database_identity(candidate.product_id)
        }
        physical_rows = {
            candidate.product_id
            for match in related
            for candidate in match.candidates
            if candidate.product_family is anchor_family
            and candidate.canonical_literal == public_product_id
        }
        if len(explicit_rows) == 1:
            lookup_product_id = next(iter(explicit_rows))
        elif len(physical_rows) == 1:
            lookup_product_id = next(iter(physical_rows))
        else:
            return None

        surface_match = (
            directly_owned[-1]
            if directly_owned
            else next(
                (
                    match
                    for match in reversed(related)
                    if normalize_database_identity(match.surface)
                    == normalize_database_identity(public_product_id)
                ),
                related[-1],
            )
        )
        return _DatabaseRelativeAnchor(
            surface=surface_match.surface,
            lookup_product_id=lookup_product_id,
            public_product_id=public_product_id,
            product_family=anchor_family,
            identity_spans=identity_spans,
            equal_source_fields=equal_source_fields,
        )

    def _compile_anchor_relative(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        database_anchor = self._database_relative_anchor_context(question)
        segments = _plan_segments(question)
        if not segments and database_anchor is not None:
            # An exact product can establish the family even when the user
            # writes only ``이 채권`` or ``상품`` after the identity.
            segments = [
                (
                    _FamilyScope(
                        start=0,
                        end=len(question),
                        product_family=database_anchor.product_family,
                        filters=(),
                    ),
                    question,
                )
            ]
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        if database_anchor is not None and database_anchor.product_family is not family:
            return None
        identifier_match = re.search(
            r"(?:\(([A-Z0-9.]{2,20})(?:,\s*장내)?\)|"
            # In ``ETF 기준식별자 ABC...`` the family acronym is not the
            # anchor. Parenthesized tickers and actual bare IDs retain the
            # existing ``기준점``/``기준상품`` grammar.
            r"(?<![A-Z0-9])((?!(?:ETF|ETN|ETP))[A-Z][A-Z0-9.]{1,19}|"
            r"KR[A-Z0-9]{10}))"
            r"(?=\s*(?:보다|대비|(?:을|를)?\s*기준|의\s*.{1,60}?"
            r"(?:넘는|웃도는|웃돌(?:고|며|는)|초과하는|밑도는|미만인)))",
            segment,
            re.IGNORECASE,
        )
        if database_anchor is None and identifier_match is None:
            return None
        ranking = _rank_spec(segment, family)
        ranking_metric: CompoundMetric | None
        ranking_direction: SortDirection | None
        limit: int | None
        if ranking is None:
            ranking_metric = None
            ranking_direction = None
            limit = None
        else:
            ranking_metric, ranking_direction, limit = ranking
        if database_anchor is not None:
            anchor_literal = database_anchor.surface
            anchor_lookup_id = database_anchor.lookup_product_id
            anchor_public_id = database_anchor.lookup_product_id
            equal_source_fields = database_anchor.equal_source_fields
            semantic_segment = _mask_anchor_identity_surfaces(
                segment,
                database_anchor.identity_spans,
            )
        else:
            assert identifier_match is not None
            anchor_literal = identifier_match.group(1) or identifier_match.group(2)
            anchor_lookup_id = anchor_literal
            anchor_public_id = anchor_literal
            equal_source_fields = ()
            semantic_segment = segment
        canonical_anaphoric_specs = _anaphoric_anchor_relative_specs(
            semantic_segment,
            family,
        )
        if canonical_anaphoric_specs is None:
            return None
        if self.database_paths and database_anchor is None:
            resolved_anchor = self._resolve_identity(anchor_public_id)
            if resolved_anchor is None or resolved_anchor[1] is not family:
                return None
            anchor_lookup_id = resolved_anchor[0]
            anchor_public_id = resolved_anchor[0]
        relative_specs: list[tuple[CompoundMetric, str]] = list(
            canonical_anaphoric_specs
        )
        for match in re.finditer(
            r"이\s*상품\s*(이상|이하|초과|미만)인\s*(.{1,60}?)(?=과\s*이\s*상품|와\s*이\s*상품|을\s*동시에|를\s*동시에|\s*동시에)",
            segment,
        ):
            metric = _metric_from_label(match.group(2), family)
            if metric is None:
                return None
            pair = (
                metric,
                {"이상": "gte", "이하": "lte", "초과": "gt", "미만": "lt"}[
                    match.group(1)
                ],
            )
            if pair not in relative_specs:
                relative_specs.append(pair)
        # Natural relative clauses may place multiple metrics after one
        # anchor: ``VOO보다 보수율은 낮고 AUM은 작은``.  Parse every
        # conjunction-local metric instead of authorizing only the first one.
        relative_tail = re.search(
            rf"{re.escape(anchor_literal)}\s*(?:보다|대비)\s*(?P<tail>.{{1,180}})",
            segment,
            re.IGNORECASE,
        )
        if relative_tail is not None:
            tail = relative_tail.group("tail")
            for clause in re.finditer(
                r"(?:^|(?:고|며|,|그리고)\s*)"
                r"(?P<label>.{1,48}?)(?:은|는|이|가|도)?\s*"
                r"(?:더|더욱)?\s*"
                r"(?P<relation>큰|작은|높은|낮은|많은|적은|크|작|높|낮|많|적)"
                r"(?=\s*(?:고|며|,|다른\s*(?:상품|종목|펀드|ETF|ETN|채권)|"
                r"나머지\s*(?:상품|종목|펀드|ETF|ETN|채권)|"
                r"상품|종목|펀드|ETF|ETN|채권|것|만|을|를|$))",
                tail,
                re.IGNORECASE,
            ):
                metric = _metric_from_label(clause.group("label"), family)
                if metric is None:
                    # Once a named anchor owns the comparison, every explicit
                    # conjunct must map to an approved metric. Keeping only the
                    # recognized conjuncts would execute a weaker screen than
                    # the user requested.
                    return None
                relation = clause.group("relation")
                greater = relation in {"큰", "높은", "많은", "크", "높", "많"}
                pair = (
                    metric,
                    "gt" if greater else "lt",
                )
                if pair not in relative_specs:
                    relative_specs.append(pair)
        # After one explicit anchor, Korean speakers often refer back to it as
        # ``그 기준채권`` or ``해당 기준 상품``.  The pronoun may
        # introduce an additional field threshold, but it can never replace or
        # create the anchor identity.
        reference_tail = re.search(
            r"(?:그|해당|이)\s*(?:기준\s*)?(?:상품|종목|채권|펀드|ETF)\s*"
            r"(?:보다|대비)\s*(?P<tail>.{1,160})",
            segment,
            re.IGNORECASE,
        )
        if reference_tail is not None:
            tail = reference_tail.group("tail")
            for clause in re.finditer(
                r"(?:^|(?:고|며|,|그리고|[\u00b7ㆍ/])\s*)"
                r"(?P<label>.{1,48}?)(?:은|는|이|가|도)?\s*"
                r"(?:더|더욱)?\s*"
                r"(?P<relation>큰|작은|높은|낮은|많은|적은|짧은|긴|나은|"
                r"크|작|높|낮|많|적|짧|길)"
                r"(?=\s*(?:고|며|,|다른\s*(?:상품|종목|펀드|ETF|채권)|"
                r"상품|종목|펀드|ETF|채권|것|만|을|를|$))",
                tail,
                re.IGNORECASE,
            ):
                metric = _metric_from_label(clause.group("label"), family)
                if metric is None:
                    return None
                relation = clause.group("relation")
                greater = relation in {
                    "큰",
                    "높은",
                    "많은",
                    "긴",
                    "나은",
                    "크",
                    "높",
                    "많",
                    "길",
                }
                pair = (metric, "gt" if greater else "lt")
                if pair not in relative_specs:
                    relative_specs.append(pair)
        # An equality-owned reference product also supplies the comparison
        # threshold for an explicitly relative metric in the same sentence:
        # ``A와 같은 운용사 중 보수가 더 낮은``.  Requiring ``보다`` again
        # would reject ordinary Korean while the exact anchor and the word
        # ``더`` already make the threshold owner unambiguous.
        if equal_source_fields:
            for clause in re.finditer(
                r"(?P<label>.{1,64}?)(?:은|는|이|가|도)?\s*"
                r"(?:더|더욱)\s*"
                r"(?P<relation>큰|작은|높은|낮은|많은|적은|짧은|긴|나은)"
                r"(?=\s*(?:(?:국내|해외|원화|공모)\s*)?"
                r"(?:상품|종목|펀드|ETF|ETN|ETP|채권|것|을|를|중|가운데|"
                r"\d+\s*개|한\s*개|두\s*개|세\s*개|네\s*개|"
                r"다섯\s*개|여섯\s*개|일곱\s*개|여덟\s*개|[,.;]|$))",
                semantic_segment,
                re.IGNORECASE,
            ):
                metric = _metric_from_label(clause.group("label"), family)
                if metric is None:
                    return None
                relation = clause.group("relation")
                pair = (
                    metric,
                    (
                        "gt"
                        if relation in {"큰", "높은", "많은", "긴", "나은"}
                        else "lt"
                    ),
                )
                if pair not in relative_specs:
                    relative_specs.append(pair)
        # ``<ID> 기준 A 이상·B 이하`` is an explicit request to use
        # the named record's A/B values as deterministic thresholds.  Multiple
        # clauses are captured independently; no threshold is guessed.
        anchor_threshold_tail = re.search(
            rf"{re.escape(anchor_literal)}\s*(?:을|를)?\s*기준(?:으로)?\s*"
            r"(?P<tail>.{1,180})",
            segment,
            re.IGNORECASE,
        )
        if anchor_threshold_tail is not None:
            for clause in re.finditer(
                r"(?:^|(?:고|며|,|그리고|[\u00b7ㆍ/])\s*)"
                r"(?P<label>.{1,64}?)(?:은|는|이|가|을|를)?\s*"
                r"(?P<operator>이상|이하|초과|미만)"
                r"(?=\s*(?:고|며|,|그리고|[\u00b7ㆍ/]|다른|나머지|상품|종목|$))",
                anchor_threshold_tail.group("tail"),
                re.IGNORECASE,
            ):
                metric = _metric_from_label(clause.group("label"), family)
                if metric is None:
                    return None
                pair = (
                    metric,
                    {"이상": "gte", "이하": "lte", "초과": "gt", "미만": "lt"}[
                        clause.group("operator")
                    ],
                )
                if pair not in relative_specs:
                    relative_specs.append(pair)
        possessive = re.search(
            rf"{re.escape(anchor_literal)}의\s*(?P<label>.{{1,64}}?)(?:을|를)?\s*"
            r"(?P<relation>넘는|웃도는|웃돌고|웃돌며|웃돌는|"
            r"초과하는|밑도는|미만인)",
            segment,
            re.IGNORECASE,
        )
        if possessive is not None:
            metric = _metric_from_label(possessive.group("label"), family)
            if metric is None:
                return None
            relation = possessive.group("relation")
            pair = (
                metric,
                "lt" if relation in {"밑도는", "미만인"} else "gt",
            )
            if pair not in relative_specs:
                relative_specs.append(pair)
        if not relative_specs:
            direct = re.search(
                rf"{re.escape(anchor_literal)}\s*(?:보다|대비)\s*"
                r"(.{1,60}?)(?:이|가|은|는)?\s*"
                r"(큰|작은|높은|낮은|많은|적은)",
                segment,
                re.IGNORECASE,
            )
            if direct is not None:
                metric = _metric_from_label(direct.group(1), family)
                relation = direct.group(2)
                if metric is None:
                    return None
                relative_specs.append(
                    (
                        metric,
                        {
                            "큰": "gt",
                            "높은": "gt",
                            "많은": "gt",
                            "작은": "lt",
                            "낮은": "lt",
                            "적은": "lt",
                        }[relation],
                    )
                )
        if not relative_specs:
            legacy = re.search(
                r"비교해\s*볼게요\.\s*(.+?)(?:이|가)\s*이\s*상품"
                r"(?:보다\s*(큰|작은)|\s*(이상|이하)인)",
                segment,
            )
            if legacy is None:
                return None
            metric = _metric_from_label(legacy.group(1), family)
            relation = legacy.group(2) or legacy.group(3)
            if metric is None or relation is None:
                return None
            relative_specs.append(
                (metric, {"큰": "gt", "작은": "lt", "이상": "gte", "이하": "lte"}[relation])
            )
        if ranking_metric is None or ranking_direction is None or limit is None:
            unique_relative = tuple(dict.fromkeys(relative_specs))
            limit = _final_limit(segment)
            if (
                len(unique_relative) != 1
                or limit is None
                or re.search(r"순|상위|하위|오름차순|내림차순", segment) is None
            ):
                return None
            ranking_metric, relative_operator = unique_relative[0]
            explicit_ascending = re.search(
                r"오름차순|낮은\s*순|작은\s*순|짧은\s*순|싼\s*순|하위",
                segment,
            )
            explicit_descending = re.search(
                r"내림차순|높은\s*순|큰\s*순|긴\s*순|상위",
                segment,
            )
            if explicit_ascending is not None and explicit_descending is not None:
                return None
            if explicit_ascending is not None:
                ranking_direction = SortDirection.ASC
            elif explicit_descending is not None:
                ranking_direction = SortDirection.DESC
            else:
                # ``금리순/규모순/성과순`` reuses the only field already
                # tied to the reference threshold; its explicit higher/lower
                # relation supplies the direction without introducing a new
                # ranking field.
                ranking_direction = (
                    SortDirection.DESC
                    if relative_operator in {"gt", "gte"}
                    else SortDirection.ASC
                )
        relative_source_fields = tuple(
            dict.fromkeys(field for metric, _ in relative_specs for field in metric.source_fields)
        )
        source_fields = tuple(
            dict.fromkeys((*relative_source_fields, *equal_source_fields))
        )
        anchor = self._find_record(family, anchor_lookup_id, source_fields)
        if anchor is None:
            return None
        relative_filters: list[tuple[CompoundMetric, str, Decimal]] = []
        for metric, operator in relative_specs:
            anchor_value = _metric_value(anchor, metric)
            if not isinstance(anchor_value, Decimal):
                return None
            relative_filters.append((metric, operator, anchor_value))
        anchor_equality_filters: list[CompoundFilter] = []
        registry = load_field_registry()
        for source_field in equal_source_fields:
            raw_value = _raw_value(anchor, source_field)
            canonical = _SOURCE_TO_CANONICAL.get((family, source_field))
            if canonical is None:
                return None
            definition = registry.require_field(canonical, [family.value])
            if raw_value is None or (
                isinstance(raw_value, str) and not raw_value.strip()
            ):
                return None
            if definition.value_type is ValueType.NUMBER and _decimal(raw_value) is None:
                return None
            if definition.source.transform is SourceTransform.CODE_MAP:
                canonical_value = self._canonical_storage_value(
                    family,
                    anchor_lookup_id,
                    canonical,
                )
                if canonical_value is None:
                    return None
                # Keep both representations: the raw code is used to produce
                # the public canonical QueryPlan value, while the normalized
                # DB value is used for exact cohort equality.  This matters
                # when one raw code has legacy display variants in the source.
                anchor_equality_filters.append(
                    CompoundFilter(
                        source_field,
                        "canonical_eq",
                        (raw_value, canonical_value),
                    )
                )
            else:
                anchor_equality_filters.append(
                    CompoundFilter(source_field, "eq", raw_value)
                )
        filters = _compiled_clause_filters(
            scope,
            semantic_segment,
            extra_represented_source_fields=tuple(
                dict.fromkeys((*source_fields, *ranking_metric.source_fields))
            ),
            allow_owned_anchor_comparison=True,
        )
        if filters is None:
            return None
        filters = _without_dataset_label_filter(scope, filters, semantic_segment)
        for equality_filter in anchor_equality_filters:
            same_field = tuple(
                item
                for item in filters
                if item.source_field == equality_filter.source_field
            )
            if same_field and any(item != equality_filter for item in same_field):
                return None
            if equality_filter not in filters:
                filters = (*filters, equality_filter)
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="anchor_relative",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=ranking_metric,
                    direction=ranking_direction,
                    limit=limit,
                    exclude_product_ids=(anchor_public_id,),
                    relative_filters=tuple(relative_filters),
                    projection_fields=source_fields,
                ),
            ),
        )

    def _compile_cohort(self, question: str, request_id: str) -> TypedCompoundPlan | None:
        matches = _family_matches(question)
        if len(matches) != 2:
            return None
        distribution = re.search(r"분포", question)
        if distribution is None:
            return None
        # Cohort execution currently has one deliberately narrow, public
        # contract: count/mean/min/max plus the three largest representatives.
        # Do not translate a requested median, sum, lower tail, different K,
        # or between-cohort statistic into that fixed calculation.
        requested_summaries: set[str] = set()
        if re.search(
            r"(?:유효\s*)?(?:상품|종목|펀드)\s*(?:수|개수)|(?:개수|건수)",
            question,
        ):
            requested_summaries.add("count")
        if re.search(r"(?:단순\s*)?평균", question):
            requested_summaries.add("mean")
        if re.search(r"중앙값|중위수", question):
            requested_summaries.add("median")
        if re.search(r"최솟값|최소값", question):
            requested_summaries.add("minimum")
        if re.search(r"최댓값|최대값|최고값", question):
            requested_summaries.add("maximum")
        if re.search(r"합계|총합", question):
            requested_summaries.add("sum")
        if requested_summaries != {"count", "mean", "minimum", "maximum"}:
            return None
        winner_cues = {
            match.group("cue")
            for match in re.finditer(
                r"평균(?:값)?(?:이|가)?\s*더\s*"
                r"(?P<cue>큰|작은|높은|낮은)\s*"
                r"(?:집단|그룹|묶음|쪽)",
                question,
                re.IGNORECASE,
            )
        }
        if len(winner_cues) > 1:
            return None
        cohort_mean_direction = None
        if winner_cues:
            cohort_mean_direction = (
                SortDirection.DESC
                if next(iter(winner_cues)) in {"큰", "높은"}
                else SortDirection.ASC
            )
        cohort_mean_difference = re.search(
            r"(?:집단|그룹|묶음)(?:\s*간|끼리의?)?\s*평균(?:값)?\s*"
            r"(?:간\s*)?(?:차이|격차)|평균(?:값)?\s*(?:간\s*)?(?:차이|격차)",
            question,
            re.IGNORECASE,
        ) is not None
        # Other between-cohort statistics remain outside this narrow typed
        # contract. Never substitute a mean comparison for max/min/sum/median.
        if re.search(
            r"(?:중앙값|중위수|합계|총합|최솟값|최소값|최댓값|최대값)"
            r"(?:이|가)?\s*더\s*(?:큰|작은|높은|낮은)\s*"
            r"(?:집단|그룹|묶음|쪽)|"
            r"(?:중앙값|중위수|합계|총합|최솟값|최소값|최댓값|최대값)\s*"
            r"(?:차이|격차)",
            question,
            re.IGNORECASE,
        ) is not None:
            return None
        representative = re.search(
            r"(?P<direction>상위|하위)\s*"
            r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*개",
            question,
        )
        if representative is None or representative.group("direction") != "상위":
            return None
        native_counts = {
            "한": 1,
            "두": 2,
            "세": 3,
            "셋": 3,
            "네": 4,
            "넷": 4,
            "다섯": 5,
            "여섯": 6,
            "일곱": 7,
            "여덟": 8,
            "아홉": 9,
            "열": 10,
        }
        raw_count = representative.group("count")
        representative_limit = (
            int(raw_count) if raw_count.isdigit() else native_counts[raw_count]
        )
        if representative_limit != 3:
            return None
        metric_surface = question[: distribution.start()]
        segments = _plan_segments(question)
        if len(segments) != 2:
            return None
        last_family_end = max(scope.end for scope in matches)
        plans: list[CompoundFamilyPlan] = []
        for scope, segment in segments:
            family = scope.product_family
            metric = _metric_from_label(metric_surface, family)
            if metric is None:
                return None
            local_text = _shared_compound_text(
                question,
                scope,
                segment,
                last_family_end=last_family_end,
            )
            filters = _compiled_clause_filters(
                scope,
                local_text,
                extra_represented_source_fields=metric.source_fields,
            )
            if filters is None:
                return None
            filters = _without_dataset_label_filter(scope, filters, local_text)
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=SortDirection.DESC,
                    limit=3,
                    cohort_label=scope.label,
                )
            )
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.AGGREGATE,
            kind="cohort",
            family_plans=tuple(plans),
            cohort_mean_direction=cohort_mean_direction,
            cohort_mean_difference=cohort_mean_difference,
        )

    def _compile_family_summary_aggregate(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile one compatible measure independently for each family.

        This is deliberately not a cross-family arithmetic comparison.  Each
        family is filtered and aggregated in its own approved database, and
        the results are only presented side-by-side.  That allows currency
        amounts such as KRW AUM when every family is explicitly KRW-filtered,
        while still preventing an unapproved FX conversion or mixed-unit
        global ranking.
        """

        if re.search(r"상품군\s*별|각\s*상품군|각각", question) is None:
            return None
        count_cue = re.search(
            r"(?:몇|\b각각\b).{0,8}(?:개|건|종목|상품)|"
            r"(?:상품|종목|펀드)\s*(?:수|개수|건수)",
            question,
        )
        average_cue = re.search(r"산술\s*평균|평균(?:값|치)?", question)
        maximum_cue = re.search(r"최(?:대|고)(?:값)?", question)
        selected = sum(item is not None for item in (count_cue, average_cue, maximum_cue))
        if selected != 1:
            return None
        if count_cue is not None:
            return self._compile_family_count_aggregate(question, request_id)

        function = (
            AggregateFunction.AVG
            if average_cue is not None
            else AggregateFunction.MAX
        )
        segments = _plan_segments(question)
        if len(segments) < 2 or len({item.product_family for item, _ in segments}) != len(
            segments
        ):
            return None
        last_family_end = max(scope.end for scope, _segment in segments)
        registry = load_field_registry()
        plans: list[CompoundFamilyPlan] = []
        canonical_fields: set[str] = set()
        units: set[str] = set()
        for scope, segment in segments:
            family = scope.product_family
            metric = _metric_from_label(question, family)
            if metric is None or metric.kind != "field":
                return None
            source_field = metric.source_fields[0]
            canonical = _SOURCE_TO_CANONICAL.get((family, source_field))
            if canonical is None:
                return None
            definition = registry.require_field(canonical, [family.value])
            if definition.value_type is not ValueType.NUMBER or not definition.aggregatable:
                return None
            canonical_fields.add(canonical)
            units.add(definition.unit)
            local_text = _shared_compound_text(
                question,
                scope,
                segment,
                last_family_end=last_family_end,
            )
            filters = _compiled_clause_filters(
                scope,
                local_text,
                extra_represented_source_fields=(source_field,),
            )
            if filters is None:
                return None
            filters = _without_dataset_label_filter(scope, filters, local_text)
            filters = _with_approved_fund_scope(family, filters)
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=metric,
                    direction=SortDirection.ASC,
                    limit=1,
                    aggregations=(CompoundAggregation(function, source_field),),
                )
            )
        if len(canonical_fields) != 1 or len(units) != 1:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.AGGREGATE,
            kind="family_summary_aggregate",
            family_plans=tuple(plans),
        )

    def _compile_family_count_aggregate(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile an explicit product count independently for each family."""

        if re.search(
            r"(?:상품|종목|펀드)\s*(?:수|개수|건수)|"
            r"(?:몇|각각).{0,10}(?:개|건|종목|상품)",
            question,
        ) is None or re.search(r"집계|세어|계산|알려|몇", question) is None:
            return None
        segments = _plan_segments(question)
        if len(segments) < 2:
            return None
        plans: list[CompoundFamilyPlan] = []
        last_family_end = max(scope.end for scope, _segment in segments)
        local_count_metrics = [
            _unique_metric_from_label(segment, scope.product_family)
            for scope, segment in segments
        ]
        per_family_valid_metric_count = (
            re.search(
                r"0\s*도\s*결측(?:값)?\s*도\s*아닌|0\s*이\s*아닌",
                question,
            )
            is not None
            and all(metric is not None for metric in local_count_metrics)
        )
        for index, (scope, segment) in enumerate(segments):
            group_source = _explicit_group_source(question, scope.product_family)
            local_metric = local_count_metrics[index]
            local_text = (
                segment
                if per_family_valid_metric_count
                else _shared_compound_text(
                    question,
                    scope,
                    segment,
                    last_family_end=last_family_end,
                )
            )
            local_text = re.sub(
                r"(?:이|가|은|는)?\s*(?:0\s*도\s*결측(?:값)?\s*도\s*아닌|0\s*이\s*아닌)",
                " ",
                local_text,
            )
            local_text = re.sub(
                r"(?:판매\s*불가|판매불가)\s*[·ㆍ/,와과및]+\s*"
                r"(?:판매\s*가능|판매가능)\s*순(?:서)?(?:대로|으로)?|"
                r"(?:판매\s*가능|판매가능)\s*[·ㆍ/,와과및]+\s*"
                r"(?:판매\s*불가|판매불가)\s*순(?:서)?(?:대로|으로)?|"
                r"상품군\s*순서(?:대로)?",
                " ",
                local_text,
            )
            filters = _compiled_clause_filters(
                scope,
                local_text,
                extra_represented_source_fields=(
                    *((group_source,) if group_source is not None else ()),
                    *(
                        local_metric.source_fields
                        if per_family_valid_metric_count and local_metric is not None
                        else ()
                    ),
                ),
            )
            if filters is None:
                return None
            filters = _without_dataset_label_filter(scope, filters, local_text)
            filters = _with_approved_fund_scope(scope.product_family, filters)
            # ``판매가능 여부별`` and equivalent contrastive group surfaces
            # name the dimension to group by; they do not request only the
            # affirmative bucket.  The ordinary linker intentionally maps
            # ``판매가능`` to a row predicate, so remove that predicate only
            # when the question explicitly asks for every value of the same
            # field.  A request such as ``판매 가능한 상품만 ... 집계`` does
            # not match these surfaces and keeps its filter.
            if group_source is not None and re.search(
                r"(?:여부|유형|종류|코드|통화|지역|속성|대분류|시장|발행사|운용사)\s*별|"
                r"(?:판매\s*불가|판매불가)\s*[·ㆍ/,와과및]+\s*"
                r"(?:판매\s*가능|판매가능)|"
                r"(?:판매\s*가능|판매가능)\s*[·ㆍ/,와과및]+\s*"
                r"(?:판매\s*불가|판매불가)",
                question,
            ):
                filters = tuple(
                    item for item in filters if item.source_field != group_source
                )
            identity_source = _DEFAULT_ORDER_FIELD_BY_FAMILY[scope.product_family]
            if per_family_valid_metric_count and local_metric is not None:
                filters = (
                    *filters,
                    CompoundFilter(local_metric.source_fields[0], "neq", 0),
                )
            plans.append(
                CompoundFamilyPlan(
                    product_family=scope.product_family,
                    filters=filters,
                    metric=CompoundMetric("field", (identity_source,)),
                    direction=SortDirection.ASC,
                    limit=100 if group_source is not None else 1,
                    group_by_source_field=group_source,
                    aggregations=(
                        CompoundAggregation(AggregateFunction.COUNT, identity_source),
                    ),
                )
            )
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.AGGREGATE,
            kind="family_count_aggregate",
            family_plans=tuple(plans),
        )

    def _compile_grouped_topk(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile ``each group -> metric TOP-K`` as one sealed search plan.

        Group qualification counts usable values of the explicitly named
        metric.  Ranking happens independently inside each qualified group;
        groups and equal-valued products use stable label/ID ordering.
        """

        segments = _plan_segments(question)
        if not segments or len(segments) > 4 or not _has_grouped_product_topk_surface(question):
            return None
        last_family_end = max(scope.end for scope, _segment in segments)
        plans: list[CompoundFamilyPlan] = []
        canonical_groups: set[str] = set()
        for scope, segment in segments:
            family = scope.product_family
            local_text = _shared_compound_text(
                question,
                scope,
                segment,
                last_family_end=last_family_end,
            )
            group_source = _explicit_group_source(local_text, family)
            ranking = _group_product_rank_spec(local_text, family)
            if group_source is None or ranking is None:
                return None
            metric, direction, per_group_limit = ranking
            if metric.kind != "field":
                return None
            group_min_spec = _group_min_count_spec(local_text, family)
            if re.search(r"유효|표본", local_text) is not None and group_min_spec is None:
                return None
            all_metric_sources = {
                source
                for _start, _end, source in _ordered_metric_mentions(local_text, family)
                if _numeric_source_field(family, source)
            }
            if len(all_metric_sources) > 1:
                rank_owner_start = (
                    0 if group_min_spec is None else group_min_spec[2].end()
                )
                rank_owner_sources = {
                    source
                    for _start, _end, source in _ordered_metric_mentions(
                        local_text[rank_owner_start:],
                        family,
                    )
                    if _numeric_source_field(family, source)
                }
                if rank_owner_sources != {metric.source_fields[0]}:
                    return None

            group_canonical = _SOURCE_TO_CANONICAL.get((family, group_source))
            if group_canonical is None:
                return None
            canonical_groups.add(group_canonical)
            explicit_values = _explicit_group_values(local_text, family, group_source)

            # HAVING and group-domain phrases are plan structure, not
            # independent row predicates for the ordinary Linker.
            filter_text = local_text
            if group_min_spec is not None:
                match = group_min_spec[2]
                filter_text = filter_text[: match.start()] + " " + filter_text[match.end() :]
            if explicit_values:
                for value in sorted(explicit_values, key=len, reverse=True):
                    filter_text = re.sub(
                        rf"(?<![0-9A-Za-z가-힣]){re.escape(value)}"
                        rf"(?![0-9A-Za-z가-힣])",
                        " ",
                        filter_text,
                        flags=re.IGNORECASE,
                    )
                filter_text = re.sub(r"\(\s*[·ㆍ,/&+와과및\s]*\)", " ", filter_text)
            filters = _compiled_clause_filters(
                scope,
                filter_text,
                extra_represented_source_fields=(
                    group_source,
                    *metric.source_fields,
                    *(
                        ()
                        if group_min_spec is None
                        else (group_min_spec[1],)
                    ),
                ),
            )
            if filters is None:
                return None
            filters = _without_dataset_label_filter(scope, filters, filter_text)
            if explicit_values:
                filters = tuple(item for item in filters if item.source_field != group_source)
                filters = (
                    *filters,
                    CompoundFilter(group_source, "in", explicit_values),
                )
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=tuple(dict.fromkeys(filters)),
                    metric=metric,
                    direction=direction,
                    limit=per_group_limit,
                    projection_fields=(group_source,),
                    group_by_source_field=group_source,
                    group_min_count=(
                        None if group_min_spec is None else group_min_spec[0]
                    ),
                    group_min_count_source_field=(
                        None if group_min_spec is None else group_min_spec[1]
                    ),
                    per_group_limit=per_group_limit,
                )
            )
        if len(plans) > 1 and len(canonical_groups) != 1:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="grouped_topk",
            family_plans=tuple(plans),
        )

    def _compile_grouped_aggregate(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile one explicit group field and its named aggregate measures.

        Grouping is schema-driven and never guessed from a cohort value.  A
        HAVING-like sample bound is carried in the sealed compound plan rather
        than being disguised as a row filter in the public QueryPlan.
        """

        segments = _plan_segments(question)
        if len(segments) != 1:
            return None
        scope, segment = segments[0]
        family = scope.product_family
        group_sources = _explicit_group_sources(segment, family)
        if not group_sources and re.search(
            r"위험\s*등급\s*(?:마다|별(?:로)?)", segment
        ) is not None:
            risk_source = _RISK_GROUP_SOURCE_BY_FAMILY.get(family)
            group_sources = () if risk_source is None else (risk_source,)
        if not group_sources:
            return None
        group_source = group_sources[0]

        group_min_spec = _group_min_count_spec(segment, family)
        aggregations: list[CompoundAggregation] = []
        identity_source = _DEFAULT_ORDER_FIELD_BY_FAMILY[family]
        if re.search(
            r"(?:상품|종목|펀드)\s*(?:개수|건수|수)|"
            r"(?:개수|건수|종목\s*수|상품\s*수|펀드\s*수)",
            segment,
        ):
            aggregations.append(
                CompoundAggregation(AggregateFunction.COUNT, identity_source)
            )

        metric = _unique_numeric_metric_from_label(segment, family)
        function_matches: list[tuple[int, AggregateFunction, re.Match[str]]] = []
        for pattern, function in (
            (r"평균", AggregateFunction.AVG),
            (r"중앙\s*값|중위수|메디안", AggregateFunction.MEDIAN),
            (r"합계|총합", AggregateFunction.SUM),
            (r"최솟값|최소값", AggregateFunction.MIN),
            (r"최댓값|최대값|최고값", AggregateFunction.MAX),
        ):
            function_matches.extend(
                (match.start(), function, match)
                for match in re.finditer(pattern, segment, re.IGNORECASE)
            )
        function_matches.sort(key=lambda item: item[0])
        previous_function_end = 0
        for _start, function, match in function_matches:
            coordinated_sources = _coordinated_metric_sources_before_function(
                segment,
                family,
                match,
                lower_bound=previous_function_end,
            )
            if coordinated_sources:
                for source in coordinated_sources:
                    aggregation = CompoundAggregation(function, source)
                    if aggregation not in aggregations:
                        aggregations.append(aggregation)
                previous_function_end = match.end()
                continue
            owned_metric = metric
            if owned_metric is None:
                # Multiple measures can coexist in one group request
                # (``AUM 합계/1년수익률 평균``).  Bind a function to
                # the nearest preceding numeric field mention.  This handles
                # Korean conjunctions (``AUM 합계와 총보수 평균``) without
                # treating the first metric as global context for both.
                prior_mentions = [
                    (start, end, source)
                    for start, end, source in _ordered_metric_mentions(
                        segment[: match.start()],
                        family,
                    )
                    if _numeric_source_field(family, source)
                ]
                if prior_mentions:
                    nearest_end = max(end for _start, end, _source in prior_mentions)
                    nearest_sources = {
                        source
                        for _start, end, source in prior_mentions
                        if end == nearest_end
                    }
                    if len(nearest_sources) == 1:
                        owned_metric = CompoundMetric(
                            "field",
                            (next(iter(nearest_sources)),),
                        )
            if owned_metric is None:
                # ``평균을 구하고, 평균이 높은 ...`` repeats the
                # aggregate function only to name the already-produced output.
                # It does not request a second measure.  Skip that reference
                # only when the same function has already acquired one clear
                # field owner and a direction follows immediately.
                if (
                    sum(item.function is function for item in aggregations) == 1
                    and _GROUP_DIRECTION_PATTERN.search(segment[match.end() : match.end() + 24])
                    is not None
                ):
                    previous_function_end = match.end()
                    continue
                return None
            if owned_metric.kind != "field":
                return None
            aggregation = CompoundAggregation(function, owned_metric.source_fields[0])
            if aggregation not in aggregations:
                aggregations.append(aggregation)
            previous_function_end = match.end()
        if group_min_spec is not None:
            _min_count, group_min_source, _min_match = group_min_spec
            valid_count = CompoundAggregation(
                AggregateFunction.COUNT,
                group_min_source,
            )
            if valid_count not in aggregations:
                aggregations.insert(0, valid_count)
        if not aggregations:
            return None
        group_order = _group_order_spec(
            segment,
            family,
            tuple(aggregations),
            after=(0 if group_min_spec is None else group_min_spec[2].end()),
        )
        # A directional group TOP-K must be represented by exactly one
        # aggregate output.  Never degrade it to label order or row ranking.
        order_tail_start = 0 if group_min_spec is None else group_min_spec[2].end()
        if (
            _GROUP_DIRECTION_PATTERN.search(segment[order_tail_start:]) is not None
            and _GROUP_RESULT_COUNT_PATTERN.search(segment[order_tail_start:]) is not None
            and group_order is None
        ):
            return None
        metric_source = (
            group_order[0].source_field
            if group_order is not None
            else next(
                (
                    item.source_field
                    for item in aggregations
                    if item.function is not AggregateFunction.COUNT
                ),
                identity_source,
            )
        )
        aggregate_sources = tuple(
            dict.fromkeys(item.source_field for item in aggregations)
        )

        # Sample-count and group-order clauses are plan structure, not row
        # filters.  Strip them before asking the ordinary Linker to preserve
        # all actual cohort predicates.
        filter_text = segment
        if group_min_spec is not None:
            min_match = group_min_spec[2]
            filter_text = (
                filter_text[: min_match.start()] + " " + filter_text[min_match.end() :]
            )
        filter_text = re.sub(
            r"상품\s*수\s*(?:가|이)?\s*많은\s*순\s*\d+\s*개|"
            r"(?:이|가|은|는)?\s*(?:0\s*도\s*결측(?:값)?\s*도\s*아닌|0\s*이\s*아닌)",
            " ",
            filter_text,
        )
        filters = _compiled_clause_filters(
            scope,
            filter_text,
            extra_represented_source_fields=(*group_sources, *aggregate_sources),
        )
        if filters is None:
            return None
        filters = _without_dataset_label_filter(scope, filters, filter_text)
        filters = _with_approved_fund_scope(family, filters)
        if group_min_spec is not None:
            group_min_source = group_min_spec[1]
            availability_filter = CompoundFilter(group_min_source, "neq", 0)
            if availability_filter not in filters:
                filters = (*filters, availability_filter)
        if metric_source != identity_source and re.search(
            r"0\s*도\s*결측(?:값)?\s*도\s*아닌|0\s*이\s*아닌",
            segment,
        ):
            filters = (*filters, CompoundFilter(metric_source, "neq", 0))
        group_min_count = None if group_min_spec is None else group_min_spec[0]
        group_min_source = None if group_min_spec is None else group_min_spec[1]
        group_limit = None if group_order is None else group_order[2]
        group_order_aggregation = None if group_order is None else group_order[0]
        direction = SortDirection.ASC if group_order is None else group_order[1]
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.AGGREGATE,
            kind="grouped_aggregate",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=filters,
                    metric=CompoundMetric("field", (metric_source,)),
                    direction=direction,
                    limit=group_limit or 100,
                    group_by_source_field=group_source,
                    group_by_source_fields=group_sources,
                    aggregations=tuple(aggregations),
                    group_min_count=group_min_count,
                    group_min_count_source_field=group_min_source,
                    group_limit=group_limit,
                    group_order_by_count=(
                        False if group_order is None else group_order[3]
                    ),
                    group_order_aggregation=group_order_aggregation,
                ),
            ),
        )

    def _compile_cross_family_named_comparison(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        """Compile an exact two-product projection across two DB families.

        This is not fuzzy cross-family retrieval.  Every name/ticker/ID must
        corroborate one of exactly two provided-DB rows, requested fields must
        exist in both families, and no rank/filter language may be discarded.
        Family plans retain source mention order so the response is stable
        without inventing a numeric winner.
        """

        matches = self.database_identity_matcher.find(question)
        if not matches or any(not item.is_unique for item in matches):
            return None
        grouped: dict[
            tuple[ProductFamily, str],
            list[DatabaseProductIdentityMatch],
        ] = {}
        for match in matches:
            candidate = match.candidates[0]
            key = (candidate.product_family, candidate.canonical_literal)
            grouped.setdefault(key, []).append(match)
        if len(grouped) != 2 or len({key[0] for key in grouped}) != 2:
            return None

        ordered_groups = sorted(
            grouped.items(),
            key=lambda item: min(match.start for match in item[1]),
        )
        resolved: list[tuple[ProductFamily, str, tuple[tuple[int, int], ...]]] = []
        for (family, canonical_literal), related in ordered_groups:
            explicit_rows = {
                candidate.product_id
                for match in related
                for candidate in match.candidates
                if candidate.product_family is family
                and candidate.canonical_literal == canonical_literal
                and candidate.alias_kind == "product_id"
                and normalize_database_identity(match.surface)
                == normalize_database_identity(candidate.product_id)
            }
            physical_rows = {
                candidate.product_id
                for match in related
                for candidate in match.candidates
                if candidate.product_family is family
                and candidate.canonical_literal == canonical_literal
            }
            if len(explicit_rows) == 1:
                physical_id = next(iter(explicit_rows))
            elif len(physical_rows) == 1:
                physical_id = next(iter(physical_rows))
            else:
                return None
            resolved.append(
                (
                    family,
                    physical_id,
                    tuple((match.start, match.end) for match in related),
                )
            )

        identity_spans = tuple(
            span for _family, _product_id, spans in resolved for span in spans
        )
        semantic_text = _mask_anchor_identity_surfaces(question, identity_spans)
        orderless_text = re.sub(
            r"(?:질문|입력|언급|상품을?\s*적은)\s*(?:한|된)?\s*"
            r"순서대로|입력한\s*순서대로|언급한\s*순서대로",
            " ",
            semantic_text,
            flags=re.IGNORECASE,
        )
        if re.search(
            r"(?:이상|이하|초과|미만|상위|하위|내림차순|오름차순|순위)|"
            r"(?:더\s*)?(?:높은|낮은|큰|작은|많은|적은)(?:\s*순)?",
            orderless_text,
            re.IGNORECASE,
        ) is not None:
            return None

        mentions_by_family: dict[
            ProductFamily,
            tuple[tuple[int, int, str, str], ...],
        ] = {}
        for family, _product_id, _spans in resolved:
            mentions: list[tuple[int, int, str, str]] = []
            for start, end, source in _ordered_metric_mentions(semantic_text, family):
                canonical = _SOURCE_TO_CANONICAL.get((family, source))
                if canonical is None:
                    continue
                definition = load_field_registry().require_field(
                    canonical,
                    [family.value],
                )
                if not definition.comparable:
                    continue
                mentions.append((start, end, canonical, source))
            mentions_by_family[family] = tuple(mentions)

        common_fields = set.intersection(
            *(
                {canonical for _start, _end, canonical, _source in mentions}
                for mentions in mentions_by_family.values()
            )
        )
        if not 1 <= len(common_fields) <= 8:
            return None
        ordered_canonical = tuple(
            dict.fromkeys(
                canonical
                for family, _product_id, _spans in resolved
                for _start, _end, canonical, _source in mentions_by_family[family]
                if canonical in common_fields
            )
        )
        if set(ordered_canonical) != common_fields:
            return None

        family_plans: list[CompoundFamilyPlan] = []
        for family, product_id, _spans in resolved:
            source_by_canonical = {
                canonical: source
                for _start, _end, canonical, source in mentions_by_family[family]
            }
            sources = tuple(source_by_canonical[field] for field in ordered_canonical)
            family_plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=(),
                    metric=CompoundMetric("field", (sources[0],)),
                    direction=SortDirection.ASC,
                    limit=1,
                    projection_fields=sources[1:],
                    include_product_ids=(product_id,),
                    identity_scope_active=True,
                )
            )
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="cross_family_named_comparison",
            family_plans=tuple(family_plans),
        )

    def _compile_named_pair(
        self,
        question: str,
        request_id: str,
    ) -> TypedCompoundPlan | None:
        # Keep identity extraction consistent with the Router. This also
        # supports a bare overseas ticker paired with a parenthesized ticker.
        from finance_agent_core.agent.router import extract_product_routing_hints

        _, routed_mentions = extract_product_routing_hints(question)
        candidates = list(
            dict.fromkeys(
                item
                for item in routed_mentions
                if re.fullmatch(r"KR[A-Z0-9]{10}|[A-Z][A-Z0-9.]{1,12}", item)
            )
        )
        resolved: list[tuple[str, ProductFamily]] = []
        for candidate in candidates:
            if any(candidate == existing for existing, _ in resolved):
                continue
            identity = self._resolve_identity(candidate)
            if identity is not None:
                resolved.append(identity)
        # Never turn “A, B and C” into an apparently successful comparison of
        # only A and B.  Every literal identifier must resolve exactly once in
        # the same provided-DB family before this narrow compiler may execute.
        if (
            not 2 <= len(resolved) <= 20
            or len(resolved) != len(candidates)
            or len({family for _, family in resolved}) != 1
        ):
            return None
        family = resolved[0][1]
        ranking = _named_set_rank_spec(
            question,
            family,
            identity_count=len(resolved),
        )
        if ranking is None:
            return None
        metric, direction, limit = ranking
        projection_fields = _named_set_projection_fields(question, family)
        if projection_fields is None:
            return None
        return TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=(
                InteractionIntent.SEARCH
                if len(resolved) > 2
                else InteractionIntent.COMPARE
            ),
            kind="named_pair",
            family_plans=(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=self._identity_filters(family),
                    metric=metric,
                    direction=direction,
                    limit=limit,
                    projection_fields=projection_fields,
                    include_product_ids=tuple(item[0] for item in resolved),
                    identity_scope_active=True,
                ),
            ),
        )

    def _identity_family(self, product_id: str) -> ProductFamily | None:
        resolved = self._resolve_identity(product_id)
        return None if resolved is None else resolved[1]

    def _resolve_identity(self, literal: str) -> tuple[str, ProductFamily] | None:
        """Resolve one literal through allowed identity columns, then canonicalize.

        A domestic ticker must become its canonical product_id before the
        frozen plan executes.  Cross-family collisions and duplicate rows are
        rejected rather than widened with an ``OR`` predicate.
        """

        exact = self.database_identity_matcher.resolve_literal(literal)
        if exact is not None:
            if not exact.is_unique:
                return None
            candidate = exact.candidates[0]
            return candidate.canonical_literal, candidate.product_family

        matches: list[tuple[str, ProductFamily]] = []
        for family, path in self.database_paths.items():
            table = _TABLE_BY_FAMILY[family]
            columns = (
                ("product_id",)
                if family is ProductFamily.FUND
                else ("product_id", "ticker")
            )
            predicates = " OR ".join(f"{column} = ?" for column in columns)
            with connect_read_only(path) as connection:
                rows = connection.execute(
                    f"SELECT product_id, "
                    f"{'NULL AS ticker' if family is ProductFamily.FUND else 'ticker'} "
                    f"FROM {table} WHERE is_quarantined = 0 AND ({predicates})",
                    tuple(literal for _ in columns),
                ).fetchall()
            if len(rows) != 1:
                if rows:
                    return None
                continue
            row = rows[0]
            canonical = (
                str(row["ticker"])
                if family in {ProductFamily.BOND, ProductFamily.OVERSEAS_ETP}
                else str(row["product_id"])
            )
            matches.append((canonical, family))
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _identity_filters(family: ProductFamily) -> tuple[CompoundFilter, ...]:
        # Exact canonical identity is already the complete scope.  Adding a
        # default venue/product-type filter can silently erase a valid ETN or
        # an off-exchange bond with the requested identifier.
        del family
        return ()

    def authorize_decision(
        self,
        decision: RouteDecision,
        compound: TypedCompoundPlan,
        *,
        unresolved_spans: tuple[str, ...] = (),
    ) -> tuple[RouteDecision, CompoundAuthorizationReceipt | None]:
        """Authorize only a complete, server-compiled compound plan.

        Safety and unsupported decisions are terminal.  A prior CLARIFY may be
        upgraded only for the two narrow representation gaps this compiler is
        designed to close: an explicit compound expression that the ordinary
        SemanticCoverageGate cannot represent, or an explicit multi-family
        SEARCH that the single-plan route cannot express.  The caller still
        owns approved-database and immutable-release checks on both sides of
        this method.
        """

        if compound.question_id != decision.draft.request_id:
            raise ValueError("compound plan request ID differs from the routed request")
        if compound.question != decision.draft.question:
            raise ValueError("compound plan question differs from the routed request")
        validated_fields = self._validate_compound_plan(compound)
        anchor_clause_coverage = self._anchor_relative_clause_coverage(compound)
        if (
            self._requires_anchor_relative_clause_coverage(compound)
            and anchor_clause_coverage is None
        ):
            return decision, None
        named_set_clause_coverage = self._named_set_clause_coverage(compound)
        if compound.kind == "named_pair" and named_set_clause_coverage is None:
            return decision, None
        canonical_clause_ids = (
            anchor_clause_coverage.clause_ids
            if anchor_clause_coverage is not None
            else (
                named_set_clause_coverage.clause_ids
                if named_set_clause_coverage is not None
                else ()
            )
        )
        canonical_anchor_scope = (
            anchor_clause_coverage is not None
            and self._anchor_route_matches_canonical_scope(
                decision,
                anchor_clause_coverage,
            )
        )
        canonical_named_set_scope = (
            named_set_clause_coverage is not None
            and self._named_set_route_matches_canonical_scope(
                decision,
                named_set_clause_coverage,
            )
        )

        typed_field_gap = (
            decision.disposition is RouteDisposition.UNSUPPORTED
            and decision.reason_code == "semantic_unmapped_action"
            and len(compound.family_plans) == 1
            and (
                (
                    compound.kind == "field_relative_ranking"
                    and bool(compound.family_plans[0].field_comparisons)
                )
                or canonical_anchor_scope
                or self._is_supported_anchor_relative_gap(decision, compound)
            )
        )
        if decision.disposition is RouteDisposition.UNSUPPORTED and not typed_field_gap:
            return decision, None
        if decision.reason_code.startswith("safety_"):
            return decision, None

        authorized = decision
        covered_spans: tuple[str, ...] = ()
        # Apply the same positive semantic-coverage proof to an apparently
        # executable Router decision.  Otherwise a valid compound clause can
        # hide an unrepresented trailing criterion and still acquire a
        # receipt.
        if unresolved_spans:
            if not all(
                self._canonical_ledger_proves_unresolved_span(
                    span,
                    compound,
                    anchor_clause_coverage=anchor_clause_coverage,
                    named_set_clause_coverage=named_set_clause_coverage,
                )
                for span in unresolved_spans
            ):
                return decision, None
            covered_spans = unresolved_spans
        if decision.disposition is RouteDisposition.EXECUTE:
            if canonical_named_set_scope and (
                set(decision.draft.product_families)
                != set(compound.requested_product_families)
                or decision.draft.intent is not compound.interaction_intent
                or decision.query_plan_intent is not Intent.SEARCH
                or decision.draft.requested_limit
                not in {None, compound.family_plans[0].limit}
            ):
                authorized = self._canonical_named_set_route_decision(
                    decision,
                    compound,
                )
            elif canonical_anchor_scope and (
                set(decision.draft.product_families)
                != set(compound.requested_product_families)
                or decision.draft.intent is not InteractionIntent.SEARCH
                or decision.query_plan_intent is not Intent.SEARCH
            ):
                authorized = self._canonical_anchor_route_decision(
                    decision,
                    compound,
                )
            else:
                authorized = self.rewrite_decision(decision, compound)
        elif decision.disposition is RouteDisposition.CLARIFY or typed_field_gap:
            # A compound-family representation gap does not erase unrelated
            # semantic leftovers.  Every unresolved surface must be proven by
            # the frozen typed plan regardless of which CLARIFY reason the
            # ordinary router emitted.
            if typed_field_gap:
                # The lexical Router cannot encode a row-field-to-row-field
                # predicate, so it reports a semantic action gap.  It is safe
                # to close only after the dedicated grammar has bound both
                # explicit fields, relation direction, gap direction and
                # result size into a reproducible typed plan.
                pass
            elif canonical_anchor_scope:
                # The canonical ledger, rather than a changing Router reason
                # string, proves this narrow reference-product grammar.  Any
                # genuinely unresolved surface was already rejected above.
                pass
            elif canonical_named_set_scope:
                # DB-owned names may contain family/metric/count-like tokens.
                # The closed-set ledger proves that those tokens are identity
                # text and that the actual comparison clause is complete.
                pass
            elif decision.reason_code == "semantic_coverage_incomplete":
                if not unresolved_spans:
                    return decision, None
            elif decision.reason_code in {
                "ambiguous_product_family",
                "clarify_execution_blocked",
                "aggregate_product_identity_unbound",
                "missing_product_identity",
            }:
                if not (
                    self._is_supported_cross_family_gap(decision, compound)
                    or self._is_supported_aggregate_threshold_gap(decision, compound)
                    or canonical_anchor_scope
                    or self._is_supported_anchor_relative_gap(decision, compound)
                    or self._is_supported_grouped_topk_gap(decision, compound)
                ):
                    return decision, None
            else:
                return decision, None

            if set(decision.draft.product_families) != set(
                compound.requested_product_families
            ) and not (
                self._is_supported_cross_family_gap(decision, compound)
                or canonical_anchor_scope
                or canonical_named_set_scope
                or self._is_supported_grouped_topk_gap(decision, compound)
            ):
                return decision, None
            if compound.interaction_intent not in {
                InteractionIntent.SEARCH,
                InteractionIntent.COMPARE,
                InteractionIntent.AGGREGATE,
            }:
                return decision, None
            if (
                compound.interaction_intent is InteractionIntent.AGGREGATE
                and compound.kind
                not in {
                    "cohort",
                    "grouped_aggregate",
                    "family_count_aggregate",
                    "family_summary_aggregate",
                }
            ):
                return decision, None
            # RouteDecision's public multi-family execution contract supports
            # only independent SEARCH.  Do not disguise cohort AGGREGATE or a
            # cross-family comparison as SEARCH merely to pass validation.
            if len(compound.product_families) > 1 and compound.interaction_intent not in {
                InteractionIntent.SEARCH,
                InteractionIntent.AGGREGATE,
            }:
                return decision, None
            if (
                decision.draft.requested_limit is not None
                and not canonical_named_set_scope
                and decision.draft.requested_limit
                not in {
                    item.limit for item in compound.family_plans
                }
                | {
                    item.shortlist_limit
                    for item in compound.family_plans
                    if item.shortlist_limit is not None
                }
                | {
                    item.group_min_count
                    for item in compound.family_plans
                    if item.group_min_count is not None
                }
            ):
                return decision, None
            if canonical_named_set_scope:
                authorized = self._canonical_named_set_route_decision(
                    decision,
                    compound,
                )
            elif canonical_anchor_scope:
                authorized = self._canonical_anchor_route_decision(
                    decision,
                    compound,
                )
            else:
                authorized = RouteDecision(
                    draft=decision.draft.model_copy(
                        update={
                            "intent": compound.interaction_intent,
                            "product_families": list(compound.product_families),
                        }
                    ),
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="typed_compound_plan_authorized",
                    reason=(
                        "서버가 질문의 필드·조건·계산·정렬·개수를 모두 Typed Compound "
                        "Plan으로 검증해 제한적으로 실행을 승인"
                    ),
                    query_plan_intent=(
                        Intent.AGGREGATE
                        if compound.interaction_intent is InteractionIntent.AGGREGATE
                        else Intent.SEARCH
                    ),
                    capability_matrix_version=decision.capability_matrix_version,
                )
        else:
            return decision, None

        receipt_payload = {
            "request_id": compound.question_id,
            "question_sha256": hashlib.sha256(compound.question.encode("utf-8")).hexdigest(),
            "plan_sha256": _compound_plan_sha256(compound),
            "prior_reason_code": decision.reason_code,
            "covered_unresolved_spans": list(covered_spans),
            "canonical_clause_ids": list(canonical_clause_ids),
            "validated_field_ids": list(validated_fields),
            "authorized_disposition": authorized.disposition.value,
            "authorized_intent": authorized.draft.intent.value,
            "authorized_families": sorted(item.value for item in authorized.draft.product_families),
            "authorized_query_plan_intent": (
                None
                if authorized.query_plan_intent is None
                else authorized.query_plan_intent.value
            ),
        }
        authority_mac = hmac.new(
            self._authorization_key,
            json.dumps(
                receipt_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        receipt = CompoundAuthorizationReceipt(
            request_id=compound.question_id,
            question_sha256=receipt_payload["question_sha256"],
            plan_sha256=receipt_payload["plan_sha256"],
            prior_reason_code=decision.reason_code,
            covered_unresolved_spans=covered_spans,
            canonical_clause_ids=canonical_clause_ids,
            validated_field_ids=validated_fields,
            authorized_intent=authorized.draft.intent.value,
            authorized_families=tuple(
                sorted(item.value for item in authorized.draft.product_families)
            ),
            authorized_query_plan_intent=(
                "" if authorized.query_plan_intent is None else authorized.query_plan_intent.value
            ),
            authority_mac=authority_mac,
        )
        return authorized, receipt

    @staticmethod
    def _is_supported_cross_family_gap(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> bool:
        if len(compound.requested_product_families) < 2:
            return False
        supported_kind = (
            compound.interaction_intent is InteractionIntent.SEARCH
            and compound.kind
            in {
                "parallel",
                "two_stage",
                "global_ranking",
                "grouped_topk",
                "cross_family_named_comparison",
            }
        ) or (
            compound.interaction_intent is InteractionIntent.AGGREGATE
            and compound.kind
            in {"family_count_aggregate", "family_summary_aggregate"}
        )
        if not supported_kind:
            return False
        if compound.kind == "cross_family_named_comparison":
            requested = set(compound.requested_product_families)
            routed = set(decision.draft.product_families)
            return (
                len(compound.family_plans) == 2
                and len(requested) == 2
                and requested == set(compound.product_families)
                and (not routed or routed.issubset(requested))
                and all(
                    item.identity_scope_active
                    and len(item.include_product_ids) == 1
                    for item in compound.family_plans
                )
            )
        scopes = _compound_family_matches(compound.question)
        explicit_families = set(item.product_family for item in scopes)
        requested_families = set(compound.requested_product_families)
        routed_families = set(decision.draft.product_families)
        return (
            len(scopes) >= 2
            and explicit_families == requested_families
            and (
                routed_families == requested_families
                or (
                    not routed_families
                    and _ALL_FAMILY_PATTERN.search(compound.question) is not None
                )
            )
        )

    @staticmethod
    def _is_supported_aggregate_threshold_gap(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Allow only the dedicated server grammar to close this DTO gap."""

        return (
            compound.kind == "aggregate_threshold"
            and compound.interaction_intent is InteractionIntent.SEARCH
            and len(compound.family_plans) == 1
            and compound.family_plans[0].aggregate_threshold is not None
            and set(compound.product_families)
            == set(decision.draft.product_families)
        )

    @staticmethod
    def _is_supported_grouped_topk_gap(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Allow the dedicated partitioned-ranking grammar to close Router gaps."""

        if (
            compound.kind != "grouped_topk"
            or compound.interaction_intent is not InteractionIntent.SEARCH
            or not compound.family_plans
        ):
            return False
        requested = set(compound.requested_product_families)
        explicit = {
            item.product_family for item in _compound_family_matches(compound.question)
        }
        routed = set(decision.draft.product_families)
        return bool(
            requested
            and requested == explicit == set(compound.product_families)
            and (not routed or routed.issubset(requested))
            and all(
                item.group_by_source_field is not None
                and item.per_group_limit == item.limit
                for item in compound.family_plans
            )
        )

    @staticmethod
    def _is_supported_anchor_relative_gap(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Allow the exact-reference compiler to close one DTO shape gap."""

        if (
            compound.kind != "anchor_relative"
            or compound.interaction_intent is not InteractionIntent.SEARCH
            or len(compound.family_plans) != 1
        ):
            return False
        plan = compound.family_plans[0]
        if len(plan.exclude_product_ids) != 1 or not plan.relative_filters:
            return False
        routed_families = set(decision.draft.product_families)
        return not routed_families or routed_families == {plan.product_family}

    def _named_set_clause_coverage(
        self,
        compound: TypedCompoundPlan,
    ) -> _NamedSetClauseCoverage | None:
        """Rebuild a complete exact-product comparison as canonical clauses.

        The proof is independent of Router residual text.  Every mentioned DB
        identity must map to exactly one frozen product, the set must be
        explicitly closed, and all non-identity metric/filter language must
        reproduce the typed plan one-for-one.
        """

        if (
            compound.kind != "named_pair"
            or len(compound.family_plans) != 1
            or compound.excluded_families
        ):
            return None
        plan = compound.family_plans[0]
        expected_intent = (
            InteractionIntent.COMPARE
            if len(plan.include_product_ids) == 2
            else InteractionIntent.SEARCH
        )
        if (
            compound.interaction_intent is not expected_intent
            or not plan.identity_scope_active
            or not 2 <= len(plan.include_product_ids) <= 20
            or plan.filters != self._identity_filters(plan.product_family)
            or plan.exclude_product_ids
            or plan.relative_filters
            or plan.shortlist_metric is not None
            or plan.shortlist_limit is not None
            or plan.field_comparisons
            or plan.aggregations
            or plan.aggregate_threshold is not None
        ):
            return None

        expected_by_key = self._canonical_plan_identity_keys(
            plan.product_family,
            plan.include_product_ids,
        )
        if expected_by_key is None:
            return None
        matches = self.database_identity_matcher.find(compound.question)
        if (
            not matches
            or any(not match.is_unique for match in matches)
            or unresolved_identity_like_spans(compound.question, matches)
        ):
            return None

        represented_ids: set[str] = set()
        for match in matches:
            owned_ids = {
                expected_by_key[key]
                for candidate in match.candidates
                if candidate.product_family is plan.product_family
                for key in (
                    (
                        candidate.product_family,
                        normalize_database_identity(candidate.canonical_literal),
                    ),
                )
                if key in expected_by_key
            }
            if len(owned_ids) != 1:
                return None
            represented_ids.update(owned_ids)
        if represented_ids != set(plan.include_product_ids):
            return None

        identity_spans = tuple((match.start, match.end) for match in matches)
        closed_count = _closed_named_set_count(
            compound.question,
            identity_spans,
            logical_product_count=len(represented_ids),
        )
        if closed_count != len(plan.include_product_ids):
            return None
        semantic_text = _mask_anchor_identity_surfaces(
            compound.question,
            identity_spans,
        )

        ranking = _named_set_rank_spec(
            semantic_text,
            plan.product_family,
            identity_count=len(plan.include_product_ids),
        )
        if ranking != (plan.metric, plan.direction, plan.limit):
            return None
        projection_fields = _named_set_projection_fields(
            semantic_text,
            plan.product_family,
        )
        if projection_fields is None or projection_fields != plan.projection_fields:
            return None

        represented_metric_sources = tuple(
            dict.fromkeys((*plan.metric.source_fields, *plan.projection_fields))
        )
        mentioned_metric_sources = tuple(
            dict.fromkeys(
                source
                for _start, _end, source in _ordered_metric_mentions(
                    semantic_text,
                    plan.product_family,
                )
            )
        )
        if set(mentioned_metric_sources) != set(represented_metric_sources):
            return None

        scope = _FamilyScope(
            start=0,
            end=len(semantic_text),
            product_family=plan.product_family,
            filters=(),
        )
        filters = _compiled_clause_filters(
            scope,
            semantic_text,
            extra_represented_source_fields=represented_metric_sources,
        )
        if filters is None:
            return None
        filters = _without_dataset_label_filter(scope, filters, semantic_text)
        if filters != plan.filters:
            redundant_family_filters = tuple(
                item for item in filters if item not in plan.filters
            )
            if (
                tuple(item for item in filters if item in plan.filters)
                != plan.filters
                or not redundant_family_filters
                or any(
                    item not in _base_family_filters(plan.product_family)
                    for item in redundant_family_filters
                )
                or not self._named_set_satisfies_filters(
                    plan,
                    redundant_family_filters,
                )
            ):
                return None

        return _NamedSetClauseCoverage(
            product_family=plan.product_family,
            product_ids=plan.include_product_ids,
            ranking_metric=plan.metric,
            ranking_direction=plan.direction,
            ranking_limit=plan.limit,
            projection_fields=plan.projection_fields,
        )

    def _canonical_plan_identity_keys(
        self,
        family: ProductFamily,
        product_ids: tuple[str, ...],
    ) -> dict[tuple[ProductFamily, str], str] | None:
        """Map every frozen ID to its DB-owned logical product identity.

        A product name, ISIN/ticker and physical bond row ID are representation
        aliases, not different products.  Resolve the frozen plan IDs through
        the same approved-DB matcher and compare their canonical logical keys;
        collisions remain fail-closed.
        """

        expected: dict[tuple[ProductFamily, str], str] = {}
        for product_id in product_ids:
            resolved = self.database_identity_matcher.resolve_literal(
                product_id,
                family=family,
            )
            if resolved is None or not resolved.is_unique:
                return None
            keys = {
                (
                    candidate.product_family,
                    normalize_database_identity(candidate.canonical_literal),
                )
                for candidate in resolved.candidates
                if candidate.product_family is family
            }
            if len(keys) != 1:
                return None
            key = next(iter(keys))
            if key in expected and expected[key] != product_id:
                return None
            expected[key] = product_id
        return expected if len(expected) == len(product_ids) else None

    def _named_set_satisfies_filters(
        self,
        plan: CompoundFamilyPlan,
        filters: tuple[CompoundFilter, ...],
    ) -> bool:
        """Verify a redundant family label against every exact product row."""

        source_fields = tuple(dict.fromkeys(item.source_field for item in filters))
        records = self._load_records(
            plan.product_family,
            filters=filters,
            source_fields=source_fields,
            product_ids=plan.include_product_ids,
            identity_scope_active=True,
        )
        represented = {
            normalize_database_identity(
                _execution_product_id(record, plan.product_family, "named_pair")
            )
            for record in records
        }
        expected = {
            normalize_database_identity(product_id)
            for product_id in plan.include_product_ids
        }
        return represented == expected

    def _named_set_route_matches_canonical_scope(
        self,
        decision: RouteDecision,
        coverage: _NamedSetClauseCoverage,
    ) -> bool:
        """Accept shallow route noise only when every mention is DB-owned."""

        routed_families = set(decision.draft.product_families)
        if routed_families and coverage.product_family not in routed_families:
            return False
        expected = self._canonical_plan_identity_keys(
            coverage.product_family,
            coverage.product_ids,
        )
        if expected is None:
            return False
        for mention in decision.draft.product_mentions:
            match = self.database_identity_matcher.resolve_literal(
                mention,
                family=coverage.product_family,
            )
            if match is None or not match.is_unique:
                return False
            represented = {
                (
                    candidate.product_family,
                    normalize_database_identity(candidate.canonical_literal),
                )
                for candidate in match.candidates
                if (
                    candidate.product_family,
                    normalize_database_identity(candidate.canonical_literal),
                )
                in expected
            }
            if len(represented) != 1:
                return False
        return True

    @staticmethod
    def _canonical_named_set_route_decision(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> RouteDecision:
        """Normalize Router name-internal noise after the closed-set proof."""

        plan = compound.family_plans[0]
        return RouteDecision(
            draft=decision.draft.model_copy(
                update={
                    "intent": compound.interaction_intent,
                    "product_families": [plan.product_family],
                    "product_mentions": list(plan.include_product_ids),
                    "requested_limit": plan.limit,
                }
            ),
            disposition=RouteDisposition.EXECUTE,
            reason_code="typed_named_set_authorized",
            reason=(
                "정확히 지명된 상품 집합·정렬 필드·방향·개수·출력 필드를 "
                "서버가 canonical clause ledger로 모두 검증해 실행"
            ),
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version=decision.capability_matrix_version,
        )

    def _anchor_relative_clause_coverage(
        self,
        compound: TypedCompoundPlan,
    ) -> _AnchorRelativeClauseCoverage | None:
        """Rebuild the complete anaphoric anchor request as canonical clauses.

        Router output is deliberately not consulted here.  Exact DB identity,
        the family-local filter parser and the typed rank parser independently
        reproduce the request.  This lets a complete request survive a shallow
        Router family/wording mismatch while retaining fail-closed behavior if
        even one conjunction disappeared from the frozen plan.
        """

        if (
            compound.kind != "anchor_relative"
            or compound.interaction_intent is not InteractionIntent.SEARCH
            or len(compound.family_plans) != 1
        ):
            return None
        plan = compound.family_plans[0]
        anchor = self._database_relative_anchor_context(
            compound.question,
            family=plan.product_family,
        )
        if anchor is None or anchor.equal_source_fields:
            # Equality-owned anchor searches have their own established
            # authorization path.  This ledger is deliberately narrow: one
            # declared reference row and explicit anaphoric thresholds.
            return None
        if plan.exclude_product_ids != (anchor.lookup_product_id,):
            return None

        segments = _plan_segments(compound.question)
        if len(segments) != 1 or segments[0][0].product_family is not plan.product_family:
            return None
        scope, segment = segments[0]
        semantic_segment = _mask_anchor_identity_surfaces(
            segment,
            anchor.identity_spans,
        )

        relative_clauses = _anaphoric_anchor_relative_specs(
            semantic_segment,
            plan.product_family,
        )
        if not relative_clauses:
            # The exact DB identity itself can own the comparative clause:
            # ``<product name>보다 <metric>이 높은/낮은``.  The compiler
            # already accepts this ordinary Korean form, so the independent
            # authorization ledger must be able to reproduce the same field
            # and direction instead of requiring the user to repeat
            # ``이 상품보다``.  An unknown metric still fails closed.
            direct = re.search(
                rf"{re.escape(anchor.surface)}\s*(?:보다|대비)\s*"
                r"(.{1,60}?)(?:이|가|은|는)?\s*"
                r"(큰|작은|높은|낮은|많은|적은)",
                segment,
                re.IGNORECASE,
            )
            if direct is not None:
                metric = _metric_from_label(direct.group(1), plan.product_family)
                if metric is None:
                    return None
                relation = direct.group(2)
                relative_clauses = (
                    (
                        metric,
                        (
                            "gt"
                            if relation in {"큰", "높은", "많은"}
                            else "lt"
                        ),
                    ),
                )
        if not relative_clauses:
            return None
        frozen_relative = tuple(
            (metric, operator)
            for metric, operator, _threshold in plan.relative_filters
        )
        if relative_clauses != frozen_relative:
            return None
        relative_sources = tuple(
            dict.fromkeys(
                source
                for metric, _operator in relative_clauses
                for source in metric.source_fields
            )
        )
        if plan.projection_fields != relative_sources:
            return None

        ranking = _rank_spec(semantic_segment, plan.product_family)
        if ranking != (plan.metric, plan.direction, plan.limit):
            return None
        filters = _compiled_clause_filters(
            scope,
            semantic_segment,
            extra_represented_source_fields=tuple(
                dict.fromkeys((*relative_sources, *plan.metric.source_fields))
            ),
            allow_owned_anchor_comparison=True,
        )
        if filters is None:
            return None
        filters = _without_dataset_label_filter(scope, filters, semantic_segment)
        if filters != plan.filters:
            return None

        # Inclusive relative thresholds would include the reference row.  In
        # that case the user must explicitly request another product (or say
        # to exclude the reference); silently removing it would change intent.
        explicit_exclusion = re.search(
            r"(?:다른|나머지)\s*(?:상품|종목|펀드|ETF|ETN|ETP|채권)|"
            r"(?:기준\s*)?(?:상품|종목|펀드|ETF|ETN|ETP|채권)\s*"
            r"(?:은|는|을|를)?\s*(?:제외|빼고|제외하고)",
            semantic_segment,
            re.IGNORECASE,
        )
        if explicit_exclusion is None and any(
            operator in {"gte", "lte"} for _metric, operator in relative_clauses
        ):
            return None

        return _AnchorRelativeClauseCoverage(
            product_family=plan.product_family,
            anchor_lookup_product_id=anchor.lookup_product_id,
            anchor_public_product_id=anchor.public_product_id,
            filters=plan.filters,
            relative_clauses=relative_clauses,
            ranking_metric=plan.metric,
            ranking_direction=plan.direction,
            ranking_limit=plan.limit,
            projection_fields=plan.projection_fields,
        )

    def _requires_anchor_relative_clause_coverage(
        self,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Identify the reference-declaration grammar governed by the ledger."""

        if compound.kind != "anchor_relative" or len(compound.family_plans) != 1:
            return False
        anchor = self._database_relative_anchor_context(
            compound.question,
            family=compound.family_plans[0].product_family,
        )
        if anchor is None or anchor.equal_source_fields:
            return False
        # Every explicit non-equality reference threshold uses the same
        # canonical clause ledger, regardless of whether Korean places the
        # metric before or after ``이 상품``. This both closes the previous
        # field-first false block and prevents a compiler-only plan from
        # bypassing clause-by-clause authorization.
        return bool(compound.family_plans[0].relative_filters)

    @staticmethod
    def _anchor_route_matches_canonical_scope(
        decision: RouteDecision,
        coverage: _AnchorRelativeClauseCoverage,
    ) -> bool:
        """Allow only Router scope that contains the DB-proven anchor family."""

        routed_families = set(decision.draft.product_families)
        if routed_families and coverage.product_family not in routed_families:
            return False
        allowed_mentions = {
            normalize_database_identity(coverage.anchor_lookup_product_id),
            normalize_database_identity(coverage.anchor_public_product_id),
        }
        return all(
            normalize_database_identity(mention) in allowed_mentions
            for mention in decision.draft.product_mentions
        )

    @staticmethod
    def _canonical_anchor_route_decision(
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> RouteDecision:
        """Normalize a shallow route only after the canonical ledger passed."""

        return RouteDecision(
            draft=decision.draft.model_copy(
                update={
                    "intent": InteractionIntent.SEARCH,
                    "product_families": list(compound.product_families),
                }
            ),
            disposition=RouteDisposition.EXECUTE,
            reason_code="typed_anchor_relative_authorized",
            reason=(
                "정확한 기준 상품·필터·상대 조건·정렬·개수·기준 상품 제외를 "
                "서버가 canonical clause ledger로 모두 검증해 검색으로 실행"
            ),
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version=decision.capability_matrix_version,
        )

    def _unresolved_span_is_proven(
        self,
        span: str,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Accept only unresolved semantics represented by frozen plan fields.

        This is deliberately a positive proof, not a stop-word remover.  A
        subjective span such as ``좋은`` or ``안전한`` matches none of the
        typed categories below and therefore remains CLARIFY.
        """

        normalized = re.sub(r"\s+", "", span).lower()
        if not normalized or span not in compound.question:
            return False
        plans = compound.family_plans
        direction = None
        if re.search(r"높|큰|상위|내림차순", normalized):
            direction = SortDirection.DESC
        elif re.search(r"낮|작|싼|하위|오름차순", normalized):
            direction = SortDirection.ASC

        anchor_identity_proven = False
        if compound.kind == "anchor_relative" and len(plans) == 1:
            anchor_public_id = plans[0].exclude_product_ids[0]
            span_identity = normalize_database_identity(span).strip(
                "\"'`‘’“”「」『』()[]{} "
            )
            anchor_identity_proven = any(
                match.is_unique
                and span_identity
                and span_identity in normalize_database_identity(match.surface)
                and any(
                    candidate.product_family is plans[0].product_family
                    and anchor_public_id
                    in {candidate.canonical_literal, candidate.product_id}
                    for candidate in match.candidates
                )
                for match in self.database_identity_matcher.find(compound.question)
            )

        named_identity_proven = False
        if compound.kind in {"cross_family_named_comparison", "named_pair"}:
            span_identity = normalize_database_identity(span).strip(
                "\"'`‘’“”「」『』()[]{} "
            )
            expected = {
                (plan.product_family, normalize_database_identity(product_id))
                for plan in plans
                for product_id in plan.include_product_ids
            }
            named_identity_proven = any(
                match.is_unique
                and span_identity
                and span_identity in normalize_database_identity(match.surface)
                and any(
                    (
                        candidate.product_family,
                        normalize_database_identity(candidate.product_id),
                    )
                    in expected
                    or (
                        candidate.product_family,
                        normalize_database_identity(candidate.canonical_literal),
                    )
                    in expected
                    for candidate in match.candidates
                )
                for match in self.database_identity_matcher.find(compound.question)
            )

        anchor_equality_proven = any(
            pattern.search(span) is not None
            and all(
                mapping.get(item.product_family) is not None
                and any(
                    part.source_field == mapping[item.product_family]
                    and part.operator in {"eq", "canonical_eq"}
                    for part in item.filters
                )
                for item in plans
            )
            for pattern, mapping in _ANCHOR_EQUAL_FIELD_ALIASES
        )

        metric_proven = any(
            pattern.search(span) is not None
            and all(
                mapping.get(item.product_family) is not None
                and mapping[item.product_family]
                in {
                    *(part.source_field for part in item.filters),
                    *item.metric.source_fields,
                    *(item.shortlist_metric.source_fields if item.shortlist_metric else ()),
                    *(
                        field
                        for metric, _operator, _value in item.relative_filters
                        for field in metric.source_fields
                    ),
                    *(
                        field
                        for left, _operator, right in item.field_comparisons
                        for field in (left, right)
                    ),
                    *item.projection_fields,
                    *_plan_group_sources(item),
                    *(aggregation.source_field for aggregation in item.aggregations),
                }
                for item in plans
            )
            for pattern, mapping in _METRIC_ALIASES
        )
        derived_proven = any(
            pattern.search(span) is not None
            and any(
                item.metric.kind
                == ("subtract" if kind == "subtract_domestic" else kind)
                and item.metric.source_fields == fields
                and item.metric.scale == scale
                for item in plans
            )
            for pattern, kind, fields, scale in _DERIVED_METRICS
        )
        direction_only = bool(
            re.fullmatch(
                r"(?:이가은는을를)?(?:가장|제일)?(?:높은|낮은|큰|작은|"
                r"싼|상위|하위|내림차순|오름차순)(?:순|부터|기준)?",
                normalized,
            )
        )
        ranking_surface = bool(
            re.search(r"순|상위|하위|내림차순|오름차순", normalized)
            and any(item.limit >= 1 for item in plans)
            and (metric_proven or derived_proven)
            and (direction is None or all(item.direction is direction for item in plans))
        )
        arithmetic_only = bool(
            re.fullmatch(
                r"(?:이가은는을를)?(?:비율|배수|차이|격차|변화율|합계|뺀|나눈)",
                normalized,
            )
        )
        explicit_derived_plan = any(
            pattern.search(compound.question) is not None
            and any(
                item.metric.kind == ("subtract" if kind == "subtract_domestic" else kind)
                and item.metric.source_fields == fields
                and item.metric.scale == scale
                for item in plans
            )
            for pattern, kind, fields, scale in _DERIVED_METRICS
        )
        return_metric_surface = bool(
            re.fullmatch(r"(?:이가은는을를)?(?:성과|성적|수익)(?:차이|격차)?", normalized)
            and explicit_derived_plan
            and any(
                all("ern_r" in field or "du_er" in field for field in item.metric.source_fields)
                for item in plans
            )
        )
        metric_placeholder_surface = bool(
            re.fullmatch(
                r"(?:이가은는을를)?(?:규모|성과|성적|수익|보수|금리|변동성)",
                normalized,
            )
            and len(plans) == 1
            and any(
                pattern.search(compound.question) is not None
                and mapping.get(plans[0].product_family)
                in plans[0].metric.source_fields
                for pattern, mapping in _METRIC_ALIASES
            )
        )
        arithmetic_surface = bool(
            re.search(r"비율|배수|차이|격차|변화율|합계|뺀|나눈", normalized)
            and (derived_proven or (arithmetic_only and explicit_derived_plan))
        )
        local_metric_placeholder = bool(
            re.fullmatch(r"(?:수치|값|지표)(?:이|가|은|는|을|를)?", normalized)
            and len(plans) > 1
            and _shared_local_metric_reference_owns_span(compound.question, span)
            and (shared_order := _shared_parallel_order(compound.question)) is not None
            and all(
                plan.metric.kind == "field"
                and len(
                    {
                        item.source_field
                        for item in plan.filters
                        if item.operator in {"gt", "gte", "lt", "lte", "between"}
                    }
                )
                == 1
                and plan.metric.source_fields[0]
                in {
                    item.source_field
                    for item in plan.filters
                    if item.operator in {"gt", "gte", "lt", "lte", "between"}
                }
                and (plan.direction, plan.limit) == shared_order
                for plan in plans
            )
        )
        shortlist_surface = bool(
            re.fullmatch(
                r"(?:이가은는을를)?(?:먼저|우선|그중|그\s*안에서|"
                r"추린|고른|선별한|남긴|거른|좁힌|한정한|다음|뒤|후)",
                normalized,
            )
            and any(
                item.shortlist_metric is not None and item.shortlist_limit is not None
                for item in plans
            )
        )
        complete_calculation_inputs_surface = bool(
            compound.kind == "two_stage"
            and _COMPLETE_CALCULATION_INPUTS.search(span) is not None
            and all(
                (
                    required_fields := tuple(
                        dict.fromkeys(
                            (
                                *item.metric.source_fields,
                                *(
                                    item.shortlist_metric.source_fields
                                    if item.shortlist_metric is not None
                                    else ()
                                ),
                            )
                        )
                    )
                )
                and all(
                    CompoundFilter(field, "neq", 0) in item.filters
                    for field in required_fields
                )
                for item in plans
            )
        )
        grouped_aggregate_surface = bool(
            compound.kind == "grouped_aggregate"
            and re.search(
                r"위험등급(?:마다|별)|그룹별|등급별|대분류별|세부분류(?:별|만)|"
                r"시장(?:명)?별|상품유형별|자산유형별|"
                r"지역(?:별|만)|운용사(?:별|만)|운용속성별|"
                r"발행(?:기관|사|자)별|거래소(?:코드)?별|"
                r"개수|건수|상품수|종목수|펀드수|"
                r"합계|평균|중앙값|중위수|메디안|최솟값|최소값|최고값|최대값|"
                r"표본|유효|결측|상위|하위|높|낮|큰|작은",
                normalized,
            )
            is not None
            and any(_plan_group_sources(item) and item.aggregations for item in plans)
            and (
                re.search(r"상위|하위|높|낮|큰|작은", normalized)
                is None
                or any(
                    item.group_order_aggregation is not None
                    and (
                        direction is None or item.direction is direction
                    )
                    for item in plans
                )
            )
        )
        grouped_product_topk_surface = bool(
            compound.kind == "grouped_topk"
            and re.search(
                r"대분류|자산(?:유형|군)|거래소(?:코드)?|운용속성|"
                r"상품유형|ETF/ETN|구분(?:별|마다)|속성별|"
                r"각|마다|별|유효|표본|상위|하위|최고|최저|"
                r"높|낮|순위|\d+(?:[·ㆍ,/]\d+)*위|동일률|ID순|씩",
                normalized,
                re.IGNORECASE,
            )
            is not None
            and all(
                item.group_by_source_field is not None
                and item.per_group_limit == item.limit
                for item in plans
            )
            and (
                direction is None
                or all(item.direction is direction for item in plans)
            )
        )
        family_count_surface = bool(
            compound.kind == "family_count_aggregate"
            and re.search(r"상품군별|상품수|종목수|펀드수|개수|건수", normalized)
            is not None
            and all(
                len(item.aggregations) == 1
                and item.aggregations[0].function is AggregateFunction.COUNT
                for item in plans
            )
        )
        family_summary_surface = bool(
            compound.kind == "family_summary_aggregate"
            and re.search(r"상품군별|각상품군|각각|평균|최대|최고", normalized)
            is not None
            and all(len(item.aggregations) == 1 for item in plans)
        )
        aggregate_threshold_surface = bool(
            compound.kind == "aggregate_threshold"
            and re.search(r"평균|중앙값|중위수|메디안", normalized)
            and any(item.aggregate_threshold is not None for item in plans)
            and all(
                (
                    item.aggregate_threshold is not None
                    and (
                        (
                            item.aggregate_threshold.function is AggregateFunction.AVG
                            and re.search(r"평균", normalized) is not None
                        )
                        or (
                            item.aggregate_threshold.function is AggregateFunction.MEDIAN
                            and re.search(r"중앙값|중위수|메디안", normalized) is not None
                        )
                    )
                )
                for item in plans
            )
        )
        field_comparison_surface = bool(
            compound.kind == "field_relative_ranking"
            and any(item.field_comparisons for item in plans)
            and re.search(
                r"보다|비해|차이|격차|높|낮|크|작|순|상위|하위",
                normalized,
            )
            is not None
        )
        single_plan_direction_proven = False
        if direction_only and len(plans) == 1 and plans[0].direction is direction:
            plan = plans[0]
            ranking = _rank_spec(compound.question, plan.product_family)
            if ranking is None and plan.aggregate_threshold is not None:
                parsed_threshold = _aggregate_threshold_spec(
                    compound.question,
                    plan.product_family,
                )
                if parsed_threshold is not None:
                    ranking = _aggregate_final_rank_spec(
                        compound.question,
                        plan.product_family,
                        threshold_end=parsed_threshold[1],
                        threshold=parsed_threshold[0],
                    )
            explicit_field_rank = (
                ranking is not None
                and ranking[0] == plan.metric
                and ranking[1] is plan.direction
                and ranking[2] == plan.limit
            )
            explicit_derived_rank = any(
                pattern.search(compound.question) is not None
                and plan.metric.kind
                == ("subtract" if kind == "subtract_domestic" else kind)
                and plan.metric.source_fields == fields
                and plan.metric.scale == scale
                for pattern, kind, fields, scale in _DERIVED_METRICS
            )
            explicit_anchor_rank = (
                compound.kind == "anchor_relative"
                and any(
                    metric == plan.metric
                    and (
                        (operator in {"gt", "gte"} and direction is SortDirection.DESC)
                        or (
                            operator in {"lt", "lte"}
                            and direction is SortDirection.ASC
                        )
                    )
                    for metric, operator, _threshold in plan.relative_filters
                )
            )
            single_plan_direction_proven = (
                explicit_field_rank or explicit_derived_rank or explicit_anchor_rank
            )
        clause_local_direction_proven = False
        if direction_only and direction is not None and len(plans) > 1:
            plans_by_family = {item.product_family: item for item in plans}
            matching_segments = [
                (scope, segment)
                for scope, segment in _plan_segments(compound.question)
                if span in segment
            ]
            clause_local_direction_proven = bool(matching_segments) and all(
                (
                    (plan := plans_by_family.get(scope.product_family)) is not None
                    and plan.direction is direction
                    and (
                        (
                            (ranking := _rank_spec(segment, scope.product_family))
                            is not None
                            and ranking[0] == plan.metric
                            and ranking[1] is plan.direction
                            and ranking[2] == plan.limit
                        )
                        or any(
                            pattern.search(segment) is not None
                            and plan.metric.kind
                            == ("subtract" if kind == "subtract_domestic" else kind)
                            and plan.metric.source_fields == fields
                            and plan.metric.scale == scale
                            for pattern, kind, fields, scale in _DERIVED_METRICS
                        )
                    )
                )
                for scope, segment in matching_segments
            )
        return (
            anchor_identity_proven
            or named_identity_proven
            or anchor_equality_proven
            or metric_proven
            or derived_proven
            or single_plan_direction_proven
            or clause_local_direction_proven
            or ranking_surface
            or arithmetic_surface
            or return_metric_surface
            or metric_placeholder_surface
            or local_metric_placeholder
            or shortlist_surface
            or complete_calculation_inputs_surface
            or grouped_aggregate_surface
            or grouped_product_topk_surface
            or family_count_surface
            or family_summary_surface
            or aggregate_threshold_surface
            or field_comparison_surface
        )

    def _canonical_family_clause_coverages(
        self,
        compound: TypedCompoundPlan,
    ) -> tuple[_CanonicalFamilyClauseCoverage, ...]:
        """Rebuild every supported family clause without consulting HCX.

        The full plan is already recompiled during authorization.  Bounded,
        parallel and cohort requests therefore need only an exact, ordered
        mapping from each server-owned family segment to the corresponding
        frozen plan.  For a two-stage request this method additionally
        reconstructs every family plan from its local clause.  In every case
        the exhaustive literal ledger below must still prove each HCX span;
        merely belonging to a segment never grants authority.  A missing or
        cross-family clause produces no coverage record.
        """

        if compound.kind not in {
            "bounded",
            "cohort",
            "derived_ranking",
            "parallel",
            "two_stage",
        } or compound.excluded_families:
            return ()
        segments = _plan_segments(compound.question)
        if len(segments) != len(compound.family_plans):
            return ()
        if compound.kind in {"bounded", "cohort", "derived_ranking", "parallel"}:
            recompiled = self.compile(compound.question, compound.question_id)
            if recompiled is None or recompiled != replace(
                compound,
                hclx_structure_spans=(),
            ):
                return ()
            coverages: list[_CanonicalFamilyClauseCoverage] = []
            search_from = 0
            for (scope, segment), plan in zip(
                segments,
                compound.family_plans,
                strict=True,
            ):
                if plan.product_family is not scope.product_family:
                    return ()
                segment_start = compound.question.find(segment, search_from)
                if segment_start < 0:
                    return ()
                segment_end = segment_start + len(segment)
                search_from = segment_end
                coverages.append(
                    _CanonicalFamilyClauseCoverage(
                        product_family=scope.product_family,
                        segment_start=segment_start,
                        segment_end=segment_end,
                        plan=plan,
                    )
                )
            return tuple(coverages)

        plan_by_family = {
            item.product_family: item for item in compound.family_plans
        }
        if len(plan_by_family) != len(compound.family_plans):
            return ()
        shared_exclusion = _shared_exclusion_prefix(compound.question, segments)
        coverages: list[_CanonicalFamilyClauseCoverage] = []
        search_from = 0
        for index, (scope, segment) in enumerate(segments):
            plan = plan_by_family.get(scope.product_family)
            if plan is None:
                return ()
            segment_start = compound.question.find(segment, search_from)
            if segment_start < 0:
                return ()
            segment_end = segment_start + len(segment)
            search_from = segment_end
            local_text = (
                f"{shared_exclusion} {segment}"
                if index > 0 and shared_exclusion
                else segment
            )
            filters = _compiled_clause_filters(scope, local_text)
            shortlist = _shortlist_spec_with_span(segment, scope.product_family)
            if filters is None or shortlist is None:
                return ()
            (
                shortlist_metric,
                shortlist_direction,
                shortlist_limit,
                shortlist_end,
            ) = shortlist
            final_text = segment[shortlist_end:]
            metric = _derived_metric(final_text, scope.product_family)
            final_ranking = _rank_spec(final_text, scope.product_family)
            if metric is None and final_ranking is not None:
                metric, direction, limit = final_ranking
            else:
                direction = _direction(final_text)
                limit = _final_limit(final_text)
            if shortlist_metric is None or metric is None or limit is None:
                return ()
            filters = _with_complete_calculation_inputs(
                filters,
                segment,
                shortlist_metric,
                metric,
            )
            rebuilt = CompoundFamilyPlan(
                product_family=scope.product_family,
                filters=filters,
                metric=metric,
                direction=direction,
                limit=limit,
                shortlist_metric=shortlist_metric,
                shortlist_limit=shortlist_limit,
                shortlist_direction=shortlist_direction,
            )
            if rebuilt != plan:
                return ()
            coverages.append(
                _CanonicalFamilyClauseCoverage(
                    product_family=scope.product_family,
                    segment_start=segment_start,
                    segment_end=segment_end,
                    plan=plan,
                )
            )
        return tuple(coverages)

    def _compound_span_has_only_owned_literals(
        self,
        span: str,
        compound: TypedCompoundPlan,
        owner: _CanonicalFamilyClauseCoverage,
    ) -> bool:
        """Subtract parser-owned literals and reject every content residue.

        Recompiling a plan proves the fields which *were* parsed, but it does
        not prove that the parser consumed every word.  This ledger therefore
        masks only literal ranges tied to the frozen family plan.  Grammar and
        presentation tokens are removed through a closed vocabulary; an
        unknown modifier such as ``친환경`` or ``로고 색깔`` remains visible and
        prevents a receipt even when a known metric occurs later in the span.
        """

        absolute_start = compound.question.find(span)
        if absolute_start < 0:
            return False
        characters = list(span)

        def own(start: int, end: int) -> None:
            left = max(0, start)
            right = min(len(characters), end)
            if left < right:
                characters[left:right] = " " * (right - left)

        def own_absolute(start: int, end: int) -> None:
            own(start - absolute_start, end - absolute_start)

        family = owner.product_family
        plan = owner.plan
        filter_sources = {item.source_field for item in plan.filters}
        metric_sources = {
            *plan.metric.source_fields,
            *(plan.shortlist_metric.source_fields if plan.shortlist_metric else ()),
        }
        represented_sources = {*filter_sources, *metric_sources}

        # Family/cohort literals are already parsed into a concrete scope and
        # its filters.  Own only matching ranges inside this exact family
        # segment, never another family's noun elsewhere in the question.
        for scope in _compound_family_matches(compound.question):
            if (
                scope.product_family is family
                and owner.segment_start <= scope.start < scope.end <= owner.segment_end
            ):
                own_absolute(scope.start, scope.end)

        def own_numeric_bound(alias_end: int, source_field: str) -> None:
            suffix = span[alias_end:]
            operators = {
                "이상": "gte",
                "이하": "lte",
                "초과": "gt",
                "미만": "lt",
                "최소": "gte",
                "적어도": "gte",
                "최대": "lte",
                "많아야": "lte",
            }
            trailing = re.match(
                r"\s*(?:이|가|은|는)?\s*"
                r"(?:(?P<prefix>최소|최대|적어도|많아야)\s*)?"
                r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*%?\s*"
                r"(?P<suffix>이상|이하|초과|미만)?"
                r"(?:인|이고|이며)?",
                suffix,
            )
            if trailing is None:
                return
            operator_word = trailing.group("prefix") or trailing.group("suffix")
            if operator_word is None:
                return
            raw_value = trailing.group("value").replace(",", "")
            value: int | float = (
                float(raw_value) if "." in raw_value else int(raw_value)
            )
            if CompoundFilter(source_field, operators[operator_word], value) in plan.filters:
                own(alias_end, alias_end + trailing.end())

        # Own exact field/alias literals only when their source column occurs
        # in this frozen plan.  Numeric predicate text is owned only when the
        # same operator/value tuple is present in the plan filters.
        for pattern, mapping in _METRIC_ALIASES:
            source_field = mapping.get(family)
            if source_field not in represented_sources:
                continue
            for match in pattern.finditer(span):
                own(match.start(), match.end())
                if source_field in filter_sources:
                    own_numeric_bound(match.end(), source_field)

        for capability in ("queryable", "sortable"):
            for mention in registry_field_mentions(span, family.value, capability):
                source_field = _CANONICAL_TO_SOURCE.get((family, mention.field_id))
                if source_field not in represented_sources:
                    continue
                own(mention.start, mention.end)
                if source_field in filter_sources:
                    own_numeric_bound(mention.end, source_field)

        for alias in rank_alias_matches(span, family):
            source_field = _CANONICAL_TO_SOURCE.get((family, alias.field_id))
            if source_field in metric_sources:
                own(alias.start, alias.end)
        for alias in filter_alias_matches(span, family):
            source_field = _CANONICAL_TO_SOURCE.get((family, alias.field_id))
            if (
                source_field in filter_sources
                and CompoundFilter(source_field, alias.operator, alias.value)
                in plan.filters
            ):
                own(alias.start, alias.end)

        expected_metrics = {
            plan.metric,
            *(tuple((plan.shortlist_metric,)) if plan.shortlist_metric else ()),
        }
        for pattern, kind, fields, scale in _DERIVED_METRICS:
            metric = CompoundMetric(
                "subtract" if kind == "subtract_domestic" else kind,
                fields,
                scale,
            )
            if metric not in expected_metrics:
                continue
            for match in pattern.finditer(span):
                own(match.start(), match.end())

        required_fields = tuple(
            dict.fromkeys(
                (
                    *plan.metric.source_fields,
                    *(plan.shortlist_metric.source_fields if plan.shortlist_metric else ()),
                )
            )
        )
        if required_fields and all(
            CompoundFilter(field, "neq", 0) in plan.filters
            for field in required_fields
        ):
            for match in _COMPLETE_CALCULATION_INPUTS.finditer(span):
                own(match.start(), match.end())

        # A derived metric owns only its closed result noun/unit.  This closes
        # model/server segmentation differences such as ``뺀 차이(%p)`` while
        # preventing an arbitrary suffix from hiding behind a valid formula.
        derived_result_suffix = {
            "subtract": re.compile(
                r"\s*(?:차이|격차)?\s*(?:\(\s*(?:%p|퍼센트\s*포인트|"
                r"거래\s*통화\s*기준)\s*\)|%p|퍼센트\s*포인트)?"
                r"(?:이|가|은|는|을|를)?",
                re.IGNORECASE,
            ),
            "add": re.compile(r"\s*(?:합계|총합)?(?:이|가|은|는|을|를)?"),
            "ratio": re.compile(r"\s*(?:비율|배수)?(?:이|가|은|는|을|를)?"),
            "relative_change": re.compile(
                r"\s*(?:변화율)?(?:\(\s*%\s*\))?(?:이|가|은|는|을|를)?"
            ),
        }
        owner_segment = compound.question[owner.segment_start : owner.segment_end]
        exact_derived_metrics: set[CompoundMetric] = set()
        for pattern, kind, fields, scale in _DERIVED_METRICS:
            metric = CompoundMetric(
                "subtract" if kind == "subtract_domestic" else kind,
                fields,
                scale,
            )
            if metric not in expected_metrics or pattern.search(owner_segment) is None:
                continue
            exact_derived_metrics.add(metric)
            for match in pattern.finditer(span):
                suffix = derived_result_suffix[metric.kind].match(span, match.end())
                if suffix is not None:
                    own(match.end(), suffix.end())

        arithmetic_word = re.fullmatch(
            r"\s*(?P<word>차이|격차|합계|총합|비율|배수|변화율)\s*",
            span,
        )
        if arithmetic_word is not None:
            allowed_kind = {
                "차이": "subtract",
                "격차": "subtract",
                "합계": "add",
                "총합": "add",
                "비율": "ratio",
                "배수": "ratio",
                "변화율": "relative_change",
            }[arithmetic_word.group("word")]
            if any(metric.kind == allowed_kind for metric in exact_derived_metrics):
                own(0, len(span))

        # Structural literals acquire no field authority by themselves. Bind
        # every requested count and ranking direction to one frozen stage
        # before removing the surrounding selection grammar. This prevents a
        # valid field from hiding a second ``상위 99개`` or the opposite tail.
        stages = [(plan.limit, plan.direction)]
        if plan.shortlist_limit is not None and plan.shortlist_metric is not None:
            stages.append((plan.shortlist_limit, plan.shortlist_direction))
        count_matches: list[tuple[re.Match[str], int]] = []
        for count_match in _STAGED_COUNT_SURFACE.finditer(span):
            raw_count = re.match(
                r"\s*(\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)",
                count_match.group(0),
            )
            if raw_count is None:
                return False
            literal = raw_count.group(1)
            count = int(literal) if literal.isdigit() else _KOREAN_SMALL_COUNTS.get(literal)
            if count is None or not any(stage_count == count for stage_count, _ in stages):
                return False
            count_matches.append((count_match, count))

        direction_pattern = re.compile(
            r"(?P<desc>상위|최상단|높은|큰|많은|긴|먼|늦은)|"
            r"(?P<asc>하위|최하단|낮은|작은|적은|짧은|가까운|빠른)",
            re.IGNORECASE,
        )
        for direction_match in direction_pattern.finditer(span):
            requested_direction = (
                SortDirection.DESC
                if direction_match.group("desc") is not None
                else SortDirection.ASC
            )
            following_counts = [
                (count_match.start() - direction_match.end(), count)
                for count_match, count in count_matches
                if 0 <= count_match.start() - direction_match.end() <= 32
            ]
            if following_counts:
                nearest_distance = min(item[0] for item in following_counts)
                nearest_counts = {
                    count for distance, count in following_counts if distance == nearest_distance
                }
                allowed_directions = {
                    stage_direction
                    for stage_count, stage_direction in stages
                    if stage_count in nearest_counts
                }
            else:
                allowed_directions = {stage_direction for _, stage_direction in stages}
            if requested_direction not in allowed_directions:
                return False

        for count_match, _count in count_matches:
            own(count_match.start(), count_match.end())
        for match in _TWO_STAGE_STRUCTURAL_SURFACE.finditer(span):
            own(match.start(), match.end())
        for match in re.finditer(r"따로", span):
            if len(compound.family_plans) < 2 or compound.kind not in {
                "parallel",
                "two_stage",
            }:
                return False
            own(match.start(), match.end())

        residual = "".join(characters)
        tokens = re.findall(
            r"[-+]?\d+(?:[.,]\d+)*|[^\W_]+(?:[._/\-][^\W_]+)*",
            residual,
            re.UNICODE,
        )
        return all(
            _TWO_STAGE_GRAMMAR_ONLY_TOKEN.fullmatch(token) is not None
            for token in tokens
        )

    def _canonical_family_clause_proves_unresolved_span(
        self,
        span: str,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Accept one long HCX span only inside one fully rebuilt family clause.

        The model's ``UNRESOLVED`` label is advisory, but its literal text is
        not ignored.  The ordinary semantic gate is rerun on that exact span;
        every issue it finds must itself be positively owned by the same
        family plan.  Subjective additions such as popularity or safety have
        no typed owner and continue to block the entire request.
        """

        start = compound.question.find(span)
        if start < 0 or compound.question.find(span, start + 1) >= 0:
            return False
        end = start + len(span)
        owners = tuple(
            item
            for item in self._canonical_family_clause_coverages(compound)
            if item.segment_start <= start and end <= item.segment_end
        )
        if len(owners) != 1:
            return False
        owner = owners[0]
        local_compound = replace(
            compound,
            # Direction and metric proof must be evaluated against the
            # owning family clause.  Keeping the full coordinated question
            # here lets a later family clause overwrite the local ranking
            # direction (for example DESC domestically, ASC overseas).
            question=compound.question[owner.segment_start : owner.segment_end],
            family_plans=(owner.plan,),
            excluded_families=(),
        )
        # A coordinated suffix can contain the field-neutral placeholder
        # ``수치/값/지표`` once while each preceding family clause owns a
        # different explicit numeric field.  The placeholder is not local to
        # the final textual segment even though Korean places the suffix
        # there.  Admit only the exact placeholder whose literal position is
        # inside the shared anaphor, and only when the complete multi-family
        # plan proves every local field, direction and limit.
        if (
            _shared_local_metric_reference_owns_span(compound.question, span)
            and self._unresolved_span_is_proven(span, compound)
        ):
            return True
        # The exhaustive literal ledger below is the positive proof for this
        # canonical two-stage clause. A narrower metric-only pre-gate used to
        # reject filter-only HCX segments even when the same exact
        # field/operator/value was already rebuilt into the frozen server plan.
        # HCX status is advisory here; no semantic token is accepted unless the
        # literal ledger and the independent SemanticCoverageGate both agree.
        if not self._compound_span_has_only_owned_literals(
            span,
            compound,
            owner,
        ):
            return False

        # Import lazily: semantic_gate deliberately imports the compound
        # grammar inside its evaluation path to avoid module-init cycles.
        from finance_agent_core.agent.semantic_gate import SemanticCoverageGate

        residual = SemanticCoverageGate().evaluate(
            span,
            interaction_intent=compound.interaction_intent.value,
        )
        # Unsupported actions are terminal.  A metric word inside the same
        # model span can never turn one into an executable database request.
        if residual.unsupported_spans:
            return False
        issues = tuple(
            dict.fromkeys(
                (
                    *residual.ambiguity_spans,
                    *residual.schema_link_gap_spans,
                )
            )
        )
        return all(
            self._unresolved_span_is_proven(issue, local_compound)
            for issue in issues
        )

    @staticmethod
    def _canonical_cohort_summary_proves_unresolved_span(
        span: str,
        compound: TypedCompoundPlan,
    ) -> bool:
        """Prove one HCX summary fragment from the typed cohort contract.

        ``_compile_cohort`` accepts exactly count/mean/min/max plus a three-row
        representative ranking.  HCX often labels those output nouns as
        ``UNRESOLVED`` because they are aggregate projections rather than one
        database field.  They are advisory only when the literal fragment
        names exactly the calculation already sealed in the reproducible
        cohort plan.  Unknown statistics, directions and result sizes remain
        unowned and therefore fail closed.
        """

        if (
            compound.kind != "cohort"
            or len(compound.family_plans) != 2
            or not span.strip()
            or compound.question.count(span) != 1
        ):
            return False
        plans = compound.family_plans
        stripped = span.strip()
        span_start = compound.question.find(span)
        span_end = span_start + len(span)
        distribution_marker = re.search(r"분포", compound.question)
        if distribution_marker is None:
            return False
        suffix = r"(?:도|를|을|은|는|이|가)?(?:\s*(?:알려|보여|제시|계산)해)?"
        punctuation = r"\s*[.,;:!?]?\s*"
        distribution = re.fullmatch(
            r"\s*(?P<metric>.+?)(?:이|가|은|는|을|를)?\s*"
            r"분포(?:를|을)?\s*(?:비교(?:해)?(?:\s*(?:주세요|줘))?)?"
            r"\s*[.,;:!?]?\s*",
            stripped,
            re.IGNORECASE,
        )
        if distribution is not None:
            metric_surface = distribution.group("metric").strip()

            def exact_metric_owned(plan: CompoundFamilyPlan) -> bool:
                sources = {
                    mapping[plan.product_family]
                    for pattern, mapping in _METRIC_ALIASES
                    if plan.product_family in mapping
                    and (match := pattern.search(metric_surface)) is not None
                    and match.start() == 0
                    and match.end() == len(metric_surface)
                }
                sources.update(
                    source
                    for mention in registry_field_mentions(
                        metric_surface,
                        plan.product_family.value,
                        "sortable",
                    )
                    if mention.start == 0
                    and mention.end == len(metric_surface)
                    and (
                        source := _CANONICAL_TO_SOURCE.get(
                            (plan.product_family, mention.field_id)
                        )
                    )
                    is not None
                )
                return plan.metric.kind == "field" and sources == {
                    plan.metric.source_fields[0]
                }

            if (
                span_start <= distribution_marker.start() < span_end
                and all(exact_metric_owned(item) for item in plans)
            ):
                return True
        # Summary projections belong only to the common tail after the
        # explicit metric-distribution clause. A matching word inside a cohort
        # name or family scope is data, not an aggregate request.
        if span_start < distribution_marker.end():
            return False
        if re.fullmatch(
            rf"(?:각(?:각)?\s*(?:집단|그룹|묶음)(?:의)?\s*)?"
            rf"(?:유효\s*)?(?:상품|종목|펀드)\s*(?:수|개수|건수){suffix}{punctuation}",
            stripped,
            re.IGNORECASE,
        ) is not None:
            return True
        if re.fullmatch(
            rf"(?:각(?:각)?\s*(?:집단|그룹|묶음)(?:의)?\s*)?"
            rf"(?:단순\s*)?평균(?:값|치)?{suffix}{punctuation}",
            stripped,
            re.IGNORECASE,
        ) is not None:
            return True
        if re.fullmatch(
            rf"(?:각(?:각)?\s*(?:집단|그룹|묶음)(?:의)?\s*)?"
            rf"(?:최솟값|최소값){suffix}{punctuation}",
            stripped,
            re.IGNORECASE,
        ) is not None:
            return True
        if re.fullmatch(
            rf"(?:각(?:각)?\s*(?:집단|그룹|묶음)(?:의)?\s*)?"
            rf"(?:최댓값|최대값|최고값){suffix}{punctuation}",
            stripped,
            re.IGNORECASE,
        ) is not None:
            return True

        representative = re.fullmatch(
            r"\s*(?:각(?:각)?\s*)?(?:집단|그룹|묶음)(?:의)?\s*"
            r"(?P<direction>상위|하위)\s*"
            r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*개"
            r"(?:도|를|을|은|는|이|가)?"
            r"(?:\s*(?:알려|보여|제시)(?:해)?\s*(?:주세요|줘)?)?"
            r"\s*[.,;:!?]?\s*",
            stripped,
            re.IGNORECASE,
        )
        if representative is not None:
            raw_count = representative.group("count")
            count = (
                int(raw_count)
                if raw_count.isdigit()
                else _KOREAN_SMALL_COUNTS.get(raw_count)
            )
            direction = (
                SortDirection.DESC
                if representative.group("direction") == "상위"
                else SortDirection.ASC
            )
            return count is not None and all(
                item.limit == count and item.direction is direction
                for item in plans
            )

        winner = re.fullmatch(
            r"\s*평균(?:값)?(?:이|가)?\s*더\s*"
            r"(?P<direction>큰|높은|작은|낮은)\s*"
            r"(?:집단|그룹|묶음|쪽)(?:도|를|을|은|는|이|가)?"
            r"(?:\s*(?:알려|보여|제시)(?:해)?\s*(?:주세요|줘)?)?"
            r"\s*[.,;:!?]?\s*",
            stripped,
            re.IGNORECASE,
        )
        if winner is not None:
            direction = (
                SortDirection.DESC
                if winner.group("direction") in {"큰", "높은"}
                else SortDirection.ASC
            )
            return compound.cohort_mean_direction is direction
        if re.fullmatch(
            r"\s*(?:집단|그룹|묶음)(?:\s*간|끼리의?)?\s*"
            r"평균(?:값)?\s*(?:간\s*)?(?:차이|격차)|"
            r"\s*평균(?:값)?\s*(?:간\s*)?(?:차이|격차)\s*",
            stripped,
            re.IGNORECASE,
        ) is not None:
            return compound.cohort_mean_difference

        summary_bundle = re.fullmatch(
            r"\s*(?:각각\s*)?(?:유효\s*)?(?:상품|종목|펀드)\s*수\s*"
            r"[,，·ㆍ/]\s*(?:단순\s*)?평균\s*[,，·ㆍ/]\s*"
            r"(?:최솟값|최소값)\s*(?:과|와|및|[,，·ㆍ/])\s*"
            r"(?:최댓값|최대값|최고값)(?:을|를)?\s*"
            r"(?:계산(?:한|하고|해)?\s*(?:뒤|후)(?:에)?\s*)?"
            r"(?:(?:각(?:각)?\s*)?(?:집단|그룹|묶음)(?:의)?\s*"
            r"(?P<direction>상위|하위)\s*"
            r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*개"
            r"(?:도|를|을|은|는)?\s*(?:알려|보여|제시)(?:해)?"
            r"\s*(?:주세요|줘)?)?\s*[.,;:!?]?\s*",
            stripped,
            re.IGNORECASE,
        )
        if summary_bundle is not None:
            raw_count = summary_bundle.group("count")
            if raw_count is None:
                return True
            count = (
                int(raw_count)
                if raw_count.isdigit()
                else _KOREAN_SMALL_COUNTS.get(raw_count)
            )
            direction = (
                SortDirection.DESC
                if summary_bundle.group("direction") == "상위"
                else SortDirection.ASC
            )
            return count is not None and all(
                item.limit == count and item.direction is direction
                for item in plans
            )

        comparison_bundle = re.fullmatch(
            r"\s*평균(?:값)?(?:이|가)?\s*더\s*"
            r"(?P<direction>큰|높은|작은|낮은)\s*"
            r"(?:집단|그룹|묶음|쪽)(?:과|와|및)\s*"
            r"(?:(?:집단|그룹|묶음)(?:\s*간|끼리의?)?\s*)?"
            r"평균(?:값)?\s*(?:간\s*)?(?:차이|격차)"
            r"(?:까지|도)?(?:\s*(?:알려|보여|제시)(?:해)?)?"
            r"(?:\s*(?:주세요|줘|부탁드립니다))?\s*[.,;:!?]?\s*",
            stripped,
            re.IGNORECASE,
        )
        if comparison_bundle is not None:
            direction = (
                SortDirection.DESC
                if comparison_bundle.group("direction") in {"큰", "높은"}
                else SortDirection.ASC
            )
            return (
                compound.cohort_mean_direction is direction
                and compound.cohort_mean_difference
            )
        return False

    def _canonical_ledger_proves_unresolved_span(
        self,
        span: str,
        compound: TypedCompoundPlan,
        *,
        anchor_clause_coverage: _AnchorRelativeClauseCoverage | None,
        named_set_clause_coverage: _NamedSetClauseCoverage | None,
    ) -> bool:
        """Treat HCX clause status as advisory only after server reconstruction.

        HCX may leave a long product identity or a complete typed scope clause
        unresolved.  Those labels cannot veto a plan when the frozen database
        identity and the canonical clause ledger independently reproduce the
        same literal text.  Conversely, a standalone semantic leftover (for
        example a subjective popularity criterion) matches neither proof and
        remains fail-closed.
        """

        if self._canonical_cohort_summary_proves_unresolved_span(span, compound):
            return True

        span_start = compound.question.find(span)
        if (
            span_start >= 0
            and compound.question.find(span, span_start + 1) < 0
            and any(
                item.segment_start <= span_start
                and span_start + len(span) <= item.segment_end
                for item in self._canonical_family_clause_coverages(compound)
            )
        ):
            # A unique span inside a fully reconstructed two-stage clause must
            # pass the complete clause-local residual check.  Do not fall back
            # to the older "one recognized token is enough" proof: that would
            # let a valid metric hide a subjective suffix in the same HCX span.
            return self._canonical_family_clause_proves_unresolved_span(
                span,
                compound,
            )
        coverage_family = (
            anchor_clause_coverage.product_family
            if anchor_clause_coverage is not None
            else (
                named_set_clause_coverage.product_family
                if named_set_clause_coverage is not None
                else None
            )
        )
        if coverage_family is None:
            # The legacy atomic proof recognizes useful fragments such as a
            # metric noun or direction, but one recognized fragment must not
            # hide an unrelated criterion in the same HCX clause.  Re-run the
            # ordinary coverage gate over the complete literal and require
            # every residual issue to have its own positive plan proof.
            from finance_agent_core.agent.semantic_gate import SemanticCoverageGate

            coverage = SemanticCoverageGate().evaluate(
                span,
                interaction_intent=compound.interaction_intent.value,
            )
            if coverage.unsupported_spans:
                return False
            issues = tuple(
                dict.fromkeys(
                    (
                        *coverage.ambiguity_spans,
                        *coverage.schema_link_gap_spans,
                    )
                )
            )
            return self._unresolved_span_is_proven(span, compound) and all(
                self._unresolved_span_is_proven(issue, compound)
                for issue in issues
            )

        # HCX commonly emits the identity-set declaration, the anchor
        # declaration, or the reference-value presentation request as one
        # conservative UNRESOLVED clause.  These clauses carry no new query
        # semantics when (and only when) their exact count/grammar reproduces
        # the already frozen canonical ledger.  Intercept them before the
        # older atomic proof so a recognized identity or metric cannot hide
        # extra words in the same model span.
        if (
            named_set_clause_coverage is not None
            and re.search(r"(?:후보|대상).{0,80}?(?:한정|제한)", span) is not None
        ):
            return self._canonical_named_set_declaration_is_owned(
                span,
                compound,
                named_set_clause_coverage,
            )
        if (
            anchor_clause_coverage is not None
            and re.search(
                r"기준\s*(?:상품|종목|펀드|ETF|ETN|ETP|채권).{0,32}"
                r"(?:수치|값|지표)",
                span,
                re.IGNORECASE,
            )
            is not None
        ):
            return self._canonical_anchor_projection_is_owned(
                span,
                anchor_clause_coverage,
            )
        if (
            anchor_clause_coverage is not None
            and re.search(
                r"(?:기준(?:점)?\s*(?:으로|을|를|삼|잡|정)|"
                r"(?:을|를)\s*기준(?:점)?(?:으로)?)",
                span,
                re.IGNORECASE,
            )
            is not None
        ):
            if self._canonical_anchor_declaration_is_owned(
                span,
                compound,
                anchor_clause_coverage,
            ):
                return True
            # HCX may merge the complete anchor declaration with the first
            # adjacent metric into one unresolved clause.  Prove the two
            # literal pieces independently instead of rejecting the request
            # because of model-dependent sentence segmentation.  Trying only
            # real whitespace/punctuation boundaries and requiring the prefix
            # to satisfy the exact declaration grammar prevents a product
            # name containing the word ``기준`` from acquiring authority.
            split_points = {
                match.end()
                for match in re.finditer(r"(?:[.;:：!?]\s*|\s+)", span)
            }
            for split_at in sorted(split_points):
                declaration = span[:split_at].strip()
                remainder = span[split_at:].strip()
                if not remainder or not self._canonical_anchor_declaration_is_owned(
                    declaration,
                    compound,
                    anchor_clause_coverage,
                ):
                    continue
                return self._canonical_ledger_proves_unresolved_span(
                    remainder,
                    compound,
                    anchor_clause_coverage=anchor_clause_coverage,
                    named_set_clause_coverage=named_set_clause_coverage,
                )
            return False

        if self._canonical_identity_span_is_owned(
            span,
            compound,
            anchor_clause_coverage=anchor_clause_coverage,
            named_set_clause_coverage=named_set_clause_coverage,
        ):
            return True
        if anchor_clause_coverage is not None and re.fullmatch(
            r"\s*(?:이|그|해당)?\s*(?:기준\s*)?"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)\s*"
            r"(?:보다|대비|이상|이하|초과|미만)(?:인)?\s*",
            span,
            re.IGNORECASE,
        ) is not None:
            # Some shallow gates return only the relational marker
            # (``상품보다``) as a residual. It carries no independent field,
            # operator or threshold once the full anchor clause ledger has
            # reproduced those items exactly.
            return True
        if self._unresolved_span_is_proven(span, compound):
            return True
        if anchor_clause_coverage is None:
            return False

        # A scope clause is accepted only when the ordinary deterministic
        # parser reconstructs at least one exact filter already present in the
        # canonical anchor ledger.  Merely mentioning a product family is not
        # enough, and no new filter can be introduced by the model.
        scopes = tuple(
            item
            for item in _compound_family_matches(span)
            if item.product_family is coverage_family
        )
        if len(scopes) != 1:
            return False
        scope = scopes[0]
        parsed = _compiled_clause_filters(
            scope,
            span,
            extra_represented_source_fields=tuple(
                dict.fromkeys(
                    (
                        *anchor_clause_coverage.ranking_metric.source_fields,
                        *(
                            source
                            for metric, _operator in anchor_clause_coverage.relative_clauses
                            for source in metric.source_fields
                        ),
                    )
                )
            ),
            allow_owned_anchor_comparison=True,
        )
        if parsed is None:
            return False
        parsed = _without_dataset_label_filter(scope, parsed, span)
        return bool(parsed) and all(
            item in anchor_clause_coverage.filters for item in parsed
        )

    def _masked_canonical_identity_span(
        self,
        span: str,
        compound: TypedCompoundPlan,
        *,
        expected: set[tuple[ProductFamily, str]],
    ) -> tuple[str, bool] | None:
        """Mask only unique DB identities owned by the frozen ledger.

        ``_mask_anchor_identity_surfaces`` also removes a closed annotation
        shell such as ``(ticker, 장내)``.  Reusing it here is important: the
        venue is part of the printed reference identity, not a second cohort
        filter.  A different or ambiguous DB identity inside the same HCX
        span is rejected rather than silently left for punctuation cleanup.
        """

        start = compound.question.find(span)
        if start < 0 or compound.question.find(span, start + 1) >= 0:
            return None
        end = start + len(span)
        owned_ranges: list[tuple[int, int]] = []
        for match in self.database_identity_matcher.find(compound.question):
            if match.start < start or match.end > end:
                continue
            if not match.is_unique:
                return None
            candidate_keys = {
                (
                    candidate.product_family,
                    normalize_database_identity(candidate.canonical_literal),
                )
                for candidate in match.candidates
            }
            represented = candidate_keys.intersection(expected)
            if not represented:
                # This is a real provided-DB identity, but not one frozen in
                # the canonical set/anchor.  It can never be presentation
                # noise for the current plan.
                return None
            if len(represented) != 1:
                return None
            owned_ranges.append((match.start - start, match.end - start))
        masked = _mask_anchor_identity_surfaces(span, tuple(owned_ranges))
        return masked, bool(owned_ranges)

    def _canonical_named_set_declaration_is_owned(
        self,
        span: str,
        compound: TypedCompoundPlan,
        coverage: _NamedSetClauseCoverage,
    ) -> bool:
        """Prove one exact closed-set declaration without trusting HCX."""

        expected_map = self._canonical_plan_identity_keys(
            coverage.product_family,
            coverage.product_ids,
        )
        if expected_map is None:
            return False
        masked_result = self._masked_canonical_identity_span(
            span,
            compound,
            expected=set(expected_map),
        )
        if masked_result is None:
            return False
        masked, _contains_identity = masked_result
        count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
        declaration = re.fullmatch(
            rf"\s*(?:후보|대상)(?:를|은|는)?\s*"
            rf"(?:이|다음|아래|위)?\s*{count}\s*(?:개\s*)?"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)(?:으?로)?만\s*"
            r"(?:한정|제한)(?:할게요|하겠습니다|합니다|했어요|해요|"
            r"해\s*주세요|해주세요)?\s*[:：]?\s*"
            r"(?:[,，.;:：·ㆍ/()\[\]{}\s]*)",
            masked,
            re.IGNORECASE,
        )
        if declaration is None:
            return False
        raw_count = declaration.group("count")
        declared_count = (
            int(raw_count)
            if raw_count.isdigit()
            else _KOREAN_SMALL_COUNTS.get(raw_count)
        )
        return declared_count == len(coverage.product_ids)

    def _canonical_anchor_declaration_is_owned(
        self,
        span: str,
        compound: TypedCompoundPlan,
        coverage: _AnchorRelativeClauseCoverage,
    ) -> bool:
        """Prove only the grammatical declaration of one DB-bound anchor."""

        expected_map = self._canonical_plan_identity_keys(
            coverage.product_family,
            (coverage.anchor_public_product_id,),
        )
        if expected_map is None:
            return False
        masked_result = self._masked_canonical_identity_span(
            span,
            compound,
            expected=set(expected_map),
        )
        if masked_result is None:
            return False
        masked, _contains_identity = masked_result
        return re.fullmatch(
            r"\s*(?:을|를)?\s*(?:(?:비교|검색)\s*)?"
            r"기준(?:점|\s*상품|\s*종목)?(?:으로)?\s*"
            r"(?:(?:삼아|삼고|잡아|잡고|정해|정하고|두고|두어|"
            r"설정해|설정하고)"
            r"(?:\s*(?:주세요|주십시오|둘게요|할게요|합니다|해요))?"
            r")?"
            r"\s*[.;:：!?]?\s*",
            masked,
            re.IGNORECASE,
        ) is not None

    @staticmethod
    def _canonical_anchor_projection_is_owned(
        span: str,
        coverage: _AnchorRelativeClauseCoverage,
    ) -> bool:
        """Prove an anaphoric request for the exact reference thresholds."""

        relative_sources = tuple(
            dict.fromkeys(
                source
                for metric, _operator in coverage.relative_clauses
                for source in metric.source_fields
            )
        )
        if (
            not relative_sources
            or coverage.projection_fields != relative_sources
        ):
            return False
        count = r"(?P<count>\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
        projection = re.fullmatch(
            rf"\s*(?:이|그|해당)?\s*기준\s*"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)(?:의)?\s*"
            rf"{count}\s*(?:개\s*)?(?:수치|값|지표)"
            r"(?:도|를|을|은|는)?(?:\s*(?:함께|같이))?"
            r"(?:\s*(?:적어|표시해|보여|알려|제공해)"
            r"(?:\s*(?:주세요|주십시오|줘요|줘)|주세요|주십시오)?)?"
            r"\s*[.;:：!?]?\s*",
            span,
            re.IGNORECASE,
        )
        if projection is None:
            return False
        raw_count = projection.group("count")
        requested_count = (
            int(raw_count)
            if raw_count.isdigit()
            else _KOREAN_SMALL_COUNTS.get(raw_count)
        )
        return requested_count == len(relative_sources)

    def _canonical_identity_span_is_owned(
        self,
        span: str,
        compound: TypedCompoundPlan,
        *,
        anchor_clause_coverage: _AnchorRelativeClauseCoverage | None,
        named_set_clause_coverage: _NamedSetClauseCoverage | None,
    ) -> bool:
        """Prove that a whole HCX span contains only frozen DB identities."""

        expected: set[tuple[ProductFamily, str]] = set()
        if anchor_clause_coverage is not None:
            anchor_keys = self._canonical_plan_identity_keys(
                anchor_clause_coverage.product_family,
                (anchor_clause_coverage.anchor_public_product_id,),
            )
            if anchor_keys is None:
                return False
            expected.update(anchor_keys)
        if named_set_clause_coverage is not None:
            named_keys = self._canonical_plan_identity_keys(
                named_set_clause_coverage.product_family,
                named_set_clause_coverage.product_ids,
            )
            if named_keys is None:
                return False
            expected.update(named_keys)
        if not expected:
            return False
        masked_result = self._masked_canonical_identity_span(
            span,
            compound,
            expected=expected,
        )
        if masked_result is None:
            return False
        residual, contains_identity = masked_result
        if not contains_identity:
            return False
        return re.sub(r"[\s()\[\]{},.:;/'\"`‘’“”「」『』_\-]+", "", residual) == ""

    @staticmethod
    def _validate_metric(
        metric: CompoundMetric,
        family: ProductFamily,
        *,
        require_sortable: bool,
    ) -> tuple[str, ...]:
        if metric.kind not in {
            "field",
            "identity_order",
            "subtract",
            "add",
            "ratio",
            "relative_change",
        }:
            raise ValueError("compound metric kind is not server-approved")
        expected_arity = {
            "field": {1},
            "identity_order": {1},
            "subtract": {2},
            "add": set(range(2, 9)),
            "ratio": {2},
            "relative_change": {2},
        }[metric.kind]
        if len(metric.source_fields) not in expected_arity:
            raise ValueError("compound metric arity is invalid")
        registry = load_field_registry()
        canonical_fields: list[str] = []
        for source in metric.source_fields:
            canonical = _SOURCE_TO_CANONICAL.get((family, source))
            if canonical is None:
                raise ValueError("compound metric has no canonical field mapping")
            definition = registry.require_field(canonical, [family.value])
            if not definition.selectable:
                raise ValueError("compound metric field is not selectable")
            if metric.kind not in {"field", "identity_order"} and (
                definition.value_type.value != "number"
            ):
                raise ValueError("derived compound metrics require numeric source fields")
            if require_sortable and metric.kind == "field" and not definition.sortable:
                raise ValueError("compound ranking field is not sortable")
            canonical_fields.append(canonical)
        return tuple(canonical_fields)

    def _validate_compound_plan(self, compound: TypedCompoundPlan) -> tuple[str, ...]:
        if (
            not compound.family_plans
            or len(compound.requested_product_families) > 4
            or len(compound.family_plans) + len(compound.excluded_families) > 4
        ):
            raise ValueError("compound plan requires one to four family plans")
        if len(compound.question) > 2000 or not compound.question.strip():
            raise ValueError("compound question is outside the public request contract")
        # Recompilation proves that the plan was produced by the current
        # server grammar rather than supplied or mutated by a caller.
        recompiled = self.compile(compound.question, compound.question_id)
        if recompiled is None or replace(
            recompiled,
            hclx_structure_spans=compound.hclx_structure_spans,
        ) != compound:
            raise ValueError("compound plan is not reproducible by the server compiler")

        registry = load_field_registry()
        if compound.cohort_mean_direction is not None or compound.cohort_mean_difference:
            if compound.kind != "cohort" or len(compound.family_plans) != 2:
                raise ValueError("cohort mean comparison requires exactly two cohorts")
            cohort_canonical_fields: set[str] = set()
            cohort_units: set[str] = set()
            for cohort_plan in compound.family_plans:
                if cohort_plan.metric.kind != "field" or len(
                    cohort_plan.metric.source_fields
                ) != 1:
                    raise ValueError("cohort mean comparison requires one source metric")
                canonical = _SOURCE_TO_CANONICAL.get(
                    (cohort_plan.product_family, cohort_plan.metric.source_fields[0])
                )
                if canonical is None:
                    raise ValueError("cohort mean metric has no canonical mapping")
                definition = registry.require_field(
                    canonical,
                    [cohort_plan.product_family.value],
                )
                if (
                    definition.value_type is not ValueType.NUMBER
                    or not definition.aggregatable
                ):
                    raise ValueError("cohort mean metric is not aggregatable")
                cohort_canonical_fields.add(canonical)
                cohort_units.add(definition.unit)
            if len(cohort_canonical_fields) != 1 or len(cohort_units) != 1:
                raise ValueError("cohort means use incompatible metrics")
        validated: list[str] = []
        global_metric_ids: set[str] = set()
        global_directions: set[SortDirection] = set()
        global_limits: set[int] = set()
        for family_plan in compound.family_plans:
            family = family_plan.product_family
            if family not in self.database_paths:
                raise ValueError("compound plan references an unbound database family")
            registry.require_executable_dataset(family.value)
            if not 1 <= family_plan.limit <= 100:
                raise ValueError("compound result limit is outside the public contract")
            if (family_plan.shortlist_metric is None) != (
                family_plan.shortlist_limit is None
            ):
                raise ValueError("compound shortlist metric and limit must be paired")
            if family_plan.shortlist_limit is not None and not (
                family_plan.limit <= family_plan.shortlist_limit <= 1000
            ):
                raise ValueError("compound shortlist limit is invalid")
            for item in family_plan.filters:
                canonical = _SOURCE_TO_CANONICAL.get((family, item.source_field))
                if canonical is None:
                    raise ValueError("compound filter has no canonical field mapping")
                definition = registry.require_field(canonical, [family.value])
                availability_exclusion = (
                    item.operator == "neq"
                    and item.value == 0
                    and definition.value_type is ValueType.NUMBER
                )
                canonical_equality = (
                    item.operator == "canonical_eq"
                    and definition.source.transform is SourceTransform.CODE_MAP
                    and isinstance(item.value, tuple)
                    and len(item.value) == 2
                    and "eq" in definition.allowed_operators
                )
                if not definition.queryable or (
                    item.operator not in definition.allowed_operators
                    and not availability_exclusion
                    and not canonical_equality
                ):
                    raise ValueError("compound filter operator is not field-registry approved")
                validated.append(canonical)
            validated.extend(
                self._validate_metric(
                    family_plan.metric,
                    family,
                    require_sortable=compound.kind
                    not in {
                        "grouped_aggregate",
                        "family_count_aggregate",
                        "family_summary_aggregate",
                    },
                )
            )
            if compound.kind in {
                "grouped_aggregate",
                "family_count_aggregate",
                "family_summary_aggregate",
            }:
                if family_plan.per_group_limit is not None:
                    raise ValueError("aggregate plan contains a per-group product limit")
                if not family_plan.aggregations:
                    raise ValueError("grouped aggregate requires group field and measures")
                group_sources = _plan_group_sources(family_plan)
                if len(group_sources) > 2:
                    raise ValueError("grouped aggregate supports at most two group fields")
                for group_source in group_sources:
                    group_canonical = _SOURCE_TO_CANONICAL.get((family, group_source))
                    if group_canonical is None:
                        raise ValueError("grouped aggregate field has no canonical mapping")
                    group_definition = registry.require_field(group_canonical, [family.value])
                    if not group_definition.selectable:
                        raise ValueError("grouped aggregate field is not selectable")
                    validated.append(group_canonical)
                if not group_sources and compound.kind == "grouped_aggregate":
                    raise ValueError("grouped aggregate requires a group field")
                for aggregation in family_plan.aggregations:
                    canonical = _SOURCE_TO_CANONICAL.get(
                        (family, aggregation.source_field)
                    )
                    if canonical is None:
                        raise ValueError("aggregate measure has no canonical mapping")
                    definition = registry.require_field(canonical, [family.value])
                    if aggregation.function is AggregateFunction.COUNT:
                        if not definition.selectable:
                            raise ValueError("aggregate count field is not selectable")
                    elif not definition.aggregatable:
                        raise ValueError("aggregate measure is not aggregatable")
                    validated.append(canonical)
                if (family_plan.group_min_count is None) != (
                    family_plan.group_min_count_source_field is None
                ):
                    raise ValueError("group sample threshold lost its metric owner")
                if family_plan.group_min_count is not None:
                    if (
                        compound.kind != "grouped_aggregate"
                        or not 1 <= family_plan.group_min_count <= 100000
                    ):
                        raise ValueError(
                            "group sample threshold is outside the approved contract"
                        )
                    assert family_plan.group_min_count_source_field is not None
                    minimum_canonical = _SOURCE_TO_CANONICAL.get(
                        (family, family_plan.group_min_count_source_field)
                    )
                    if minimum_canonical is None:
                        raise ValueError("group sample metric has no canonical mapping")
                    minimum_definition = registry.require_field(
                        minimum_canonical,
                        [family.value],
                    )
                    if (
                        minimum_definition.value_type is not ValueType.NUMBER
                        or CompoundAggregation(
                            AggregateFunction.COUNT,
                            family_plan.group_min_count_source_field,
                        )
                        not in family_plan.aggregations
                    ):
                        raise ValueError("group sample threshold is not a valid-value count")
                if (family_plan.group_limit is None) != (
                    family_plan.group_order_aggregation is None
                ):
                    raise ValueError("group order lost its aggregate output owner")
                if family_plan.group_limit is not None:
                    if not (
                        compound.kind == "grouped_aggregate"
                        and 1 <= family_plan.group_limit <= 100
                        and family_plan.limit == family_plan.group_limit
                        and family_plan.group_order_aggregation
                        in family_plan.aggregations
                    ):
                        raise ValueError("group result limit is outside the approved contract")
                    if family_plan.group_order_by_count != (
                        family_plan.group_order_aggregation.function
                        is AggregateFunction.COUNT
                    ):
                        raise ValueError("group count-order flag conflicts with aggregate order")
                elif family_plan.group_order_by_count:
                    raise ValueError("group count order has no result limit")
            elif compound.kind == "grouped_topk":
                if (
                    family_plan.group_by_source_field is None
                    or family_plan.per_group_limit is None
                    or family_plan.per_group_limit != family_plan.limit
                    or not 1 <= family_plan.per_group_limit <= 100
                    or family_plan.aggregations
                    or family_plan.group_limit is not None
                    or family_plan.group_order_aggregation is not None
                    or family_plan.group_order_by_count
                ):
                    raise ValueError("grouped product TOP-K is outside the approved contract")
                group_canonical = _SOURCE_TO_CANONICAL.get(
                    (family, family_plan.group_by_source_field)
                )
                if group_canonical is None:
                    raise ValueError("grouped product field has no canonical mapping")
                group_definition = registry.require_field(group_canonical, [family.value])
                if not group_definition.selectable:
                    raise ValueError("grouped product field is not selectable")
                validated.append(group_canonical)
                if (family_plan.group_min_count is None) != (
                    family_plan.group_min_count_source_field is None
                ):
                    raise ValueError("grouped product sample threshold lost its metric")
                if family_plan.group_min_count is not None:
                    if not 1 <= family_plan.group_min_count <= 100000:
                        raise ValueError("grouped product sample threshold is invalid")
                    assert family_plan.group_min_count_source_field is not None
                    minimum_canonical = _SOURCE_TO_CANONICAL.get(
                        (family, family_plan.group_min_count_source_field)
                    )
                    if minimum_canonical is None:
                        raise ValueError("grouped product sample field has no mapping")
                    minimum_definition = registry.require_field(
                        minimum_canonical,
                        [family.value],
                    )
                    if minimum_definition.value_type is not ValueType.NUMBER:
                        raise ValueError("grouped product HAVING requires a numeric field")
                    validated.append(minimum_canonical)
            elif (
                family_plan.group_by_source_field is not None
                or family_plan.group_by_source_fields
                or family_plan.aggregations
                or family_plan.group_min_count is not None
                or family_plan.group_min_count_source_field is not None
                or family_plan.per_group_limit is not None
            ):
                raise ValueError("non-group plan contains aggregate fields")
            if family_plan.aggregate_threshold is not None:
                if compound.kind != "aggregate_threshold" or len(compound.family_plans) != 1:
                    raise ValueError("aggregate threshold requires one dedicated family plan")
                threshold = family_plan.aggregate_threshold
                if (
                    threshold.metric.kind != "field"
                    or threshold.function
                    not in {AggregateFunction.AVG, AggregateFunction.MEDIAN}
                    or threshold.operator not in {"gt", "gte", "lt", "lte"}
                ):
                    raise ValueError("aggregate threshold contract is not server-approved")
                threshold_fields = self._validate_metric(
                    threshold.metric,
                    family,
                    require_sortable=False,
                )
                if len(threshold_fields) != 1:
                    raise ValueError("aggregate threshold requires one source field")
                threshold_definition = registry.require_field(
                    threshold_fields[0],
                    [family.value],
                )
                if (
                    threshold_definition.value_type is not ValueType.NUMBER
                    or not threshold_definition.aggregatable
                    or threshold.operator
                    not in threshold_definition.allowed_operators
                ):
                    raise ValueError(
                        "aggregate threshold field or operator is not executable"
                    )
                validated.extend(threshold_fields)
            elif compound.kind == "aggregate_threshold":
                raise ValueError("aggregate threshold plan is missing its threshold")
            if compound.kind == "global_ranking":
                if family_plan.metric.kind != "field":
                    raise ValueError("global ranking requires one source field per family")
                canonical_metric = _SOURCE_TO_CANONICAL.get(
                    (family, family_plan.metric.source_fields[0])
                )
                if canonical_metric is None:
                    raise ValueError("global ranking metric has no canonical mapping")
                definition = registry.require_field(canonical_metric, [family.value])
                numeric_comparable = (
                    definition.value_type is ValueType.NUMBER
                    and definition.unit
                    not in {"source_currency_amount", "source_quantity"}
                )
                text_comparable = (
                    definition.value_type is ValueType.STRING
                    and canonical_metric == "product_name"
                )
                if not (numeric_comparable or text_comparable):
                    raise ValueError("global ranking metric is not directly comparable")
                global_metric_ids.add(canonical_metric)
                global_directions.add(family_plan.direction)
                global_limits.add(family_plan.limit)
            if family_plan.shortlist_metric is not None:
                validated.extend(
                    self._validate_metric(
                        family_plan.shortlist_metric,
                        family,
                        require_sortable=True,
                    )
                )
            for metric, operator, _threshold in family_plan.relative_filters:
                if operator not in {"gt", "gte", "lt", "lte"}:
                    raise ValueError("compound relative operator is not approved")
                validated.extend(
                    self._validate_metric(metric, family, require_sortable=False)
                )
            for left_source, operator, right_source in family_plan.field_comparisons:
                if operator not in {"gt", "gte", "lt", "lte"}:
                    raise ValueError("compound field comparison operator is not approved")
                left = _SOURCE_TO_CANONICAL.get((family, left_source))
                right = _SOURCE_TO_CANONICAL.get((family, right_source))
                if left is None or right is None or left == right:
                    raise ValueError("compound field comparison mapping is invalid")
                left_definition = registry.require_field(left, [family.value])
                right_definition = registry.require_field(right, [family.value])
                if (
                    left_definition.value_type is not ValueType.NUMBER
                    or right_definition.value_type is not ValueType.NUMBER
                    or left_definition.unit != right_definition.unit
                    or operator not in left_definition.allowed_operators
                    or operator not in right_definition.allowed_operators
                ):
                    raise ValueError("compound field comparison fields are incompatible")
                validated.extend((left, right))
            if family_plan.field_comparisons and (
                compound.kind != "field_relative_ranking"
                or len(compound.family_plans) != 1
                or len(family_plan.field_comparisons) != 1
            ):
                raise ValueError(
                    "field comparison requires one dedicated server-owned plan"
                )
            if (
                compound.kind == "field_relative_ranking"
                and not family_plan.field_comparisons
            ):
                raise ValueError("field comparison plan lost its row predicate")
            for source in family_plan.projection_fields:
                canonical = _SOURCE_TO_CANONICAL.get((family, source))
                if canonical is None:
                    raise ValueError("compound projection has no canonical mapping")
                definition = registry.require_field(canonical, [family.value])
                if not definition.selectable:
                    raise ValueError("compound projection field is not selectable")
                validated.append(canonical)
            if family_plan.identity_scope_active:
                valid_identity_count = (
                    len(family_plan.include_product_ids) == 1
                    if compound.kind == "cross_family_named_comparison"
                    else 2 <= len(family_plan.include_product_ids) <= 20
                )
                if not valid_identity_count:
                    raise ValueError("compound identity scope must contain two to twenty IDs")
            elif family_plan.include_product_ids:
                raise ValueError("compound product IDs require an explicit identity scope")
            # Construct the exact public DTO before granting execution
            # authority.  Enum/code-map mismatches must end as a controlled
            # planning refusal before any read-only SQL runs, never as a
            # post-query Pydantic error at the HTTP boundary.
            self._query_plan(compound, family_plan)
        if compound.kind == "global_ranking" and (
            len(compound.family_plans) < 2
            or len(global_metric_ids) != 1
            or len(global_directions) != 1
            or len(global_limits) != 1
        ):
            raise ValueError("global ranking families must share metric, direction and limit")
        if compound.kind == "grouped_topk":
            grouped_fields = {
                _SOURCE_TO_CANONICAL.get(
                    (plan.product_family, plan.group_by_source_field)
                )
                for plan in compound.family_plans
            }
            ranking_fields = {
                _SOURCE_TO_CANONICAL.get(
                    (plan.product_family, plan.metric.source_fields[0])
                )
                for plan in compound.family_plans
            }
            if (
                compound.interaction_intent is not InteractionIntent.SEARCH
                or None in grouped_fields
                or None in ranking_fields
                or len(grouped_fields) != 1
                or len(ranking_fields) != 1
                or len({plan.direction for plan in compound.family_plans}) != 1
                or len({plan.per_group_limit for plan in compound.family_plans}) != 1
            ):
                raise ValueError("grouped product plans must share group and ranking semantics")
        if compound.kind == "family_count_aggregate":
            if (
                compound.interaction_intent is not InteractionIntent.AGGREGATE
                or len(compound.family_plans) < 2
                or any(
                    len(plan.aggregations) != 1
                    or plan.aggregations[0].function is not AggregateFunction.COUNT
                    or plan.aggregations[0].source_field
                    != _DEFAULT_ORDER_FIELD_BY_FAMILY[plan.product_family]
                    or plan.metric.source_fields
                    != (_DEFAULT_ORDER_FIELD_BY_FAMILY[plan.product_family],)
                    for plan in compound.family_plans
                )
            ):
                raise ValueError("family count aggregate is outside the frozen contract")
            grouped_fields = {
                _SOURCE_TO_CANONICAL.get(
                    (plan.product_family, plan.group_by_source_field)
                )
                for plan in compound.family_plans
                if plan.group_by_source_field is not None
            }
            if grouped_fields and (
                len(grouped_fields) != 1
                or any(plan.group_by_source_field is None for plan in compound.family_plans)
            ):
                raise ValueError("family count groups must share one canonical field")
        if compound.kind == "family_summary_aggregate":
            summary_canonical: set[str] = set()
            summary_units: set[str] = set()
            summary_functions: set[AggregateFunction] = set()
            for plan in compound.family_plans:
                if (
                    _plan_group_sources(plan)
                    or len(plan.aggregations) != 1
                    or plan.aggregations[0].function
                    not in {AggregateFunction.AVG, AggregateFunction.MAX}
                    or plan.metric.kind != "field"
                    or plan.metric.source_fields
                    != (plan.aggregations[0].source_field,)
                ):
                    raise ValueError("family summary aggregate is outside the approved contract")
                canonical = _SOURCE_TO_CANONICAL.get(
                    (plan.product_family, plan.aggregations[0].source_field)
                )
                if canonical is None:
                    raise ValueError("family summary aggregate has no canonical field")
                definition = registry.require_field(canonical, [plan.product_family.value])
                summary_canonical.add(canonical)
                summary_units.add(definition.unit)
                summary_functions.add(plan.aggregations[0].function)
            if (
                compound.interaction_intent is not InteractionIntent.AGGREGATE
                or len(compound.family_plans) < 2
                or len(summary_canonical) != 1
                or len(summary_units) != 1
                or len(summary_functions) != 1
            ):
                raise ValueError("family summary aggregates must share field, unit and function")
        if compound.excluded_families:
            if compound.kind != "global_ranking" or len(global_metric_ids) != 1:
                raise ValueError("family exclusions require one global ranking metric")
            canonical_metric = next(iter(global_metric_ids))
            explicit_families = {
                item.product_family for item in _compound_family_matches(compound.question)
            }
            included_families = set(compound.product_families)
            seen_excluded: set[ProductFamily] = set()
            definition = registry.fields[canonical_metric]
            for exclusion in compound.excluded_families:
                if (
                    exclusion.product_family in included_families
                    or exclusion.product_family in seen_excluded
                    or exclusion.product_family not in explicit_families
                    or exclusion.canonical_field != canonical_metric
                    or exclusion.reason_code != "field_unavailable_in_provided_schema"
                    or exclusion.product_family.value in definition.datasets
                    or (exclusion.product_family, canonical_metric) in _CANONICAL_TO_SOURCE
                ):
                    raise ValueError("global ranking family exclusion is not schema-proven")
                seen_excluded.add(exclusion.product_family)
            if explicit_families != set(compound.requested_product_families):
                raise ValueError("global ranking did not account for every explicit family")
        elif set(compound.requested_product_families) != set(compound.product_families):
            raise ValueError("non-global compound plan cannot omit a requested family")
        return tuple(sorted(set(validated)))

    def _find_record(
        self,
        family: ProductFamily,
        product_id: str,
        source_fields: tuple[str, ...],
    ) -> _SourceRecord | None:
        records = self._load_records(
            family,
            filters=(),
            source_fields=source_fields,
            product_ids=(product_id,),
        )
        return records[0] if records else None

    def _canonical_storage_value(
        self,
        family: ProductFamily,
        product_id: str,
        canonical_field: str,
    ) -> str | int | float | bool | None:
        """Read one normalized column for an already exact-bound DB row.

        Most relative cohort attributes are compared in their source form.
        CODE_MAP fields are different: a source code can coexist with legacy
        normalized labels, so equality must follow the exact normalized value
        of the reference row.  The field name comes exclusively from the
        approved Registry and is checked against the table schema before it is
        interpolated into this read-only statement.
        """

        if not canonical_field.replace("_", "").isalnum():
            raise ValueError("unsafe canonical storage field")
        table = _TABLE_BY_FAMILY[family]
        identity_column = (
            "product_id"
            if ":" in product_id
            or family in {ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}
            else "ticker"
        )
        with connect_read_only(self.database_paths[family]) as connection:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if canonical_field not in columns:
                return None
            rows = connection.execute(
                f"SELECT {canonical_field} AS value FROM {table} "
                f"WHERE is_quarantined = 0 AND {identity_column} = ?",
                (product_id,),
            ).fetchall()
        if len(rows) != 1:
            return None
        value = rows[0]["value"]
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    def rewrite_decision(
        self,
        decision: RouteDecision,
        compound: TypedCompoundPlan,
    ) -> RouteDecision:
        # A compound parser proves only that a deterministic calculation can
        # be represented. It is not an authorization boundary. CONTROL and
        # UNSUPPORTED routes, including capability locks, therefore remain
        # terminal and can never be upgraded by recognizing richer grammar.
        if decision.disposition is not RouteDisposition.EXECUTE:
            return decision
        if compound.question_id != decision.draft.request_id:
            raise ValueError("compound plan request ID differs from the authorized route")
        if compound.question != decision.draft.question:
            raise ValueError("compound plan question differs from the authorized route")
        if compound.interaction_intent is not decision.draft.intent:
            if (
                compound.kind == "grouped_topk"
                and compound.interaction_intent is InteractionIntent.SEARCH
                and decision.draft.intent is InteractionIntent.AGGREGATE
                and decision.query_plan_intent is Intent.AGGREGATE
                and set(compound.requested_product_families)
                == set(decision.draft.product_families)
            ):
                return RouteDecision(
                    draft=decision.draft.model_copy(
                        update={"intent": InteractionIntent.SEARCH}
                    ),
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="typed_grouped_topk_authorized",
                    reason=(
                        "그룹 기준·유효 표본·그룹 내 정렬·개수를 서버가 "
                        "Typed Compound Plan으로 완전히 검증해 검색으로 실행"
                    ),
                    query_plan_intent=Intent.SEARCH,
                    capability_matrix_version=decision.capability_matrix_version,
                )
            if (
                compound.kind == "anchor_relative"
                and compound.interaction_intent is InteractionIntent.SEARCH
                and decision.draft.intent is InteractionIntent.COMPARE
                and decision.query_plan_intent is Intent.COMPARE
                and set(compound.requested_product_families)
                == set(decision.draft.product_families)
            ):
                # Equality/relative wording looks like a two-product COMPARE
                # to the shallow router, but this grammar returns a ranked
                # cohort around one exact DB anchor.  Normalize only after the
                # anchor, copied equality attributes, threshold, metric,
                # direction and limit are all frozen in the typed plan.
                return RouteDecision(
                    draft=decision.draft.model_copy(
                        update={"intent": InteractionIntent.SEARCH}
                    ),
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="typed_anchor_relative_authorized",
                    reason=(
                        "정확한 기준 상품의 동등 조건과 상대 임계값을 서버가 "
                        "검증해 상품 검색으로 실행"
                    ),
                    query_plan_intent=Intent.SEARCH,
                    capability_matrix_version=decision.capability_matrix_version,
                )
            if (
                compound.kind == "grouped_aggregate"
                and compound.interaction_intent is InteractionIntent.AGGREGATE
                and decision.draft.intent is InteractionIntent.SEARCH
                and decision.query_plan_intent is Intent.SEARCH
                and set(compound.requested_product_families)
                == set(decision.draft.product_families)
            ):
                # A sentence ending in ``큰 지역 8개`` resembles a
                # product SEARCH to the shallow router.  The dedicated grammar
                # has nevertheless bound an explicit group axis, aggregate
                # function, HAVING metric, order and group limit.  Normalize
                # only this fully represented shape to AGGREGATE.
                return RouteDecision(
                    draft=decision.draft.model_copy(
                        update={"intent": InteractionIntent.AGGREGATE}
                    ),
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="typed_grouped_aggregate_authorized",
                    reason=(
                        "그룹 기준·유효 표본·집계·정렬·개수를 서버가 "
                        "Typed Compound Plan으로 완전히 검증해 집계로 실행"
                    ),
                    query_plan_intent=Intent.AGGREGATE,
                    capability_matrix_version=decision.capability_matrix_version,
                )
            if (
                compound.kind == "cohort"
                and compound.interaction_intent is InteractionIntent.AGGREGATE
                and decision.draft.intent is InteractionIntent.SEARCH
                and decision.query_plan_intent is Intent.SEARCH
                and set(compound.requested_product_families)
                == set(decision.draft.product_families)
            ):
                return RouteDecision(
                    draft=decision.draft.model_copy(
                        update={"intent": InteractionIntent.AGGREGATE}
                    ),
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="typed_cohort_aggregate_authorized",
                    reason=(
                        "두 집단의 범위·동일 수치·집계·대표 상품·평균 비교를 "
                        "서버가 Typed Compound Plan으로 완전히 검증해 집계로 실행"
                    ),
                    query_plan_intent=Intent.AGGREGATE,
                    capability_matrix_version=decision.capability_matrix_version,
                )
            if not (
                compound.kind == "aggregate_threshold"
                and compound.interaction_intent is InteractionIntent.SEARCH
                and decision.draft.intent is InteractionIntent.AGGREGATE
                and decision.query_plan_intent is Intent.AGGREGATE
                and set(compound.requested_product_families)
                == set(decision.draft.product_families)
            ):
                raise ValueError("compound plan intent differs from the authorized route")
            # ``평균/중앙값보다 ... 상품`` uses an aggregate only to derive a
            # server-owned threshold; the public operation is still SEARCH.
            # Normalize only after both threshold and final ranking were
            # compiled by the deterministic grammar.
            return RouteDecision(
                draft=decision.draft.model_copy(
                    update={"intent": InteractionIntent.SEARCH}
                ),
                disposition=RouteDisposition.EXECUTE,
                reason_code="typed_aggregate_threshold_authorized",
                reason=(
                    "동일 후보군의 집계값을 서버가 임계값으로 계산한 뒤 "
                    "검증된 상품 검색으로 실행"
                ),
                query_plan_intent=Intent.SEARCH,
                capability_matrix_version=decision.capability_matrix_version,
            )
        if set(compound.requested_product_families) != set(
            decision.draft.product_families
        ):
            raise ValueError("compound plan families differ from the authorized route")
        if decision.query_plan_intent is None:
            raise ValueError("compound execution requires an authorized QueryPlan intent")
        if compound.excluded_families:
            return decision.model_copy(
                update={
                    "draft": decision.draft.model_copy(
                        update={"product_families": list(compound.product_families)}
                    )
                }
            )
        return decision

    def execute(self, compound: TypedCompoundPlan) -> CompoundExecution:
        executions = tuple(self._execute_family(compound, item) for item in compound.family_plans)
        if compound.kind == "cohort" and (
            compound.cohort_mean_direction is not None
            or compound.cohort_mean_difference
        ):
            scope_values = {item.comparison_scope_value for item in executions}
            if any(value is not None for value in scope_values) and (
                None in scope_values or len(scope_values) != 1
            ):
                raise CohortComparisonUnavailableError(
                    "cohort_comparison_scope_mismatch"
                )
        global_rows: tuple[tuple[int, object, ProductEvidence, Decimal | str], ...] = ()
        if compound.kind == "global_ranking":
            combined: list[tuple[int, object, ProductEvidence, Decimal | str]] = []
            for family_index, execution in enumerate(executions):
                for record, product, value in zip(
                    execution.product_records,
                    execution.products,
                    execution.metric_values,
                    strict=True,
                ):
                    if not isinstance(value, (Decimal, str)):
                        raise ValueError("global ranking requires one comparable metric type")
                    combined.append((family_index, record, product, value))
            direction = compound.family_plans[0].direction
            if all(isinstance(item[3], Decimal) for item in combined):
                factor = Decimal("-1") if direction is SortDirection.DESC else Decimal("1")
                combined.sort(
                    key=lambda item: (
                        factor * item[3],  # type: ignore[operator]
                        compound.family_plans[item[0]].product_family.value,
                        _public_product_id(
                            item[1], compound.family_plans[item[0]].product_family
                        ),
                        str(item[1].product_id),
                    )
                )
            elif all(isinstance(item[3], str) for item in combined):
                combined.sort(
                    key=lambda item: (
                        compound.family_plans[item[0]].product_family.value,
                        _public_product_id(
                            item[1], compound.family_plans[item[0]].product_family
                        ),
                        str(item[1].product_id),
                    )
                )
                combined.sort(
                    key=lambda item: str(item[3]).casefold(),
                    reverse=direction is SortDirection.DESC,
                )
            else:
                raise ValueError("global ranking mixed numeric and text metric values")
            global_rows = tuple(combined[: compound.family_plans[0].limit])
            executions = tuple(
                replace(
                    execution,
                    product_records=tuple(
                        record
                        for owner, record, _product, _value in global_rows
                        if owner == family_index
                    ),
                    products=tuple(
                        product
                        for owner, _record, product, _value in global_rows
                        if owner == family_index
                    ),
                    metric_values=tuple(
                        value
                        for owner, _record, _product, value in global_rows
                        if owner == family_index
                    ),
                )
                for family_index, execution in enumerate(executions)
            )
        lines = [
            (
                "질문에 명시된 상품군별 범위로 제공 DB의 고유 상품 ID를 집계했습니다."
                if compound.kind
                in {"family_count_aggregate", "family_summary_aggregate"}
                else "질문에 명시된 필터와 계산식을 제공 DB 원천값에 그대로 적용했습니다. "
                "수치의 0·결측 여부는 필드별 승인된 availability 정책을 적용했습니다. "
                "결측·UNKNOWN 값은 순위에서 제외했습니다."
            )
        ]
        if compound.kind == "global_ranking":
            lines.append(
                "공통 지표와 단위로 상품군을 통합 정렬했습니다. "
                f"[통합 순위] 검증 후보 {sum(item.candidate_count for item in executions):,}개, "
                f"전체 상품군에서 반환 {len(global_rows)}개"
            )
            for exclusion in compound.excluded_families:
                lines.append(
                    f"[{exclusion.product_family.value} 제외] 제공 데이터 schema에 "
                    f"{exclusion.canonical_field} 필드가 없어 통합 순위에서 제외했습니다."
                )
            for rank, (family_index, record, _product, value) in enumerate(
                global_rows,
                start=1,
            ):
                family = compound.family_plans[family_index].product_family
                lines.append(
                    f"{rank}. [{family.value}] {record.product_name} "
                    f"({_public_product_id(record, family)}) — 계산값 {value} "
                    f"[근거: {record.source_id} 원본 행 {record.source_row}, "
                    f"기준일 {record.dynamic_as_of.isoformat()}]"
                )
            return CompoundExecution(
                plan=compound,
                families=executions,
                answer="\n".join(lines),
                ordered_products=tuple(
                    product for _owner, _record, product, _value in global_rows
                ),
            )
        for index, execution in enumerate(executions, start=1):
            label = execution.plan.cohort_label or execution.plan.product_family.value
            lines.append(
                f"[{index}. {label}] 고유 상품 수 {execution.candidate_count:,}개"
                if compound.kind
                in {"family_count_aggregate", "family_summary_aggregate"}
                else f"[{index}. {label}] 검증 후보 {execution.candidate_count:,}개, "
                f"반환 {len(execution.products)}개"
            )
            if execution.shortlist_ids:
                lines.append("1차 후보: " + ", ".join(execution.shortlist_ids))
            if execution.aggregate_summary is not None:
                count, mean, minimum, maximum = execution.aggregate_summary
                lines.append(
                    f"유효 상품 수 {count}, 단순평균 {mean}, 최솟값 {minimum}, 최댓값 {maximum}"
                )
            if execution.aggregate_threshold_evidence is not None:
                threshold = execution.aggregate_threshold_evidence
                canonical = _SOURCE_TO_CANONICAL.get(
                    (
                        execution.plan.product_family,
                        threshold.metric.source_fields[0],
                    ),
                    threshold.metric.source_fields[0],
                )
                function_label = (
                    "평균"
                    if threshold.function is AggregateFunction.AVG
                    else "중앙값"
                )
                operator_label = {
                    "gt": "초과",
                    "gte": "이상",
                    "lt": "미만",
                    "lte": "이하",
                }[threshold.operator]
                lines.append(
                    f"집계 기준: {canonical} 유효값 {threshold.valid_count:,}개의 "
                    f"{function_label} {threshold.value} {operator_label} "
                    f"[근거: {threshold.source_id}, 기준일 "
                    f"{threshold.as_of_start.isoformat()}~{threshold.as_of_end.isoformat()}, "
                    f"입력 SHA-256 {threshold.input_sha256}]"
                )
            for group in execution.grouped_aggregates:
                rendered_values = []
                for item in group.aggregates:
                    canonical = _SOURCE_TO_CANONICAL.get(
                        (execution.plan.product_family, item.source_field),
                        item.source_field,
                    )
                    value = "값 없음" if item.value is None else str(item.value)
                    rendered_values.append(
                        f"{canonical} {item.function.value}={value}"
                    )
                lines.append(
                    f"{group.group_value}: {', '.join(rendered_values)} "
                    f"[근거: {group.source_id}, 기준일 {group.as_of.isoformat()}]"
                )
            if compound.kind == "grouped_topk":
                count_by_group = dict(execution.grouped_product_valid_counts)
                previous_group: str | int | bool | None = None
                group_rank = 0
                for group_value, record, value in zip(
                    execution.grouped_product_values,
                    execution.product_records,
                    execution.metric_values,
                    strict=True,
                ):
                    if group_value != previous_group:
                        previous_group = group_value
                        group_rank = 0
                        lines.append(
                            f"[{group_value}] 유효 순위 후보 "
                            f"{count_by_group[group_value]:,}개"
                        )
                    group_rank += 1
                    identifier = _execution_product_id(
                        record,
                        execution.plan.product_family,
                        compound.kind,
                    )
                    lines.append(
                        f"{group_rank}. {record.product_name} ({identifier}) — "
                        f"계산값 {value} [근거: {record.source_id} 원본 행 "
                        f"{record.source_row}, 기준일 {record.dynamic_as_of.isoformat()}]"
                    )
                continue
            for rank, (record, value) in enumerate(
                zip(execution.product_records, execution.metric_values, strict=True),
                start=1,
            ):
                identifier = _execution_product_id(
                    record,
                    execution.plan.product_family,
                    compound.kind,
                )
                lines.append(
                    f"{rank}. {record.product_name} ({identifier}) — 계산값 {value} "
                    f"[근거: {record.source_id} 원본 행 {record.source_row}, "
                    f"기준일 {record.dynamic_as_of.isoformat()}]"
                )
        if compound.kind == "cohort" and (
            compound.cohort_mean_direction is not None
            or compound.cohort_mean_difference
        ):
            summaries = tuple(item.aggregate_summary for item in executions)
            if len(summaries) != 2 or any(item is None for item in summaries):
                lines.append("집단 평균 비교: 유효한 수치가 없어 확인할 수 없습니다.")
            else:
                first = summaries[0]
                second = summaries[1]
                assert first is not None and second is not None
                first_mean = first[1]
                second_mean = second[1]
                first_label = (
                    executions[0].plan.cohort_label
                    or executions[0].plan.product_family.value
                )
                second_label = (
                    executions[1].plan.cohort_label
                    or executions[1].plan.product_family.value
                )
                comparison_parts: list[str] = []
                if compound.cohort_mean_direction is not None:
                    if first_mean == second_mean:
                        comparison_parts.append(
                            f"두 집단의 평균이 같습니다({first_mean})."
                        )
                    else:
                        choose_first = (
                            first_mean > second_mean
                            if compound.cohort_mean_direction is SortDirection.DESC
                            else first_mean < second_mean
                        )
                        winner = first_label if choose_first else second_label
                        direction_label = (
                            "큰"
                            if compound.cohort_mean_direction is SortDirection.DESC
                            else "작은"
                        )
                        comparison_parts.append(
                            f"평균이 더 {direction_label} 집단은 {winner}입니다."
                        )
                if compound.cohort_mean_difference:
                    comparison_parts.append(
                        f"두 집단 평균 차이는 {abs(first_mean - second_mean)}입니다."
                    )
                lines.append("집단 평균 비교: " + " ".join(comparison_parts))
        return CompoundExecution(
            plan=compound,
            families=executions,
            answer="\n".join(lines),
            ordered_products=tuple(
                product for execution in executions for product in execution.products
            ),
        )

    def execute_authorized(
        self,
        compound: TypedCompoundPlan,
        receipt: CompoundAuthorizationReceipt,
    ) -> CompoundExecution:
        """Execute and independently recompute the complete result before use."""

        self._require_receipt(compound, receipt)
        execution = self.execute(compound)
        self.verify_authorized_execution(execution, receipt)
        return execution

    def authorized_grouped_aggregate_plan(
        self,
        compound: TypedCompoundPlan,
        receipt: CompoundAuthorizationReceipt,
    ) -> QueryPlan:
        """Expose the exact public aggregate plan bound to a compound receipt.

        Grouped aggregation is fully representable by one ordinary AGGREGATE
        QueryPlan, so the routed service can send it through the standard
        aggregate Oracle, verifier and evidence adapter.  Other compound
        shapes deliberately remain on the compound executor because their
        staged or derived semantics cannot be preserved by one QueryPlan.
        """

        self._require_receipt(compound, receipt)
        if compound.kind != "grouped_aggregate" or len(compound.family_plans) != 1:
            raise ValueError("authorized plan is not one grouped aggregate")
        plan = self._query_plan(compound, compound.family_plans[0])
        if plan.intent is not Intent.AGGREGATE:
            raise ValueError("grouped aggregate produced a non-aggregate QueryPlan")
        return plan

    def verify_authorized_execution(
        self,
        execution: CompoundExecution,
        receipt: CompoundAuthorizationReceipt,
    ) -> None:
        """Verify a precomputed compound result, including document intersections."""

        compound = execution.plan
        self._require_receipt(compound, receipt)
        # A fresh read/recalculation detects altered candidate counts, order,
        # arithmetic or evidence before the result crosses the service
        # boundary.  It intentionally does not trust the first execution's
        # in-memory records.
        expected = self.execute(compound)
        if self._execution_signature(execution) != self._execution_signature(expected):
            raise ValueError("compound execution failed independent verification")

    def _require_receipt(
        self,
        compound: TypedCompoundPlan,
        receipt: CompoundAuthorizationReceipt,
    ) -> None:
        if receipt.request_id != compound.question_id:
            raise ValueError("compound authorization request ID changed")
        if receipt.question_sha256 != hashlib.sha256(
            compound.question.encode("utf-8")
        ).hexdigest():
            raise ValueError("compound authorization question changed")
        if receipt.plan_sha256 != _compound_plan_sha256(compound):
            raise ValueError("compound authorization plan changed")
        if not receipt.validated_field_ids:
            raise ValueError("compound authorization has no validated field")
        validated_fields = self._validate_compound_plan(compound)
        if receipt.validated_field_ids != validated_fields:
            raise ValueError("compound authorization validated fields changed")
        anchor_clause_coverage = self._anchor_relative_clause_coverage(compound)
        named_set_clause_coverage = self._named_set_clause_coverage(compound)
        canonical_clause_ids = (
            anchor_clause_coverage.clause_ids
            if anchor_clause_coverage is not None
            else (
                named_set_clause_coverage.clause_ids
                if named_set_clause_coverage is not None
                else ()
            )
        )
        if receipt.canonical_clause_ids != canonical_clause_ids:
            raise ValueError("compound authorization canonical clause coverage changed")
        if any(
            span not in compound.question
            or not self._canonical_ledger_proves_unresolved_span(
                span,
                compound,
                anchor_clause_coverage=anchor_clause_coverage,
                named_set_clause_coverage=named_set_clause_coverage,
            )
            for span in receipt.covered_unresolved_spans
        ):
            raise ValueError("compound authorization semantic coverage changed")
        receipt_payload = {
            "request_id": receipt.request_id,
            "question_sha256": receipt.question_sha256,
            "plan_sha256": receipt.plan_sha256,
            "prior_reason_code": receipt.prior_reason_code,
            "covered_unresolved_spans": list(receipt.covered_unresolved_spans),
            "canonical_clause_ids": list(receipt.canonical_clause_ids),
            "validated_field_ids": list(receipt.validated_field_ids),
            "authorized_disposition": RouteDisposition.EXECUTE.value,
            "authorized_intent": receipt.authorized_intent,
            "authorized_families": list(receipt.authorized_families),
            "authorized_query_plan_intent": receipt.authorized_query_plan_intent or None,
        }
        if receipt.authorized_intent != compound.interaction_intent.value:
            raise ValueError("compound authorization intent changed")
        if receipt.authorized_families != tuple(
            sorted(item.value for item in compound.product_families)
        ):
            raise ValueError("compound authorization families changed")
        if not receipt.authorized_query_plan_intent:
            raise ValueError("compound authorization QueryPlan intent is missing")
        expected_mac = hmac.new(
            self._authorization_key,
            json.dumps(
                receipt_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(receipt.authority_mac, expected_mac):
            raise ValueError("compound authorization MAC is invalid")

    @staticmethod
    def _execution_signature(execution: CompoundExecution) -> str:
        payload = {
            "plan_sha256": _compound_plan_sha256(execution.plan),
            "answer": execution.answer,
            "families": [
                {
                    "plan": _compound_plan_payload(
                        TypedCompoundPlan(
                            question_id=execution.plan.question_id,
                            question=execution.plan.question,
                            interaction_intent=execution.plan.interaction_intent,
                            kind=execution.plan.kind,
                            family_plans=(item.plan,),
                            hclx_structure_spans=execution.plan.hclx_structure_spans,
                        )
                    )["families"][0],
                    "query_plan": item.query_plan.model_dump(mode="json"),
                    "candidate_count": item.candidate_count,
                    "product_ids": [
                        _public_product_id(record, item.plan.product_family)
                        for record in item.product_records
                    ],
                    "shortlist_ids": list(item.shortlist_ids),
                    "metric_values": [str(value) for value in item.metric_values],
                    "aggregate_summary": (
                        None
                        if item.aggregate_summary is None
                        else [str(value) for value in item.aggregate_summary]
                    ),
                    "comparison_scope_value": item.comparison_scope_value,
                    "aggregate_threshold_evidence": (
                        None
                        if item.aggregate_threshold_evidence is None
                        else {
                            "metric": {
                                "kind": item.aggregate_threshold_evidence.metric.kind,
                                "source_fields": list(
                                    item.aggregate_threshold_evidence.metric.source_fields
                                ),
                                "scale": str(
                                    item.aggregate_threshold_evidence.metric.scale
                                ),
                            },
                            "function": item.aggregate_threshold_evidence.function.value,
                            "operator": item.aggregate_threshold_evidence.operator,
                            "value": str(item.aggregate_threshold_evidence.value),
                            "valid_count": item.aggregate_threshold_evidence.valid_count,
                            "source_id": item.aggregate_threshold_evidence.source_id,
                            "as_of_start": (
                                item.aggregate_threshold_evidence.as_of_start.isoformat()
                            ),
                            "as_of_end": (
                                item.aggregate_threshold_evidence.as_of_end.isoformat()
                            ),
                            "input_sha256": (
                                item.aggregate_threshold_evidence.input_sha256
                            ),
                        }
                    ),
                    "grouped_aggregates": [
                        {
                            "group_value": group.group_value,
                            "source_id": group.source_id,
                            "as_of": group.as_of.isoformat(),
                            "row_count": group.row_count,
                            "aggregates": [
                                {
                                    "function": value.function.value,
                                    "source_field": value.source_field,
                                    "value": (
                                        None if value.value is None else str(value.value)
                                    ),
                                    "valid_count": value.valid_count,
                                    "missing_count": value.missing_count,
                                }
                                for value in group.aggregates
                            ],
                        }
                        for group in item.grouped_aggregates
                    ],
                    "grouped_product_values": list(item.grouped_product_values),
                    "grouped_product_valid_counts": [
                        [group, count]
                        for group, count in item.grouped_product_valid_counts
                    ],
                    "evidence": [
                        product.model_dump(mode="json") for product in item.products
                    ],
                }
                for item in execution.families
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _execute_family(
        self,
        compound: TypedCompoundPlan,
        plan: CompoundFamilyPlan,
    ) -> CompoundFamilyExecution:
        comparison_scope_source = None
        if compound.kind == "cohort" and (
            compound.cohort_mean_direction is not None
            or compound.cohort_mean_difference
        ):
            canonical_metric = _SOURCE_TO_CANONICAL.get(
                (plan.product_family, plan.metric.source_fields[0])
            )
            if canonical_metric is None:
                raise ValueError("cohort comparison metric has no canonical mapping")
            metric_definition = load_field_registry().require_field(
                canonical_metric,
                [plan.product_family.value],
            )
            if "trading_currency" in metric_definition.comparison_scope:
                comparison_scope_source = _CANONICAL_TO_SOURCE.get(
                    (plan.product_family, "trading_currency")
                )
                if comparison_scope_source is None:
                    raise ValueError("cohort comparison has no trading-currency field")
        source_fields = tuple(
            dict.fromkeys(
                (
                    *(item.source_field for item in plan.filters),
                    *plan.metric.source_fields,
                    *(plan.shortlist_metric.source_fields if plan.shortlist_metric else ()),
                    *(
                        field
                        for metric, _operator, _threshold in plan.relative_filters
                        for field in metric.source_fields
                    ),
                    *(
                        field
                        for left, _operator, right in plan.field_comparisons
                        for field in (left, right)
                    ),
                    *(
                        plan.aggregate_threshold.metric.source_fields
                        if plan.aggregate_threshold is not None
                        else ()
                    ),
                    *plan.projection_fields,
                    *_plan_group_sources(plan),
                    *(
                        (plan.group_min_count_source_field,)
                        if plan.group_min_count_source_field is not None
                        else ()
                    ),
                    *(item.source_field for item in plan.aggregations),
                    *((comparison_scope_source,) if comparison_scope_source else ()),
                )
            )
        )
        records = self._load_records(
            plan.product_family,
            filters=plan.filters,
            source_fields=source_fields,
            product_ids=plan.include_product_ids,
            identity_scope_active=plan.identity_scope_active,
        )
        if compound.kind == "family_count_aggregate":
            return (
                self._execute_grouped_family(compound, plan, records)
                if _plan_group_sources(plan)
                else self._execute_family_count(compound, plan, records)
            )
        if compound.kind == "family_summary_aggregate":
            return self._execute_family_summary(compound, plan, records)
        if compound.kind == "grouped_aggregate":
            return self._execute_grouped_family(compound, plan, records)
        if compound.kind == "grouped_topk":
            return self._execute_grouped_topk_family(compound, plan, records)
        threshold_evidence = (
            None
            if plan.aggregate_threshold is None
            else _aggregate_threshold_evidence(
                records,
                plan.aggregate_threshold,
                plan.product_family,
            )
        )
        eligible_records: list[_SourceRecord] = []
        for record in records:
            public_id = _execution_product_id(
                record,
                plan.product_family,
                compound.kind,
            )
            if public_id in plan.exclude_product_ids:
                continue
            field_comparison_failed = False
            for left_source, operator, right_source in plan.field_comparisons:
                left_value = _decimal(_raw_value(record, left_source))
                right_value = _decimal(_raw_value(record, right_source))
                if (
                    left_value is None
                    or right_value is None
                    or not self._compare(left_value, operator, right_value)
                ):
                    field_comparison_failed = True
                    break
            if field_comparison_failed:
                continue
            if threshold_evidence is not None:
                threshold_value = _metric_value(record, threshold_evidence.metric)
                if not isinstance(threshold_value, Decimal) or not self._compare(
                    threshold_value,
                    threshold_evidence.operator,
                    threshold_evidence.value,
                ):
                    continue
            for relative_metric, operator, threshold in plan.relative_filters:
                relative_value = _metric_value(record, relative_metric)
                if not isinstance(relative_value, Decimal) or not self._compare(
                    relative_value, operator, threshold
                ):
                    break
            else:
                eligible_records.append(record)
                continue
            # At least one relative constraint failed.
            continue

        candidates: list[tuple[_SourceRecord, Decimal | str, Decimal | str | None]] = []
        shortlist_ids: tuple[str, ...] = ()
        if plan.shortlist_metric is not None:
            # Stage 1 is owned only by its shortlist metric.  Requiring the
            # final metric here changes the user's operation into
            # ``availability filter -> shortlist`` and can promote a product
            # that was outside the requested first-stage top-N.  Select the
            # shortlist first, then apply final-metric availability strictly
            # inside that frozen set.
            stage_one: list[tuple[_SourceRecord, Decimal]] = []
            seen_stage_one_ids: set[str] = set()
            for record in eligible_records:
                public_id = _execution_product_id(
                    record,
                    plan.product_family,
                    compound.kind,
                )
                if public_id in seen_stage_one_ids:
                    continue
                shortlist_value = _metric_value(record, plan.shortlist_metric)
                if not isinstance(shortlist_value, Decimal):
                    continue
                seen_stage_one_ids.add(public_id)
                stage_one.append((record, shortlist_value))
            candidate_count = len(stage_one)
            shortlist_factor = (
                Decimal("-1")
                if plan.shortlist_direction is SortDirection.DESC
                else Decimal("1")
            )
            selected_stage_one = sorted(
                stage_one,
                key=lambda item: (
                    shortlist_factor * item[1],
                    _public_product_id(item[0], plan.product_family),
                    item[0].product_id,
                ),
            )[: plan.shortlist_limit]
            shortlist_ids = tuple(
                _execution_product_id(
                    item[0],
                    plan.product_family,
                    compound.kind,
                )
                for item in selected_stage_one
            )
            for record, shortlist_value in selected_stage_one:
                metric_value = _metric_value(record, plan.metric)
                if metric_value is not None:
                    candidates.append((record, metric_value, shortlist_value))
        else:
            seen_candidate_ids: set[str] = set()
            for record in eligible_records:
                public_id = _execution_product_id(
                    record,
                    plan.product_family,
                    compound.kind,
                )
                if public_id in seen_candidate_ids:
                    continue
                metric_value = _metric_value(record, plan.metric)
                if metric_value is None:
                    continue
                # Select the first *eligible* source row for each public
                # product.  Deduplicating inside ``_load_records`` used to
                # select the first workbook row before metric availability was
                # known, so a missing value could erase a later valid duplicate
                # from ranking and aggregation results.
                seen_candidate_ids.add(public_id)
                candidates.append((record, metric_value, None))
            candidate_count = len(candidates)

        comparison_scope_value = None
        if compound.kind == "cohort" and (
            compound.cohort_mean_direction is not None
            or compound.cohort_mean_difference
        ):
            if not candidates:
                raise CohortComparisonUnavailableError("cohort_metric_values_unavailable")
            if comparison_scope_source is not None:
                currency_definition = load_field_registry().require_field(
                    "trading_currency",
                    [plan.product_family.value],
                )
                currencies: set[str] = set()
                for record, _metric_value_for_sort, _shortlist_value in candidates:
                    normalized_currency = _normalized_evidence_value(
                        _raw_value(record, comparison_scope_source),
                        currency_definition,
                    )
                    if normalized_currency is None:
                        raise CohortComparisonUnavailableError(
                            "cohort_trading_currency_unavailable"
                        )
                    currencies.add(str(normalized_currency).strip().casefold())
                if len(currencies) != 1:
                    raise CohortComparisonUnavailableError(
                        "cohort_trading_currency_not_uniform"
                    )
                comparison_scope_value = next(iter(currencies))

        if compound.kind in {
            "named_pair",
            "cross_family_named_comparison",
        } and plan.identity_scope_active:
            candidate_ids = {
                _execution_product_id(
                    record,
                    plan.product_family,
                    compound.kind,
                )
                for record, _metric_value_for_sort, _shortlist_value in candidates
            }
            unavailable_product_ids = tuple(
                product_id
                for product_id in plan.include_product_ids
                if product_id not in candidate_ids
            )
            if unavailable_product_ids:
                raise NamedComparisonIncompleteError(
                    product_family=plan.product_family,
                    unavailable_product_ids=unavailable_product_ids,
                    metric_source_fields=plan.metric.source_fields,
                )

        if all(isinstance(item[1], Decimal) for item in candidates):
            factor = Decimal("-1") if plan.direction is SortDirection.DESC else Decimal("1")
            ranked = sorted(
                candidates,
                key=lambda item: (
                    factor
                    * (item[1] if isinstance(item[1], Decimal) else Decimal("0")),
                    _public_product_id(item[0], plan.product_family),
                    item[0].product_id,
                ),
            )[: plan.limit]
        elif all(isinstance(item[1], str) for item in candidates):
            # Metric direction must never reverse the deterministic tie-break.
            # Seed public product-ID ascending order, then rely on Python's
            # stable sort for the requested text-metric direction.
            public_id_ordered = sorted(
                candidates,
                key=lambda item: (
                    _public_product_id(item[0], plan.product_family),
                    item[0].product_id,
                ),
            )
            ranked = sorted(
                public_id_ordered,
                key=lambda item: str(item[1]).casefold(),
                reverse=plan.direction is SortDirection.DESC,
            )[: plan.limit]
        else:
            raise ValueError("compound ranking mixed numeric and text values")
        records = tuple(item[0] for item in ranked)
        metric_values = tuple(item[1] for item in ranked)
        evidence_fields = tuple(
            dict.fromkeys(
                (
                    *(item.source_field for item in plan.filters),
                    *plan.metric.source_fields,
                    *(plan.shortlist_metric.source_fields if plan.shortlist_metric else ()),
                    *(
                        field
                        for metric, _operator, _threshold in plan.relative_filters
                        for field in metric.source_fields
                    ),
                    *(
                        field
                        for left, _operator, right in plan.field_comparisons
                        for field in (left, right)
                    ),
                    *(
                        plan.aggregate_threshold.metric.source_fields
                        if plan.aggregate_threshold is not None
                        else ()
                    ),
                    *plan.projection_fields,
                )
            )
        )
        products = tuple(
            self._product_evidence(
                record,
                plan.product_family,
                evidence_fields,
                preserve_physical_identity=compound.kind
                in {"anchor_relative", "cross_family_named_comparison"},
            )
            for record in records
        )
        aggregate_summary = None
        if compound.kind == "cohort" and candidates:
            values = [item[1] for item in candidates]
            if any(not isinstance(value, Decimal) for value in values):
                raise ValueError("compound cohort aggregation requires numeric values")
            numeric_values = [value for value in values if isinstance(value, Decimal)]
            aggregate_summary = (
                len(numeric_values),
                sum(numeric_values, Decimal("0")) / Decimal(len(numeric_values)),
                min(numeric_values),
                max(numeric_values),
            )
        return CompoundFamilyExecution(
            plan=plan,
            query_plan=self._query_plan(
                compound,
                plan,
                aggregate_threshold_value=(
                    None if threshold_evidence is None else threshold_evidence.value
                ),
            ),
            candidate_count=candidate_count,
            products=products,
            product_records=records,
            shortlist_ids=shortlist_ids,
            metric_values=metric_values,
            aggregate_summary=aggregate_summary,
            comparison_scope_value=comparison_scope_value,
            aggregate_threshold_evidence=threshold_evidence,
        )

    def _execute_family_count(
        self,
        compound: TypedCompoundPlan,
        plan: CompoundFamilyPlan,
        records: tuple[_SourceRecord, ...],
    ) -> CompoundFamilyExecution:
        """Count distinct provided-DB product identities for one family."""

        if len(plan.aggregations) != 1:
            raise ValueError("family count aggregate requires one measure")
        identity_source = plan.aggregations[0].source_field
        unique: dict[str, _SourceRecord] = {}
        for record in records:
            if _raw_value(record, identity_source) is None:
                continue
            # Aggregation is over the normalized provided-DB product identity.
            # Bond tickers can legitimately have multiple market/date rows;
            # collapsing them to the display ticker changes official counts.
            unique.setdefault(record.product_id, record)
        manifest = self.record_cache.get(self.database_paths[plan.product_family]).manifest
        as_of = (
            max(record.dynamic_as_of for record in unique.values())
            if unique
            else manifest.source_snapshot_date
        )
        count = len(unique)
        group = CompoundGroupResult(
            group_value=plan.product_family.value,
            aggregates=(
                CompoundAggregateValue(
                    function=AggregateFunction.COUNT,
                    source_field=identity_source,
                    value=count,
                    valid_count=count,
                    missing_count=0,
                ),
            ),
            source_id=_SOURCE_ID_BY_FAMILY[plan.product_family],
            as_of=as_of,
            row_count=count,
        )
        return CompoundFamilyExecution(
            plan=plan,
            query_plan=self._query_plan(compound, plan),
            candidate_count=count,
            products=(),
            product_records=(),
            shortlist_ids=(),
            metric_values=(),
            grouped_aggregates=(group,),
        )

    def _execute_family_summary(
        self,
        compound: TypedCompoundPlan,
        plan: CompoundFamilyPlan,
        records: tuple[_SourceRecord, ...],
    ) -> CompoundFamilyExecution:
        """Aggregate one non-zero numeric metric over distinct products."""

        if len(plan.aggregations) != 1:
            raise ValueError("family summary aggregate requires one measure")
        aggregation = plan.aggregations[0]
        if aggregation.function not in {AggregateFunction.AVG, AggregateFunction.MAX}:
            raise ValueError("family summary function is not approved")
        unique_values: dict[str, tuple[_SourceRecord, Decimal]] = {}
        for record in records:
            value = _decimal(
                _raw_value(record, aggregation.source_field),
                allow_zero=False,
            )
            if value is None:
                continue
            unique_values.setdefault(
                _public_product_id(record, plan.product_family),
                (record, value),
            )
        numeric = [value for _record, value in unique_values.values()]
        aggregate_value: Decimal | None
        if not numeric:
            aggregate_value = None
        elif aggregation.function is AggregateFunction.AVG:
            aggregate_value = sum(numeric, Decimal("0")) / Decimal(len(numeric))
        else:
            aggregate_value = max(numeric)
        manifest = self.record_cache.get(self.database_paths[plan.product_family]).manifest
        as_of = (
            max(record.dynamic_as_of for record, _value in unique_values.values())
            if unique_values
            else manifest.source_snapshot_date
        )
        group = CompoundGroupResult(
            group_value=plan.product_family.value,
            aggregates=(
                CompoundAggregateValue(
                    function=aggregation.function,
                    source_field=aggregation.source_field,
                    value=aggregate_value,
                    valid_count=len(numeric),
                    missing_count=len(records) - len(numeric),
                ),
            ),
            source_id=_SOURCE_ID_BY_FAMILY[plan.product_family],
            as_of=as_of,
            row_count=len(numeric),
        )
        return CompoundFamilyExecution(
            plan=plan,
            query_plan=self._query_plan(compound, plan),
            candidate_count=len(numeric),
            products=(),
            product_records=(),
            shortlist_ids=(),
            metric_values=(),
            grouped_aggregates=(group,),
        )

    def _execute_grouped_topk_family(
        self,
        compound: TypedCompoundPlan,
        plan: CompoundFamilyPlan,
        records: tuple[_SourceRecord, ...],
    ) -> CompoundFamilyExecution:
        if plan.group_by_source_field is None or plan.per_group_limit is None:
            raise ValueError("grouped product TOP-K plan is incomplete")
        canonical_group = _SOURCE_TO_CANONICAL.get(
            (plan.product_family, plan.group_by_source_field)
        )
        if canonical_group is None:
            raise ValueError("grouped product field has no canonical mapping")
        group_definition = load_field_registry().require_field(
            canonical_group,
            [plan.product_family.value],
        )
        grouped: dict[str | int | bool, list[_SourceRecord]] = {}
        for record in records:
            raw_group = _raw_value(record, plan.group_by_source_field)
            if raw_group is None or not str(raw_group).strip():
                continue
            group_value = _normalized_evidence_value(raw_group, group_definition)
            if (
                group_value is None
                or (
                    group_definition.value_type is ValueType.ENUM
                    and group_value not in group_definition.enum_values
                )
            ):
                continue
            if not isinstance(group_value, (str, int, bool)):
                raise ValueError("grouped product value is not a scalar")
            grouped.setdefault(group_value, []).append(record)

        evidence_fields = tuple(
            dict.fromkeys(
                (
                    *(item.source_field for item in plan.filters),
                    *plan.metric.source_fields,
                    plan.group_by_source_field,
                    *(
                        (plan.group_min_count_source_field,)
                        if plan.group_min_count_source_field is not None
                        else ()
                    ),
                    *plan.projection_fields,
                )
            )
        )
        selected_records: list[_SourceRecord] = []
        selected_values: list[Decimal | str] = []
        selected_groups: list[str | int | bool] = []
        valid_counts: list[tuple[str | int | bool, int]] = []
        candidate_count = 0
        for group_value in sorted(grouped, key=lambda value: str(value).casefold()):
            group_records = grouped[group_value]
            threshold_valid_count = len(
                {
                    _public_product_id(record, plan.product_family)
                    for record in group_records
                    if plan.group_min_count_source_field is None
                    or _decimal(
                        _raw_value(record, plan.group_min_count_source_field),
                        allow_zero=False,
                    )
                    is not None
                }
            )
            if (
                plan.group_min_count is not None
                and threshold_valid_count < plan.group_min_count
            ):
                continue

            candidates: list[tuple[_SourceRecord, Decimal | str]] = []
            seen_ids: set[str] = set()
            for record in group_records:
                public_id = _public_product_id(record, plan.product_family)
                if public_id in seen_ids:
                    continue
                metric_value = _metric_value(record, plan.metric)
                if metric_value is None:
                    continue
                seen_ids.add(public_id)
                candidates.append((record, metric_value))
            if not candidates:
                continue
            if all(isinstance(value, Decimal) for _record, value in candidates):
                factor = (
                    Decimal("-1")
                    if plan.direction is SortDirection.DESC
                    else Decimal("1")
                )
                candidates.sort(
                    key=lambda item: (
                        factor * item[1],  # type: ignore[operator]
                        _public_product_id(item[0], plan.product_family),
                    )
                )
            elif all(isinstance(value, str) for _record, value in candidates):
                candidates.sort(
                    key=lambda item: _public_product_id(item[0], plan.product_family)
                )
                candidates.sort(
                    key=lambda item: str(item[1]).casefold(),
                    reverse=plan.direction is SortDirection.DESC,
                )
            else:
                raise ValueError("grouped product ranking mixed metric types")
            candidate_count += len(candidates)
            valid_counts.append((group_value, len(candidates)))
            for record, value in candidates[: plan.per_group_limit]:
                selected_records.append(record)
                selected_values.append(value)
                selected_groups.append(group_value)

        products = tuple(
            self._product_evidence(record, plan.product_family, evidence_fields)
            for record in selected_records
        )
        return CompoundFamilyExecution(
            plan=plan,
            query_plan=self._query_plan(compound, plan),
            candidate_count=candidate_count,
            products=products,
            product_records=tuple(selected_records),
            shortlist_ids=(),
            metric_values=tuple(selected_values),
            grouped_product_values=tuple(selected_groups),
            grouped_product_valid_counts=tuple(valid_counts),
        )

    def _execute_grouped_family(
        self,
        compound: TypedCompoundPlan,
        plan: CompoundFamilyPlan,
        records: tuple[_SourceRecord, ...],
    ) -> CompoundFamilyExecution:
        group_sources = _plan_group_sources(plan)
        if not group_sources or not plan.aggregations:
            raise ValueError("grouped aggregate plan is incomplete")
        canonical_groups = tuple(
            _SOURCE_TO_CANONICAL.get((plan.product_family, source))
            for source in group_sources
        )
        if any(item is None for item in canonical_groups):
            raise ValueError("grouped aggregate field has no canonical mapping")
        group_definitions = tuple(
            load_field_registry().require_field(
                canonical,
                [plan.product_family.value],
            )
            for canonical in canonical_groups
            if canonical is not None
        )
        grouped: dict[
            str | int | bool | tuple[str | int | bool, ...],
            list[_SourceRecord],
        ] = {}
        seen_product_ids: set[str] = set()
        for record in records:
            public_id = record.product_id
            if public_id in seen_product_ids:
                continue
            normalized_values: list[str | int | bool] = []
            for source, definition in zip(
                group_sources,
                group_definitions,
                strict=True,
            ):
                raw = _raw_value(record, source)
                if raw is None or not str(raw).strip():
                    break
                canonical = _normalized_evidence_value(raw, definition)
                if (
                    canonical is None
                    or (
                        definition.value_type is ValueType.ENUM
                        and canonical not in definition.enum_values
                    )
                    or not isinstance(canonical, (str, int, bool))
                ):
                    break
                normalized_values.append(canonical)
            if len(normalized_values) != len(group_sources):
                continue
            seen_product_ids.add(public_id)
            group_value: str | int | bool | tuple[str | int | bool, ...] = (
                normalized_values[0]
                if len(normalized_values) == 1
                else tuple(normalized_values)
            )
            grouped.setdefault(group_value, []).append(record)

        results: list[CompoundGroupResult] = []
        for group_value in sorted(grouped, key=lambda value: str(value).casefold()):
            group_records = grouped[group_value]
            threshold_valid_count = len(group_records)
            if plan.group_min_count_source_field is not None:
                threshold_valid_count = sum(
                    _decimal(
                        _raw_value(record, plan.group_min_count_source_field),
                        allow_zero=False,
                    )
                    is not None
                    for record in group_records
                )
            if (
                plan.group_min_count is not None
                and threshold_valid_count < plan.group_min_count
            ):
                continue
            values: list[CompoundAggregateValue] = []
            for aggregation in plan.aggregations:
                if aggregation.function is AggregateFunction.COUNT:
                    canonical_count = _SOURCE_TO_CANONICAL.get(
                        (plan.product_family, aggregation.source_field)
                    )
                    count_definition = (
                        None
                        if canonical_count is None
                        else load_field_registry().require_field(
                            canonical_count,
                            [plan.product_family.value],
                        )
                    )
                    if (
                        count_definition is not None
                        and count_definition.value_type is ValueType.NUMBER
                    ):
                        aggregate_value = sum(
                            _decimal(
                                _raw_value(record, aggregation.source_field),
                                allow_zero=False,
                            )
                            is not None
                            for record in group_records
                        )
                    else:
                        aggregate_value = sum(
                            1
                            for record in group_records
                            if (
                                (raw := _raw_value(record, aggregation.source_field))
                                is not None
                                and str(raw).strip()
                            )
                        )
                    numeric: list[Decimal] = []
                else:
                    numeric = [
                        value
                        for record in group_records
                        if (
                            value := _decimal(
                                _raw_value(record, aggregation.source_field),
                                allow_zero=False,
                            )
                        )
                        is not None
                    ]
                    if not numeric:
                        aggregate_value = None
                    elif aggregation.function is AggregateFunction.SUM:
                        aggregate_value = sum(numeric, Decimal(0))
                    elif aggregation.function is AggregateFunction.AVG:
                        aggregate_value = sum(numeric, Decimal(0)) / Decimal(
                            len(numeric)
                        )
                    elif aggregation.function is AggregateFunction.MIN:
                        aggregate_value = min(numeric)
                    elif aggregation.function is AggregateFunction.MAX:
                        aggregate_value = max(numeric)
                    elif aggregation.function is AggregateFunction.MEDIAN:
                        ordered = sorted(numeric)
                        midpoint = len(ordered) // 2
                        aggregate_value = (
                            ordered[midpoint]
                            if len(ordered) % 2
                            else (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)
                        )
                    else:
                        raise ValueError("unsupported grouped aggregate function")
                values.append(
                    CompoundAggregateValue(
                        function=aggregation.function,
                        source_field=aggregation.source_field,
                        value=aggregate_value,
                        valid_count=(
                            int(aggregate_value or 0)
                            if aggregation.function is AggregateFunction.COUNT
                            else len(numeric)
                        ),
                        missing_count=(
                            len(group_records) - int(aggregate_value or 0)
                            if aggregation.function is AggregateFunction.COUNT
                            else len(group_records) - len(numeric)
                        ),
                    )
                )
            results.append(
                CompoundGroupResult(
                    group_value=group_value,
                    aggregates=tuple(values),
                    source_id=group_records[0].source_id,
                    as_of=max(record.dynamic_as_of for record in group_records),
                    row_count=threshold_valid_count,
                )
            )
        if plan.group_order_aggregation is not None:
            order_spec = plan.group_order_aggregation
            factor = (
                Decimal("-1")
                if plan.direction is SortDirection.DESC
                else Decimal("1")
            )

            def group_order_key(
                group: CompoundGroupResult,
            ) -> tuple[int, Decimal, str]:
                matches = [
                    item
                    for item in group.aggregates
                    if item.function is order_spec.function
                    and item.source_field == order_spec.source_field
                ]
                if len(matches) != 1:
                    raise ValueError("group order aggregate output is missing or duplicated")
                raw_value = matches[0].value
                if raw_value is None:
                    return (1, Decimal("0"), str(group.group_value).casefold())
                return (
                    0,
                    factor * Decimal(str(raw_value)),
                    str(group.group_value).casefold(),
                )

            results.sort(key=group_order_key)
        elif plan.group_order_by_count:
            # Kept as a fail-closed compatibility guard for mutated/old plans;
            # current compilers always bind the exact COUNT aggregation above.
            results.sort(
                key=lambda group: (
                    -next(
                        int(value.value)
                        for value in group.aggregates
                        if value.function is AggregateFunction.COUNT
                        and value.value is not None
                    ),
                    str(group.group_value).casefold(),
                )
            )
        qualified_candidate_count = sum(item.row_count for item in results)
        if plan.group_limit is not None:
            results = results[: plan.group_limit]
        return CompoundFamilyExecution(
            plan=plan,
            query_plan=self._query_plan(compound, plan),
            candidate_count=qualified_candidate_count,
            products=(),
            product_records=(),
            shortlist_ids=(),
            metric_values=(),
            grouped_aggregates=tuple(results),
        )

    def _load_records(
        self,
        family: ProductFamily,
        *,
        filters: tuple[CompoundFilter, ...],
        source_fields: tuple[str, ...],
        product_ids: tuple[str, ...] = (),
        identity_scope_active: bool = False,
    ) -> tuple[_SourceRecord, ...]:
        """Read only the source cells required by a frozen compound plan.

        The shared RecordSnapshotCache intentionally omits ``source_values`` to
        keep the normal Runtime small. Compound calculations therefore use a
        parameterized, read-only projection instead of loading every workbook
        cell into process memory.
        """

        if any(not field.replace("_", "").isalnum() for field in source_fields):
            raise ValueError("unsafe source field in compound plan")
        table = _TABLE_BY_FAMILY[family]
        registry = load_field_registry()
        canonical_fields = {
            source_field: _SOURCE_TO_CANONICAL.get((family, source_field), source_field)
            for source_field in source_fields
        }
        for canonical in canonical_fields.values():
            registry.require_field(canonical, [family.value])
        raw_source_fields = tuple(
            dict.fromkeys(
                (
                    *source_fields,
                    *(
                        column
                        for canonical in canonical_fields.values()
                        for column in registry.require_field(
                            canonical,
                            [family.value],
                        ).source.columns
                    ),
                )
            )
        )
        if any(not field.replace("_", "").isalnum() for field in raw_source_fields):
            raise ValueError("unsafe raw evidence field in compound plan")
        aliases = {
            field: f"source_{index}" for index, field in enumerate(raw_source_fields)
        }
        dataset = registry.require_dataset(family.value)
        key_aliases = {
            field: f"source_key_{index}"
            for index, field in enumerate(dataset.primary_key)
        }
        quality_aliases = {
            source_field: f"quality_{index}"
            for index, source_field in enumerate(source_fields)
        }
        quality_reason_aliases = {
            source_field: f"quality_reason_{index}"
            for index, source_field in enumerate(source_fields)
        }
        if family is ProductFamily.OVERSEAS_ETP:
            quality_projections = []
            quality_reason_projections = []
            for source_field in source_fields:
                canonical = canonical_fields[source_field]
                quality_column, reason_column = _OVERSEAS_QUALITY_COLUMNS.get(
                    canonical,
                    ("row_quality", None),
                )
                quality_projections.append(
                    f"{quality_column} AS {quality_aliases[source_field]}"
                )
                quality_reason_projections.append(
                    "NULL AS " + quality_reason_aliases[source_field]
                    if reason_column is None
                    else f"{reason_column} AS {quality_reason_aliases[source_field]}"
                )
        else:
            quality_projections = [
                f"json_extract(field_quality_json, '$.{canonical_fields[field]}') "
                f"AS {quality_aliases[field]}"
                for field in source_fields
            ]
            quality_reason_projections = [
                f"json_extract(field_quality_reasons_json, '$.{canonical_fields[field]}') "
                f"AS {quality_reason_aliases[field]}"
                for field in source_fields
            ]
        ticker_projection = "NULL AS ticker" if family is ProductFamily.FUND else "ticker"
        remaining_days_projection = (
            "remaining_days AS normalized_remaining_days"
            if family is ProductFamily.BOND
            else "NULL AS normalized_remaining_days"
        )
        projections = [
            "product_id",
            "product_name",
            ticker_projection,
            "source_row",
            "source_snapshot_date",
            "static_as_of",
            "dynamic_as_of",
            remaining_days_projection,
            *(
                f"json_extract(source_values_json, '$.{field}') AS {alias}"
                for field, alias in aliases.items()
            ),
            *(
                f"json_extract(source_values_json, '$.{field}') AS {alias}"
                for field, alias in key_aliases.items()
            ),
            *quality_projections,
            *quality_reason_projections,
        ]
        where = ["is_quarantined = 0"]
        parameters: list[str | int | float | bool] = []
        for item in filters:
            if not item.source_field.replace("_", "").isalnum():
                raise ValueError("unsafe filter field in compound plan")
            canonical_filter = _SOURCE_TO_CANONICAL.get((family, item.source_field))
            definition = (
                None
                if canonical_filter is None
                else registry.require_field(canonical_filter, [family.value])
            )
            if definition is not None and definition.source.transform is SourceTransform.CONSTANT:
                constant = _normalized_evidence_value(
                    definition.source.constant_value,
                    definition,
                )
                if item.operator != "eq" or constant != item.value:
                    return ()
                continue
            expression = (
                "remaining_days"
                if family is ProductFamily.BOND and item.source_field == "remaining_days"
                else (
                    canonical_filter
                    if item.operator == "canonical_eq"
                    and canonical_filter is not None
                    else f"json_extract(source_values_json, '$.{item.source_field}')"
                )
            )
            if item.operator in {"eq", "canonical_eq"}:
                parameter = (
                    item.value[1]
                    if item.operator == "canonical_eq"
                    and isinstance(item.value, tuple)
                    and len(item.value) == 2
                    else item.value
                )
                where.append(
                    f"LOWER(TRIM(CAST({expression} AS TEXT))) = LOWER(TRIM(CAST(? AS TEXT)))"
                )
            elif item.operator == "neq":
                if definition is not None and definition.value_type is ValueType.NUMBER:
                    where.append(
                        f"{expression} IS NOT NULL AND "
                        f"CAST({expression} AS REAL) != CAST(? AS REAL)"
                    )
                else:
                    where.append(
                        f"LOWER(TRIM(CAST({expression} AS TEXT))) != "
                        "LOWER(TRIM(CAST(? AS TEXT)))"
                    )
            elif item.operator == "contains":
                if not isinstance(item.value, str) or not item.value.strip():
                    raise ValueError("compound CONTAINS filter requires text")
                where.append(
                    f"LOWER(CAST({expression} AS TEXT)) LIKE "
                    "'%' || LOWER(CAST(? AS TEXT)) || '%' ESCAPE '\\'"
                )
                parameters.append(
                    item.value.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                continue
            elif item.operator in {"gt", "gte", "lt", "lte"}:
                sql_operator = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[item.operator]
                nonzero_guard = (
                    ""
                    if item.source_field == "remaining_days"
                    else f"CAST({expression} AS REAL) != 0 AND "
                )
                where.append(
                    nonzero_guard
                    + f"CAST({expression} AS REAL) {sql_operator} CAST(? AS REAL)"
                )
            elif item.operator == "in":
                if not isinstance(item.value, tuple) or not item.value:
                    raise ValueError("compound IN filter requires a non-empty tuple")
                placeholders = ", ".join(
                    "LOWER(TRIM(CAST(? AS TEXT)))" for _ in item.value
                )
                where.append(
                    f"LOWER(TRIM(CAST({expression} AS TEXT))) IN ({placeholders})"
                )
                parameters.extend(item.value)
                continue
            elif item.operator == "between":
                if not isinstance(item.value, tuple) or len(item.value) != 2:
                    raise ValueError("compound BETWEEN filter requires two endpoints")
                nonzero_guard = (
                    ""
                    if item.source_field == "remaining_days"
                    else f"CAST({expression} AS REAL) != 0 AND "
                )
                where.append(
                    nonzero_guard
                    + f"CAST({expression} AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)"
                )
                parameters.extend(item.value)
                continue
            else:
                raise ValueError(f"unsupported compound filter: {item.operator}")
            parameters.append(parameter if item.operator == "canonical_eq" else item.value)
        if identity_scope_active and not product_ids:
            return ()
        if product_ids:
            placeholders = ", ".join("?" for _ in product_ids)
            identity_column = (
                "product_id"
                if any(":" in product_id for product_id in product_ids)
                else (
                    "ticker"
                    if family in {ProductFamily.BOND, ProductFamily.OVERSEAS_ETP}
                    else "product_id"
                )
            )
            where.append(f"{identity_column} IN ({placeholders})")
            parameters.extend(product_ids)
        sql = (
            f"SELECT {', '.join(projections)} FROM {table} "
            f"WHERE {' AND '.join(where)} ORDER BY source_row"
        )
        with connect_read_only(self.database_paths[family]) as connection:
            rows: list[sqlite3.Row] = connection.execute(sql, parameters).fetchall()
        records: list[_SourceRecord] = []
        for row in rows:
            raw_values: dict[str, str | int | bool | None] = {}
            field_quality: dict[str, QualityStatus] = {}
            field_quality_reasons: dict[str, str | None] = {}
            for field, alias in aliases.items():
                raw = (
                    row["normalized_remaining_days"]
                    if family is ProductFamily.BOND and field == "remaining_days"
                    else row[alias]
                )
                raw_values[field] = str(raw) if isinstance(raw, float) else raw
            for field in source_fields:
                canonical = canonical_fields[field]
                raw_quality = row[quality_aliases[field]]
                # A NULL per-row override means "use the registry policy";
                # it is not evidence that the source value is UNKNOWN.
                if raw_quality is not None:
                    try:
                        field_quality[canonical] = QualityStatus(str(raw_quality))
                    except ValueError:
                        field_quality[canonical] = QualityStatus.UNKNOWN
                raw_reason = row[quality_reason_aliases[field]]
                field_quality_reasons[canonical] = (
                    None if raw_reason is None else str(raw_reason)
                )
            records.append(
                _SourceRecord(
                    product_id=str(row["product_id"]),
                    product_name=str(row["product_name"]),
                    ticker=None if row["ticker"] is None else str(row["ticker"]),
                    source_id=_SOURCE_ID_BY_FAMILY[family],
                    source_row=int(row["source_row"]),
                    dynamic_as_of=date.fromisoformat(str(row["dynamic_as_of"])),
                    source_values=raw_values,
                    source_key={
                        field: str(row[alias])
                        for field, alias in key_aliases.items()
                    },
                    source_snapshot_date=date.fromisoformat(
                        str(row["source_snapshot_date"])
                    ),
                    static_as_of=date.fromisoformat(str(row["static_as_of"])),
                    remaining_days=(
                        None
                        if row["normalized_remaining_days"] is None
                        else int(row["normalized_remaining_days"])
                    ),
                    field_quality=field_quality,
                    field_quality_reasons=field_quality_reasons,
                )
            )
        return tuple(records)

    @staticmethod
    def _compare(value: Decimal, operator: str, threshold: Decimal) -> bool:
        return {
            "gt": value > threshold,
            "gte": value >= threshold,
            "lt": value < threshold,
            "lte": value <= threshold,
        }[operator]

    def _query_plan(
        self,
        compound: TypedCompoundPlan,
        family_plan: CompoundFamilyPlan,
        *,
        aggregate_threshold_value: Decimal | None = None,
    ) -> QueryPlan:
        registry = load_field_registry()
        constraints: list[Constraint] = []
        for item in family_plan.filters:
            canonical = _SOURCE_TO_CANONICAL.get((family_plan.product_family, item.source_field))
            if canonical is None:
                continue
            definition = registry.require_field(canonical, [family_plan.product_family.value])
            # Numeric zero/missing exclusion is the organizer-approved field
            # availability policy, not a user-selectable QueryPlan operator.
            # It remains sealed in ``TypedCompoundPlan`` and is revalidated
            # immediately before read-only execution, while the public DTO
            # omits a NEQ operator that this field registry deliberately does
            # not expose to arbitrary callers.
            if (
                item.operator == "neq"
                and item.value == 0
                and definition.value_type is ValueType.NUMBER
            ):
                continue
            value: str | int | float | bool | list[str | int | float | bool]
            contract_value = (
                item.value[0]
                if item.operator == "canonical_eq"
                and isinstance(item.value, tuple)
                and len(item.value) == 2
                else item.value
            )
            value = (
                list(
                    dict.fromkeys(
                        _canonical_constraint_value(raw, definition)
                        for raw in contract_value
                    )
                )
                if isinstance(contract_value, tuple)
                else _canonical_constraint_value(contract_value, definition)
            )
            operator = {
                "eq": ConstraintOperator.EQ,
                "canonical_eq": ConstraintOperator.EQ,
                "neq": ConstraintOperator.NEQ,
                "in": ConstraintOperator.IN,
                "gt": ConstraintOperator.GT,
                "gte": ConstraintOperator.GTE,
                "lt": ConstraintOperator.LT,
                "lte": ConstraintOperator.LTE,
                "between": ConstraintOperator.BETWEEN,
                "contains": ConstraintOperator.CONTAINS,
            }[item.operator]
            constraints.append(
                Constraint(
                    field=canonical,
                    operator=operator,
                    value=value,
                    unit=Unit(definition.unit),
                    strength=ConstraintStrength.LOCKED,
                )
            )
        if family_plan.include_product_ids:
            identity_field = {
                ProductFamily.BOND: "ticker",
                ProductFamily.DOMESTIC_ETP: "product_id",
                ProductFamily.OVERSEAS_ETP: "ticker",
                ProductFamily.FUND: "product_id",
            }[family_plan.product_family]
            definition = registry.require_field(
                identity_field,
                [family_plan.product_family.value],
            )
            constraints.append(
                Constraint(
                    field=identity_field,
                    operator=ConstraintOperator.IN,
                    value=[
                        (
                            (
                                product_id.split(":", 1)[0]
                                if family_plan.product_family is ProductFamily.BOND
                                else product_id.split(":", 1)[1]
                            )
                            if identity_field == "ticker" and ":" in product_id
                            else product_id
                        )
                        for product_id in family_plan.include_product_ids
                    ],
                    unit=Unit(definition.unit),
                    strength=ConstraintStrength.LOCKED,
                )
            )
        for metric, operator, threshold in family_plan.relative_filters:
            if metric.kind == "field":
                canonical = _SOURCE_TO_CANONICAL.get(
                    (family_plan.product_family, metric.source_fields[0])
                )
                if canonical is not None:
                    definition = registry.require_field(
                        canonical,
                        [family_plan.product_family.value],
                    )
                    constraints.append(
                        Constraint(
                            field=canonical,
                            operator=ConstraintOperator(operator),
                            value=float(threshold),
                            unit=Unit(definition.unit),
                            strength=ConstraintStrength.LOCKED,
                        )
                    )
        if (
            family_plan.aggregate_threshold is not None
            and aggregate_threshold_value is not None
        ):
            threshold_metric = family_plan.aggregate_threshold.metric
            canonical = _SOURCE_TO_CANONICAL.get(
                (family_plan.product_family, threshold_metric.source_fields[0])
            )
            if canonical is None:
                raise ValueError("aggregate threshold has no canonical field mapping")
            definition = registry.require_field(
                canonical,
                [family_plan.product_family.value],
            )
            constraints.append(
                Constraint(
                    field=canonical,
                    operator=ConstraintOperator(
                        family_plan.aggregate_threshold.operator
                    ),
                    value=float(aggregate_threshold_value),
                    unit=Unit(definition.unit),
                    strength=ConstraintStrength.LOCKED,
                )
            )
        canonical_metric = (
            _SOURCE_TO_CANONICAL.get(
                (family_plan.product_family, family_plan.metric.source_fields[0])
            )
            if family_plan.metric.kind == "field"
            else None
        )
        ranking = (
            [
                Ranking(
                    field=canonical_metric,
                    direction=family_plan.direction,
                    nulls=NullPlacement.LAST,
                )
            ]
            if canonical_metric is not None
            else []
        )
        fields = [
            _SOURCE_TO_CANONICAL[(family_plan.product_family, source)]
            for source in (
                *family_plan.metric.source_fields,
                *(
                    family_plan.shortlist_metric.source_fields
                    if family_plan.shortlist_metric
                    else ()
                ),
                *(
                    family_plan.aggregate_threshold.metric.source_fields
                    if family_plan.aggregate_threshold is not None
                    else ()
                ),
                *family_plan.projection_fields,
            )
            if (family_plan.product_family, source) in _SOURCE_TO_CANONICAL
        ]
        if compound.kind in {
            "grouped_aggregate",
            "family_count_aggregate",
            "family_summary_aggregate",
        }:
            group_sources = _plan_group_sources(family_plan)
            if compound.kind == "grouped_aggregate" and not group_sources:
                raise ValueError("grouped aggregate requires a group field")
            group_fields = [
                _SOURCE_TO_CANONICAL[(family_plan.product_family, source)]
                for source in group_sources
            ]
            aggregate_payload = [
                Aggregation(
                    function=item.function,
                    field=_SOURCE_TO_CANONICAL[
                        (family_plan.product_family, item.source_field)
                    ],
                )
                for item in family_plan.aggregations
            ]
            aggregate_fields = [item.field for item in aggregate_payload]
            return QueryPlan(
                schema_version="1.0",
                question_id=compound.question_id,
                intent=Intent.AGGREGATE,
                product_families=[family_plan.product_family],
                constraints=constraints,
                ranking=[],
                projection=search_projection(
                    family_plan.product_family,
                    *group_fields,
                    *aggregate_fields,
                ),
                limit=family_plan.limit,
                intent_payload=IntentPayload(
                    comparison_fields=[],
                    group_by=group_fields,
                    aggregations=aggregate_payload,
                    explain_product_ids=[],
                ),
                ambiguities=[],
                unsupported_conditions=[],
            )
        return QueryPlan(
            schema_version="1.0",
            question_id=compound.question_id,
            intent=Intent.SEARCH,
            product_families=[family_plan.product_family],
            constraints=constraints,
            ranking=ranking,
            projection=search_projection(family_plan.product_family, *fields),
            limit=family_plan.limit,
            intent_payload=IntentPayload(
                comparison_fields=[],
                group_by=[],
                aggregations=[],
                explain_product_ids=[],
            ),
            ambiguities=[],
            unsupported_conditions=[],
        )

    def _product_evidence(
        self,
        record: object,
        family: ProductFamily,
        source_fields: tuple[str, ...],
        *,
        preserve_physical_identity: bool = False,
    ) -> ProductEvidence:
        fields: list[FieldEvidence] = []
        for source_field in source_fields or ("pd_itm_no",):
            raw = _raw_value(record, source_field)
            canonical = _SOURCE_TO_CANONICAL.get((family, source_field), source_field)
            definition = load_field_registry().require_field(canonical, [family.value])
            value = _normalized_evidence_value(raw, definition)
            as_of = {
                AsOfBasis.DYNAMIC: record.dynamic_as_of,
                AsOfBasis.SNAPSHOT: record.source_snapshot_date or record.dynamic_as_of,
                AsOfBasis.STATIC: record.static_as_of or record.dynamic_as_of,
            }[definition.as_of_basis]
            quality = record.field_quality.get(canonical, definition.quality)
            quality_reason = record.field_quality_reasons.get(canonical)
            source_columns = list(definition.source.columns)
            if (
                raw is None
                and definition.source.transform is not SourceTransform.CONSTANT
                and quality is QualityStatus.VALID
            ):
                quality = QualityStatus.UNKNOWN
                quality_reason = quality_reason or f"{canonical}_missing"
            fields.append(
                FieldEvidence(
                    canonical_field=canonical,
                    source_dataset=family.value,
                    source_id=record.source_id,
                    source_key=dict(record.source_key),
                    source_row=record.source_row,
                    source_columns=source_columns,
                    raw_values={
                        column: record.source_values.get(column)
                        for column in source_columns
                    },
                    normalized_value=value,
                    unit=definition.unit,
                    as_of=as_of,
                    quality=quality,
                    quality_reason=quality_reason,
                )
            )
        return ProductEvidence(
            product_id=(
                str(record.product_id)
                if preserve_physical_identity
                else _public_product_id(record, family)
            ),
            product_name=record.product_name,
            ticker=getattr(record, "ticker", None),
            fields=fields,
        )


__all__ = [
    "AggregateThresholdUnavailableError",
    "CohortComparisonUnavailableError",
    "CompoundAuthorizationReceipt",
    "CompoundAggregateThreshold",
    "CompoundAggregateThresholdEvidence",
    "CompoundExecution",
    "NamedComparisonIncompleteError",
    "TypedCompoundPlan",
    "TypedCompoundQueryEngine",
]
