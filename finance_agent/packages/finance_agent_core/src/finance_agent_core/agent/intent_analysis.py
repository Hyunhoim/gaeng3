from __future__ import annotations

import re
import unicodedata
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class IntentAnalysisDraft(IntentAnalysisModel):
    """Non-executable HCX interpretation with no field or SQL authority."""

    schema_version: Literal["1.0"] = "1.0"
    intent: InteractionIntent
    product_families: list[ProductFamily] = Field(max_length=4)
    evidence_spans: list[str] = Field(max_length=8)
    needs_clarification: bool
    knowledge_requirements: list[KnowledgeIntentRequirement] = Field(
        default_factory=list,
        max_length=8,
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
        return self


class IntentAnalysisProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft: ...


_KNOWLEDGE_CAPABILITY_CUES: dict[KnowledgeCapability, re.Pattern[str]] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"(?:편입|보유(?:한|하고|종목)|구성\s*종목|담은)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|모회사|관계사",
        re.IGNORECASE,
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"섹터|테마|산업.{0,12}(?:관련|연결|투자)|(?:관련|연결).{0,12}(?:섹터|테마|산업)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"(?:펀드.{0,20})?(?:구조|투자\s*전략|운용\s*전략|판매\s*동향|운용\s*동향|"
        r"전략\s*동향|향후\s*일정)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험\s*요인|리스크\s*요인|어떤\s*위험",
        re.IGNORECASE,
    ),
}
_EXTERNAL_CORPORATE_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)인\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80})",
    re.IGNORECASE,
)
_EXTERNAL_CORPORATE_PARENT = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*의\s*"
    r"(?P<relation>자회사|계열사|가족사)(?=[을를이가은는\s])",
    re.IGNORECASE,
)
_EXTERNAL_HOLDING_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:이|가|을|를)?\s*(?P<relation>편입(?:이\s*확인되는|된|한|되어|하고)?|"
    r"보유(?:가\s*확인되는|된|한|하고)?)",
    re.IGNORECASE,
)
_EXTERNAL_THEME_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]{2,40}\s*(?:섹터|테마|산업))",
    re.IGNORECASE,
)
_EXTERNAL_FUND_SUBJECT = re.compile(
    r"(?P<subject>[0-9A-Za-z가-힣&·+._/-]+(?:\s+[0-9A-Za-z가-힣&·+._/-]+){0,4}"
    r"\s*펀드)(?:의)?\s*(?P<relation>구조|투자\s*전략|운용\s*동향|판매\s*동향)",
    re.IGNORECASE,
)
_EXTERNAL_RISK_RELATION = re.compile(
    r"핵심\s*위험\s*요인|위험\s*요인|리스크\s*요인",
    re.IGNORECASE,
)
_BRANDED_PRODUCT = re.compile(
    r"(?:KODEX|TIGER|ACE|SOL|RISE|PLUS|KIWOOM|WON|TIME)\s+"
    r"[0-9A-Za-z가-힣&+._/-]{2,80}",
    re.IGNORECASE,
)
_PRODUCT_LIKE_EXTERNAL_SUBJECT = re.compile(
    r"ETF|ETN|KODEX|TIGER|ACE|SOL|RISE|PLUS|KIWOOM|WON|TIME",
    re.IGNORECASE,
)
_PORTFOLIO_ELIGIBILITY = re.compile(r"편입\s*가능", re.IGNORECASE)
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
        "상품",
    }
)


def _entity_surface_key(value: str) -> str:
    surface = value.strip().casefold()
    if len(surface) >= 3 and surface[-1:] in {"은", "는", "이", "가", "을", "를", "와", "과"}:
        return surface[:-1]
    return surface


