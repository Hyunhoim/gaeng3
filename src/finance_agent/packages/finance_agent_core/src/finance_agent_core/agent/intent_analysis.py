from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.product_family_language import (
    explicit_etp_market_families,
    has_standalone_bond_product_scope,
    has_standalone_public_fund_scope,
)
from finance_agent_core.agent.semantic_aliases import typed_alias_matches
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import KnowledgeIntentRequirement
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)


class IntentAnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticFrameStatus(StrEnum):
    """Whether HCX could bind one literal clause to the closed server schema."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class SemanticFrameOperation(StrEnum):
    NONE = "none"
    FILTER = "filter"
    RANK = "rank"
    PROJECT = "project"
    AGGREGATE = "aggregate"
    EXTERNAL_RELATION = "external_relation"


class SemanticFrameOperator(StrEnum):
    NONE = "none"
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    BETWEEN = "between"
    CONTAINS = "contains"


class SemanticFrameDirection(StrEnum):
    NONE = "none"
    ASC = "asc"
    DESC = "desc"


_SEMANTIC_FRAME_ASCENDING_CUE = re.compile(
    r"낮(?:은|게|다|고|으며|아|을수록)|"
    r"작(?:은|게|다|고|으며|아|을수록)|"
    r"적(?:은|게|다|고|으며|어|을수록)|"
    r"짧(?:은|게|다|고|으며|아|을수록)|"
    r"빠른|빠르(?:게|다|고|며)|이른|이르(?:게|다|고|며)|"
    r"저렴(?:한|하게|하다)|싼|하위|최저|최소|오름차순|"
    r"ascending|bottom|lowest|smallest",
    re.IGNORECASE,
)
_SEMANTIC_FRAME_DESCENDING_CUE = re.compile(
    r"높(?:은|게|다|고|으며|아|을수록)|"
    r"큰|크(?:게|다|고|며)|클수록|"
    r"많(?:은|게|다|고|으며|아|을수록)|"
    r"긴|길(?:게|다|고|며|수록)|늦(?:은|게|다|고|으며|어|을수록)|"
    r"비싼|비싸(?:게|다)|활발(?:한|하게|하다)|상위|최고|최대|내림차순|"
    r"descending|top|highest|largest",
    re.IGNORECASE,
)
_SEMANTIC_FRAME_RANGE_CUE = re.compile(
    r"사이|범위|부터.{0,80}까지|"
    r"(?:이상|초과).{0,80}(?:이하|미만)|"
    r"(?:이하|미만).{0,80}(?:이상|초과)",
    re.IGNORECASE,
)
_SEMANTIC_FRAME_OPERATOR_CUES: dict[SemanticFrameOperator, re.Pattern[str]] = {
    SemanticFrameOperator.GTE: re.compile(r"이상|적어도|최소|≥|(?<![<>=])>=(?!=)", re.IGNORECASE),
    SemanticFrameOperator.GT: re.compile(
        r"초과|넘는|웃도는|보다\s*(?:높|크|많)|(?<![<>=])>(?!=)",
        re.IGNORECASE,
    ),
    SemanticFrameOperator.LTE: re.compile(
        r"이하|최대|많아야|넘지\s*않|≤|(?<![<>=])<=(?!=)",
        re.IGNORECASE,
    ),
    SemanticFrameOperator.LT: re.compile(
        r"미만|밑도는|보다\s*(?:낮|작|적)|(?<![<>=])<(?!=)",
        re.IGNORECASE,
    ),
    SemanticFrameOperator.EQ: re.compile(r"정확히|같은|동일|(?<![<>=])=(?!=)", re.IGNORECASE),
    SemanticFrameOperator.NEQ: re.compile(
        r"같지\s*않|아닌|제외|빼고|(?<![<>=])!=(?!=)", re.IGNORECASE
    ),
}
_SEMANTIC_FRAME_DERIVED_EXPRESSION = re.compile(
    r"(?:에서|보다).{0,80}?(?:뺀|빼서|차이|격차)|"
    r"(?:을|를).{0,80}?(?:으?로)\s*(?:나눈|나누어|나눠)|"
    r"대비.{0,40}?(?:비율|배수|차이|격차)|"
    r"(?:합산|더한|곱한)|(?:와|과|및).{0,80}?합계",
    re.IGNORECASE,
)


def _mask_semantic_frame_literal(surface: str, literal: str) -> str:
    """Whitespace-mask one grounded literal without shifting clause offsets."""

    if not literal:
        return surface
    start = surface.find(literal)
    if start < 0:
        return surface
    return f"{surface[:start]}{' ' * len(literal)}{surface[start + len(literal) :]}"


def _mask_semantic_frame_limit(surface: str, literal: str) -> str:
    """Mask a TOP-K span and its quantity-only 최소/최대 modifier."""

    if not literal:
        return surface
    start = surface.find(literal)
    if start < 0:
        return surface
    prefix = surface[:start]
    quantity_modifier = re.search(r"(?:최소|최대)\s*$", prefix)
    mask_start = quantity_modifier.start() if quantity_modifier is not None else start
    end = start + len(literal)
    return f"{surface[:mask_start]}{' ' * (end - mask_start)}{surface[end:]}"


class IntentExecutionClause(IntentAnalysisModel):
    """Bounded HCX SemanticFrame clause with no direct execution authority.

    HCX may select only one Registry field/capability and quote literal user
    spans.  Numbers, dates, identifiers and limits are *not* values supplied by
    the model: the server reparses the quoted spans and locks the resulting
    values before Compiler/SQL authority can be issued.
    """

    sequence: int = Field(ge=1, le=24)
    role: Literal[
        "scope",
        "filter",
        "shortlist",
        "ranking",
        "relation",
        "projection",
        "presentation",
    ]
    text_span: str = Field(min_length=1, max_length=500)
    product_families: list[ProductFamily] = Field(default_factory=list, max_length=4)
    semantic_status: SemanticFrameStatus = SemanticFrameStatus.NOT_APPLICABLE
    field_id: str = Field(default="__none__", min_length=2, max_length=127)
    operation: SemanticFrameOperation = SemanticFrameOperation.NONE
    operator: SemanticFrameOperator = SemanticFrameOperator.NONE
    direction: SemanticFrameDirection = SemanticFrameDirection.NONE
    field_span: str = Field(default="", max_length=160)
    value_span: str = Field(default="", max_length=160)
    limit_span: str = Field(default="", max_length=80)
    subject_span: str = Field(default="", max_length=160)
    knowledge_capability: str = Field(default="none", min_length=4, max_length=80)

    @model_validator(mode="after")
    def validate_clause(self) -> IntentExecutionClause:
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("clause product_families must be unique")
        if self.field_id != "__none__" and re.fullmatch(
            r"[a-z][a-z0-9_]{1,127}", self.field_id
        ) is None:
            raise ValueError("semantic frame field_id is not canonical")
        valid_capabilities = {"none", *(item.value for item in KnowledgeCapability)}
        if self.knowledge_capability not in valid_capabilities:
            raise ValueError("semantic frame knowledge capability is unknown")

        if self.semantic_status is not SemanticFrameStatus.RESOLVED:
            if any(
                (
                    self.field_id != "__none__",
                    self.operation is not SemanticFrameOperation.NONE,
                    self.operator is not SemanticFrameOperator.NONE,
                    self.direction is not SemanticFrameDirection.NONE,
                    bool(self.field_span),
                    bool(self.value_span),
                    bool(self.limit_span),
                    bool(self.subject_span),
                    self.knowledge_capability != "none",
                )
            ):
                raise ValueError("unresolved semantic frame cannot carry execution meaning")
            return self

        if self.operation is SemanticFrameOperation.EXTERNAL_RELATION:
            if (
                self.role != "relation"
                or self.field_id != "__none__"
                or self.knowledge_capability == "none"
                or not self.subject_span
                or self.field_span
                or self.value_span
                or self.limit_span
                or self.operator is not SemanticFrameOperator.NONE
                or self.direction is not SemanticFrameDirection.NONE
            ):
                raise ValueError("external relation frame is incomplete")
            return self

        if self.field_id == "__none__" or self.knowledge_capability != "none":
            raise ValueError("resolved field frame requires exactly one canonical field")
        if not self.field_span:
            raise ValueError("resolved field frame requires a literal field span")
        if self.operation is SemanticFrameOperation.FILTER:
            if self.role not in {"filter", "shortlist"}:
                raise ValueError("filter frame role differs")
            if self.operator is SemanticFrameOperator.NONE or not self.value_span:
                raise ValueError("filter frame requires operator and literal value span")
            if self.direction is not SemanticFrameDirection.NONE:
                raise ValueError("filter frame cannot carry ranking direction")
        elif self.operation is SemanticFrameOperation.RANK:
            if self.role not in {"ranking", "shortlist"}:
                raise ValueError("ranking frame role differs")
            if self.direction is SemanticFrameDirection.NONE:
                raise ValueError("ranking frame requires direction")
            if self.operator is not SemanticFrameOperator.NONE or self.value_span:
                raise ValueError("ranking frame cannot carry a filter value")
        elif self.operation is SemanticFrameOperation.PROJECT:
            if self.role != "projection":
                raise ValueError("projection frame role differs")
            if (
                self.operator is not SemanticFrameOperator.NONE
                or self.direction is not SemanticFrameDirection.NONE
                or self.value_span
            ):
                raise ValueError("projection frame cannot carry filter/ranking values")
        elif self.operation is SemanticFrameOperation.AGGREGATE:
            if self.role not in {"projection", "ranking"}:
                raise ValueError("aggregate frame role differs")
            if (
                self.operator is not SemanticFrameOperator.NONE
                or self.direction is not SemanticFrameDirection.NONE
                or self.value_span
            ):
                raise ValueError("aggregate frame cannot carry filter/ranking values")
        else:
            raise ValueError("resolved semantic frame requires an operation")
        return self


class IntentAnalysisDraft(IntentAnalysisModel):
    """Non-executable HCX interpretation with no field or SQL authority."""

    schema_version: Literal["1.0"] = "1.0"
    semantic_frame_version: Literal["2.0"] = "2.0"
    intent: InteractionIntent
    product_families: list[ProductFamily] = Field(max_length=4)
    evidence_spans: list[str] = Field(max_length=8)
    needs_clarification: bool
    knowledge_requirements: list[KnowledgeIntentRequirement] = Field(
        default_factory=list,
        max_length=8,
    )
    execution_clauses: list[IntentExecutionClause] = Field(
        default_factory=list,
        max_length=16,
    )

    @model_validator(mode="after")
    def validate_shape(self) -> IntentAnalysisDraft:
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("product_families must be unique")
        if len(self.evidence_spans) != len(set(self.evidence_spans)):
            raise ValueError("evidence_spans must be unique")
        knowledge_keys = [
            (item.capability, item.subject_span, item.relation_span)
            for item in self.knowledge_requirements
        ]
        if len(knowledge_keys) != len(set(knowledge_keys)):
            raise ValueError("knowledge requirements must be unique")
        if self.intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}:
            if not self.needs_clarification and self.intent is InteractionIntent.CLARIFY:
                raise ValueError("CLARIFY intent must acknowledge clarification")
        elif self.needs_clarification:
            raise ValueError("executable intent cannot request clarification")
        if self.needs_clarification and self.knowledge_requirements:
            raise ValueError("clarification intent cannot carry executable knowledge requirements")
        if (
            self.intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
            and self.execution_clauses
        ):
            raise ValueError("control intent cannot carry execution clauses")
        sequences = [item.sequence for item in self.execution_clauses]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("execution clause sequence must be unique and ordered")
        spans = [item.text_span for item in self.execution_clauses]
        if len(spans) != len(set(spans)):
            raise ValueError("execution clause spans must be unique")
        allowed_families = set(self.product_families)
        if any(
            not set(item.product_families).issubset(allowed_families)
            for item in self.execution_clauses
        ):
            raise ValueError("execution clause introduced an undeclared product family")
        return self


class IntentAnalysisProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft: ...


_KNOWLEDGE_CAPABILITY_CUES: dict[KnowledgeCapability, re.Pattern[str]] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"(?:편입(?:\s*(?:사실|이력|내역))?|"
        r"보유(?:한|하고|하는|종목)?|구성\s*종목|담(?:은|고\s*있는)|"
        r"담긴|넣(?:은|고\s*있는)|들어간|(?:들어|담겨)\s*있|"
        r"포함(?:된|한|하고|하는|되어)|구성\s*종목(?:으)?로\s*둔|"
        r"가진|가지(?:는|고\s*있는|고\s*있|고))",
        re.IGNORECASE,
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|모회사|관계사|가족사|그룹사|"
        r"(?:계열|관계|소속|산하)\s*(?:기업|회사|법인)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"섹터|테마|산업.{0,12}(?:관련|연결|연관|연계|접점|투자)|"
        r"(?:관련|연결|연관|연계|접점).{0,12}(?:섹터|테마|산업)|"
        r"(?:관련|연결|연관|연계|접점).{0,12}"
        r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드)|상품)|"
        r"노출\s*(?:된|되는|되어|한|하는|이력|기록|내역)|"
        r"관련\s*(?:된\s*)?(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP)|"
        r"관련\s*(?:투자\s*)?상품|"
        r"[0-9A-Za-z가-힣&·+._/-]{2,40}\s+(?:국내\s*|해외\s*)?"
        r"(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"(?:펀드.{0,20})?(?:구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
        r"(?:어떤\s*)?(?:방식|형태)(?:으)?로\s*(?:구성|조성|설계)"
        r"(?:된|되어\s*있는)|"
        r"자금\s*(?:조달|배분)\s*구조|투자\s*(?:전략|방침)|운용\s*전략|"
        r"(?:판매|모집|출시)(?:\s*[·ㆍ,/와과및]\s*(?:운용|투자))?"
        r"(?:\s*(?:동향|현황|일정|완료|종료|진행\s*(?:상황|현황)))|"
        r"(?:투자|운용)\s*전략\s*동향|운용\s*(?:동향|기조)|"
        r"운용\s*(?:방향|방침|계획|현황)|전략\s*동향|"
        r"(?:향후|앞으로)\s*(?:운용\s*)?(?:일정|계획)|"
        r"진행\s*(?:상황|현황))",
        re.IGNORECASE,
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험\s*요인|리스크\s*요인|어떤\s*위험|"
        r"투자\s*위험(?:을|은|이|도)?\s*(?:알려|설명|정리|확인)|"
        r"손실.{0,12}유의|주의\s*(?:사항|점)|유의\s*(?:사항|점)|"
        r"원금\s*손실\s*가능성",
        re.IGNORECASE,
    ),
}
_THEME_RELATION_SPAN_CUE = re.compile(
    r"(?:관련|연결|연관|연계|접점)(?:\s*(?:된|있는|이력|기록|내역))?",
    re.IGNORECASE,
)


def knowledge_capability_signal_matches(
    capability: KnowledgeCapability,
    text: str,
) -> bool:
    """Use one server-owned capability grammar at sanitize and compile boundaries."""

    if _KNOWLEDGE_CAPABILITY_CUES[capability].search(text) is not None:
        return True
    # HCX may ground only the relation phrase (``연결 이력이 있는``) while
    # the adjacent subject span owns ``우주항공 테마``.  Admit that bounded
    # split here; callers still require the complete question to carry the
    # stricter theme/sector/industry surface before retrieval is authorized.
    return (
        capability is KnowledgeCapability.SECTOR_THEME_HISTORY
        and _THEME_RELATION_SPAN_CUE.search(text) is not None
    )


def question_has_knowledge_capability_signal(
    capability: KnowledgeCapability,
    question: str,
    *,
    subject: str | None = None,
) -> bool:
    """Require the complete user question to carry the capability meaning."""

    if capability is KnowledgeCapability.SECTOR_THEME_HISTORY:
        if re.search(r"섹터|테마|산업|노출", question, re.IGNORECASE) is not None:
            return True
        if subject is not None and re.search(
            rf"{re.escape(subject)}\s+(?:국내\s*|해외\s*)?"
            r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))",
            question,
            re.IGNORECASE,
        ) is not None:
            # ``반도체 ETF의 위험요인`` is an explicit taxonomy-to-product
            # relation. Exact DB product names are screened separately before
            # this capability is admitted.
            return True
        if subject is None or re.search(
            r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))",
            subject,
            re.IGNORECASE,
        ) is not None:
            return False
        return (
            _THEME_RELATION_SPAN_CUE.search(question) is not None
            and re.search(
                r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드)|투자\s*상품)",
                question,
                re.IGNORECASE,
            )
            is not None
        )
    return _KNOWLEDGE_CAPABILITY_CUES[capability].search(question) is not None


def external_frame_supports_requirement(
    question: str,
    draft: IntentAnalysisDraft,
    requirement: KnowledgeIntentRequirement,
) -> bool:
    """Return whether one closed HCX relation frame grounds this requirement.

    This grants only permission to *attempt* official-source retrieval.  The
    subject and relation must be literal user spans and the capability must be
    one of the closed enum values already validated by ``IntentExecutionClause``.
    Product identity, evidence truth and the provided-DB intersection remain
    independent downstream server checks.
    """

    return any(
        clause.semantic_status is SemanticFrameStatus.RESOLVED
        and clause.operation is SemanticFrameOperation.EXTERNAL_RELATION
        and clause.knowledge_capability == requirement.capability.value
        and clause.subject_span == requirement.subject_span
        and clause.text_span == requirement.relation_span
        and _evidence_span_is_grounded(question, clause.subject_span)
        and _evidence_span_is_grounded(question, clause.text_span)
        for clause in draft.execution_clauses
    )

# The approved external corpus proves observed positive relationships.  It
# cannot prove that a relationship is absent from every relevant document.
# Keep this policy beside the capability grammar so every caller applies the
# same negation boundary before a positive requirement can be derived.
_NEGATED_EXTERNAL_RELATION = re.compile(
    r"(?:"
    r"(?:편입|보유|포함)\s*(?:하(?:고\s*있)?|되)?\s*지\s*않(?:은|는|았던|았던\s*|고)|"
    r"(?:담|넣)\s*지\s*않(?:은|는|았던|고)|"
    r"(?:관련|연관|연결|노출)(?:이|가|은|는)?\s*(?:없(?:는|었던|고)|되지\s*않(?:은|는|았던|고))|"
    r"(?:자회사|계열사|관계사|가족사|그룹사)(?:이|가|은|는)?\s*아닌"
    r")",
    re.IGNORECASE,
)
_EXTERNAL_CORPORATE_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)인\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=$|(?:이|가|을|를|은|는)?\s*(?:편입|보유|담|넣|포함|들어|ETF|ETN|ETP))",
    re.IGNORECASE,
)
_EXTERNAL_CORPORATE_SELECTION = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*(?:의\s*)?"
    r"(?:자회사|계열사|가족사)\s*(?:가운데|중(?:에서)?)\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=(?:이|가|을|를)?\s*(?:편입|보유|담|넣|포함|들어))",
    re.IGNORECASE,
)
_EXTERNAL_CORPORATE_PARENT = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?P<relation>(?:(?:의\s*)?(?:주요\s*)?"
    r"(?:자회사|계열사|가족사|그룹사|"
    r"(?:계열|관계)\s*(?:기업|회사|법인))|"
    r"(?:소속|산하)\s*(?:주요\s*)?"
    r"(?:자회사|계열사|가족사|그룹사|기업|회사|법인)))"
    r"(?=[을를이가은는\s])",
    re.IGNORECASE,
)
_EXTERNAL_HOLDING_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:이|가|을|를)?\s*(?P<relation>"
    # A quantity/weight adverb modifies the holdings edge, not its entity.
    # Keeping it inside the grounded relation span both preserves ``애플`` as
    # the subject in ``애플을 많이 보유한`` and lets the coverage gate account
    # for every meaningful token without granting the adverb entity authority.
    r"(?:(?:(?:비중(?:을|이)?\s*)?(?:(?:가장|제일)\s*)?"
    r"(?:많이|적게|크게|작게)|(?:높은|낮은|큰|작은)\s*비중(?:으)?로)\s*)?"
    r"(?:공통\s*(?:으로\s*)?)?"
    r"(?:편입(?:(?:\s*(?:사실|이력|내역))?"
    r"(?:이|가)?\s*확인(?:되는|된)|된|한|되어|하고)?"
    r"(?!\s*(?:표|내역|종목|비중|기준일|날짜|시점))|"
    r"보유(?!\s*(?:내역|표|종목))(?:가\s*확인되는|된|한|하고|하는|했는지)?|"
    r"담(?:은|긴|고\s*있는)|넣(?:은|고\s*있는)|"
    r"포함(?:된|한|하고|하는|되어)|"
    r"구성\s*종목(?:에|안에)\s*(?:들어|담겨)\s*있(?:는|고)?|"
    r"(?:들어|담겨)\s*있(?:는|고)?|들어간|구성\s*종목(?:으)?로\s*둔|"
    r"가진|가지(?:는|고\s*있는|고\s*있|고)))"
    r"(?!\s*(?:내역|표|종목|비중|기준일|날짜|시점))",
    re.IGNORECASE,
)
_EXTERNAL_HOLDING_VIA_PROFILE = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:이|가|을|를)?\s*"
    r"(?P<relation>(?:실제\s*)?(?:구성\s*종목|포트폴리오)(?:으)?로\s*"
    r"(?:담(?:은|고\s*있는)|넣(?:은|고\s*있는)|두(?:고|는|었고)|"
    r"포함(?:한|하고)))",
    re.IGNORECASE,
)
_EXTERNAL_HOLDING_ACTION = re.compile(
    r"편입(?:(?:\s*(?:사실|이력|내역))?(?:이|가)?\s*확인(?:되는|된)|"
    r"된|한|되어|하고)?(?!\s*(?:표|내역|종목|비중|기준일|날짜|시점))|"
    r"보유(?!\s*(?:내역|표|종목))(?:가\s*확인되는|된|한|하고|하는|했는지)?|"
    r"담(?:은|긴|고\s*있는)|넣(?:은|고\s*있는)|"
    r"포함(?:된|한|하고|하는|되어)|"
    r"구성\s*종목(?:에|안에)\s*(?:들어|담겨)\s*있(?:는|고)?|"
    r"(?:들어|담겨)\s*있(?:는|고)?|들어간|구성\s*종목(?:으)?로\s*둔|"
    r"가진|가지(?:는|고\s*있는|고\s*있|고)",
    re.IGNORECASE,
)
_HOLDING_LIST_BOUNDARY = re.compile(
    r"(?:공식\s*)?(?:상위\s*\d{1,3}\s*개\s*)?"
    r"(?:편입표|편입\s*내역|편입\s*종목|구성\s*종목|종목|표|내역)"
    r"(?:\s*(?:자료|문서))?"
    r"\s*(?:에서|중|(?:안)?에|(?:을|를)\s*(?:모두|전부)?\s*"
    r"(?:기준으로|보면|대조(?:해서|하여|하고|한\s*뒤|후|해)|"
    r"확인(?:해서|하여|하고|한\s*뒤|하면|후|해)))\s*",
    re.IGNORECASE,
)
_EXTERNAL_CLASSIFIED_THEME = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,40}"
    r"(?:\s+[0-9A-Za-z가-힣&·+._/-]{2,40}){0,5}\s+"
    r"(?:섹터|테마|산업))"
    r"(?P<relation>\s*(?:으)?로\s*"
    r"(?:소개|분류|언급|명시|표현|설명)"
    r"(?:된|되는|되어|되었(?:던|고)?|한|하는|했던))",
    re.IGNORECASE,
)
_EXTERNAL_THEME_SUBJECT = re.compile(
    # Sentence punctuation is never part of a taxonomy entity.  Excluding it
    # prevents ``찾아주세요. 테마 연결 근거`` from becoming a second theme.
    r"(?P<subject>[0-9A-Za-z가-힣&·+_/-]{2,40}\s*(?:섹터|테마|산업))",
    re.IGNORECASE,
)
_EXTERNAL_THEME_LINK_TAIL = re.compile(
    r"\s*(?:과|와|에|와의)?\s*"
    r"(?:관련|연결|연관|연계|접점|노출)(?:\s*(?:성|이력|기록))?(?:이|가)?\s*"
    r"(?:있는|확인되는|확인된|된|되어|있고|있으며)?",
    re.IGNORECASE,
)
_EXTERNAL_RELATED_ETP_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,60}?)\s*(?:과|와)?\s*"
    r"(?P<relation>(?:관련|연관|연결|연계|접점)\s*(?:(?:이력|기록|성)(?:이|가)?\s*)?"
    r"(?:된|있는|확인된)?\s*(?:국내\s*|해외\s*)?"
    r"(?:상장\s*(?:ETF|ETN|ETP|지수\s*(?:상품|펀드))|ETF|ETN|ETP))",
    re.IGNORECASE,
)
_EXTERNAL_RELATED_PRODUCT_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,60})\s*"
    r"(?P<relation>관련\s*(?:투자\s*)?상품)",
    re.IGNORECASE,
)
_EXTERNAL_NAMED_ETP_SUBJECT = re.compile(
    r"(?P<subject>[A-Za-z][A-Za-z0-9._-]{1,40}"
    r"(?:\s+[A-Za-z][A-Za-z0-9._-]{1,40}){0,3})\s+"
    r"(?P<relation>(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP))",
    re.IGNORECASE,
)
_GENERIC_ASCII_ETP_SUBJECTS = frozenset(
    {"ai", "esg", "global", "korea", "us", "usa", "usd", "world"}
)
_EXTERNAL_FUND_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]+(?:\s+[0-9A-Za-z가-힣&·+._/-]+){0,4}"
    r"\s*펀드)(?:의|은|는|이|가)?\s*(?:어떤\s*)?"
    r"(?P<relation>구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
    r"(?:방식|형태)(?:으)?로\s*(?:구성|조성|설계)(?:된|되어\s*있는)|"
    r"자금\s*(?:조달|배분)\s*구조|(?:투자|운용)\s*전략\s*동향|"
    r"투자\s*(?:전략|방침)|운용\s*전략|운용\s*기조|"
    r"(?:판매|모집|출시)(?:\s*[·ㆍ,/와과및]\s*(?:운용|투자))?"
    r"(?:\s*(?:동향|현황|일정|완료|종료|진행\s*(?:상황|현황)))(?:\s*(?:후|뒤))?|"
    r"운용\s*(?:동향|방향|방침|계획|현황)|전략\s*동향|"
    r"(?:향후|앞으로)\s*(?:운용\s*)?(?:일정|계획)|"
    r"진행\s*(?:상황|현황))",
    re.IGNORECASE,
)
_EXTERNAL_FUND_RELATION = re.compile(
    r"구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
    r"(?:어떤\s*)?(?:방식|형태)(?:으)?로\s*(?:구성|조성|설계)"
    r"(?:된|되어\s*있는)|"
    r"자금\s*(?:조달|배분)\s*구조|(?:투자|운용)\s*전략\s*동향|"
    r"투자\s*(?:전략|방침)|운용\s*전략|운용\s*기조|"
    r"(?:판매|모집|출시)(?:\s*[·ㆍ,/와과및]\s*(?:운용|투자))?"
    r"(?:\s*(?:동향|현황|일정|완료|종료|진행\s*(?:상황|현황)))(?:\s*(?:후|뒤))?|"
    r"운용\s*(?:동향|방향|방침|계획|현황)|전략\s*동향|"
    r"(?:향후|앞으로)\s*(?:운용\s*)?(?:일정|계획)|"
    r"진행\s*(?:상황|현황)",
    re.IGNORECASE,
)
_EXTERNAL_FUND_PREPOSED = re.compile(
    r"(?P<relation>(?:판매|모집|출시)(?:\s*[·ㆍ,/와과및]\s*(?:운용|투자))?"
    r"\s*(?:동향|현황|일정)|(?:투자|운용)\s*전략\s*동향|운용\s*동향)"
    r"(?:이|가)?\s*(?:발표된|확인된|공개된|있는|나온)?\s*"
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,60}\s*(?:공모\s*)?펀드)",
    re.IGNORECASE,
)
_EXTERNAL_TAXONOMY_ETP_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,40})\s*"
    r"(?P<relation>(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP))",
    re.IGNORECASE,
)
_EXTERNAL_RISK_RELATION = re.compile(
    r"핵심\s*위험\s*요인|위험\s*요인|리스크\s*요인|"
    r"투자\s*위험(?:을|은|이|도)?\s*(?:알려|설명|정리|확인)|"
    r"손실.{0,12}유의(?:점|사항)?|주의\s*(?:사항|점)|"
    r"유의\s*(?:사항|점)|원금\s*손실\s*가능성",
    re.IGNORECASE,
)
_EXPLICIT_BOND_ISSUER_LINK = (
    r"{owner}\s*(?:이|가)?\s*(?:직접\s*)?발행(?:한|하는|했(?:던|고)?)\s*"
    r"(?:원화|국내)?\s*채권|"
    r"(?:발행\s*(?:기관|사|자))(?:가|는|은|이)?\s*[:：]?\s*{owner}"
    r"(?:인|으로|로|인\s*경우|인\s*채권)?"
)


def _source_owner_is_explicit_bond_issuer(question: str, owner: str) -> bool:
    """Require a literal DB-confirmed owner to be linked to the bond scope.

    A company name merely occurring beside ``위험요인`` is insufficient.  The
    user must explicitly say that the company issued the bond, so an asset
    manager or a constituent company cannot accidentally acquire issuer-risk
    authority.
    """

    if not owner or owner not in question:
        return False
    return (
        re.search(
            _EXPLICIT_BOND_ISSUER_LINK.format(owner=re.escape(owner)),
            question,
            re.IGNORECASE,
        )
        is not None
    )
_DEPENDENT_RISK_PRODUCT_SCOPE = re.compile(
    r"국내\s*(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))|"
    r"해외\s*(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))|"
    r"(?:공모\s*)?펀드|(?:원화|국내)\s*채권|"
    r"(?<![0-9A-Za-z가-힣])(?:ETF|ETN|ETP|상품)"
    r"(?=(?:의|을|를|은|는|이|가|에서|중|\s|$))",
    re.IGNORECASE,
)
_DEPENDENT_RISK_RESULT_SCOPE = re.compile(
    r"(?:최종\s*)?후보(?:들)?(?:별)?(?:로)?|"
    r"(?:이들|그들|전부|모두|양쪽|둘)(?:의|이|가|을|를|은|는|별로)?",
    re.IGNORECASE,
)
_BRANDED_PRODUCT = re.compile(
    r"(?:KODEX|TIGER|ACE|SOL|RISE|PLUS|KIWOOM|WON|TIME)\s+"
    r"[0-9A-Za-z가-힣&+._/-]{2,80}",
    re.IGNORECASE,
)
_PRODUCT_LIKE_EXTERNAL_SUBJECT = re.compile(
    # Product brands must be token-like.  A raw substring check turns ordinary
    # company names such as ``Skyworks Solutions`` into product identities
    # because ``Sol`` happens to occur inside ``Solutions``.  Latin letters on
    # either side therefore close the match, while concatenated Korean labels
    # and numeric product names (for example KODEX200) remain supported.
    r"(?<![A-Za-z])(?:ETF|ETN|KODEX|TIGER|ACE|SOL|RISE|PLUS|KIWOOM|WON|TIME)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_PORTFOLIO_ELIGIBILITY = re.compile(r"편입(?:이|가)?\s*가능", re.IGNORECASE)
_GENERIC_ETP_SURFACE = re.compile(
    r"(?<![A-Za-z])(?:ETF|ETN|ETP)(?![A-Za-z])|상장\s*지수\s*(?:상품|펀드)",
    re.IGNORECASE,
)
_NON_ENTITY_HOLDING_SUBJECTS = frozenset(
    {
        "공식",
        "자료",
        "내역",
        "공식자료",
        "공식내역",
        "자회사",
        "계열사",
        "가족사",
        "그룹사",
        "계열기업",
        "계열회사",
        "계열법인",
        "관계기업",
        "관계회사",
        "관계법인",
        "소속기업",
        "소속회사",
        "소속법인",
        "기업",
        "회사",
        "법인",
        "상품",
        "최근",
        "최근에",
        "요즘",
        "현재",
        "새로",
        "많이",
        "적게",
        "크게",
        "작게",
        "가장많이",
        "제일많이",
        "있는",
        "해당",
        "관련",
        "모두",
        "함께",
        "공통",
        "공통으로",
        "하나",
        "하나이상",
        "이상",
        "구성종목에",
        "구성종목안에",
        "구성종목으로",
        "포트폴리오에",
        "포트폴리오안에",
        "값",
        "수치",
        "정보",
        "조건",
        "데이터",
        "내용",
        "항목",
        "필드",
        "컬럼",
        "비용",
        "수수료",
        "보수",
        "혜택",
        "연관된",
        "관련된",
        "연결된",
        "노출된",
        "산업",
        "담은",
        "담긴",
        "보유한",
        "편입한",
        "넣은",
        "포함된",
        "가진",
    }
)
_RELATIONAL_MODIFIER_ONLY = re.compile(
    r"^(?:(?:관련|연관|연결|노출)(?:된|한|되는|되어|있는)?|"
    r"(?:소개|분류|언급|명시|표현|설명)(?:된|한|되는|되어|하는)?|"
    r"(?:담|보유|편입|포함|넣)(?:은|긴|한|된|하는|하고\s*있는)|"
    r"들어(?:간|가\s*있는)|가진)$",
    re.IGNORECASE,
)
_HOLDING_PROJECTION_ONLY = re.compile(
    r"^(?:편입|보유|구성\s*종목)\s*(?:비중|기준일|날짜|시점|순위|정보)$",
    re.IGNORECASE,
)
_GENERIC_HOLDING_REFERENCE = re.compile(
    r"^(?:(?:이|그|해당|각|두|세|여러|위|아래|앞선|전술한)\s*)?"
    r"(?:종목|기업|회사|상품|구성\s*종목)(?:들)?(?:의|이|가|을|를|은|는)?$|"
    r"^(?:이들|그들|전부|모두|양쪽|둘)(?:의|이|가|을|를|은|는)?$",
    re.IGNORECASE,
)
_HOLDING_SET_QUANTIFIER_SUBJECT = re.compile(
    r"(?:(?:중|가운데)\s*)?(?:모두|전부|함께|동시에|둘\s*다|"
    r"적어도\s*하나|최소\s*하나|하나(?:\s*이상|라도)?|어느\s*하나(?:라도)?)",
    re.IGNORECASE,
)
_HOLDING_RANK_FRAGMENT_SUBJECT = re.compile(
    r"^(?:(?:상위|하위|TOP|톱)\s*)?\d{1,3}\s*(?:개|종목)?(?:\s*(?:안)?에)?$",
    re.IGNORECASE,
)
_NON_HOLDING_LIST_CLAUSE = re.compile(
    r"(?:최근\s*\d|동안|이전|이후|"
    r"(?:테마|섹터|산업)(?=\s|와|과|을|를|이|가|은|는|$)|"
    r"(?:연결|연관|관련)(?:\s*(?:이력|기록|성))?|이력|동향|"
    r"위험\s*요인|리스크\s*요인|구조|전략|판매|모집|일정|상황)",
    re.IGNORECASE,
)
_RANK_POSITION_PREFIX = re.compile(
    r"(?:상위|하위|TOP)\s*\d{1,3}\s*(?:개|종목)?(?:\s*안)?에\s*$",
    re.IGNORECASE,
)
_UNVERIFIED_HOLDING_RANK_POSITION = re.compile(
    r"(?:상위|하위|TOP)\s*\d{1,3}\s*(?:개|종목)?(?:\s*안)?에\s*"
    r"(?:들어|포함|편입|자리|위치)",
    re.IGNORECASE,
)
_HOLDING_DOCUMENT_TOP_N = re.compile(
    r"(?P<position>(?:상위|TOP|톱)\s*(?P<position_n>\d{1,2})\s*"
    r"(?:개|종목)?(?:\s*(?:안)?에)?)"
    r"(?=\s*(?:들어|포함|편입|자리|위치))|"
    r"(?P<membership>(?:상위|TOP|톱)\s*(?P<membership_n>\d{1,2})\s*"
    r"(?:개|종목)?(?:\s*(?:안)?에)?)"
    r"(?=[^.!?\n]{1,180}?(?:모두|전부|둘\s*다|두\s*종목(?:이|가)?)\s*"
    r"(?:있는지|존재(?:하는지|하는가|여부)|들어\s*있는지|포함(?:됐는지|되었는지|여부)))|"
    r"(?P<before>(?:상위|TOP|톱)\s*(?P<before_n>\d{1,2})\s*"
    r"(?:개|종목)?)\s*(?:편입|구성|보유)\s*(?:표|내역|종목)|"
    r"(?:편입|구성|보유)\s*(?:표|내역|종목)(?:에서|의)?\s*"
    r"(?P<after>(?:상위|TOP|톱)\s*(?P<after_n>\d{1,2})\s*"
    r"(?:개|종목)?)|"
    r"(?P<checked>(?:상위|TOP|톱)\s*(?P<checked_n>\d{1,2})\s*"
    r"(?:개\s*)?종목)(?:을|를)\s*(?:모두|전부)?\s*(?:확인|대조)",
    re.IGNORECASE,
)
_HOLDING_DOCUMENT_TOP_N_TABLE = re.compile(
    r"(?P<table>(?:상위|TOP|톱)\s*(?P<table_n>\d{1,2})\s*"
    r"(?:개|종목)?\s*(?:편입|구성|보유)?\s*표)|"
    r"(?P<intersection>(?:상위|TOP|톱)\s*(?P<intersection_n>\d{1,2})\s*"
    r"(?:개|종목)?)(?=\s*에서\s*(?:서로\s*)?(?:공통|겹치는|중복)\s*종목)|"
    r"(?P<bounded>(?:상위|TOP|톱)\s*(?P<bounded_n>\d{1,2})\s*"
    r"(?:개\s*)?종목)(?=\s*(?:중(?:에서)?|(?:을|를)\s*"
    r"(?:모두|전부)?\s*(?:보면|확인|대조)))",
    re.IGNORECASE,
)
_HOLDING_DOCUMENT_CONTEXT = re.compile(
    r"팩트\s*시트|가이드(?:\s*북)?|공식\s*(?:자료|문서|공시)|"
    r"편입\s*(?:표|내역|종목)|구성\s*(?:표|내역|종목)|"
    r"보유\s*(?:표|내역|종목)|포트폴리오|holdings?",
    re.IGNORECASE,
)
_HOLDING_DOCUMENT_NAMED_MEMBERSHIP_TAIL = re.compile(
    r"\s*(?P<entities>[0-9A-Za-z가-힣&·+._/',， -]{2,100}?)\s*"
    r"(?:(?:두|세|여러)\s*(?:종목|기업)(?:이|가|을|를)?\s*)?"
    r"(?:모두|전부|둘\s*다|함께|동시에)\s*"
    r"(?P<decision>있는지|존재(?:하는지|하는가|여부)|"
    r"들어\s*있는지|포함(?:됐는지|되었는지|여부))",
    re.IGNORECASE,
)
_GENERIC_EXTERNAL_SUBJECTS = frozenset(
    {
        "상품",
        "종목",
        "채권",
        "펀드",
        "etf",
        "etn",
        "etp",
        "국내etf",
        "해외etf",
        "운용전략",
        "투자전략",
        "위험등급",
        "테마",
        "섹터",
        "산업",
    }
)


def _entity_surface_key(value: str) -> str:
    surface = value.strip().casefold()
    if len(surface) >= 3 and surface[-1:] in {"은", "는", "이", "가", "을", "를", "와", "과"}:
        return surface[:-1]
    return surface


_HOLDING_DISCOURSE_PREFIX = re.compile(
    r"^(?:(?:그|이)\s*(?:중|가운데)(?:에서|서)?)[,，:]?\s*",
    re.IGNORECASE,
)
_HOLDING_MEMBERSHIP_SUBJECT_TAIL = re.compile(
    r"\s*(?:종목(?:이|가)?\s*)?(?:상위|TOP|톱)\s*\d{1,3}\s*"
    r"(?:개|종목)?(?:\s*(?:안)?에)?\s*$",
    re.IGNORECASE,
)


def _normalize_holding_subject(value: str) -> str:
    """Remove only a bounded discourse prefix from a constituent identity.

    ``그중 삼성전자`` points back to an already described document or
    shortlist; ``그중`` is not part of the company name.  Prefix matching is
    anchored and token based so an identity such as ``중국전력`` is preserved.
    The returned subject remains a literal substring of the user question.
    """

    surface = value.strip(" ‘’'\"()[],:;.")
    normalized = _HOLDING_DISCOURSE_PREFIX.sub("", surface, count=1).strip()
    normalized = _HOLDING_MEMBERSHIP_SUBJECT_TAIL.sub("", normalized, count=1).strip()
    return normalized if len(_literal_evidence_key(normalized)) >= 2 else surface


def _holding_subject_is_generic(value: str) -> bool:
    """Reject pronouns and projection nouns that carry no constituent identity."""

    surface = value.strip(" ‘’'\"()[],:;.")
    normalized = surface.replace(" ", "").casefold()
    return (
        not normalized
        or normalized in _NON_ENTITY_HOLDING_SUBJECTS
        or normalized in _GENERIC_EXTERNAL_SUBJECTS
        or _GENERIC_HOLDING_REFERENCE.fullmatch(surface) is not None
        or _HOLDING_SET_QUANTIFIER_SUBJECT.fullmatch(surface) is not None
        or _HOLDING_RANK_FRAGMENT_SUBJECT.fullmatch(surface) is not None
    )


def _classified_theme_parts(
    question: str,
    match: re.Match[str],
) -> tuple[str, str, tuple[int, int]]:
    """Keep the literal taxonomy after a document/time/publisher prefix.

    The extraction is syntax based.  For example, ``공식 자료에서 AI 반도체
    테마로 소개된`` binds ``AI 반도체 테마`` rather than turning the
    document lead-in into the entity.  The returned evidence span is still an
    exact substring of the user's question.
    """

    raw_subject = match.group("subject")
    offset = 0
    for boundary in re.finditer(
        r"(?:^|\s)[^\s]{1,40}(?:에서|까지|부터)\s+",
        raw_subject,
        re.IGNORECASE,
    ):
        offset = boundary.end()
    subject = raw_subject[offset:].strip()
    publisher_prefix = re.match(
        r"(?:(?:자산\s*)?운용사|발행사)(?:가|의|에서)?\s+",
        subject,
        re.IGNORECASE,
    )
    if publisher_prefix is not None:
        subject = subject[publisher_prefix.end() :].strip()
    subject_offset = raw_subject.rfind(subject)
    start = match.start("subject") + max(0, subject_offset)
    return subject, question[start : match.end()], (start, match.end())


_CORPORATE_DESCENDANT_CUE = re.compile(
    r"자회사|계열사|가족사|그룹사|"
    r"(?:계열|관계|소속|산하)\s*(?:기업|회사|법인)",
    re.IGNORECASE,
)


def _remove_redundant_corporate_holding_bridges(
    requirements: list[KnowledgeIntentRequirement],
    *,
    corporate_parent: str | None,
) -> list[KnowledgeIntentRequirement]:
    """Keep one server-owned holding edge for a corporate descendant chain.

    In a question such as ``A사의 자회사를 편입한 ETF``, the deterministic
    extractor binds the holding edge to ``A사`` and the composite compiler
    makes that edge depend on the verified subsidiary relation.  HCX may also
    restate the same edge with the longer subject ``A사의 자회사``.  Executing
    both descriptions does not add evidence; it spends the bounded search
    budget twice and can prevent later product-risk verification.

    Prefer the deterministic direct-parent edge only when both descriptions
    are present.  Other named holding subjects remain untouched, so a real
    conjunction such as ``A사의 자회사와 B사를 편입`` still requires both
    independently verified relations.
    """

    if corporate_parent is None:
        return requirements
    parent_key = _literal_evidence_key(corporate_parent)
    direct_parent_key = _entity_surface_key(corporate_parent)
    has_direct_parent_holding = any(
        item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        and _entity_surface_key(item.subject_span) == direct_parent_key
        for item in requirements
    )
    if not has_direct_parent_holding:
        return requirements

    return [
        item
        for item in requirements
        if not (
            item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and _entity_surface_key(item.subject_span) != direct_parent_key
            and parent_key in _literal_evidence_key(item.subject_span)
            and _CORPORATE_DESCENDANT_CUE.search(item.subject_span) is not None
        )
    ]


def _literal_evidence_key(value: str) -> str:
    """Normalize presentation-only Unicode while preserving literal token order."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _strip_trailing_case_particle(value: str) -> str:
    """Remove only a phonologically valid attached Korean case particle.

    A generic ``str.strip('이가을를')`` corrupts legitimate company names
    such as ``비에이치아이``.  The preceding Hangul syllable determines
    whether 이/을 (jongseong present) or 가/를 (jongseong absent) is a
    plausible attached particle.  Latin identifiers accept either convention.
    """

    match = re.fullmatch(r"(.+?)(이|가|을|를)", value)
    if match is None:
        return value
    stem, particle = match.groups()
    last = stem[-1]
    if last.isascii() and last.isalnum():
        return stem
    codepoint = ord(last)
    if not 0xAC00 <= codepoint <= 0xD7A3:
        return value
    has_jongseong = (codepoint - 0xAC00) % 28 != 0
    expected = {"이", "을"} if has_jongseong else {"가", "를"}
    return stem if particle in expected else value


