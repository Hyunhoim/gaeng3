from __future__ import annotations

import re
from collections.abc import Iterable

from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
    PlanningPolicy,
    PlanningTrace,
)
from finance_agent_core.agent.safety import (
    SafetyDisposition,
    SafetyEnvelope,
    safety_policy_surface,
)
from finance_agent_core.agent.semantic_gate import (
    SemanticCoverageDecision,
    SemanticCoverageGate,
)
from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)

_COMPARE = re.compile(
    r"비교|대조|차이|나란히|versus|\bvs\b|"
    r"(?:둘|두\s*(?:개(?:의)?|상품|공모\s*펀드|펀드|채권|ETF|ETN))"
    r".{0,40}?중(?:에서)?|"
    r"어느\s*(?:게|것이).{0,40}?(?:높|낮|크|작|많|적)",
    re.IGNORECASE,
)
_AGGREGATE = re.compile(
    r"몇\s*개|개수(?!\s*[:：]\s*\d+)|건수|평균|합계|총합|집계|분포|비중|"
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?\s*(?:계산|집계|총합|알려)|"
    r"최댓값|최대값|최솟값|최소값|최고값|최저값|"
    r"(?:AUM|보수율|수익률|이율|잔존일수|듀레이션)\s*(?:최대|최소)",
    re.IGNORECASE,
)
_EXPLAIN = re.compile(r"설명|무슨\s*뜻|뭐(?:야|고)|의미|장점|요소|왜\s")
_DEFINITION = re.compile(r"무슨\s*뜻|뭐(?:야|고)|의미")
_DETAIL = re.compile(
    r"상세|세부|자세히|어떤\s*상품|정보\s*조회|상품\s*(?:번호|ID|아이디)|"
    r"종목\s*(?:코드|번호)|티커",
    re.IGNORECASE,
)
_AMBIGUOUS = re.compile(
    r"적당한|괜찮은|안전한|좋은\s*상품|추천(?:해|하|받|할\s*만한)|"
    r"좀\s*낮아도|많이\s*주는|제일\s*수익률|리스크.*신경\s*안|"
    r"뭐가\s*더\s*좋|어느.*더\s*좋|"
    r"(?:가격.*왜|왜.*가격)",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(
    r"전망|예측|예상\s*수익|수익\s*보장|원금\s*보장|"
    r"매수(?:를)?\s*추천|투자\s*추천|사야\s*할|사면\s*좋|가장\s*좋은|"
    r"상승할\s*것으로\s*예상|"
    r"기대되는\s*(?:고)?수익|오를\s*것|오를까|호재|악재|"
    r"유상증자|유상감자|사모\s*CB|"
    r"기관.*(?:구매|순매수|매매량)|"
    r"오늘.*가장.*(?:오른|상승)|"
    r"국내주식.*해외주식|해외주식.*국내주식",
    re.IGNORECASE,
)
_EXTERNAL_POLICY = re.compile(r"세율|세금|과세|거래\s*수수료", re.IGNORECASE)
_UNAVAILABLE_MARKET_DATA = re.compile(
    r"거래량|괴리율|배당.*(?:가장|최대)|"
    r"최근\s*[2-9]\s*년.*수익률|[2-9]\s*년간.*수익률|"
    r"우주항공.*수익률|하락장.*잘\s*버티|"
    r"변동성.*(?:낮|높|제일|가장)",
    re.IGNORECASE,
)
_MIXED_ETP_COST = re.compile(
    r"(?=.*ETF)(?=.*ETN)(?=.*(?:보수|수수료|비용))",
    re.IGNORECASE,
)
_EXPLICIT_TOTAL_EXPENSE_RATIO = re.compile(r"총\s*보수(?:율)?|보수율", re.IGNORECASE)
_TICKER_ORDERING = re.compile(
    r"(?:티커|종목\s*코드).{0,24}"
    r"(?:오름차순|내림차순|큰\s*순|작은\s*순|높은\s*순|낮은\s*순|순서)",
    re.IGNORECASE,
)
_RANKED_SET_SEARCH = re.compile(
    r"끼리(?:만)?.{0,100}?"
    r"(?:오름차순|내림차순|높은\s*순|낮은\s*순|큰\s*순|작은\s*순|"
    r"많은\s*순|적은\s*순|상위|하위|ascending|descending|"
    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest)",
    re.IGNORECASE,
)
_EXPLICIT_ETP_TYPE = re.compile(
    r"(?:ETP\s*유형|ETF\s*여부|상품\s*유형)(?:이|가|은|는)?\s*[:：]?\s*(?:ETF|ETN)|"
    r"(?<![A-Z])(?:ETF|ETN)(?:인(?!지)|이며|이고|인데|에\s*해당|로\s*되어)",
    re.IGNORECASE,
)
_OVERSEAS_UNAVAILABLE_METRIC = re.compile(
    r"수익률|변동성|하락장|오른|S&P\s*500",
    re.IGNORECASE,
)
_FINANCE_SCOPE_SIGNAL = re.compile(
    r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])|공모\s*펀드|국내\s*채권|"
    r"(?:금융|투자)\s*상품|"
    r"상품(?:군|유형|목록|조회|검색|추천|을|를|이|가|은|는|중|\s*있(?:어|나|나요))|"
    r"AUM|수익률|총\s*보수|보수율|매수\s*수익률|잔존\s*일수|듀레이션|"
    r"종목\s*코드|티커|만기|채권|펀드",
    re.IGNORECASE,
)
_FUND_UNAVAILABLE_DETAIL = re.compile(
    r"최근\s*3\s*년.*수익률|언제든.*(?:꺼내|환매)",
    re.IGNORECASE,
)
_QUOTED = (
    re.compile(r'"([^"\n]+)"'),
    re.compile(r"'([^'\n]+)'"),
    re.compile(r"“([^”\n]+)”"),
    re.compile(r"‘([^’\n]+)’"),
)
_KNOWN_ID = re.compile(
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+|"
    r"[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
    re.IGNORECASE,
)
_LABELED_ID = re.compile(
    r"(?:상품\s*(?:번호|ID|아이디)|종목\s*(?:코드|번호)|티커)"
    r"(?:가|는|은|이)?\s*[:：]?\s*"
    r"([A-Z0-9._:-]{2,30})",
    re.IGNORECASE,
)
_CONTROL_FAMILY_PRIORITY = {
    ProductFamily.FUND: 0,
    ProductFamily.BOND: 1,
    ProductFamily.DOMESTIC_ETP: 2,
    ProductFamily.OVERSEAS_ETP: 3,
}


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _product_mentions(question: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    for pattern in _QUOTED:
        mentions.extend((match.start(), match.group(1)) for match in pattern.finditer(question))
    mentions.extend((match.start(), match.group(0)) for match in _KNOWN_ID.finditer(question))
    mentions.extend((match.start(), match.group(1)) for match in _LABELED_ID.finditer(question))
    return _ordered_unique(value for _, value in sorted(mentions))


def _product_families(question: str) -> list[ProductFamily]:
    mentions: list[tuple[int, ProductFamily]] = []

    def add_matches(pattern: str, family: ProductFamily, *, flags: int = 0) -> None:
        mentions.extend((match.start(), family) for match in re.finditer(pattern, question, flags))

    add_matches(r"공모\s*펀드|공모펀드", ProductFamily.FUND, flags=re.IGNORECASE)
    add_matches(
        r"국내\s*채권(?!\s*형[^와과,\n]{0,12}?(?:ETF|ETN|ETP))|"
        r"회사채|국채|국공채|국고채|특수채|금융채|"
        r"지역개발채|도시철도공채|채권\s*상품|채권(?!형)",
        ProductFamily.BOND,
        flags=re.IGNORECASE,
    )

    etp_token = r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])"
    domestic_pattern = (
        rf"(?:국내|한국|코스피|코스닥)(?!\s*채권)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}[^와과,\n]{{0,30}}?(?:국내|한국)(?!\s*채권)"
    )
    overseas_pattern = (
        rf"(?:해외|글로벌)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}\s*해외"
    )
    domestic_matches = list(re.finditer(domestic_pattern, question, re.IGNORECASE))
    overseas_matches = list(re.finditer(overseas_pattern, question, re.IGNORECASE))
    overseas_matches = [
        match
        for match in overseas_matches
        if re.search(
            rf"(?:국내|한국)[^와과,\n]{{0,20}}?{etp_token}",
            match.group(0),
            re.IGNORECASE,
        )
        is None
    ]
    mentions.extend((match.start(), ProductFamily.DOMESTIC_ETP) for match in domestic_matches)
    mentions.extend((match.start(), ProductFamily.OVERSEAS_ETP) for match in overseas_matches)

    if re.search(etp_token, question, re.IGNORECASE):
        explicit_etp_family = domestic_matches or overseas_matches
        if not explicit_etp_family:
            if re.search(r"해외|글로벌", question, re.IGNORECASE):
                match = re.search(r"해외|글로벌", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif re.search(r"국내|한국", question, re.IGNORECASE):
                match = re.search(r"국내|한국", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.DOMESTIC_ETP))
            elif re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE):
                match = re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif re.search(r"미국", question):
                match = re.search(r"미국", question)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))

    domestic_brand = re.search(r"\b(?:TIGER|ACE)\b", question, re.IGNORECASE)
    if domestic_brand is not None:
        mentions.append((domestic_brand.start(), ProductFamily.DOMESTIC_ETP))
    domestic_underlying = re.search(
        r"(?:삼성전자.*하이닉스|하이닉스.*삼성전자).*레버리지",
        question,
        re.IGNORECASE,
    )
    if domestic_underlying is not None:
        mentions.append((domestic_underlying.start(), ProductFamily.DOMESTIC_ETP))
    overseas_ticker = re.search(r"(?<![A-Z])SOXL(?![A-Z])", question, re.IGNORECASE)
    if overseas_ticker is not None:
        mentions.append((overseas_ticker.start(), ProductFamily.OVERSEAS_ETP))

    fund = re.search(r"(?<!사모)\s*펀드", question, re.IGNORECASE)
    if fund is not None:
        mentions.append((fund.start(), ProductFamily.FUND))

    ordered = [family for _, family in sorted(mentions, key=lambda item: item[0])]
    return list(dict.fromkeys(ordered))


