from __future__ import annotations

import re

from finance_agent_core.answering.context import required_evidence_fields
from finance_agent_core.answering.models import (
    AnswerVerification,
    GroundedAnswerContext,
    GroundedAnswerDraft,
)
from finance_agent_core.config import QualityStatus, load_field_registry

_NUMBER_TOKEN = re.compile(r"[-+]?[0-9０-９]+(?:[.,][0-9０-９]+)?")
_UNCONTROLLED_CURRENCY_SYMBOL = re.compile(r"[₩$€¥]")
_UNSUPPORTED_INTERPRETATION = re.compile(
    r"추천|매수(?!(?:수익률|가능수량|가능여부|\s+가능\s+(?:수량|여부)))|"
    r"매도|보장|예측|전망|유리|불리|안전|수익성|"
    r"투자\s*결정|투자\s*가치|좋은|나쁜"
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
            if len(value.strip()) >= 2
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
