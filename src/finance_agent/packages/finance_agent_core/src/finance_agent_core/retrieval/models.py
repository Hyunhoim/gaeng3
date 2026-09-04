from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentSourceKind(StrEnum):
    PROVIDED = "provided"
    EXTERNAL_APPROVED = "external_approved"


class DocumentInput(RetrievalModel):
    document_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)
    source_uri: str = Field(min_length=1, max_length=2000)
    source_kind: DocumentSourceKind
    as_of: date
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document text cannot be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if (
                not key
                or len(key) > 64
                or not key.replace("_", "").isalnum()
                or not item
                or len(item) > 500
            ):
                raise ValueError("metadata requires short alphanumeric keys and non-empty values")
        return value


class DocumentFilters(RetrievalModel):
    source_kinds: list[DocumentSourceKind] = Field(default_factory=list, max_length=2)
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    as_of_on_or_before: date | None = None
    metadata_equals: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_filters(self) -> DocumentFilters:
        if len(self.source_kinds) != len(set(self.source_kinds)):
            raise ValueError("source_kinds must be unique")
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("document_ids must be unique")
        DocumentInput.validate_metadata(self.metadata_equals)
        return self


class DocumentSearchRequest(RetrievalModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    filters: DocumentFilters = Field(default_factory=DocumentFilters)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query cannot be blank")
        return value


class DocumentEvidence(RetrievalModel):
    evidence_id: str
    document_id: str
    chunk_ordinal: int = Field(ge=0)
    title: str
    text: str
    source_uri: str
    source_kind: DocumentSourceKind
    as_of: date
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, str]
    relevance_score: float = Field(ge=0)


class DocumentSearchResponse(RetrievalModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["found", "not_found"]
    query: str
    evidence: list[DocumentEvidence]

    @model_validator(mode="after")
    def validate_status(self) -> DocumentSearchResponse:
        if (self.status == "found") != bool(self.evidence):
            raise ValueError("found status and evidence must agree")
        return self


class DocumentIngestionResult(RetrievalModel):
    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_count: int = Field(ge=1)
    inserted: bool