def _requested_limit(question: str) -> int | None:
    labeled_matches = re.findall(
        r"(?:개수|제한)\s*[:：]\s*(\d+)",
        question,
        flags=re.IGNORECASE,
    )
    if labeled_matches:
        value = int(labeled_matches[-1])
        return value if 1 <= value <= 100 else None
    if re.search(
        r"(?:^|\s)(?:하나|한\s*개)(?:만)?\s*"
        r"(?:보여|알려|찾아|골라|선정|추천|조회)",
        question,
        flags=re.IGNORECASE,
    ):
        return 1
    matches = re.findall(r"(\d+)\s*(?:개|건)(?!월)", question)
    if not matches:
        return None
    value = int(matches[-1])
    return value if 1 <= value <= 100 else None


def _intent(question: str, families: list[ProductFamily]) -> InteractionIntent:
    policy_surface = safety_policy_surface(question)
    if (
        _UNSUPPORTED.search(policy_surface)
        or _EXTERNAL_POLICY.search(question)
        or _UNAVAILABLE_MARKET_DATA.search(question)
        or (
            families == [ProductFamily.OVERSEAS_ETP]
            and _OVERSEAS_UNAVAILABLE_METRIC.search(question)
            and not _COMPARE.search(question)
        )
        or (ProductFamily.FUND in families and _FUND_UNAVAILABLE_DETAIL.search(question))
    ):
        return InteractionIntent.UNSUPPORTED
    exact_two_product_comparison = bool(
        _COMPARE.search(question) and len(_product_mentions(question)) == 2
    )
    if _AMBIGUOUS.search(policy_surface) or (
        _MIXED_ETP_COST.search(question)
        and _EXPLICIT_ETP_TYPE.search(question) is None
        and _EXPLICIT_TOTAL_EXPENSE_RATIO.search(question) is None
        and not exact_two_product_comparison
    ):
        return InteractionIntent.CLARIFY
    if _DEFINITION.search(question):
        return InteractionIntent.EXPLAIN
    if _RANKED_SET_SEARCH.search(question) and len(_product_mentions(question)) != 2:
        return InteractionIntent.SEARCH
    if _COMPARE.search(question):
        return InteractionIntent.COMPARE
    if _AGGREGATE.search(question):
        return InteractionIntent.AGGREGATE
    if _TICKER_ORDERING.search(question):
        return InteractionIntent.SEARCH
    if _EXPLAIN.search(question):
        return InteractionIntent.EXPLAIN
    if _DETAIL.search(question):
        return InteractionIntent.DETAIL
    return InteractionIntent.SEARCH