def holding_document_top_n_matches(
    question: str,
) -> tuple[tuple[int, int, int, str], ...]:
    """Return literal document-internal holdings ranks with source offsets.

    A product result limit (for example, ``AUM 상위 3개``) is intentionally
    excluded.  The accepted forms either bind the rank to a holdings table or
    use it as the input to an explicit set intersection.  All detected limits
    must agree; conflicting document scopes stay unresolved and therefore
    cannot authorize retrieval.
    """

    # Local import keeps this low-level intent contract free from module-load
    # cycles while reusing the same Field Registry authority as QueryPlan.
    from finance_agent_core.agent.field_language import registry_field_mentions

    found: list[tuple[int, int, int, str]] = []
    for pattern, names in (
        (
            _HOLDING_DOCUMENT_TOP_N,
            ("position", "membership", "before", "after", "checked"),
        ),
        (_HOLDING_DOCUMENT_TOP_N_TABLE, ("table", "intersection", "bounded")),
    ):
        for match in pattern.finditer(question):
            for name in names:
                span = match.group(name)
                if span is None:
                    continue
                local_context = question[
                    max(0, match.start(name) - 180) : match.end(name) + 80
                ]
                direct_holdings_table = re.search(
                    r"(?:편입|구성|보유)\s*표", span, re.IGNORECASE
                )
                prefix = question[max(0, match.start(name) - 72) : match.start(name)]
                rank_has_product_metric = any(
                    re.fullmatch(
                        r"\s*(?:(?:이|가|은|는|의)\s*)?",
                        prefix[mention.end :],
                    )
                    is not None
                    for family in ProductFamily
                    for mention in registry_field_mentions(
                        prefix,
                        family.value,
                        "sortable",
                    )
                )
                if name in {
                    "position",
                    "membership",
                    "table",
                    "intersection",
                    "bounded",
                } and (
                    direct_holdings_table is None
                    and (
                        rank_has_product_metric
                        or _HOLDING_DOCUMENT_CONTEXT.search(local_context) is None
                    )
                ):
                    # A product result such as ``AUM 상위 10개 표`` must not
                    # acquire document-evidence authority merely because it
                    # shares the word ``상위`` with a holdings table.
                    continue
                limit = int(match.group(f"{name}_n"))
                found.append((match.start(name), match.end(name), limit, span.strip()))
                break
    if not found or len({item[2] for item in found}) != 1:
        return ()
    # Alternatives can recognize a short prefix and its wider table phrase at
    # the same position.  Preserve only the widest literal proof while keeping
    # a separate later membership clause (``상위 N개에 들어 있는``).
    canonical: list[tuple[int, int, int, str]] = []
    for item in sorted(set(found), key=lambda value: (value[0], -value[1], value[3])):
        if any(
            existing[0] <= item[0]
            and item[1] <= existing[1]
            and existing[2] == item[2]
            for existing in canonical
        ):
            continue
        canonical.append(item)
    return tuple(sorted(canonical, key=lambda item: (item[0], item[1], item[3])))


