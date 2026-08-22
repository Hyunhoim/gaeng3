from __future__ import annotations

import hashlib
import re

from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.agent.semantic_resolution import (
    ResidualSpan,
    ResolvedSpan,
    ResolvedSpanLedger,
    SpanRole,
)
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent

_FAMILY_PATTERNS = {
    ProductFamily.BOND: re.compile(r"국내\s*채권|채권"),
    ProductFamily.DOMESTIC_ETP: re.compile(r"국내\s*(?:ETF|ETN|ETP)", re.IGNORECASE),
    ProductFamily.OVERSEAS_ETP: re.compile(r"해외\s*(?:ETF|ETN|ETP)", re.IGNORECASE),
    ProductFamily.FUND: re.compile(r"공모\s*펀드|펀드"),
}
_RANKING_NEARBY = re.compile(
    r"높|낮|크|작|많|적|상위|하위|오름차순|내림차순|정렬|순(?:으로|서|위)?",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"(?P<value>\d+)\s*(?:개|건)(?!월)")


def _find_span(question: str, text: str, occupied: set[tuple[int, int]]) -> tuple[int, int] | None:
    for match in re.finditer(re.escape(text), question, flags=re.IGNORECASE):
        bounds = match.span()
        if bounds not in occupied:
            return bounds
    return None


def build_resolved_span_ledger(
    *,
    question: str,
    interaction_intent: InteractionIntent,
    product_families: tuple[ProductFamily, ...],
    coverage: SemanticCoverageDecision,
) -> ResolvedSpanLedger:
    """Build a bounded request-local coverage ledger from deterministic evidence."""

    occupied: set[tuple[int, int]] = set()
    residuals: list[ResidualSpan] = []
    for text in coverage.schema_link_gap_spans:
        bounds = _find_span(question, text, occupied)
        if bounds is None:
            continue
        occupied.add(bounds)
        residuals.append(
            ResidualSpan(
                text=question[bounds[0] : bounds[1]],
                start=bounds[0],
                end=bounds[1],
            )
        )

    resolved: list[ResolvedSpan] = []
    for family in product_families:
        match = _FAMILY_PATTERNS[family].search(question)
        if match is None:
            continue
        resolved.append(
            ResolvedSpan(
                text=match.group(0),
                start=match.start(),
                end=match.end(),
                role=SpanRole.PRODUCT_FAMILY,
            )
        )

    registry = load_field_registry()
    family_names = {family.value for family in product_families}
    for field_id, definition in registry.fields.items():
        if family_names and not family_names.intersection(definition.datasets):
            continue
        phrases = sorted(
            {definition.label, *definition.aliases},
            key=lambda value: (-len(value), value),
        )
        match = None
        for phrase in phrases:
            if not phrase.strip():
                continue
            candidate = re.search(re.escape(phrase), question, flags=re.IGNORECASE)
            if candidate is not None:
                match = candidate
                break
        if match is None or any(
            start < match.end() and match.start() < end for start, end in occupied
        ):
            continue
        nearby = question[match.start() : min(len(question), match.end() + 24)]
        role = SpanRole.RANKING if _RANKING_NEARBY.search(nearby) else SpanRole.HARD_FILTER
        resolved.append(
            ResolvedSpan(
                text=match.group(0),
                start=match.start(),
                end=match.end(),
                role=role,
                field_id=field_id,
            )
        )

    if limit_match := _LIMIT.search(question):
        resolved.append(
            ResolvedSpan(
                text=limit_match.group(0),
                start=limit_match.start(),
                end=limit_match.end(),
                role=SpanRole.LIMIT,
            )
        )

    return ResolvedSpanLedger(
        question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
        interaction_intent=interaction_intent,
        product_families=product_families,
        resolved_spans=tuple(
            sorted(
                {
                    (item.start, item.end, item.role, item.field_id): item for item in resolved
                }.values(),
                key=lambda item: (item.start, item.end, item.role.value),
            )
        ),
        residual_spans=tuple(sorted(residuals, key=lambda item: (item.start, item.end))),
    )


__all__ = ["build_resolved_span_ledger"]
