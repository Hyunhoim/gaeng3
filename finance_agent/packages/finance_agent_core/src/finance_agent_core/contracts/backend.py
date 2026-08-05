from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.routed_service import RoutedAgentResult
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import (
    AggregateEvidence,
    ComparisonEvidence,
    DatabaseManifest,
    ProductEvidence,
)
from finance_agent_core.retrieval import DocumentEvidence


class BackendContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackendStatus(StrEnum):
    SUCCESS = "success"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not_found"
    ERROR = "error"


class BackendAnswerMode(StrEnum):
    CONTROL = "control"
    DETERMINISTIC = "deterministic"
    LLM_GROUNDED = "llm_grounded"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class BackendErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    DATASET_UNAVAILABLE = "dataset_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL_ERROR = "internal_error"


class BackendAgentRequest(BackendContractModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    locale: Literal["ko-KR"] = "ko-KR"

    @model_validator(mode="after")
    def reject_blank_values(self) -> BackendAgentRequest:
        if not self.request_id.strip() or not self.question.strip():
            raise ValueError("request_id and question cannot be blank")
        return self


class BackendClarification(BackendContractModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    required_fields: list[str] = Field(min_length=1, max_length=10)
    options: list[str] = Field(default_factory=list, max_length=20)


class BackendError(BackendContractModel):
    code: BackendErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool


class SourceCitation(BackendContractModel):
    citation_id: str = Field(min_length=1, max_length=300)
    kind: Literal[
        "product_field",
        "comparison_field",
        "aggregate_field",
        "document_chunk",
    ]
    label: str = Field(min_length=1, max_length=500)
    source_id: str = Field(min_length=1, max_length=300)
    source_locator: str = Field(min_length=1, max_length=1000)
    as_of: date
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class BackendAgentResponse(BackendContractModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1, max_length=128)
    status: BackendStatus
    intent: InteractionIntent
    product_families: list[ProductFamily] = Field(max_length=4)
    answer: str = Field(min_length=1)
    query_plan: QueryPlan | None
    candidate_count: int | None = Field(default=None, ge=0)
    products: list[ProductEvidence]
    comparisons: list[ComparisonEvidence] = Field(default_factory=list)
    aggregates: list[AggregateEvidence] = Field(default_factory=list)
    documents: list[DocumentEvidence]
    citations: list[SourceCitation]
    as_of_dates: list[date]
    warnings: list[str]
    answer_mode: BackendAnswerMode
    fallback_used: bool
    provider_model: str | None
    clarification: BackendClarification | None
    error: BackendError | None
    source_manifest: DatabaseManifest | None

    @model_validator(mode="after")
    def validate_state(self) -> BackendAgentResponse:
        if self.query_plan is not None and self.query_plan.question_id != self.request_id:
            raise ValueError("response and QueryPlan request IDs differ")
        if len(self.citations) != len({item.citation_id for item in self.citations}):
            raise ValueError("citation IDs must be unique")
        if len(self.as_of_dates) != len(set(self.as_of_dates)):
            raise ValueError("as_of_dates must be unique")
        if self.status is BackendStatus.SUCCESS:
            if (
                not self.products
                and not self.comparisons
                and not self.aggregates
                and not self.documents
            ):
                raise ValueError(
                    "success response requires product, aggregate, or document evidence"
                )
            if self.error is not None or self.clarification is not None:
                raise ValueError("success response cannot contain control details")
        elif self.status is BackendStatus.NOT_FOUND:
            if (
                self.products
                or self.comparisons
                or self.aggregates
                or self.documents
                or self.citations
            ):
                raise ValueError("not_found response cannot contain evidence")
            if self.candidate_count != 0:
                raise ValueError("not_found response requires candidate_count=0")
        elif self.status is BackendStatus.CLARIFICATION:
            if self.clarification is None or self.error is not None:
                raise ValueError("clarification response requires clarification details")
            if (
                self.products
                or self.comparisons
                or self.aggregates
                or self.documents
                or self.candidate_count is not None
            ):
                raise ValueError("clarification response cannot contain executed results")
        elif self.status is BackendStatus.UNSUPPORTED:
            if (
                self.error is not None
                or self.products
                or self.comparisons
                or self.aggregates
                or self.documents
            ):
                raise ValueError("unsupported response cannot contain error or evidence")
            if self.candidate_count is not None:
                raise ValueError("unsupported response cannot contain candidate_count")
        elif self.status is BackendStatus.ERROR:
            if self.error is None:
                raise ValueError("error response requires an error object")
            if (
                self.query_plan is not None
                or self.candidate_count is not None
                or self.products
                or self.comparisons
                or self.aggregates
                or self.documents
                or self.citations
                or self.as_of_dates
                or self.warnings
                or self.clarification is not None
                or self.provider_model is not None
                or self.source_manifest is not None
            ):
                raise ValueError("error response cannot contain executed or control evidence")
            if self.answer_mode is not BackendAnswerMode.CONTROL or self.fallback_used:
                raise ValueError("error response requires control mode without fallback")
        if self.status is not BackendStatus.ERROR and self.error is not None:
            raise ValueError("non-error response cannot contain an error object")
        if self.fallback_used != (self.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK):
            raise ValueError("fallback_used and answer_mode must agree")
        return self


def _product_citations(products: list[ProductEvidence]) -> list[SourceCitation]:
    citations: list[SourceCitation] = []
    for product in products:
        for field in product.fields:
            field_ref = f"{product.product_id}:{field.canonical_field}"
            columns = "/".join(field.source_columns) or "constant"
            citations.append(
                SourceCitation(
                    citation_id=f"product:{field_ref}",
                    kind="product_field",
                    label=f"{product.product_name} · {field.canonical_field}",
                    source_id=field.source_id,
                    source_locator=(f"{field.source_dataset} row {field.source_row} · {columns}"),
                    as_of=field.as_of,
                    evidence_refs=[field_ref],
                )
            )
    return citations


def _aggregate_citations(aggregates: list[AggregateEvidence]) -> list[SourceCitation]:
    citations: list[SourceCitation] = []
    for evidence in aggregates:
        group_text = ", ".join(
            f"{field}={value if value is not None else 'unknown'}"
            for field, value in evidence.group_values.items()
        )
        columns = "/".join(evidence.source_columns) or "constant"
        locator = f"{evidence.source_dataset} aggregate · rows={evidence.row_count} · {columns}"
        if group_text:
            locator += f" · group={group_text}"
        citations.append(
            SourceCitation(
                citation_id=f"aggregate:{evidence.evidence_id}",
                kind="aggregate_field",
                label=(
                    f"{evidence.label} · {evidence.function.value} · "
                    f"valid={evidence.valid_count}, missing={evidence.missing_count}"
                ),
                source_id=evidence.source_id,
                source_locator=locator,
                as_of=evidence.as_of_end or evidence.source_snapshot_date,
                evidence_refs=[evidence.evidence_id],
            )
        )
    return citations


def _comparison_citations(
    comparisons: list[ComparisonEvidence],
) -> list[SourceCitation]:
    citations: list[SourceCitation] = []
    for comparison in comparisons:
        grounded = [
            cell
            for cell in comparison.cells
            if cell.source_id is not None
            and cell.source_row is not None
            and cell.as_of is not None
            and cell.evidence_ref is not None
        ]
        if not grounded:
            continue
        locators = [
            (
                f"{cell.product_id}: {cell.source_dataset} row {cell.source_row} · "
                f"{'/'.join(cell.source_columns) or 'constant'}"
            )
            for cell in grounded
        ]
        source_ids = list(
            dict.fromkeys(cell.source_id for cell in grounded if cell.source_id is not None)
        )
        citations.append(
            SourceCitation(
                citation_id=f"comparison:{comparison.canonical_field}",
                kind="comparison_field",
                label=f"{comparison.label} · 두 상품 비교",
                source_id="/".join(source_ids),
                source_locator="; ".join(locators),
                as_of=max(cell.as_of for cell in grounded if cell.as_of is not None),
                evidence_refs=[
                    cell.evidence_ref for cell in grounded if cell.evidence_ref is not None
                ],
            )
        )
    return citations


def routed_result_to_backend(result: RoutedAgentResult) -> BackendAgentResponse:
    status = {
        "executed": (
            BackendStatus.NOT_FOUND if result.candidate_count == 0 else BackendStatus.SUCCESS
        ),
        "clarify": BackendStatus.CLARIFICATION,
        "unsupported": BackendStatus.UNSUPPORTED,
    }[result.status]
    if result.answer_composition is None:
        answer_mode = (
            BackendAnswerMode.CONTROL
            if result.status != "executed"
            else BackendAnswerMode.DETERMINISTIC
        )
        provider_model = None
    else:
        answer_mode = BackendAnswerMode(result.answer_composition.mode)
        provider_model = result.answer_composition.model
    citations = [
        *_product_citations(result.products),
        *_comparison_citations(result.comparisons),
        *_aggregate_citations(result.aggregates),
    ]
    as_of_dates = sorted(
        {
            *(field.as_of for product in result.products for field in product.fields),
            *(
                cell.as_of
                for comparison in result.comparisons
                for cell in comparison.cells
                if cell.as_of is not None
            ),
            *(
                bound
                for evidence in result.aggregates
                for bound in (
                    evidence.as_of_start,
                    evidence.as_of_end,
                    evidence.source_snapshot_date,
                )
                if bound is not None
            ),
        }
    )
    clarification = None
    if status is BackendStatus.CLARIFICATION:
        required_fields = {
            "missing_product_identity": ["product_identity"],
            "ambiguous_product_family": ["product_family"],
            "subjective_condition": ["objective_threshold"],
        }.get(result.decision.reason_code, ["query_condition"])
        clarification = BackendClarification(
            code=result.decision.reason_code,
            message=result.decision.reason,
            required_fields=required_fields,
            options=[],
        )
    return BackendAgentResponse(
        request_id=result.request_id,
        status=status,
        intent=result.decision.draft.intent,
        product_families=result.decision.draft.product_families,
        answer=result.answer,
        query_plan=result.query_plan,
        candidate_count=result.candidate_count,
        products=result.products,
        comparisons=[] if status is BackendStatus.NOT_FOUND else result.comparisons,
        aggregates=result.aggregates,
        documents=[],
        citations=[] if status is BackendStatus.NOT_FOUND else citations,
        as_of_dates=[] if status is BackendStatus.NOT_FOUND else as_of_dates,
        warnings=result.warnings,
        answer_mode=answer_mode,
        fallback_used=answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK,
        provider_model=provider_model,
        clarification=clarification,
        error=None,
        source_manifest=result.source_manifest,
    )


def backend_contract_schemas() -> dict[str, dict[str, Any]]:
    return {
        "request": BackendAgentRequest.model_json_schema(),
        "response": BackendAgentResponse.model_json_schema(),
    }