def holding_document_top_n_scope(question: str) -> tuple[int, str] | None:
    """Return one literal, unambiguous holdings-table Top-N scope.

    This parser distinguishes a document-internal constituent rank from the
    number of products returned by the Agent.  Conflicting document limits are
    left unresolved, so callers remain fail-closed instead of choosing one.
    """

    found = holding_document_top_n_matches(question)
    if not found:
        return None
    _start, _end, limit, span = found[0]
    return limit, span


def _listed_holding_entities(question: str) -> tuple[tuple[str, str], ...]:
    """Extract enumerated holding entities immediately governed by one verb.

    The extractor is syntax based: it does not know any company or product
    name.  It deliberately requires either a document-table boundary or at
    least two explicitly separated entities, so ordinary prose cannot open an
    external holdings search accidentally.
    """

    found: list[tuple[int, str, str]] = []
    for action in _EXTERNAL_HOLDING_ACTION.finditer(question):
        prefix_start = max(0, action.start() - 220)
        prefix = question[prefix_start : action.start()]
        if (
            _RANK_POSITION_PREFIX.search(prefix)
            and holding_document_top_n_scope(question) is None
        ):
            # A holdings relation proves membership, not its row position.
            # Until the evidence contract carries and verifies a constituent
            # rank, do not silently weaken ``top 10`` into ``held anywhere``.
            continue
        boundaries = list(_HOLDING_LIST_BOUNDARY.finditer(prefix))
        bounded = bool(boundaries)
        clause_boundaries = list(
            re.finditer(
                r"(?<![A-Za-z])(?:ETF|ETN|ETP|상품)(?:와|과|,|\s+및)\s*",
                prefix,
                re.IGNORECASE,
            )
        )
        # A nearer product-clause boundary supersedes an older table/list
        # boundary.  Otherwise entities from the first clause leak into a
        # second independent relation.
        if clause_boundaries and (
            not boundaries or clause_boundaries[-1].end() > boundaries[-1].end()
        ):
            surface = prefix[clause_boundaries[-1].end() :]
            bounded = False
        if bounded:
            surface = prefix[boundaries[-1].end() :]
        elif not clause_boundaries:
            # Without an official-table boundary, require an explicit list
            # conjunction close to the holding action.
            surface = prefix[-140:]
            # A previous product clause must not leak its entity list into the
            # next holding verb (``A와 B가 포함된 ETF와 C가 들어간 ETF``).
            # This is a syntax boundary only; it does not contain any product
            # or company vocabulary.
            local_clause_boundaries = list(
                re.finditer(
                    r"(?<![A-Za-z])(?:ETF|ETN|ETP|상품)(?:와|과|,|\s+및)\s*",
                    surface,
                    re.IGNORECASE,
                )
            )
            if local_clause_boundaries:
                surface = surface[local_clause_boundaries[-1].end() :]
        # A verified document-internal rank belongs to the evidence scope,
        # not to the constituent name.  Remove it before splitting the named
        # entities while keeping the literal scope for the typed planner.
        if holding_document_top_n_scope(question) is not None:
            surface = _RANK_POSITION_PREFIX.sub("", surface).strip()
            surface = re.sub(
                r"\s*종목(?:이|가|을|를|은|는)?\s*$",
                "",
                surface,
                flags=re.IGNORECASE,
            )
        # Korean speakers commonly stack list/count modifiers (``두 종목을
        # 모두 함께``).  Peel the bounded suffix to a fixed point rather than
        # assuming one modifier of each kind in one fixed order; otherwise the
        # last modifier becomes part of the second company name.
        for _ in range(4):
            before = surface
            surface = re.sub(
                r"\s*(?:구성\s*종목(?:으)?로|포트폴리오(?:에|안에)|"
                r"편입\s*종목(?:으)?로)\s*$",
                "",
                surface,
            )
            surface = re.sub(
                r"\s*(?:(?:중|가운데)\s*)?(?:모두|전부|함께|동시에|둘\s*다|"
                r"적어도\s*하나|최소\s*하나|하나(?:\s*이상|라도)?|"
                r"어느\s*하나(?:라도)?)(?:를|을|이|가)?\s*$",
                "",
                surface,
            )
            surface = re.sub(
                r"\s*(?:두|세|여러)\s*(?:종목|기업)(?:을|를|이|가)?\s*$",
                "",
                surface,
            )
            if surface == before:
                break
        # Remove a product-profile/date prefix if it survived the boundary.
        if "에서" in surface:
            surface = surface.rsplit("에서", 1)[-1]
        parts = [
            value.strip(" ‘’'\"()[]")
            for value in re.split(
                r"\s*,\s*|\s+(?:또는|혹은)\s+|\s+및\s+|\s*[·ㆍ]\s*|"
                r"(?<=[0-9A-Za-z가-힣])(?:와|과)(?=[0-9A-Za-z가-힣])|"
                r"(?<=[0-9A-Za-z가-힣])(?:와|과|이나|(?<!하)나)\s+",
                surface,
            )
        ]
        cleaned_parts: list[str] = []
        for index, value in enumerate(parts):
            value = re.sub(
                r"\s*(?:중|가운데)\s*(?:(?:적어도|최소)\s*)?"
                r"(?:어느\s*)?(?:하나|한\s*개)(?:\s*이상|라도)?\s*$",
                "",
                value,
            ).strip(" ‘’'\"()[]")
            if index == len(parts) - 1:
                # The final list item sits immediately before the governing
                # action and therefore may retain one Korean case particle.
                # Earlier items are separated by explicit conjunctions and
                # are left byte-for-byte intact.
                value = _strip_trailing_case_particle(value)
            cleaned_parts.append(value)
        parts = [
            value
            for value in cleaned_parts
            if 2 <= len(_literal_evidence_key(value)) <= 80
            and _entity_surface_key(value) not in _NON_ENTITY_HOLDING_SUBJECTS
            and _PRODUCT_LIKE_EXTERNAL_SUBJECT.search(value) is None
            and _NON_HOLDING_LIST_CLAUSE.search(value) is None
        ]
        if len(parts) < 2 and not bounded:
            continue
        # A bounded table phrase may contain just one requested constituent.
        for value in parts:
            position = question.rfind(value, prefix_start, action.start())
            if position >= 0:
                found.append((position, value, action.group(0)))
    unique: dict[tuple[str, str], tuple[int, str, str]] = {}
    for item in found:
        unique.setdefault((_entity_surface_key(item[1]), item[2].casefold()), item)
    return tuple((item[1], item[2]) for item in sorted(unique.values()))


