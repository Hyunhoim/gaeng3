from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PRODUCT_FAMILIES = frozenset({"bond", "domestic_etp", "overseas_etp", "fund"})


class IndependentHybridContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@runtime_checkable
class HybridSubplanProtocol(Protocol):
    """Minimal surface shared by independently compiled subplans.

    The contract deliberately does not import either concrete plan type.  The
    caller supplies a plan whose ``question`` is the exact projected text
    owned by that subplan, not the parent question.
    """

    question_id: str
    question: str


def _jsonable(value: object) -> object:
    """Return a strict, deterministic JSON value for a plan snapshot."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("hybrid plan snapshot mappings require string keys")
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("hybrid plan snapshots forbid non-finite numbers")
        return value
    raise TypeError(f"unsupported hybrid plan snapshot value: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HybridSubplanSnapshot(IndependentHybridContractModel):
    """Canonical, type-neutral snapshot of one independently compiled plan."""

    schema_version: Literal["1.0"] = "1.0"
    subplan_kind: Literal["external_knowledge", "database_compound"]
    question_id: str = Field(min_length=1, max_length=128)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_plan_json: str = Field(min_length=2, max_length=1_000_000)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_snapshot(self) -> HybridSubplanSnapshot:
        try:
            payload = json.loads(self.canonical_plan_json)
        except (TypeError, ValueError) as error:
            raise ValueError("hybrid subplan snapshot is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("hybrid subplan snapshot must be a JSON object")
        if _canonical_json(payload) != self.canonical_plan_json:
            raise ValueError("hybrid subplan snapshot JSON is not canonical")
        if _sha256_text(self.canonical_plan_json) != self.plan_sha256:
            raise ValueError("hybrid subplan snapshot hash changed")
        if payload.get("question_id") != self.question_id:
            raise ValueError("hybrid subplan snapshot question ID changed")
        question = payload.get("question")
        if not isinstance(question, str) or _sha256_text(question) != self.question_sha256:
            raise ValueError("hybrid subplan snapshot question changed")
        return self


def canonical_subplan_snapshot(
    plan: HybridSubplanProtocol,
    *,
    subplan_kind: Literal["external_knowledge", "database_compound"],
) -> HybridSubplanSnapshot:
    """Snapshot a concrete plan without importing its defining module."""

    question_id = getattr(plan, "question_id", None)
    question = getattr(plan, "question", None)
    if not isinstance(question_id, str) or not isinstance(question, str):
        raise TypeError("hybrid subplans require string question_id and question fields")
    payload = _jsonable(plan)
    if not isinstance(payload, dict):
        raise TypeError("hybrid subplans must serialize to a JSON object")
    canonical_plan_json = _canonical_json(payload)
    return HybridSubplanSnapshot(
        subplan_kind=subplan_kind,
        question_id=question_id,
        question_sha256=_sha256_text(question),
        canonical_plan_json=canonical_plan_json,
        plan_sha256=_sha256_text(canonical_plan_json),
    )


class HybridSourceRange(IndependentHybridContractModel):
    """One exact, immutable source slice from the parent question."""

    source_start: int = Field(ge=0, le=1999)
    source_end: int = Field(ge=1, le=2000)
    source_text_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_offsets(self) -> HybridSourceRange:
        if self.source_start >= self.source_end:
            raise ValueError("hybrid source range is empty")
        return self

    @property
    def canonical_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.model_dump(mode="json")))


def _source_ranges(
    question: str,
    ranges: Sequence[tuple[int, int]],
) -> tuple[HybridSourceRange, ...]:
    built: list[HybridSourceRange] = []
    for start, end in ranges:
        if start < 0 or end > len(question) or start >= end:
            raise ValueError("hybrid source range is outside the parent question")
        built.append(
            HybridSourceRange(
                source_start=start,
                source_end=end,
                source_text_sha256=_sha256_text(question[start:end]),
            )
        )
    return tuple(built)


def _validate_ordered_ranges(
    label: str,
    ranges: Sequence[HybridSourceRange],
) -> None:
    previous_end = -1
    for source_range in ranges:
        if source_range.source_start < previous_end:
            raise ValueError(f"{label} source ranges must be ordered and non-overlapping")
        previous_end = source_range.source_end


def _projection_text(
    question: str,
    ranges: Sequence[HybridSourceRange],
) -> str:
    return " ".join(
        question[source_range.source_start : source_range.source_end].strip()
        for source_range in ranges
    )


class IndependentHybridSection(IndependentHybridContractModel):
    """Source ranges owned by one independently compiled subplan."""

    section_kind: Literal["external_evidence", "database_query"]
    source_ranges: tuple[HybridSourceRange, ...] = Field(min_length=1, max_length=4)
    subplan: HybridSubplanSnapshot

    @model_validator(mode="after")
    def validate_kind_and_ranges(self) -> IndependentHybridSection:
        expected_subplan_kind = (
            "external_knowledge"
            if self.section_kind == "external_evidence"
            else "database_compound"
        )
        if self.subplan.subplan_kind != expected_subplan_kind:
            raise ValueError("hybrid section and subplan kinds differ")
        _validate_ordered_ranges(self.section_kind, self.source_ranges)
        return self

    @property
    def canonical_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.model_dump(mode="json")))


class IndependentHybridPlan(IndependentHybridContractModel):
    """Independently compiled clauses with exhaustive source ownership."""

    schema_version: Literal["1.0"] = "1.0"
    plan_kind: Literal["independent_hybrid"] = "independent_hybrid"
    question_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    combination_policy: Literal["independent_sections"] = "independent_sections"
    combination_policy_ranges: tuple[HybridSourceRange, ...] = Field(
        min_length=1,
        max_length=4,
    )
    external_section: IndependentHybridSection
    database_section: IndependentHybridSection

    @classmethod
    def from_subplans(
        cls,
        *,
        question_id: str,
        question: str,
        external_source_ranges: Sequence[tuple[int, int]],
        external_plan: HybridSubplanProtocol,
        database_source_ranges: Sequence[tuple[int, int]],
        database_plan: HybridSubplanProtocol,
        combination_policy_ranges: Sequence[tuple[int, int]],
    ) -> IndependentHybridPlan:
        return cls(
            question_id=question_id,
            question=question,
            combination_policy_ranges=_source_ranges(
                question,
                combination_policy_ranges,
            ),
            external_section=IndependentHybridSection(
                section_kind="external_evidence",
                source_ranges=_source_ranges(question, external_source_ranges),
                subplan=canonical_subplan_snapshot(
                    external_plan,
                    subplan_kind="external_knowledge",
                ),
            ),
            database_section=IndependentHybridSection(
                section_kind="database_query",
                source_ranges=_source_ranges(question, database_source_ranges),
                subplan=canonical_subplan_snapshot(
                    database_plan,
                    subplan_kind="database_compound",
                ),
            ),
        )

    @model_validator(mode="after")
    def validate_independent_sections(self) -> IndependentHybridPlan:
        _validate_ordered_ranges(
            "combination policy",
            self.combination_policy_ranges,
        )
        owned_by: list[str | None] = [None] * len(self.question)
        groups = (
            ("external", self.external_section.source_ranges),
            ("database", self.database_section.source_ranges),
            ("policy", self.combination_policy_ranges),
        )
        for owner, ranges in groups:
            for source_range in ranges:
                if source_range.source_end > len(self.question):
                    raise ValueError("hybrid source range exceeds the parent question")
                source_text = self.question[source_range.source_start : source_range.source_end]
                if not any(character.isalnum() for character in source_text):
                    raise ValueError("hybrid source range has no meaningful text")
                if _sha256_text(source_text) != source_range.source_text_sha256:
                    raise ValueError("hybrid source range text changed")
                for index in range(source_range.source_start, source_range.source_end):
                    if owned_by[index] is not None:
                        raise ValueError("hybrid source ranges cannot overlap")
                    owned_by[index] = owner

        for index, character in enumerate(self.question):
            if character.isalnum() and owned_by[index] is None:
                raise ValueError("hybrid question contains unbound meaningful text")

        external_projection = _projection_text(
            self.question,
            self.external_section.source_ranges,
        )
        database_projection = _projection_text(
            self.question,
            self.database_section.source_ranges,
        )
        if self.external_section.subplan.question_id != self.question_id:
            raise ValueError("hybrid external subplan question ID differs from the parent")
        if self.database_section.subplan.question_id != self.question_id:
            raise ValueError("hybrid database subplan question ID differs from the parent")
        if self.external_section.subplan.question_sha256 != _sha256_text(external_projection):
            raise ValueError("hybrid external subplan question differs from its projection")
        if self.database_section.subplan.question_sha256 != _sha256_text(database_projection):
            raise ValueError("hybrid database subplan question differs from its projection")
        return self

    @property
    def question_sha256(self) -> str:
        return _sha256_text(self.question)

    @property
    def combination_policy_ranges_sha256(self) -> str:
        return _sha256_text(
            _canonical_json(
                [
                    source_range.model_dump(mode="json")
                    for source_range in self.combination_policy_ranges
                ]
            )
        )

    @property
    def canonical_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.model_dump(mode="json")))


class HybridDatabaseBinding(IndependentHybridContractModel):
    product_family: Literal["bond", "domestic_etp", "overseas_etp", "fund"]
    database_sha256: str = Field(pattern=_SHA256_PATTERN)


class HybridAuthorityReceipt(IndependentHybridContractModel):
    """HMAC-bound authority for one exact hybrid plan and release context."""

    schema_version: Literal["1.0"] = "1.0"
    authority_kind: Literal["server_authorized_independent_hybrid"] = (
        "server_authorized_independent_hybrid"
    )
    question_id: str = Field(min_length=1, max_length=128)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    combination_policy: Literal["independent_sections"] = "independent_sections"
    combination_policy_ranges_sha256: str = Field(pattern=_SHA256_PATTERN)
    hybrid_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_section_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_section_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    release_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_bindings: tuple[HybridDatabaseBinding, ...] = Field(
        min_length=1,
        max_length=4,
    )
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_mac: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_database_bindings(self) -> HybridAuthorityReceipt:
        families = [item.product_family for item in self.database_bindings]
        if families != sorted(families) or len(families) != len(set(families)):
            raise ValueError("hybrid database bindings must be unique and sorted")
        if not set(families) <= _PRODUCT_FAMILIES:
            raise ValueError("hybrid database binding has an unknown product family")
        return self

    @staticmethod
    def _require_authority_key(authority_key: bytes) -> None:
        if not isinstance(authority_key, bytes) or len(authority_key) < 32:
            raise ValueError("hybrid authority key must contain at least 32 bytes")

    @staticmethod
    def _database_bindings(
        database_sha256s: Mapping[str, str],
    ) -> tuple[HybridDatabaseBinding, ...]:
        if not database_sha256s:
            raise ValueError("hybrid authority requires at least one database hash")
        return tuple(
            HybridDatabaseBinding(
                product_family=family,  # type: ignore[arg-type]
                database_sha256=digest,
            )
            for family, digest in sorted(database_sha256s.items())
        )

    @classmethod
    def _binding_payload(
        cls,
        *,
        plan: IndependentHybridPlan,
        release_manifest_sha256: str,
        database_bindings: Sequence[HybridDatabaseBinding],
        source_registry_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "authority_kind": "server_authorized_independent_hybrid",
            "question_id": plan.question_id,
            "question_sha256": plan.question_sha256,
            "combination_policy": plan.combination_policy,
            "combination_policy_ranges_sha256": (plan.combination_policy_ranges_sha256),
            "hybrid_plan_sha256": plan.canonical_sha256,
            "external_section_sha256": plan.external_section.canonical_sha256,
            "database_section_sha256": plan.database_section.canonical_sha256,
            "external_plan_sha256": plan.external_section.subplan.plan_sha256,
            "database_plan_sha256": plan.database_section.subplan.plan_sha256,
            "release_manifest_sha256": release_manifest_sha256,
            "database_bindings": [item.model_dump(mode="json") for item in database_bindings],
            "source_registry_sha256": source_registry_sha256,
        }

    @classmethod
    def bind(
        cls,
        *,
        plan: IndependentHybridPlan,
        release_manifest_sha256: str,
        database_sha256s: Mapping[str, str],
        source_registry_sha256: str,
        authority_key: bytes,
    ) -> HybridAuthorityReceipt:
        cls._require_authority_key(authority_key)
        database_bindings = cls._database_bindings(database_sha256s)
        payload = cls._binding_payload(
            plan=plan,
            release_manifest_sha256=release_manifest_sha256,
            database_bindings=database_bindings,
            source_registry_sha256=source_registry_sha256,
        )
        binding_sha256 = _sha256_text(_canonical_json(payload))
        authority_mac = hmac.new(
            authority_key,
            _canonical_json({**payload, "binding_sha256": binding_sha256}).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return cls(
            **payload,
            binding_sha256=binding_sha256,
            authority_mac=authority_mac,
        )

    def verify(
        self,
        *,
        plan: IndependentHybridPlan,
        release_manifest_sha256: str,
        database_sha256s: Mapping[str, str],
        source_registry_sha256: str,
        authority_key: bytes,
    ) -> None:
        """Raise on any changed range, subplan or release input."""

        self._require_authority_key(authority_key)
        database_bindings = self._database_bindings(database_sha256s)
        payload = self._binding_payload(
            plan=plan,
            release_manifest_sha256=release_manifest_sha256,
            database_bindings=database_bindings,
            source_registry_sha256=source_registry_sha256,
        )
        expected_binding = _sha256_text(_canonical_json(payload))
        expected_fields = {
            **payload,
            "binding_sha256": expected_binding,
        }
        actual_fields = self.model_dump(mode="json", exclude={"authority_mac"})
        if _canonical_json(actual_fields) != _canonical_json(expected_fields):
            raise ValueError("hybrid authority receipt binding changed")
        expected_mac = hmac.new(
            authority_key,
            _canonical_json(expected_fields).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(self.authority_mac, expected_mac):
            raise ValueError("hybrid authority receipt MAC is invalid")
