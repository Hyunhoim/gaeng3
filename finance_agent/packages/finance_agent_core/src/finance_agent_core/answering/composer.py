from __future__ import annotations

import time

from finance_agent_core.agent.providers.hyperclova import hyperclova_failure_reason
from finance_agent_core.answering.context import build_grounded_answer_context
from finance_agent_core.answering.models import (
    AnswerComposition,
    AnswerVerification,
    GroundedAnswerContext,
    GroundedAnswerDraft,
    GroundedAnswerProvider,
)
from finance_agent_core.answering.verifier import AnswerVerifier
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import Intent, QueryPlan
from finance_agent_core.domain import ProductEvidence, VerifiedSearch


def _evidence_citation(
    context: GroundedAnswerContext,
    result_ref: str,
    fields: list[str],
) -> str:
    index = int(result_ref.removeprefix("result_")) - 1
    product = context.products[index]
    by_name = {field.canonical_field: field for field in product.fields}
    registry = load_field_registry()
    citations: list[str] = []
    for field_name in fields:
        field = by_name[field_name]
        label = registry.require_field(
            field_name,
            [context.source_manifest.dataset],
        ).label
        source_columns = "/".join(field.source_columns) or "constant"
        citations.append(
            f"{label}: {field.source_id} 원본 행 {field.source_row}, "
            f"{source_columns}, 기준일 {field.as_of.isoformat()}"
        )
    return "; ".join(citations)


def _compile_answer(
    context: GroundedAnswerContext,
    draft: GroundedAnswerDraft,
) -> str:
    explanations = [
        (
            f"{index}. {product.explanation} "
            f"[근거: {_evidence_citation(context, product.result_ref, product.evidence_fields)}]"
        )
        for index, product in enumerate(draft.products, start=1)
    ]
    narrative = "\n".join(
        [
            f"요약: {draft.lead}",
            "상품별 근거 해설:",
            *explanations,
        ]
    )
    return f"{narrative}\n{context.deterministic_answer}"


def _failed_verification(message: str) -> AnswerVerification:
    return AnswerVerification(
        passed=False,
        checks={"provider_completed": False},
        violations=[message],
    )


def compose_grounded_answer(
    *,
    question: str,
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
    provider: GroundedAnswerProvider,
) -> AnswerComposition:
    context = build_grounded_answer_context(
        question=question,
        plan=plan,
        verified=verified,
        products=products,
    )
    if plan.intent is Intent.COMPARE:
        requested_ids = {
            str(product_id)
            for constraint in plan.constraints
            if constraint.field == "product_id" and isinstance(constraint.value, list)
            for product_id in constraint.value
        }
        if {product.product_id for product in products} != requested_ids:
            return AnswerComposition(
                mode="deterministic",
                answer=context.deterministic_answer,
                model=provider.model_name,
                generation_latency_ms=0,
                draft=None,
                verification=AnswerVerification(
                    passed=True,
                    checks={"comparison_incomplete_deterministic": True},
                    violations=[],
                ),
            )
    if not context.products:
        return AnswerComposition(
            mode="deterministic",
            answer=context.deterministic_answer,
            model=provider.model_name,
            generation_latency_ms=0,
            draft=None,
            verification=AnswerVerification(
                passed=True,
                checks={"empty_result_deterministic": True},
                violations=[],
            ),
        )

    started = time.perf_counter()
    try:
        draft = provider.generate_grounded_answer(context)
    except Exception as error:  # noqa: BLE001 - safe fallback is the contract
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return AnswerComposition(
            mode="deterministic_fallback",
            answer=context.deterministic_answer,
            model=provider.model_name,
            generation_latency_ms=latency_ms,
            draft=None,
            verification=_failed_verification(f"{type(error).__name__}: {error}"),
            provider_failure_reason=hyperclova_failure_reason(error),
        )

    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    verifier = AnswerVerifier()
    draft_verification = verifier.verify(context, draft)
    if not draft_verification.passed:
        return AnswerComposition(
            mode="deterministic_fallback",
            answer=context.deterministic_answer,
            model=provider.model_name,
            generation_latency_ms=latency_ms,
            draft=draft,
            verification=draft_verification,
        )
    compiled_answer = _compile_answer(context, draft)
    verification = verifier.verify_compiled(context, draft, compiled_answer)
    if not verification.passed:
        return AnswerComposition(
            mode="deterministic_fallback",
            answer=context.deterministic_answer,
            model=provider.model_name,
            generation_latency_ms=latency_ms,
            draft=draft,
            verification=verification,
        )
    return AnswerComposition(
        mode="llm_grounded",
        answer=compiled_answer,
        model=provider.model_name,
        generation_latency_ms=latency_ms,
        draft=draft,
        verification=verification,
    )