def _listed_document_membership_entities(
    question: str,
) -> tuple[tuple[str, str], ...]:
    """Recover named constituents from a bounded official-document membership check.

    Some natural questions state ``the document's top 10 contains A and B``
    without repeating a holdings verb.  The rank parser already proves that
    ``top 10`` belongs to an official holdings document rather than a DB
    product ranking.  This extractor reuses that proof and requires an
    explicit collective membership decision, so it cannot turn a generic
    product Top-N question into external-search authority.
    """

    found: list[tuple[int, str, str]] = []
    for scope_start, scope_end, _limit, _literal in holding_document_top_n_matches(
        question
    ):
        tail = question[scope_end : min(len(question), scope_end + 180)]
        membership = _HOLDING_DOCUMENT_NAMED_MEMBERSHIP_TAIL.match(tail)
        if membership is None:
            continue
        relation_end = scope_end + membership.end("decision")
        relation_span = question[scope_start:relation_end]
        if len(relation_span) > 120:
            continue
        entity_surface = membership.group("entities").strip()
        parts = [
            value.strip(" ‘’'\"()[]")
            for value in re.split(
                r"\s*[,，]\s*|\s+(?:또는|혹은|및)\s+|\s*[·ㆍ]\s*|"
                r"(?<=[0-9A-Za-z가-힣])(?:와|과)(?=[0-9A-Za-z가-힣])|"
                r"(?<=[0-9A-Za-z가-힣])(?:와|과)\s+",
                entity_surface,
            )
        ]
        cleaned: list[str] = []
        for index, value in enumerate(parts):
            if index == len(parts) - 1:
                value = _strip_trailing_case_particle(value)
            if (
                2 <= len(_literal_evidence_key(value)) <= 80
                and not _holding_subject_is_generic(value)
                and _PRODUCT_LIKE_EXTERNAL_SUBJECT.search(value) is None
                and _NON_HOLDING_LIST_CLAUSE.search(value) is None
            ):
                cleaned.append(value)
        if len(cleaned) < 2:
            continue
        for value in cleaned:
            position = question.find(value, scope_end, relation_end)
            if position >= 0:
                found.append((position, value, relation_span))
    unique: dict[str, tuple[int, str, str]] = {}
    for item in found:
        unique.setdefault(_entity_surface_key(item[1]), item)
    return tuple((item[1], item[2]) for item in sorted(unique.values()))