def _literal_evidence_key(value: str) -> str:
    """Normalize presentation-only Unicode while preserving literal token order."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _evidence_span_is_grounded(question: str, span: str) -> bool:
    if not span.strip():
        return False
    if span in question:
        return True
    normalized_span = _literal_evidence_key(span)
    return bool(normalized_span) and normalized_span in _literal_evidence_key(question)


def _explicit_industry_relation(question: str, subject: str) -> bool:
    """Distinguish a real industry relation from an ETF name suffix."""

    return (
        not subject.endswith("산업")
        or re.search(
            rf"{re.escape(subject)}.{{0,12}}(?:관련|연결|투자)",
            question,
            re.IGNORECASE,
        )
        is not None
    )


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
        cue = _KNOWLEDGE_CAPABILITY_CUES[requirement.capability]
        if cue.search(question) is None or cue.search(requirement.relation_span) is None:
            return False
        if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS and re.search(
            r"편입\s*가능", requirement.relation_span, re.IGNORECASE
        ):
            return False
    return True


def sanitize_server_authorized_knowledge_draft(
    question: str,
    draft: IntentAnalysisDraft,
    *,
    fallback_families: tuple[ProductFamily, ...] = (),
    product_mentions: tuple[str, ...] = (),
) -> IntentAnalysisDraft:
    """Compile explicit server relations and use HCX only for entity aliases.

    HCX is mandatory for Intent analysis, but a model-proposed relation cannot
    open the network.  The server derives every executable relation from the
    question itself; HCX may contribute at most a spelling alias to the exact
    same capability and subject.
    """

    authorized: list[KnowledgeIntentRequirement] = []
    seen: set[tuple[KnowledgeCapability, str, str]] = set()

    def append(candidate: KnowledgeIntentRequirement) -> None:
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
            and _KNOWLEDGE_CAPABILITY_CUES[requirement.capability].search(question) is not None
        ):
            key = (requirement.capability, _entity_surface_key(requirement.subject_span))
            proposed_aliases.setdefault(key, set()).update(requirement.search_aliases)

    def aliases_for(capability: KnowledgeCapability, subject: str) -> tuple[str, ...]:
        # HCX may copy aliases that the user explicitly supplied, but it cannot
        # establish a cross-language company identity from pretrained memory.
        # Official-document alias verification belongs to the evidence path.
        return tuple(
            sorted(
                alias
                for alias in proposed_aliases.get((capability, _entity_surface_key(subject)), set())
                if alias in question
            )
        )

    corporate_pair = _EXTERNAL_CORPORATE_PAIR.search(question)
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
    if _PORTFOLIO_ELIGIBILITY.search(question) is None:
        for match in _EXTERNAL_HOLDING_SUBJECT.finditer(question):
            subject = match.group("subject")
            normalized_subject = subject.replace(" ", "").casefold()
            if normalized_subject in _NON_ENTITY_HOLDING_SUBJECTS and corporate_parent is not None:
                subject = corporate_parent
                normalized_subject = subject.replace(" ", "").casefold()
            if (
                _PRODUCT_LIKE_EXTERNAL_SUBJECT.search(subject) is None
                and normalized_subject not in _NON_ENTITY_HOLDING_SUBJECTS
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
    theme = _EXTERNAL_THEME_SUBJECT.search(question)
    if theme is not None:
        theme_subject = theme.group("subject")
        # ``산업`` is also a common suffix inside an ETF product name (for
        # example ``KODEX 2차전지산업``).  Unlike the explicit ``테마`` and
        # ``섹터`` nouns, a bare industry suffix opens external retrieval only
        # when the question also states an industry relation.  This prevents a
        # product identity from being silently converted into a theme filter.
        industry_relation_is_explicit = _explicit_industry_relation(
            question,
            theme_subject,
        )
        if industry_relation_is_explicit:
            append(
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                    subject_span=theme_subject,
                    relation_span=theme_subject,
                    search_aliases=aliases_for(
                        KnowledgeCapability.SECTOR_THEME_HISTORY,
                        theme_subject,
                    ),
                )
            )
    fund = _EXTERNAL_FUND_SUBJECT.search(question)
    if fund is not None:
        append(
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                subject_span=fund.group("subject"),
                relation_span=fund.group("relation"),
                search_aliases=aliases_for(
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    fund.group("subject"),
                ),
            )
        )
    risk = _EXTERNAL_RISK_RELATION.search(question)
    if risk is not None:
        branded_subjects = {
            value.strip("‘’'\" ,은는이가을를와과중")
            for value in _BRANDED_PRODUCT.findall(question)
            if value.strip("‘’'\" ,은는이가을를와과중")
        }
        mention_subjects = {
            value.strip("‘’'\" ,은는이가을를와과중")
            for value in product_mentions
            if value.strip("‘’'\" ,은는이가을를와과중")
        }
        risk_subjects = branded_subjects or mention_subjects
        if not risk_subjects and corporate_parent is not None and "상품" in question:
            risk_subjects = {"상품"}
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
    families = tuple(sorted(set(draft.product_families), key=lambda item: item.value))
    if not families:
        families = tuple(sorted(set(fallback_families), key=lambda item: item.value))
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
        "intent_analysis_advisory_clarification",
        "intent_analysis_advisory_intent_conflict",
        "intent_analysis_advisory_family_conflict",
    ]


def verify_intent_analysis(
    question: str,
    server_decision: RouteDecision,
    draft: IntentAnalysisDraft,
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

    # Deterministic terminal decisions remain terminal. HCX participates in
    # intent analysis but can never downgrade UNSUPPORTED/CLARIFY to execution.
    if server_decision.disposition is not RouteDisposition.EXECUTE:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_verified",
        )

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
    "sanitize_server_authorized_knowledge_draft",
    "verify_intent_analysis",
]