class IntentRouter:
    """Deterministic, fail-closed router in front of any model-generated plan."""

    def __init__(
        self,
        matrix: CapabilityMatrix | None = None,
        *,
        safety_envelope: SafetyEnvelope | None = None,
        semantic_coverage_gate: SemanticCoverageGate | None = None,
        planning_policy: PlanningPolicy | None = None,
        hclx_planning_enabled: bool = False,
    ) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
        self.matrix = matrix or load_capability_matrix()
        self.safety_envelope = safety_envelope or SafetyEnvelope()
        self.semantic_coverage_gate = semantic_coverage_gate or SemanticCoverageGate()
        self._authority_planning_policy = AdaptiveShadowPlanningPolicy(
            hclx_planning_enabled=hclx_planning_enabled
        )
        self.planning_policy = planning_policy or self._authority_planning_policy

    def route(self, question: str, request_id: str) -> RouteDecision:
        """Return the frozen legacy contract while evaluating Stage 1 shadow policy."""

        return self.route_with_planning(question, request_id).route_decision

    def route_with_planning(self, question: str, request_id: str) -> PlanningTrace:
        """Pair the legacy route with non-enforcing, server-owned shadow metadata."""

        return self._evaluate_with_planning(question, request_id)

    def _planning_trace(
        self,
        route_decision: RouteDecision,
        coverage: SemanticCoverageDecision,
    ) -> PlanningTrace:
        # Pydantic frozen models are shallow: nested product-family and mention
        # lists can still be changed in place. Snapshot the authoritative route
        # before an injected shadow policy sees an isolated copy.
        trusted_route = RouteDecision.model_validate_json(route_decision.model_dump_json())
        try:
            planning_coverage = coverage
            if (
                trusted_route.draft.intent
                in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
                and not coverage.unsupported_spans
            ):
                # The legacy gate intentionally skips deeper scanning when the
                # lexical router already chose a control intent. Shadow policy
                # performs an additional read-only scan so an unsupported
                # action (for example CSV export) still outranks ambiguity,
                # without changing the frozen legacy RouteDecision.
                supplemental = self.semantic_coverage_gate.evaluate(
                    trusted_route.draft.question,
                    interaction_intent=None,
                    check_exclusions=False,
                )
                if supplemental.unsupported_spans:
                    planning_coverage = SemanticCoverageDecision(
                        ambiguity_spans=coverage.ambiguity_spans,
                        unsupported_spans=tuple(
                            dict.fromkeys(
                                (
                                    *coverage.unsupported_spans,
                                    *supplemental.unsupported_spans,
                                )
                            )
                        ),
                        schema_link_gap_spans=coverage.schema_link_gap_spans,
                    )
            authoritative = self._authority_planning_policy.decide(
                RouteDecision.model_validate_json(trusted_route.model_dump_json()),
                planning_coverage,
            )
            candidate = authoritative
            if self.planning_policy is not self._authority_planning_policy:
                candidate = self.planning_policy.decide(
                    RouteDecision.model_validate_json(trusted_route.model_dump_json()),
                    planning_coverage,
                )
            if not isinstance(candidate, PlanningDecision):
                raise TypeError("planning policy must return PlanningDecision")
            # Pydantic model_copy(update=...) does not re-run validators. Treat
            # an injected policy as untrusted and rebuild from plain data before
            # accepting its authority flags.
            planning_decision = PlanningDecision.model_validate(candidate.model_dump(mode="python"))
            if planning_decision != authoritative:
                raise ValueError("planning policy differs from adaptive-shadow-v1 authority")
            return PlanningTrace(
                route_decision=trusted_route,
                planning_decision=planning_decision,
            )
        except Exception:
            # A malformed or escalating policy result must not reach Compiler,
            # HCLX, SQL, or Oracle. Existing control reasons remain intact; an
            # otherwise executable route is converted to a stable fail-closed
            # response without exposing the exception text.
            if trusted_route.disposition is RouteDisposition.EXECUTE:
                closed_draft = MinimalQueryDraft.model_validate(
                    {
                        **trusted_route.draft.model_dump(mode="python"),
                        "intent": InteractionIntent.UNSUPPORTED,
                    }
                )
                trusted_route = self._control(
                    closed_draft,
                    RouteDisposition.UNSUPPORTED,
                    "planning_policy_error",
                    "내부 계획 정책을 안전하게 검증하지 못해 요청을 실행하지 않았습니다.",
                )
            planning_decision = PlanningDecision.fail_closed(trusted_route)
        return PlanningTrace(
            route_decision=trusted_route,
            planning_decision=planning_decision,
        )

    def _evaluate_with_planning(
        self,
        question: str,
        request_id: str,
    ) -> PlanningTrace:
        safety = self.safety_envelope.evaluate(question)
        stripped = safety.normalized_question
        if not stripped:
            raise ValueError("question cannot be blank")
        if not request_id.strip():
            raise ValueError("request_id cannot be blank")

        families = _product_families(stripped)
        intent = _intent(stripped, families)
        cross_family_control = len(families) > 1 and intent is not InteractionIntent.SEARCH
        coverage = self.semantic_coverage_gate.evaluate(
            stripped,
            interaction_intent=intent.value,
            check_exclusions=False,
        )
        if safety.disposition is SafetyDisposition.UNSUPPORTED or coverage.unsupported_spans:
            intent = InteractionIntent.UNSUPPORTED
        elif safety.disposition is SafetyDisposition.CLARIFY or (
            coverage.ambiguity_spans and not cross_family_control
        ):
            intent = InteractionIntent.CLARIFY
        if len(families) > 1 and intent is not InteractionIntent.SEARCH:
            # Control responses do not execute a family sequence. Keep their DTO order
            # stable across router vocabulary changes and preserve the frozen contract.
            families = sorted(families, key=_CONTROL_FAMILY_PRIORITY.__getitem__)
        mentions = _product_mentions(stripped)
        draft = MinimalQueryDraft(
            request_id=request_id,
            question=stripped,
            intent=intent,
            product_families=families,
            product_mentions=mentions,
            requested_limit=_requested_limit(stripped),
        )

        if safety.blocked:
            assert safety.gate is not None
            assert safety.reason is not None
            disposition = (
                RouteDisposition.CLARIFY
                if safety.disposition is SafetyDisposition.CLARIFY
                else RouteDisposition.UNSUPPORTED
            )
            return self._planning_trace(
                self._control(
                    draft,
                    disposition,
                    f"safety_{safety.gate.value}",
                    safety.reason,
                ),
                coverage,
            )
        if coverage.unsupported_spans:
            spans = ", ".join(coverage.unsupported_spans)
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "semantic_unmapped_action",
                    (f"읽기 전용 데이터 조회로 실행할 수 없는 조건·행동: {spans}")[:500],
                ),
                coverage,
            )
        if coverage.ambiguity_spans and not cross_family_control:
            spans = ", ".join(coverage.ambiguity_spans)
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "semantic_coverage_incomplete",
                    f"필드나 기준을 하나로 확정할 수 없는 조건: {spans}"[:500],
                ),
                coverage,
            )
        if not families and _FINANCE_SCOPE_SIGNAL.search(stripped) is None:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "safety_scope",
                    "현재 서비스는 승인된 금융상품 데이터 조회만 지원합니다.",
                ),
                coverage,
            )
        if intent is InteractionIntent.UNSUPPORTED:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "prohibited_financial_request",
                    "제공 데이터로 검증할 수 없는 예측·보장·단정적 추천 요청",
                ),
                coverage,
            )
        if intent is InteractionIntent.CLARIFY:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "subjective_condition",
                    "판단 기준이나 임계값이 명시되지 않은 주관적 조건",
                ),
                coverage,
            )
        if not families:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "ambiguous_product_family",
                    "실행할 상품군을 확정할 수 없음",
                ),
                coverage,
            )
        if len(families) > 1:
            if intent is not InteractionIntent.SEARCH:
                return self._planning_trace(
                    self._control(
                        draft,
                        RouteDisposition.CLARIFY,
                        "ambiguous_product_family",
                        "복수 상품군은 현재 상품군별 독립 검색만 지원",
                    ),
                    coverage,
                )
            capabilities = [
                self.matrix.require(family, InteractionIntent.SEARCH) for family in families
            ]
            blocked = next(
                (
                    capability
                    for capability in capabilities
                    if capability.status != "executable"
                    or capability.query_plan_intent is not Intent.SEARCH
                ),
                None,
            )
            if blocked is not None:
                return self._planning_trace(
                    self._control(
                        draft,
                        RouteDisposition.UNSUPPORTED,
                        "capability_not_implemented",
                        blocked.reason,
                    ),
                    coverage,
                )
            return self._planning_trace(
                RouteDecision(
                    draft=draft,
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="cross_family_search_executable",
                    reason="복수 상품군을 각각 독립적으로 검색하고 검증",
                    query_plan_intent=Intent.SEARCH,
                    capability_matrix_version=self.matrix.matrix_version,
                ),
                coverage,
            )
        if intent in {InteractionIntent.DETAIL, InteractionIntent.EXPLAIN} and not mentions:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "missing_product_identity",
                    "상세 조회·설명에는 정확한 상품번호나 종목코드가 필요",
                ),
                coverage,
            )
        if intent is InteractionIntent.COMPARE and len(mentions) != 2:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "missing_product_identity",
                    "비교에는 서로 다른 두 상품의 정확한 식별자가 필요",
                ),
                coverage,
            )

        capability = self.matrix.require(families[0], intent)
        if capability.status == "unsupported":
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "capability_not_implemented",
                    capability.reason,
                ),
                coverage,
            )
        if capability.status == "control":
            disposition = (
                RouteDisposition.CLARIFY
                if intent is InteractionIntent.CLARIFY
                else RouteDisposition.UNSUPPORTED
            )
            return self._planning_trace(
                self._control(
                    draft,
                    disposition,
                    f"{intent.value}_control",
                    capability.reason,
                ),
                coverage,
            )
        return self._planning_trace(
            RouteDecision(
                draft=draft,
                disposition=RouteDisposition.EXECUTE,
                reason_code="capability_executable",
                reason=capability.reason,
                query_plan_intent=capability.query_plan_intent,
                capability_matrix_version=self.matrix.matrix_version,
            ),
            coverage,
        )

    def _control(
        self,
        draft: MinimalQueryDraft,
        disposition: RouteDisposition,
        reason_code: str,
        reason: str,
    ) -> RouteDecision:
        return RouteDecision(
            draft=draft,
            disposition=disposition,
            reason_code=reason_code,
            reason=reason,
            query_plan_intent=None,
            capability_matrix_version=self.matrix.matrix_version,
        )