def knowledge_requirement_signal_matches(
    question: str,
    requirement: KnowledgeIntentRequirement,
) -> bool:
    """Validate ordinary relation cues or one server-proved document membership.

    This is the shared sanitize/compile boundary.  It deliberately does not
    broaden ``knowledge_capability_signal_matches`` because a bare ``is in the
    top 10`` phrase has holdings meaning only after the complete question has
    passed the official-document Top-N grammar above.
    """

    if knowledge_capability_signal_matches(
        requirement.capability,
        requirement.relation_span,
    ):
        return True
    if requirement.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS:
        return False
    if requirement.relation_span not in question or requirement.subject_span not in question:
        return False
    return any(
        subject == requirement.subject_span and relation == requirement.relation_span
        for subject, relation in _listed_document_membership_entities(question)
    )


def _evidence_span_is_grounded(question: str, span: str) -> bool:
    if not span.strip():
        return False
    if span in question:
        return True
    normalized_span = _literal_evidence_key(span)
    return bool(normalized_span) and normalized_span in _literal_evidence_key(question)


_PARENTHESIZED_PRODUCT_IDENTIFIER = re.compile(
    r"\s*\((?:KR[A-Z0-9]{10}|[A-Z][A-Z0-9.]{1,12})(?:,\s*장내)?\)",
    re.IGNORECASE,
)


def _relation_span_is_exact_product_identity(question: str, relation_span: str) -> bool:
    """Return whether an apparent English ETP relation is a named DB product.

    ``Kimi 관련 해외 ETF`` states an external relation.  In contrast,
    ``Keating Active ETF(KEAT.O)`` is one exact product identity: the ``ETF``
    suffix belongs to the display name and must not open Document retrieval.
    This boundary is grammar-based and applies to unseen product names.
    """

    cursor = 0
    while (position := question.find(relation_span, cursor)) >= 0:
        tail = question[position + len(relation_span) :]
        if _PARENTHESIZED_PRODUCT_IDENTIFIER.match(tail) is not None:
            return True
        cursor = position + len(relation_span)
    return False


_SAFE_CROSS_SCRIPT_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9&+._' -]{1,78}")
_GENERIC_SEARCH_ALIASES = frozenset(
    {
        "asset manager",
        "bond",
        "company",
        "etf",
        "etn",
        "etp",
        "fund",
        "holding",
        "holdings",
        "official",
        "product",
        "theme",
    }
)


def _safe_entity_search_alias(question: str, subject: str, alias: str) -> bool:
    """Admit only a bounded spelling candidate, never new retrieval authority.

    A literal alias is already grounded. A non-literal alias must be an ASCII
    cross-script spelling of a Hangul subject. The candidate still has no
    evidentiary value: live extraction requires its exact span, the relation
    signal and one provided-DB product identity in the same official excerpt.
    """

    normalized = " ".join(alias.casefold().split())
    if normalized in _GENERIC_SEARCH_ALIASES:
        return False
    if _evidence_span_is_grounded(question, alias):
        subject_key = _literal_evidence_key(subject)
        alias_key = _literal_evidence_key(alias)
        if subject_key == alias_key:
            return True
        # A user-supplied bilingual spelling must be locally attached to the
        # subject. Another English publisher/product token elsewhere in the
        # question is not an alias of the subject.
        bilingual = (
            rf"(?:{re.escape(subject)})\s*[(/·]\s*(?:{re.escape(alias)})"
            rf"|(?:{re.escape(alias)})\s*[(/·]\s*(?:{re.escape(subject)})"
        )
        return re.search(bilingual, question, re.IGNORECASE) is not None
    if re.search(r"[가-힣]", subject) is None or _SAFE_CROSS_SCRIPT_ALIAS.fullmatch(alias) is None:
        return False
    if len(normalized.split()) > 5:
        return False
    return any(character.isalpha() for character in alias)


def _explicit_industry_relation(question: str, subject: str) -> bool:
    """Distinguish a real industry relation from an ETF name suffix."""

    return (
        not subject.endswith("산업")
        or re.search(
            rf"{re.escape(subject)}.{{0,12}}(?:관련|연결|연관|연계|접점|노출|투자)",
            question,
            re.IGNORECASE,
        )
        is not None
    )


def _taxonomy_subject_is_inside_exact_product_name(
    question: str,
    subject: str,
    exact_product_surfaces: tuple[str, ...] = (),
) -> bool:
    """Return whether a theme-like token belongs to one exact DB identity.

    Official Korean product names frequently contain ``테마``/``섹터``/``산업``
    before ``증권상장지수투자신탁``. When the same uninterrupted name ends in
    an organizer identifier, that token is identity text—not an external
    relation. An explicit relation verb still keeps a real theme query eligible.
    """

    identity_surfaces = tuple(exact_product_surfaces) + tuple(
        match.group(0) for match in _BRANDED_PRODUCT.finditer(question)
    )
    identity_ranges = tuple(
        (match.start(), match.end())
        for surface in identity_surfaces
        for match in re.finditer(re.escape(surface), question, re.IGNORECASE)
    )
    subject_occurrences = tuple(re.finditer(re.escape(subject), question, re.IGNORECASE))
    if (
        identity_ranges
        and subject_occurrences
        and all(
            any(start <= match.start() and match.end() <= end for start, end in identity_ranges)
            for match in subject_occurrences
        )
    ):
        # Every occurrence belongs to a DB-resolved or server-recognized brand
        # identity.  If the same taxonomy text appears again outside that
        # identity (for example ``KODEX 우주항공 ... 우주항공 테마``), the
        # second occurrence remains a real external-relation request.
        return True
    if (
        re.search(
            rf"{re.escape(subject)}.{{0,80}}(?:관련|연결|연관|연계|접점|편입|보유)",
            question,
            re.IGNORECASE,
        )
        is not None
    ):
        return False
    cursor = 0
    while (position := question.find(subject, cursor)) >= 0:
        tail = question[position + len(subject) :]
        if (
            re.match(
                r"(?:[0-9A-Za-z가-힣&·+._/-]|\([^)]{1,30}\)){1,120}"
                r"\((?:KR[A-Z0-9]{10}|[A-Z][A-Z0-9.]{1,12})(?:,\s*장내)?\)",
                tail,
                re.IGNORECASE,
            )
            is not None
        ):
            return True
        cursor = position + len(subject)
    return False


def _subject_is_locked_schema_value(
    question: str,
    subject: str,
    families: tuple[ProductFamily, ...],
) -> bool:
    """Return whether Lexical already binds the apparent entity to a DB field.

    English enum labels such as ``Money Market``, ``Mixed Assets`` and
    ``Global Emerging Markets`` are valid provided-data values. They must not
    be reinterpreted as organizations merely because they precede ``ETF``.
    """

    from finance_agent_core.agent.linker import build_lexical_hints

    target = _literal_evidence_key(subject)
    search_families = families or tuple(ProductFamily)
    for family in search_families:
        hints = build_lexical_hints(
            question,
            family.value,
            force_product_family_hint=True,
        )
        for constraint in hints["required_constraints"]:
            values = (
                constraint["value"]
                if isinstance(constraint["value"], list)
                else [constraint["value"]]
            )
            if any(
                isinstance(value, str) and _literal_evidence_key(value) == target
                for value in values
            ):
                return True
    return False


def _grounded_requirement_is_server_admissible(
    question: str,
    requirement: KnowledgeIntentRequirement,
    *,
    families: tuple[ProductFamily, ...] = (),
    exact_product_surfaces: tuple[str, ...] = (),
    hclx_frame_backed: bool = False,
) -> bool:
    """Validate one HCX relation proposal without granting it retrieval authority.

    Natural-language subjects and relation verbs are not always adjacent.  A
    bounded HCX proposal may bridge that syntax only when both spans occur in
    the user question, the relation span contains the server-owned capability
    cue, and the subject is neither a generic connective nor a provided-data
    enum value.  Official-source evidence and provided-DB intersection remain
    mandatory downstream, so this admits a search candidate, never a fact.
    """

    if not (
        _evidence_span_is_grounded(question, requirement.subject_span)
        and _evidence_span_is_grounded(question, requirement.relation_span)
    ):
        return False
    if not hclx_frame_backed and not knowledge_requirement_signal_matches(
        question,
        requirement,
    ):
        return False
    subject = requirement.subject_span.strip("‘’'\" ,은는이가을를와과중")
    normalized_subject = subject.replace(" ", "").casefold()
    if len(_literal_evidence_key(subject)) < 2:
        return False
    if normalized_subject in _NON_ENTITY_HOLDING_SUBJECTS:
        return False
    if normalized_subject in _GENERIC_EXTERNAL_SUBJECTS:
        return False
    if requirement.capability is KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND:
        exact_subject = _literal_evidence_key(subject)
        family_scope = set(families)
        if (
            "펀드" not in normalized_subject
            and (
                family_scope != {ProductFamily.FUND}
                or not any(
                    exact_subject == _literal_evidence_key(surface)
                    for surface in exact_product_surfaces
                )
            )
        ):
            return False
    if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
        if _PORTFOLIO_ELIGIBILITY.search(question) is not None:
            return False
        if (
            _UNVERIFIED_HOLDING_RANK_POSITION.search(question) is not None
            and holding_document_top_n_scope(question) is None
        ):
            return False
        if _HOLDING_PROJECTION_ONLY.fullmatch(requirement.relation_span.strip()) is not None:
            return False
        if _holding_subject_is_generic(subject):
            return False
        if _PRODUCT_LIKE_EXTERNAL_SUBJECT.search(subject) is not None:
            return False
    if (
        requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
        and _relation_span_is_exact_product_identity(question, requirement.relation_span)
    ):
        return False
    if (
        requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
        and not hclx_frame_backed
        and not question_has_knowledge_capability_signal(
            requirement.capability,
            question,
            subject=subject,
        )
    ):
        return False
    return not _subject_is_locked_schema_value(question, subject, families)


def has_server_authorized_knowledge_requirements(
    question: str,
    draft: IntentAnalysisDraft,
) -> bool:
    """Require deterministic lexical evidence before HCX may open web retrieval.

    HCX remains mandatory for Intent analysis, but its knowledge requirements
    are proposals only.  A plain DB field such as ``운용사`` or ``위험등급``
    cannot acquire external-network authority through a model misclassification.
    """

    if not draft.knowledge_requirements:
        return False
    for requirement in draft.knowledge_requirements:
        frame_backed = external_frame_supports_requirement(
            question,
            draft,
            requirement,
        )
        if not (
            _evidence_span_is_grounded(question, requirement.subject_span)
            and _evidence_span_is_grounded(question, requirement.relation_span)
            and (
                frame_backed
                or knowledge_requirement_signal_matches(question, requirement)
            )
        ):
            return False
        if (
            requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
            and not frame_backed
            and not question_has_knowledge_capability_signal(
                requirement.capability,
                question,
                subject=requirement.subject_span,
            )
        ):
            return False
        if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS and re.search(
            r"편입(?:이|가)?\s*가능", requirement.relation_span, re.IGNORECASE
        ):
            return False
        if (
            requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and _UNVERIFIED_HOLDING_RANK_POSITION.search(question) is not None
            and holding_document_top_n_scope(question) is None
        ):
            return False
    return True


def has_external_knowledge_surface(question: str) -> bool:
    """Return whether a server-owned relation vocabulary occurs in the question.

    This is only a cheap identity-resolution preflight; it grants no retrieval
    authority.  Keeping it beside the typed extractors prevents FastAPI wiring
    from maintaining a smaller, drifting copy of the external-relation grammar.
    The sanitized requirements and source/evidence guards remain authoritative.
    """

    cue_patterns = _KNOWLEDGE_CAPABILITY_CUES.values()
    return any(pattern.search(question) is not None for pattern in cue_patterns)


def has_negated_external_relation(question: str) -> bool:
    """Return whether a literal external relation is requested in the negative.

    This is a control decision, not a relation extractor.  A positive evidence
    cache or live-search result cannot establish non-membership, so matching
    questions must abstain before HCX or lexical parsing can erase negation.
    """

    return _NEGATED_EXTERNAL_RELATION.search(question) is not None


