from __future__ import annotations

import re

from finance_agent_core.answering.context import required_evidence_fields
from finance_agent_core.answering.models import (
    AnswerComposition,
    AnswerVerification,
    GroundedAnswerContext,
    GroundedAnswerDraft,
)
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily

_NUMBER_TOKEN = re.compile(r"[-+]?[0-9０-９]+(?:[.,][0-9０-９]+)?")
_UNCONTROLLED_CURRENCY_SYMBOL = re.compile(r"[₩$€¥]")
_UNSUPPORTED_INTERPRETATION = re.compile(
    r"추천|매수(?!(?:수익률|가능수량|가능여부|\s+가능\s+(?:수량|여부)))|"
    r"매도|보장|예측|전망|유리|불리|안전|수익성|"
    r"투자\s*결정|투자\s*가치|좋은|나쁜"
)
_FAMILY_REFERENCE_PATTERNS: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.BOND: re.compile(r"국내\s*채권|국내채권|채권\s*상품"),
    ProductFamily.DOMESTIC_ETP: re.compile(
        r"(?:국내|한국)\s*(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    ProductFamily.OVERSEAS_ETP: re.compile(
        r"(?:해외|글로벌|미국)\s*(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    ProductFamily.FUND: re.compile(r"공모\s*펀드|공모펀드"),
}
_FAMILY_SECTION_LABELS = {
    ProductFamily.BOND: "국내채권",
    ProductFamily.DOMESTIC_ETP: "국내 ETP",
    ProductFamily.OVERSEAS_ETP: "해외 ETP",
    ProductFamily.FUND: "공모펀드",
}
_CROSS_FAMILY_OPERATION = re.compile(
    r"상품군.{0,30}(?:비교|대비|보다|합산|합계|우열)|"
    r"(?:다른|타)\s*상품군|"
    r"(?:직접\s*)?(?:비교|대비|합산|합계|우열)\s*(?:하면|하여|해서|했을|결과)|"
    r"(?:보다|대비).{0,40}(?:높|낮|크|작|많|적|좋|나쁘|우수|열위|유리|불리)|"
    r"더\s*(?:우수|열위|유리|불리)"
)


def _trusted_number_tokens(context: GroundedAnswerContext) -> set[str]:
    registry = load_field_registry()
    product_family = context.source_manifest.dataset
    labels = {
        registry.require_field(field.canonical_field, [product_family]).label
        for product in context.products
        for field in product.fields
    }
    return {token for label in labels for token in _NUMBER_TOKEN.findall(label)}


class AnswerVerifier:
    """Verify structured narrative choices before facts are compiled into an answer."""

    def verify(
        self,
        context: GroundedAnswerContext,
        draft: GroundedAnswerDraft,
    ) -> AnswerVerification:
        checks: dict[str, bool] = {}
        violations: list[str] = []

        expected_refs = [f"result_{index}" for index in range(1, len(context.products) + 1)]
        actual_refs = [product.result_ref for product in draft.products]
        checks["product_order_exact"] = actual_refs == expected_refs
        if not checks["product_order_exact"]:
            violations.append(
                "draft opaque result references or order differ from verified results"
            )

        expected_warning_codes = [warning.code for warning in context.warnings]
        checks["warning_codes_exact"] = draft.acknowledged_warning_codes == expected_warning_codes
        if not checks["warning_codes_exact"]:
            violations.append("warning acknowledgement differs from required warning codes")

        controlled_text = [draft.lead]
        controlled_text.extend(product.explanation for product in draft.products)
        trusted_tokens = _trusted_number_tokens(context)
        checks["prose_numbers_are_grounded"] = all(
            set(_NUMBER_TOKEN.findall(text)) <= trusted_tokens
            and not _UNCONTROLLED_CURRENCY_SYMBOL.search(text)
            for text in controlled_text
        )
        if not checks["prose_numbers_are_grounded"]:
            violations.append(
                "model prose contains a number not grounded in an allowed field label"
            )

        checks["prose_has_no_advice_or_forecast"] = all(
            not _UNSUPPORTED_INTERPRETATION.search(text) for text in controlled_text
        )
        if not checks["prose_has_no_advice_or_forecast"]:
            violations.append("model prose contains advice, forecast, or value judgement")

        forbidden_identifiers = {
            value.casefold()
            for product in context.products
            for value in (product.product_id, product.product_name, product.ticker)
            if value is not None and len(value.strip()) >= 2
        }
        checks["prose_has_no_product_identifiers"] = all(
            not any(identifier in text.casefold() for identifier in forbidden_identifiers)
            for text in controlled_text
        )
        if not checks["prose_has_no_product_identifiers"]:
            violations.append("model prose contains a product name, ticker, or product ID")

        evidence_by_ref = {
            f"result_{index}": {field.canonical_field: field for field in product.fields}
            for index, product in enumerate(context.products, start=1)
        }
        all_fields_valid = True
        all_fields_usable = True
        required_fields_covered = True
        required = required_evidence_fields(context)
        for product_draft in draft.products:
            available = evidence_by_ref.get(product_draft.result_ref, {})
            selected = set(product_draft.evidence_fields)
            if not selected <= set(available):
                all_fields_valid = False
            usable = {
                name
                for name, evidence in available.items()
                if evidence.normalized_value is not None
                and evidence.quality in {QualityStatus.VALID, QualityStatus.PARTIAL}
            }
            if not selected <= usable:
                all_fields_usable = False
            required_usable = set(required) & usable
            if required_usable and not required_usable <= selected:
                required_fields_covered = False

        checks["evidence_fields_exist"] = all_fields_valid
        checks["evidence_fields_usable"] = all_fields_usable
        checks["required_evidence_covered"] = required_fields_covered
        if not all_fields_valid:
            violations.append("draft cites a field absent from product evidence")
        if not all_fields_usable:
            violations.append("draft cites missing or unusable product evidence")
        if not required_fields_covered:
            violations.append("draft omits usable ranking or lookup evidence")

        passed = all(checks.values())
        return AnswerVerification(
            passed=passed,
            checks=checks,
            violations=violations,
        )

    def verify_compiled(
        self,
        context: GroundedAnswerContext,
        draft: GroundedAnswerDraft,
        answer: str,
    ) -> AnswerVerification:
        """Verify the final server-compiled facts as well as the model draft."""

        draft_verification = self.verify(context, draft)
        checks = dict(draft_verification.checks)
        violations = list(draft_verification.violations)

        checks["compiled_core_exact"] = answer.endswith(context.deterministic_answer)
        if not checks["compiled_core_exact"]:
            violations.append(
                "compiled answer does not preserve the exact verified product order and values"
            )

        evidence_by_ref = {
            f"result_{index}": {field.canonical_field: field for field in product.fields}
            for index, product in enumerate(context.products, start=1)
        }
        citations_exact = True
        for product in draft.products:
            available = evidence_by_ref.get(product.result_ref, {})
            for field_name in product.evidence_fields:
                evidence = available.get(field_name)
                if evidence is None:
                    citations_exact = False
                    continue
                source_columns = "/".join(evidence.source_columns) or "constant"
                citation = (
                    f"{evidence.source_id} 원본 행 {evidence.source_row}, "
                    f"{source_columns}, 기준일 {evidence.as_of.isoformat()}"
                )
                if citation not in answer:
                    citations_exact = False
        checks["compiled_evidence_citations_exact"] = citations_exact
        if not citations_exact:
            violations.append(
                "compiled answer omits or changes field-level source and as-of evidence"
            )

        source_date = context.source_manifest.source_snapshot_date.isoformat()
        checks["compiled_source_date_present"] = source_date in answer
        if not checks["compiled_source_date_present"]:
            violations.append("compiled answer omits the source snapshot date")

        return AnswerVerification(
            passed=all(checks.values()),
            checks=checks,
            violations=violations,
        )