def sanitize_server_authorized_knowledge_draft(
    question: str,
    draft: IntentAnalysisDraft,
    *,
    fallback_families: tuple[ProductFamily, ...] = (),
    product_mentions: tuple[str, ...] = (),
    source_owner_mentions: tuple[str, ...] = (),
) -> IntentAnalysisDraft:
    """Compile explicit server relations and admit bounded HCX syntax bridges.

    HCX is mandatory for Intent analysis, but a model-proposed relation cannot
    open the network on its own.  The server derives common relations directly
    and admits residual non-adjacent syntax only when HCX returns literal
    subject/relation spans that satisfy a server-owned capability policy.  HCX
    may additionally contribute a bounded spelling alias; evidence and product
    identity are still verified independently downstream.
    """

    if has_negated_external_relation(question):
        return draft.model_copy(
            update={
                "intent": InteractionIntent.CLARIFY,
                "needs_clarification": True,
                "knowledge_requirements": [],
                "execution_clauses": [],
            }
        )

    authorized: list[KnowledgeIntentRequirement] = []
    seen: set[tuple[KnowledgeCapability, str, str]] = set()

    def append(candidate: KnowledgeIntentRequirement) -> None:
        if candidate.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
            normalized_subject = _normalize_holding_subject(candidate.subject_span)
            if normalized_subject != candidate.subject_span:
                candidate = candidate.model_copy(
                    update={
                        "subject_span": normalized_subject,
                        "search_aliases": tuple(
                            alias
                            for alias in candidate.search_aliases
                            if _safe_entity_search_alias(
                                question,
                                normalized_subject,
                                alias,
                            )
                        ),
                    }
                )
        key = (
            candidate.capability,
            candidate.subject_span,
            candidate.relation_span,
        )
        if key not in seen:
            authorized.append(candidate)
            seen.add(key)

    proposed_aliases: dict[tuple[KnowledgeCapability, str], set[str]] = {}
    for requirement in draft.knowledge_requirements:
        if (
            requirement.subject_span in question
            and requirement.relation_span in question
            and question_has_knowledge_capability_signal(
                requirement.capability,
                question,
                subject=requirement.subject_span,
            )
        ):
            subject = (
                _normalize_holding_subject(requirement.subject_span)
                if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                else requirement.subject_span
            )
            key = (requirement.capability, _entity_surface_key(subject))
            proposed_aliases.setdefault(key, set()).update(requirement.search_aliases)

    def aliases_for(capability: KnowledgeCapability, subject: str) -> tuple[str, ...]:
        # HCX has candidate authority only. The deterministic capability and
        # subject must already exist in the question, source selection does not
        # use these aliases, and the official exact-span/DB-intersection path
        # independently verifies every admitted relation.
        if capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
            subject = _normalize_holding_subject(subject)
        aliases = {
            alias
            for alias in proposed_aliases.get((capability, _entity_surface_key(subject)), set())
            if _safe_entity_search_alias(question, subject, alias)
        }
        if capability is KnowledgeCapability.SECTOR_THEME_HISTORY:
            # A taxonomy suffix states the relation kind, not part of the
            # entity itself.  The base is a literal substring supplied by the
            # user (for example ``우주항공`` from ``우주항공 테마``), so this
            # improves official-document recall without inventing a synonym.
            base = re.sub(r"\s*(?:테마|섹터|산업)\s*$", "", subject).strip()
            if len(_literal_evidence_key(base)) >= 2 and base in question:
                aliases.add(base)
        return tuple(sorted(aliases))

    corporate_pair = _EXTERNAL_CORPORATE_PAIR.search(question)
    corporate_selection = _EXTERNAL_CORPORATE_SELECTION.search(question)
    corporate_parent = None
    if corporate_pair is not None:
        corporate_parent = corporate_pair.group("parent")
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                subject_span=corporate_parent,
                relation_span=corporate_pair.group(0),
                search_aliases=aliases_for(
                    KnowledgeCapability.CORPORATE_SUBSIDIARY,
                    corporate_parent,
                ),
            )
        )
    elif corporate_selection is not None:
        corporate_parent = corporate_selection.group("parent")
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                subject_span=corporate_parent,
                relation_span=corporate_selection.group(0),
                search_aliases=aliases_for(
                    KnowledgeCapability.CORPORATE_SUBSIDIARY,
                    corporate_parent,
                ),
            )
        )
    else:
        corporate_parent_match = _EXTERNAL_CORPORATE_PARENT.search(question)
        if corporate_parent_match is not None:
            corporate_parent = corporate_parent_match.group("parent")
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                    subject_span=corporate_parent,
                    relation_span=corporate_parent_match.group(0),
                    search_aliases=aliases_for(
                        KnowledgeCapability.CORPORATE_SUBSIDIARY,
                        corporate_parent,
                    ),
                )
            )
    listed_holding_keys: set[str] = set()
    holding_table_boundaries = tuple(_HOLDING_LIST_BOUNDARY.finditer(question))
    source_profile_keys = {
        _entity_surface_key(surface)
        for surface in product_mentions
        if any(
            occurrence.end() <= boundary.start()
            and re.search(
                r"[.!?。！？;；\n]",
                question[occurrence.end() : boundary.start()],
            )
            is None
            for occurrence in re.finditer(re.escape(surface), question, re.IGNORECASE)
            for boundary in holding_table_boundaries
        )
    }
    if _PORTFOLIO_ELIGIBILITY.search(question) is None:
        for subject, relation in _listed_document_membership_entities(question):
            listed_holding_keys.add(
                _entity_surface_key(_normalize_holding_subject(subject))
            )
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span=subject,
                    relation_span=relation,
                    search_aliases=aliases_for(
                        KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        subject,
                    ),
                )
            )
        for subject, relation in _listed_holding_entities(question):
            listed_holding_keys.add(
                _entity_surface_key(_normalize_holding_subject(subject))
            )
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span=subject,
                    relation_span=relation,
                    search_aliases=aliases_for(
                        KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        subject,
                    ),
                )
            )
        for match in _EXTERNAL_HOLDING_VIA_PROFILE.finditer(question):
            subject = _normalize_holding_subject(match.group("subject"))
            listed_holding_keys.add(_entity_surface_key(subject))
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span=subject,
                    relation_span=match.group("relation"),
                    search_aliases=aliases_for(
                        KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        subject,
                    ),
                )
            )
        for match in _EXTERNAL_HOLDING_SUBJECT.finditer(question):
            if (
                _RANK_POSITION_PREFIX.search(
                    question[max(0, match.start("relation") - 32) : match.start("relation")]
                )
                and holding_document_top_n_scope(question) is None
            ):
                continue
            subject = _normalize_holding_subject(match.group("subject"))
            subject_key = _entity_surface_key(subject)
            # In ``A, B 두 상품의 공식 편입표를 보면 X, Y를 담은 ETF``
            # A/B identify the source documents; they are not constituents.
            # The table boundary and DB-confirmed product surfaces make this
            # distinction without any issuer- or fixture-specific vocabulary.
            if subject_key in source_profile_keys or any(
                key
                and subject_key
                and (key in subject_key or subject_key in key)
                for key in listed_holding_keys
            ):
                continue
            if _holding_subject_is_generic(subject) and corporate_parent is not None:
                subject = corporate_parent
            if (
                _PRODUCT_LIKE_EXTERNAL_SUBJECT.search(subject) is None
                and not _holding_subject_is_generic(subject)
                and _entity_surface_key(subject) not in listed_holding_keys
            ):
                append(
                    KnowledgeIntentRequirement(
                        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        subject_span=subject,
                        relation_span=match.group("relation"),
                        search_aliases=aliases_for(
                            KnowledgeCapability.PORTFOLIO_HOLDINGS,
                            subject,
                        ),
                    )
                )
    # Only identities already resolved against the provided DB may suppress a
    # taxonomy/relationship interpretation.  The syntax-only extractor is
    # useful for risk subject proposals, but treating every ``Name ETF`` span
    # as an exact product would hide unseen theme/entity relations before the
    # provided-DB intersection can adjudicate them.
    exact_named_product_surfaces = tuple(product_mentions)
    classified_theme_ranges: list[tuple[int, int]] = []
    for classified_theme in _EXTERNAL_CLASSIFIED_THEME.finditer(question):
        theme_subject, relation_span, relation_range = _classified_theme_parts(
            question,
            classified_theme,
        )
        if (
            len(_literal_evidence_key(re.sub(r"\s*(?:테마|섹터|산업)$", "", theme_subject)))
            < 2
            or _taxonomy_subject_is_inside_exact_product_name(
                question,
                theme_subject,
                exact_named_product_surfaces,
            )
            or _subject_is_locked_schema_value(
                question,
                theme_subject,
                fallback_families,
            )
        ):
            continue
        classified_theme_ranges.append(relation_range)
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span=theme_subject,
                relation_span=relation_span,
                search_aliases=aliases_for(
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                    theme_subject,
                ),
            )
        )
    for theme in _EXTERNAL_THEME_SUBJECT.finditer(question):
        if any(
            start <= theme.start() and theme.end() <= end
            for start, end in classified_theme_ranges
        ):
            continue
        theme_subject = theme.group("subject")
        relation_tail = _EXTERNAL_THEME_LINK_TAIL.match(question, theme.end())
        theme_relation_span = (
            question[theme.start() : relation_tail.end()]
            if relation_tail is not None and relation_tail.end() > 0
            else theme_subject
        )
        # ``산업`` is also a common suffix inside an ETF product name (for
        # example ``KODEX 2차전지산업``).  Unlike the explicit ``테마`` and
        # ``섹터`` nouns, a bare industry suffix opens external retrieval only
        # when the question also states an industry relation.  This prevents a
        # product identity from being silently converted into a theme filter.
        industry_relation_is_explicit = _explicit_industry_relation(
            question,
            theme_subject,
        )
        if industry_relation_is_explicit and not _taxonomy_subject_is_inside_exact_product_name(
            question,
            theme_subject,
            exact_named_product_surfaces,
        ):
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                    subject_span=theme_subject,
                    relation_span=theme_relation_span,
                    search_aliases=aliases_for(
                        KnowledgeCapability.SECTOR_THEME_HISTORY,
                        theme_subject,
                    ),
                )
            )
    related_etp = _EXTERNAL_RELATED_ETP_SUBJECT.search(question)
    if (
        related_etp is not None
        and not _taxonomy_subject_is_inside_exact_product_name(
            question,
            related_etp.group("subject"),
            exact_named_product_surfaces,
        )
        and _grounded_requirement_is_server_admissible(
            question,
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span=related_etp.group("subject"),
                relation_span=related_etp.group("relation"),
            ),
            families=fallback_families,
        )
    ):
        subject = related_etp.group("subject")
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span=subject,
                relation_span=related_etp.group("relation"),
                search_aliases=aliases_for(
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                    subject,
                ),
            )
        )
    else:
        named_etp = next(
            (
                match
                for match in _EXTERNAL_NAMED_ETP_SUBJECT.finditer(question)
                if match.group("subject").casefold() not in _GENERIC_ASCII_ETP_SUBJECTS
                and not match.group("subject").isupper()
            ),
            None,
        )
        if named_etp is not None:
            subject = named_etp.group("subject")
            if (
                not _taxonomy_subject_is_inside_exact_product_name(
                    question,
                    subject,
                    exact_named_product_surfaces,
                )
                and not _relation_span_is_exact_product_identity(question, named_etp.group(0))
                and not _subject_is_locked_schema_value(
                    question,
                    subject,
                    fallback_families,
                )
            ):
                append(
                    KnowledgeIntentRequirement(
                        capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                        subject_span=subject,
                        relation_span=named_etp.group(0),
                        search_aliases=aliases_for(
                            KnowledgeCapability.SECTOR_THEME_HISTORY,
                            subject,
                        ),
                    )
                )
    related_product = _EXTERNAL_RELATED_PRODUCT_SUBJECT.search(question)
    if related_product is not None:
        subject = related_product.group("subject")
        candidate = KnowledgeIntentRequirement(
            capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
            subject_span=subject,
            relation_span=related_product.group("relation"),
            search_aliases=aliases_for(
                KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject,
            ),
        )
        if not _taxonomy_subject_is_inside_exact_product_name(
            question,
            subject,
            exact_named_product_surfaces,
        ) and _grounded_requirement_is_server_admissible(
            question,
            candidate,
            families=fallback_families,
        ):
            append(candidate)
    fund = _EXTERNAL_FUND_SUBJECT.search(question)
    if fund is not None:
        subject = re.sub(
            r"^(?:(?:20\d{2}\s*년\s*)?\d{1,2}\s*(?:[~～-]\s*\d{1,2}\s*)?월\s*)",
            "",
            fund.group("subject"),
        ).strip()
        # Preserve every explicit relation in a compound request such as
        # ``구조와 운용 동향``. A single ``search()`` used to retain only the
        # first noun and incorrectly declared the partially evidenced answer
        # complete.
        relation_matches = tuple(_EXTERNAL_FUND_RELATION.finditer(question, fund.end("subject")))
        for relation_match in relation_matches or (fund,):
            relation_span = (
                relation_match.group("relation")
                if relation_match.re is _EXTERNAL_FUND_SUBJECT
                else relation_match.group(0)
            )
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    subject_span=subject,
                    relation_span=relation_span,
                    search_aliases=aliases_for(
                        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                        subject,
                    ),
                )
            )
    elif (fund_preposed := _EXTERNAL_FUND_PREPOSED.search(question)) is not None:
        subject = fund_preposed.group("subject").strip()
        relation_span = fund_preposed.group("relation")
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                subject_span=subject,
                relation_span=relation_span,
                search_aliases=aliases_for(
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    subject,
                ),
            )
        )
    # A taxonomy noun immediately before a generic ETP scope (for example
    # ``반도체 ETF의 공시상 위험요인``) is a bounded official-document
    # relation request, not an exact product identity. Branded names and
    # provided-schema enum values remain excluded by the same server gates.
    taxonomy_etp = _EXTERNAL_TAXONOMY_ETP_SUBJECT.search(question)
    has_holding_requirement = any(
        item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS for item in authorized
    )
    if (
        taxonomy_etp is not None
        and _EXTERNAL_RISK_RELATION.search(question) is not None
        and not has_holding_requirement
        and not _taxonomy_subject_is_inside_exact_product_name(
            question,
            taxonomy_etp.group("subject"),
            exact_named_product_surfaces,
        )
        and taxonomy_etp.group("subject").replace(" ", "").casefold()
        not in _GENERIC_EXTERNAL_SUBJECTS
        and _RELATIONAL_MODIFIER_ONLY.fullmatch(taxonomy_etp.group("subject").strip())
        is None
        and not _subject_is_locked_schema_value(
            question,
            taxonomy_etp.group("subject"),
            fallback_families,
        )
    ):
        subject = taxonomy_etp.group("subject")
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span=subject,
                relation_span=taxonomy_etp.group(0),
                search_aliases=aliases_for(
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                    subject,
                ),
            )
        )
    risk = _EXTERNAL_RISK_RELATION.search(question)
    if risk is not None:
        holding_subject_keys = {
            _entity_surface_key(item.subject_span)
            for item in authorized
            if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        }
        branded_subjects = {
            value.strip("‘’'\" ,은는이가을를와과중의")
            for value in _BRANDED_PRODUCT.findall(question)
            if value.strip("‘’'\" ,은는이가을를와과중의")
        }
        mention_subjects = {
            value.strip("‘’'\" ,은는이가을를와과중의")
            for value in product_mentions
            if value.strip("‘’'\" ,은는이가을를와과중의")
            and _entity_surface_key(value) not in holding_subject_keys
        }
        risk_subjects = branded_subjects | mention_subjects
        family_scope = {*fallback_families, *draft.product_families}
        issuer_risk_subjects: set[str] = set()
        if ProductFamily.BOND in family_scope:
            issuer_risk_subjects.update(
                owner
                for owner in source_owner_mentions
                if _source_owner_is_explicit_bond_issuer(question, owner)
            )
        if issuer_risk_subjects:
            # The explicit issuer grammar owns the risk scope.  A bond code or
            # name used to identify the product must not become a second,
            # independent risk requirement that demands a product prospectus
            # in addition to the issuer's official filing.
            risk_subjects = issuer_risk_subjects
        has_relation_selected_products = any(
            item.capability
            in {
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.SECTOR_THEME_HISTORY,
            }
            for item in authorized
        )
        if has_relation_selected_products:
            # In ``A/B의 편입표에서 X를 담은 ETF의 위험요인`` the named
            # A/B products are the candidate universe, not independent risk
            # targets.  Risk evidence belongs to whichever products survive
            # the relation join and DB ranking.  Bind one literal family scope
            # and let ``depends_on`` enforce that product-level relationship.
            result_scopes = tuple(
                _DEPENDENT_RISK_RESULT_SCOPE.finditer(
                    question,
                    max(0, risk.start() - 80),
                    risk.start(),
                )
            )
            scopes = tuple(_DEPENDENT_RISK_PRODUCT_SCOPE.finditer(question, 0, risk.start()))
            latest_result_scope = result_scopes[-1] if result_scopes else None
            latest_product_scope = scopes[-1] if scopes else None
            # A quantifier such as ``모두`` describes how constituent
            # requirements combine; it is not the product whose risk should
            # be retrieved. Prefer a later, literal product-family scope and
            # retain the result anaphor only when no closer product noun exists.
            if latest_product_scope is not None and (
                latest_result_scope is None
                or latest_product_scope.start() > latest_result_scope.start()
            ):
                risk_subjects = {latest_product_scope.group(0)}
            elif latest_result_scope is not None:
                risk_subjects = {latest_result_scope.group(0)}
        if not risk_subjects and corporate_parent is not None and "상품" in question:
            risk_subjects = {"상품"}
        if not risk_subjects and has_relation_selected_products:
            # A risk request that follows a holdings screen refers to the
            # verified/ranked product result, not to the holding company.  Use
            # only a literal family/product scope from the question here; the
            # plan compiler then binds the risk requirement to every holdings
            # requirement via ``depends_on``.  This admits no product and no
            # external fact by itself.
            scopes = tuple(_DEPENDENT_RISK_PRODUCT_SCOPE.finditer(question, 0, risk.start()))
            if scopes:
                risk_subjects = {scopes[-1].group(0)}
        for subject in sorted(risk_subjects):
            if subject in question:
                append(
                    KnowledgeIntentRequirement(
                        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                        subject_span=subject,
                        relation_span=risk.group(0),
                        search_aliases=aliases_for(
                            KnowledgeCapability.PRODUCT_RISK_FACTORS,
                            subject,
                        ),
                    )
                )
    # HCX may bridge non-adjacent syntax such as ``Apple·삼성전자 중 하나
    # 이상이 상위 10개에 들어 있는 ETF``.  Retain only proposals whose
    # literal subject and relation spans pass the same deterministic capability
    # policy.  This is deliberately after the lexical extractors so common
    # forms stay deterministic and HCX fills only the residual syntax gap.
    for requirement in draft.knowledge_requirements:
        if (
            requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
            and _taxonomy_subject_is_inside_exact_product_name(
                question,
                requirement.subject_span,
                exact_named_product_surfaces,
            )
        ):
            continue
        if not _grounded_requirement_is_server_admissible(
            question,
            requirement,
            families=fallback_families or tuple(draft.product_families),
            exact_product_surfaces=exact_named_product_surfaces,
            hclx_frame_backed=external_frame_supports_requirement(
                question,
                draft,
                requirement,
            ),
        ):
            continue
        if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
            # A correct HCX bridge may restate a deterministic list as one
            # combined subject (``Apple·Samsung``).  Once the server has
            # recovered every literal item, keeping both representations would
            # repeat the same official searches and can change ALL/ANY set
            # semantics.  Prefer the split server-owned form; HCX remains the
            # fallback only when deterministic syntax recovery found nothing.
            combined_key = _literal_evidence_key(requirement.subject_span)
            requirement_subject_key = _entity_surface_key(
                _normalize_holding_subject(requirement.subject_span)
            )
            if requirement_subject_key in source_profile_keys:
                continue
            covered_keys = {
                key
                for key in listed_holding_keys
                if key and _literal_evidence_key(key) in combined_key
            }
            if requirement_subject_key not in listed_holding_keys and covered_keys:
                continue
        if any(
            existing.capability is requirement.capability
            and _entity_surface_key(existing.subject_span)
            == _entity_surface_key(requirement.subject_span)
            for existing in authorized
        ):
            continue
        append(
            requirement.model_copy(
                update={
                    "search_aliases": aliases_for(
                        requirement.capability,
                        requirement.subject_span,
                    )
                }
            )
        )
    authorized = _remove_redundant_corporate_holding_bridges(
        authorized,
        corporate_parent=corporate_parent,
    )
    # Product-family scope is an execution boundary, not an HCX preference.
    # Explicit server-owned lexical evidence therefore wins whenever it exists.
    # A bare ETF/ETN/ETP surface names the common domestic+overseas product class;
    # HCX must not silently narrow that cross-family request to one market.
    fallback_family_set = set(fallback_families)
    explicit_etp_families = explicit_etp_market_families(question)
    if (
        explicit_etp_families
        and ProductFamily.BOND in fallback_family_set
        and not has_standalone_bond_product_scope(question)
    ):
        # ``해외 채권 ETF`` names an overseas ETP asset type, not a second
        # standalone bond-product request.  Never preserve that lexical/HCX
        # false positive across the server authorization boundary.
        fallback_family_set.remove(ProductFamily.BOND)
    if (
        _GENERIC_ETP_SURFACE.search(question) is not None
        and not explicit_etp_families
    ):
        generic_etp_families = {
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        }
        if (
            ProductFamily.FUND in fallback_family_set
            and has_standalone_public_fund_scope(question)
        ):
            generic_etp_families.add(ProductFamily.FUND)
        if (
            ProductFamily.BOND in fallback_family_set
            and has_standalone_bond_product_scope(question)
        ):
            generic_etp_families.add(ProductFamily.BOND)
        families = tuple(
            sorted(
                generic_etp_families,
                key=lambda item: item.value,
            )
        )
    elif fallback_family_set:
        families = tuple(sorted(fallback_family_set, key=lambda item: item.value))
    elif related_product is not None and authorized:
        families = tuple(ProductFamily)
    else:
        families = tuple(sorted(set(draft.product_families), key=lambda item: item.value))
    if any(
        item.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
        and re.search(
            rf"{re.escape(item.subject_span)}\s+(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))",
            item.relation_span,
            re.IGNORECASE,
        )
        for item in authorized
    ):
        # A bare ETP label has no domestic/overseas authority. Preserve any
        # exact DB-resolved family and add both ETP universes for intersection.
        families = tuple(
            sorted(
                {*families, ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP},
                key=lambda item: item.value,
            )
        )
    intent = draft.intent
    if authorized and intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}:
        intent = InteractionIntent.SEARCH
    return draft.model_copy(
        update={
            "intent": intent,
            "product_families": list(families),
            "needs_clarification": False if authorized else draft.needs_clarification,
            "knowledge_requirements": authorized,
        }
    )


class IntentAnalysisVerification(IntentAnalysisModel):
    passed: bool
    reason_code: Literal[
        "intent_analysis_verified",
        "intent_analysis_invalid_evidence",
        "intent_analysis_structure_missing",
        "intent_analysis_structure_missing_advisory",
        "intent_analysis_structure_ungrounded",
        "intent_analysis_structure_ungrounded_advisory",
        "intent_analysis_structure_semantic_coverage_missing",
        "intent_analysis_structure_semantic_advisory",
        "intent_analysis_structure_role_missing",
        "intent_analysis_structure_role_advisory",
        "intent_analysis_semantic_frame_invalid",
        "intent_analysis_semantic_frame_schema_advisory",
        "intent_analysis_advisory_clarification",
        "intent_analysis_advisory_intent_conflict",
        "intent_analysis_advisory_family_conflict",
    ]


_STRUCTURE_RANKING_CUE = re.compile(
    r"(?:상위|하위|TOP\s*\d*|높은\s*순|낮은\s*순|큰\s*순|작은\s*순|"
    r"가장\s*(?:높|낮|크|작))",
    re.IGNORECASE,
)
_STRUCTURE_FILTER_CUE = re.compile(
    r"(?:[-+]?\d[\d,.]*\s*%?\s*(?:이상|이하|초과|미만)|"
    r"(?:최소|최대|적어도|많아야)\s*[-+]?\d)",
    re.IGNORECASE,
)
_STRUCTURE_RELATION_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "holdings",
        re.compile(
            r"편입|보유|구성\s*종목|담(?:은|고\s*있는)|넣(?:은|고\s*있는)|"
            r"들어간|(?:들어|담겨)\s*있",
            re.IGNORECASE,
        ),
    ),
    (
        "corporate",
        re.compile(
            r"자회사|계열사|관계사|"
            r"(?:계열|관계|소속|산하)\s*(?:기업|회사|법인)",
            re.IGNORECASE,
        ),
    ),
    ("theme", re.compile(r"테마|섹터|산업\s*(?:연결|연계|접점)", re.IGNORECASE)),
    ("trend", re.compile(r"동향|(?:연결|연계)\s*이력", re.IGNORECASE)),
    (
        "risk",
        re.compile(
            r"위험\s*요인|손실\s*(?:관련\s*)?유의|"
            r"주의\s*(?:사항|점)|유의\s*(?:사항|점)",
            re.IGNORECASE,
        ),
    ),
)
_STRUCTURE_SEQUENCE_CUE = re.compile(
    r"먼저|우선|그\s*안에서|(?:추린|고른|선별한|거른|남긴)\s*(?:뒤|후|다음)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _StructureSemanticAnchor:
    start: int
    end: int
    role: str
    group_key: str


def _surface_ranges(question: str, surfaces: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for surface in surfaces:
        cursor = 0
        while surface and (position := question.find(surface, cursor)) >= 0:
            ranges.append((position, position + len(surface)))
            cursor = position + len(surface)
    # Product display names tied to an explicit organizer identifier are
    # identity surfaces even when the legacy mention extractor does not yet
    # recognize the six-character domestic display code.  A geographic word
    # inside such a name (for example ``미국``) must not be reinterpreted as an
    # independent DB filter.
    for match in re.finditer(
        r"(?:[A-Za-z가-힣][A-Za-z0-9가-힣._+-]*\s*){1,4}\([A-Z0-9]{6,13}\)",
        question,
        re.IGNORECASE,
    ):
        ranges.append(match.span())
    return tuple(ranges)


def _structure_semantic_anchors(
    question: str,
    *,
    product_mentions: tuple[str, ...] = (),
) -> tuple[_StructureSemanticAnchor, ...]:
    """Return server-recognized meaning spans HCX must have actually read.

    Clause roles are deliberately not execution authority.  HCX frequently
    labels one combined phrase as ``ranking`` while the same literal phrase
    contains a numeric filter or first-stage shortlist.  Requiring the label to
    be identical caused safe, fully compiled queries to be rejected.  The
    stable contract is instead that every deterministic meaning anchor occurs
    inside at least one grounded HCX clause; field IDs, values and SQL remain
    server-owned.
    """

    anchors: dict[tuple[int, int], _StructureSemanticAnchor] = {}
    mention_ranges = _surface_ranges(question, product_mentions)
    for match in typed_alias_matches(question):
        if any(left <= match.start and match.end <= right for left, right in mention_ranges):
            continue
        group_key = ":".join(
            (
                "typed",
                match.operation,
                match.field_id,
                str(match.operator or match.direction or ""),
                str(match.value if match.value is not None else ""),
            )
        )
        anchors[(match.start, match.end)] = _StructureSemanticAnchor(
            start=match.start,
            end=match.end,
            role=("ranking" if match.operation == "rank" else match.operation),
            group_key=group_key,
        )
    for match in _STRUCTURE_RANKING_CUE.finditer(question):
        normalized = _literal_evidence_key(match.group(0))
        group = "topk" if re.search(r"상위|하위|top", normalized, re.IGNORECASE) else normalized
        anchors.setdefault(
            match.span(),
            _StructureSemanticAnchor(
                start=match.start(),
                end=match.end(),
                role="ranking",
                group_key=f"ranking:{group}",
            ),
        )
    for match in _STRUCTURE_FILTER_CUE.finditer(question):
        anchors.setdefault(
            match.span(),
            _StructureSemanticAnchor(
                start=match.start(),
                end=match.end(),
                role="filter",
                group_key=f"filter:{_literal_evidence_key(match.group(0))}",
            ),
        )
    for relation_kind, pattern in _STRUCTURE_RELATION_CUES:
        for match in pattern.finditer(question):
            anchors.setdefault(
                match.span(),
                _StructureSemanticAnchor(
                    start=match.start(),
                    end=match.end(),
                    role="relation",
                    group_key=f"relation:{relation_kind}",
                ),
            )
    return tuple(anchors[key] for key in sorted(anchors))


def _grounded_clause_ranges(
    question: str,
    clauses: list[IntentExecutionClause],
) -> tuple[tuple[int, int, str], ...] | None:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for clause in clauses:
        position = question.find(clause.text_span, cursor)
        if position < 0:
            # A later repeated surface may be absent even though the first one
            # is valid.  Reuse is forbidden by the DTO, so a whole-question
            # fallback remains deterministic and safe.
            position = question.find(clause.text_span)
        if position < 0:
            return None
        ranges.append((position, position + len(clause.text_span), clause.role))
        cursor = position + len(clause.text_span)
    if [item[0] for item in ranges] != sorted(item[0] for item in ranges):
        return None
    return tuple(ranges)


def _verify_execution_structure(
    question: str,
    draft: IntentAnalysisDraft,
    server_decision: RouteDecision,
) -> IntentAnalysisVerification | None:
    """Validate mandatory HCX clause structure without granting execution authority."""

    clauses = draft.execution_clauses
    if not clauses:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_structure_missing_advisory",
        )
    clause_ranges = _grounded_clause_ranges(question, clauses)
    if clause_ranges is None:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_structure_ungrounded_advisory",
        )

    anchors = _structure_semantic_anchors(
        question,
        product_mentions=tuple(server_decision.draft.product_mentions),
    )
    covered_groups = {
        anchor.group_key
        for anchor in anchors
        if any(left <= anchor.start and anchor.end <= right for left, right, _ in clause_ranges)
    }
    uncovered = [anchor for anchor in anchors if anchor.group_key not in covered_groups]
    if uncovered:
        # The clauses prove HCX participated in grounded Intent analysis, but
        # their segmentation is not execution authority.  The server-owned
        # SemanticCoverageGate, Typed QueryPlan compiler and Verifier already
        # reject a dropped numeric filter, rank direction or relation before
        # SQL/external evidence execution.  Turning HCX's partial restatement
        # into a second veto made valid requests nondeterministic without
        # adding safety, so retain the difference as an auditable advisory.
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_structure_semantic_advisory",
        )

    # Roles are retained for audit and downstream candidate ordering only.  A
    # combined or differently segmented natural-language clause is semantically
    # equivalent when it covers the same literal anchors.  Treat role drift as
    # advisory instead of granting HCX a veto over a server-verified plan.
    expected_roles = {anchor.role for anchor in anchors}
    observed_roles = {role for _, _, role in clause_ranges}
    if _STRUCTURE_SEQUENCE_CUE.search(question) is not None:
        expected_roles.update({"shortlist", "ranking"})
    compatible = {
        "filter": {"filter", "scope", "shortlist", "ranking"},
        "ranking": {"ranking", "shortlist"},
        "relation": {"relation", "scope", "filter"},
        "shortlist": {"shortlist", "ranking"},
    }
    if any(
        not observed_roles.intersection(compatible.get(role, {role})) for role in expected_roles
    ):
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_structure_role_advisory",
        )
    return None