class CrossFamilyAnswerVerifier:
    """Verify that isolated family drafts stay isolated in the compiled envelope."""

    def verify(
        self,
        *,
        family_compositions: list[tuple[ProductFamily, AnswerComposition]],
        family_answers: list[tuple[ProductFamily, str]],
        answer: str,
        safety_notice: str,
    ) -> AnswerVerification:
        checks: dict[str, bool] = {}
        violations: list[str] = []

        checks["family_verifications_passed"] = all(
            composition.verification.passed for _, composition in family_compositions
        )
        if not checks["family_verifications_passed"]:
            for family, composition in family_compositions:
                violations.extend(
                    f"{family.value}: {violation}"
                    for violation in composition.verification.violations
                )

        prose_isolated = True
        no_cross_operation = True
        for family, composition in family_compositions:
            if composition.draft is None:
                continue
            prose = [
                composition.draft.lead,
                *(product.explanation for product in composition.draft.products),
            ]
            other_family_patterns = [
                pattern
                for other_family, pattern in _FAMILY_REFERENCE_PATTERNS.items()
                if other_family is not family
            ]
            if any(pattern.search(text) for text in prose for pattern in other_family_patterns):
                prose_isolated = False
            if any(_CROSS_FAMILY_OPERATION.search(text) for text in prose):
                no_cross_operation = False

        checks["family_prose_isolated"] = prose_isolated
        if not prose_isolated:
            violations.append("model prose mentions a different product family")
        checks["no_cross_family_operation"] = no_cross_operation
        if not no_cross_operation:
            violations.append(
                "model prose attempts cross-family comparison, aggregation, or ranking"
            )

        expected_answer = "\n\n".join(
            [
                *(
                    f"[{_FAMILY_SECTION_LABELS[family]}]\n{family_answer}"
                    for family, family_answer in family_answers
                ),
                safety_notice,
            ]
        )
        checks["family_answer_alignment"] = [
            (family, composition.answer) for family, composition in family_compositions
        ] == family_answers
        if not checks["family_answer_alignment"]:
            violations.append("compiled family sections differ from verified compositions")
        checks["compiled_envelope_exact"] = answer == expected_answer
        if not checks["compiled_envelope_exact"]:
            violations.append("compiled cross-family answer differs from the server envelope")
        checks["safety_notice_exact"] = (
            answer.endswith(safety_notice) and answer.count(safety_notice) == 1
        )
        if not checks["safety_notice_exact"]:
            violations.append("cross-family safety notice is missing or duplicated")

        return AnswerVerification(
            passed=all(checks.values()),
            checks=checks,
            violations=violations,
        )