def _verify_semantic_frame(
    question: str,
    draft: IntentAnalysisDraft,
) -> IntentAnalysisVerification | None:
    """Reject only malformed HCX authority claims; unresolved clauses may fall back.

    This check never turns an HCX field into a QueryPlan.  It proves that every
    claimed field belongs to the closed Registry/capability set and that all
    model evidence remains literal user text.  Compiler/semantic admission
    independently recheck the same facts before SQL.
    """

    registry = None
    has_non_authoritative_schema_claim = False
    for clause in draft.execution_clauses:
        spans = (
            clause.text_span,
            clause.field_span,
            clause.value_span,
            clause.limit_span,
            clause.subject_span,
        )
        if any(span and not _evidence_span_is_grounded(question, span) for span in spans):
            return IntentAnalysisVerification(
                passed=False,
                reason_code="intent_analysis_semantic_frame_invalid",
            )
        if clause.semantic_status is not SemanticFrameStatus.RESOLVED:
            continue
        nested_spans = (
            clause.field_span,
            clause.value_span,
            clause.subject_span,
        )
        if any(span and span not in clause.text_span for span in nested_spans):
            if clause.operation is SemanticFrameOperation.EXTERNAL_RELATION:
                # A relation subject outside its claimed relation clause can
                # alter what the live search asks for.  Keep that execution
                # boundary strict even when the same words occur elsewhere in
                # the question.
                return IntentAnalysisVerification(
                    passed=False,
                    reason_code="intent_analysis_semantic_frame_invalid",
                )
            # HCX sometimes attaches a question-grounded value from a nearby
            # clause to the current field clause (for example, an exact
            # reference-product name as the value for ``이 상품 이상``).  It is
            # not executable evidence for this clause, but it is also not a
            # fabricated span: the deterministic compiler may already have a
            # complete canonical plan.  Record the drift as advisory and drop
            # the whole clause from downstream hints via
            # ``semantic_frame_clause_is_schema_admissible``.
            has_non_authoritative_schema_claim = True
            continue
        if clause.limit_span and clause.limit_span not in clause.text_span:
            # HCX may segment ``순자산이 큰 순`` and ``3개 보여줘`` into
            # separate clauses while attaching the literal limit to the
            # ranking clause.  Accept that harmless segmentation only when the
            # exact limit occurs once in the user question and is owned by one
            # compatible, non-relation clause with the same family scope.
            clause_families = set(clause.product_families or draft.product_families)
            compatible_owner = any(
                owner is not clause
                and owner.role in {"scope", "shortlist", "ranking", "presentation"}
                and clause.limit_span in owner.text_span
                and (
                    not owner.product_families
                    or not clause_families
                    or bool(clause_families.intersection(owner.product_families))
                )
                for owner in draft.execution_clauses
            )
            if question.count(clause.limit_span) != 1 or not compatible_owner:
                # HCX clause segmentation is advisory for DB execution.  In
                # particular, repeated limits (for example one ``14개`` per
                # family) can be attached to the neighbouring clause even
                # though the deterministic compound compiler has already
                # bound both counts exactly.  Drop this whole HCX clause from
                # downstream field hints instead of making the model's
                # non-deterministic segmentation a second execution veto.
                # The literal is still required to occur in the question by
                # the grounding check above; external-relation subjects keep
                # their separate strict boundary below.
                has_non_authoritative_schema_claim = True
                continue
        if clause.operation is SemanticFrameOperation.EXTERNAL_RELATION:
            try:
                capability = KnowledgeCapability(clause.knowledge_capability)
            except ValueError:
                return IntentAnalysisVerification(
                    passed=False,
                    reason_code="intent_analysis_semantic_frame_invalid",
                )
            if not any(
                requirement.capability is capability
                and requirement.subject_span == clause.subject_span
                and requirement.relation_span == clause.text_span
                for requirement in draft.knowledge_requirements
            ):
                return IntentAnalysisVerification(
                    passed=False,
                    reason_code="intent_analysis_semantic_frame_invalid",
                )
            continue
        if registry is None:
            from finance_agent_core.config import load_field_registry

            registry = load_field_registry()
        families = tuple(clause.product_families or draft.product_families)
        if not families:
            has_non_authoritative_schema_claim = True
            continue
        for family in families:
            try:
                definition = registry.require_field(clause.field_id, [family.value])
            except ValueError:
                # HCX is the primary semantic adviser, not a second policy
                # engine. A hallucinated family/field pairing is ignored and
                # audited; it cannot veto a complete server plan or enter a
                # QueryPlan without Registry and hard-filter admission. An
                # eligible unresolved field may proceed only to the bounded
                # Schema Dense fallback and the same server checks.
                has_non_authoritative_schema_claim = True
                continue
            capability = {
                SemanticFrameOperation.FILTER: definition.queryable,
                SemanticFrameOperation.RANK: definition.sortable,
                SemanticFrameOperation.PROJECT: definition.selectable,
                SemanticFrameOperation.AGGREGATE: definition.aggregatable,
            }.get(clause.operation, False)
            if not capability:
                has_non_authoritative_schema_claim = True
                continue
            if (
                clause.operation is SemanticFrameOperation.FILTER
                and clause.operator.value not in definition.allowed_operators
            ):
                has_non_authoritative_schema_claim = True
    if has_non_authoritative_schema_claim:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_semantic_frame_schema_advisory",
        )
    return None


def semantic_frame_clause_is_schema_admissible(
    draft: IntentAnalysisDraft,
    clause: IntentExecutionClause,
) -> bool:
    """Return whether one HCX clause may remain a downstream field hint.

    Intent verification can record a bad HCX family/field pairing as advisory
    when the deterministic server plan is already complete.  That does not
    make the bad clause usable elsewhere: every downstream semantic consumer
    must first pass this closed Registry/capability and literal-meaning check.
    The helper grants no QueryPlan authority, does not infer a missing family,
    and refuses model-supplied operators, directions or derived expressions
    that are not proven by the clause text.
    """

    if clause.semantic_status is not SemanticFrameStatus.RESOLVED:
        return False
    if clause.operation is SemanticFrameOperation.EXTERNAL_RELATION:
        return clause.subject_span in clause.text_span and any(
            requirement.capability.value == clause.knowledge_capability
            and requirement.subject_span == clause.subject_span
            and requirement.relation_span == clause.text_span
            for requirement in draft.knowledge_requirements
        )
    if clause.field_span not in clause.text_span:
        return False
    if clause.limit_span and clause.limit_span not in clause.text_span:
        # A limit owned by another clause is not executable HCX evidence.  The
        # server may independently recover it through a canonical QueryPlan,
        # but this model clause cannot be used for semantic promotion.
        return False
    non_field_surface = _mask_semantic_frame_literal(clause.text_span, clause.field_span)
    non_field_surface = _mask_semantic_frame_limit(non_field_surface, clause.limit_span)
    if _SEMANTIC_FRAME_DERIVED_EXPRESSION.search(non_field_surface) is not None:
        # One Registry field cannot stand in for an arithmetic expression or
        # a relationship between two metrics.  The typed compound compiler
        # may still prove and execute that expression independently.
        return False
    if clause.operation is SemanticFrameOperation.RANK:
        ascending = _SEMANTIC_FRAME_ASCENDING_CUE.search(non_field_surface) is not None
        descending = _SEMANTIC_FRAME_DESCENDING_CUE.search(non_field_surface) is not None
        if ascending == descending:
            # A direction is execution meaning.  It must be uniquely present
            # in the literal clause rather than supplied from model priors.
            return False
        literal_direction = SemanticFrameDirection.ASC if ascending else SemanticFrameDirection.DESC
        if clause.direction is not literal_direction:
            return False
    if clause.operation is SemanticFrameOperation.FILTER:
        if clause.value_span not in clause.text_span:
            return False
        if _SEMANTIC_FRAME_RANGE_CUE.search(non_field_surface) is not None:
            if clause.operator is not SemanticFrameOperator.BETWEEN:
                return False
        else:
            literal_operators = {
                operator
                for operator, cue in _SEMANTIC_FRAME_OPERATOR_CUES.items()
                if cue.search(non_field_surface) is not None
            }
            if len(literal_operators) > 1:
                return False
            if literal_operators:
                literal_operator = next(iter(literal_operators))
                compatible = (
                    {SemanticFrameOperator.NEQ, SemanticFrameOperator.NOT_IN}
                    if literal_operator is SemanticFrameOperator.NEQ
                    else {literal_operator}
                )
                if clause.operator not in compatible:
                    return False
            elif clause.operator in {
                SemanticFrameOperator.LT,
                SemanticFrameOperator.LTE,
                SemanticFrameOperator.GT,
                SemanticFrameOperator.GTE,
                SemanticFrameOperator.BETWEEN,
            }:
                # Ordered thresholds cannot be inferred from a bare number.
                return False
    families = tuple(clause.product_families or draft.product_families)
    if not families or clause.field_id == "__none__":
        return False
    from finance_agent_core.config import load_field_registry

    registry = load_field_registry()
    for family in families:
        try:
            definition = registry.require_field(clause.field_id, [family.value])
        except ValueError:
            return False
        capability = {
            SemanticFrameOperation.FILTER: definition.queryable,
            SemanticFrameOperation.RANK: definition.sortable,
            SemanticFrameOperation.PROJECT: definition.selectable,
            SemanticFrameOperation.AGGREGATE: definition.aggregatable,
        }.get(clause.operation, False)
        if not capability:
            return False
        if (
            clause.operation is SemanticFrameOperation.FILTER
            and clause.operator.value not in definition.allowed_operators
        ):
            return False
    return True


def verify_intent_analysis(
    question: str,
    server_decision: RouteDecision,
    draft: IntentAnalysisDraft,
    *,
    require_execution_structure: bool = False,
) -> IntentAnalysisVerification:
    """Cross-check HCX without allowing it to upgrade server authority."""

    knowledge_spans = [
        span
        for requirement in draft.knowledge_requirements
        for span in (requirement.subject_span, requirement.relation_span)
    ]
    if any(
        not _evidence_span_is_grounded(question, span)
        for span in [*draft.evidence_spans, *knowledge_spans]
    ):
        return IntentAnalysisVerification(
            passed=False,
            reason_code="intent_analysis_invalid_evidence",
        )

    semantic_frame_failure = _verify_semantic_frame(question, draft)
    if semantic_frame_failure is not None:
        return semantic_frame_failure

    # Deterministic terminal decisions remain terminal. HCX participates in
    # intent analysis but can never downgrade UNSUPPORTED/CLARIFY to execution.
    if server_decision.disposition is not RouteDisposition.EXECUTE:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_verified",
        )

    if require_execution_structure:
        structure_failure = _verify_execution_structure(question, draft, server_decision)
        if structure_failure is not None:
            return structure_failure

    # HCX participation is mandatory, but the model has no authority to veto a
    # deterministic plan that already passed SafetyEnvelope and semantic
    # coverage.  A disagreement is retained as an auditable advisory signal;
    # it never upgrades a blocked route and never changes fields, filters,
    # direction, limit, or SQL authority.
    if draft.needs_clarification or draft.intent in {
        InteractionIntent.CLARIFY,
        InteractionIntent.UNSUPPORTED,
    }:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_advisory_clarification",
        )
    if draft.intent is not server_decision.draft.intent:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_advisory_intent_conflict",
        )
    # Product-family order carries no execution meaning.  Require the exact
    # same unique set while allowing HCX to serialize multi-family mentions in
    # a different order from the deterministic router.
    if set(draft.product_families) != set(server_decision.draft.product_families):
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_advisory_family_conflict",
        )
    return IntentAnalysisVerification(
        passed=True,
        reason_code="intent_analysis_verified",
    )


__all__ = [
    "IntentAnalysisDraft",
    "IntentAnalysisProvider",
    "IntentAnalysisVerification",
    "IntentExecutionClause",
    "external_frame_supports_requirement",
    "has_external_knowledge_surface",
    "has_negated_external_relation",
    "has_server_authorized_knowledge_requirements",
    "holding_document_top_n_matches",
    "holding_document_top_n_scope",
    "knowledge_capability_signal_matches",
    "knowledge_requirement_signal_matches",
    "question_has_knowledge_capability_signal",
    "sanitize_server_authorized_knowledge_draft",
    "semantic_frame_clause_is_schema_admissible",
    "verify_intent_analysis",
]
