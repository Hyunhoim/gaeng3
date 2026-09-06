from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from finance_agent_core.agent.compound_query import (
    CompoundAuthorizationReceipt,
    CompoundExecution,
    TypedCompoundPlan,
    _compound_plan_sha256,
)
from finance_agent_core.agent.intent_analysis import has_external_knowledge_surface
from finance_agent_core.contracts.external_knowledge import (
    ExternalKnowledgeOutcomeStatus,
    ExternalKnowledgeSearchPlan,
)
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntimeResult,
)


class HybridClauseOwner(StrEnum):
    EXTERNAL_EVIDENCE = "external_evidence"
    DATABASE_COMPOUND = "database_compound"
    CONTROL = "control"


class HybridPreflightCode(StrEnum):
    NOT_APPLICABLE = "hybrid_not_applicable"
    CLAUSE_BOUNDARY_AMBIGUOUS = "hybrid_clause_boundary_ambiguous"
    CLAUSE_OWNERSHIP_INCOMPLETE = "hybrid_clause_ownership_incomplete"
    INTEGRATED_RANKING_FORBIDDEN = "hybrid_integrated_ranking_forbidden"
    EXTERNAL_AUTHORITY_MISSING = "hybrid_external_authority_missing"
    DATABASE_AUTHORITY_MISSING = "hybrid_database_authority_missing"
    FAMILY_SCOPE_OVERLAP = "hybrid_family_scope_overlap"


class HybridPreflightBlocked(ValueError):
    """Safe control signal emitted before either read-only executor is called."""

    def __init__(self, code: HybridPreflightCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class HybridExecutionAborted(RuntimeError):
    """No hybrid result may escape when one independently verified section fails."""

    def __init__(self, reason_code: str, *, cause: Exception | None = None) -> None:
        self.reason_code = reason_code
        # Preserve the infrastructure cause for the outer HTTP boundary.  A
        # timeout, release mismatch, DB failure, or provider outage must keep
        # its retry/status semantics instead of being flattened to a generic
        # non-retryable 500.  The exception still carries no partial result.
        self.cause = cause
        super().__init__(reason_code)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HybridSourceSpan:
    owner: HybridClauseOwner
    start: int
    end: int
    text: str
    text_sha256: str

    @classmethod
    def from_question(
        cls,
        question: str,
        *,
        owner: HybridClauseOwner,
        start: int,
        end: int,
    ) -> HybridSourceSpan:
        if start < 0 or end <= start or end > len(question):
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid source span is outside the original question",
            )
        text = question[start:end]
        return cls(
            owner=owner,
            start=start,
            end=end,
            text=text,
            text_sha256=_sha256_text(text),
        )


@dataclass(frozen=True, slots=True)
class HybridClauseProjection:
    owner: Literal[
        HybridClauseOwner.EXTERNAL_EVIDENCE,
        HybridClauseOwner.DATABASE_COMPOUND,
    ]
    source_spans: tuple[HybridSourceSpan, ...]
    question: str
    question_sha256: str

    @classmethod
    def from_spans(
        cls,
        *,
        owner: Literal[
            HybridClauseOwner.EXTERNAL_EVIDENCE,
            HybridClauseOwner.DATABASE_COMPOUND,
        ],
        spans: tuple[HybridSourceSpan, ...],
    ) -> HybridClauseProjection:
        if not spans or any(span.owner is not owner for span in spans):
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid execution clause has no uniquely owned source span",
            )
        question = " ".join(span.text.strip() for span in spans if span.text.strip())
        if not question:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid execution clause is blank",
            )
        return cls(
            owner=owner,
            source_spans=spans,
            question=question,
            question_sha256=_sha256_text(question),
        )


@dataclass(frozen=True, slots=True)
class HybridClausePartition:
    question: str
    question_sha256: str
    spans: tuple[HybridSourceSpan, ...]
    external: HybridClauseProjection
    database: HybridClauseProjection

    def validate(self) -> None:
        if _sha256_text(self.question) != self.question_sha256:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid question hash changed",
            )
        cursor = 0
        for span in self.spans:
            if span.start != cursor or span.end <= span.start:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid source spans overlap or leave an unowned gap",
                )
            if self.question[span.start : span.end] != span.text:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid source span no longer matches the original question",
                )
            if _sha256_text(span.text) != span.text_sha256:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid source span hash changed",
                )
            cursor = span.end
        if cursor != len(self.question):
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid source spans do not cover the complete question",
            )
        for projection in (self.external, self.database):
            expected = tuple(span for span in self.spans if span.owner is projection.owner)
            if projection.source_spans != expected:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid projection differs from its uniquely owned spans",
                )
            rebuilt = HybridClauseProjection.from_spans(
                owner=projection.owner,
                spans=expected,
            )
            if rebuilt != projection:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid projection text or hash changed",
                )


_INDEPENDENT_BOUNDARY = re.compile(
    r"(?:별도\s*(?:의\s*)?(?:비교군|검색군|결과군)(?:으로|은|에서는|에서)?|"
    r"(?:그|이)와\s*별개로)",
    re.IGNORECASE,
)
_NO_GLOBAL_MERGE = re.compile(
    r"(?:통합\s*(?:순위|점수)(?:를|로)?\s*"
    r"(?:만들\s*지|만들지|않|금지|제외)|"
    r"(?:하나의\s*(?:점수|순위)(?:로)?\s*)?(?:합치지|섞지)|"
    r"각각\s*(?:계산|검색|정리)|"
    r"별도\s*(?:의\s*)?(?:비교군|검색군|결과군))",
    re.IGNORECASE,
)
_NO_GLOBAL_POLICY = re.compile(
    r"(?:(?:두\s*결과|각\s*(?:상품군|결과)|"
    r"(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP|펀드|채권))"
    r"(?:를|은|는|와|과)?\s*)?"
    r"(?:통합\s*(?:순위|점수)[^.!?]{0,40}"
    r"(?:만들\s*지|만들지|합치지|섞지|않)|"
    r"(?:하나의\s*(?:점수|순위)(?:로)?\s*)?(?:합치지|섞지))"
    r"[^.!?]*(?:[.!?]|$)",
    re.IGNORECASE,
)
_POSITIVE_GLOBAL_RANKING = re.compile(
    r"(?:통합\s*(?:순위|점수)|"
    r"(?:하나로|한데|함께)\s*(?:합쳐|섞어).{0,20}(?:상위|순위)|"
    r"(?:전체|모두)\s*(?:상품)?\s*(?:통합)?\s*순위)",
    re.IGNORECASE,
)
_TRAILING_EXTERNAL_PRESENTATION = re.compile(
    r"(?:자료|문서|출처|공식\s*근거|발행일|기준일|손실\s*(?:관련\s*)?유의|위험\s*요인)",
    re.IGNORECASE,
)
_EXTERNAL_EXECUTION = re.compile(
    r"(?:편입|보유|구성\s*종목|담(?:은|긴|고\s*있는)|포함(?:된|한)|"
    r"테마|섹터|자회사|계열사|위험\s*요인|손실.{0,12}유의|"
    r"투자\s*전략|운용\s*(?:동향|현황))",
    re.IGNORECASE,
)
_OFFICIAL_DOCUMENT_SOURCE = re.compile(
    r"(?:공식\s*)?(?:자료|문서|가이드|보고서|공시|브로슈어|운용\s*보고서)",
    re.IGNORECASE,
)
_DOCUMENT_LOOKUP_ACTION = re.compile(
    r"(?:확인|검증|대조|검색|찾아?|살펴|알려|설명|정리)",
    re.IGNORECASE,
)
_DOCUMENT_RELATION_DETAIL = re.compile(
    r"(?:같은\s*(?:자료|문서)|상위\s*\d{1,3}\s*개|비중(?:\s*합계)?|"
    r"구성\s*종목|편입\s*(?:내역|종목|표)|투자\s*전략|"
    r"(?:테마|섹터).{0,16}(?:연결|연관|관련|이력)|위험\s*요인)",
    re.IGNORECASE,
)
_DATABASE_EXECUTION = re.compile(
    r"(?:이상|이하|초과|미만|큰\s*순|작은\s*순|높은\s*순|낮은\s*순|"
    r"상위\s*\d+\s*개|먼저\s*추|후보|계산\s*입력값|기준점)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r".*?(?:[.!?](?=\s|$)|$)", re.DOTALL)


def _contains_positive_global_ranking(question: str) -> bool:
    masked = list(question)
    negative_matches = (
        *_NO_GLOBAL_MERGE.finditer(question),
        *_NO_GLOBAL_POLICY.finditer(question),
    )
    for match in negative_matches:
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return _POSITIVE_GLOBAL_RANKING.search("".join(masked)) is not None


def _has_external_clause_signal(text: str) -> bool:
    """Recognize evidence work without treating a generic product name as enough.

    Some official-document questions describe a table instead of using a
    canonical relation verb (for example ``같은 자료의 상위 10개 ... 비중
    합계 검증``).  Requiring a source, a lookup action, and document content
    keeps those clauses external without widening every ``... ETF`` mention.
    """

    typed_relation = (
        has_external_knowledge_surface(text)
        and _EXTERNAL_EXECUTION.search(text) is not None
    )
    document_evidence = all(
        pattern.search(text) is not None
        for pattern in (
            _OFFICIAL_DOCUMENT_SOURCE,
            _DOCUMENT_LOOKUP_ACTION,
            _DOCUMENT_RELATION_DETAIL,
        )
    )
    return typed_relation or document_evidence


def _validate_external_span_hints(
    question: str,
    hints: tuple[tuple[int, int], ...],
    *,
    boundary_start: int,
    boundary_end: int,
) -> tuple[tuple[int, int], ...]:
    """Validate server-derived literal spans used only as ownership hints.

    Hints do not grant retrieval authority.  They only let an already
    sanitized requirement tell the partitioner which independent clause owns
    external evidence.  A hint that crosses the control boundary is rejected
    rather than guessed.
    """

    ordered = tuple(sorted(hints))
    previous_end = -1
    for start, end in ordered:
        if start < 0 or end <= start or end > len(question) or start < previous_end:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "external span hint is invalid or overlaps another hint",
            )
        if start < boundary_end and end > boundary_start:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "external span hint crosses the independent-clause boundary",
            )
        previous_end = end
    return ordered


def _range_has_external_hint(
    hints: tuple[tuple[int, int], ...],
    start: int,
    end: int,
) -> bool:
    return any(hint_start >= start and hint_end <= end for hint_start, hint_end in hints)


def _sentence_ranges(question: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for match in _SENTENCE.finditer(question, start, end):
        segment_start, segment_end = match.span()
        if segment_start >= end:
            break
        segment_end = min(segment_end, end)
        if question[segment_start:segment_end].strip():
            ranges.append((segment_start, segment_end))
    return tuple(ranges)


def _carve_control_policies(
    question: str,
    owners: list[tuple[int, int, HybridClauseOwner]],
) -> list[tuple[int, int, HybridClauseOwner]]:
    """Remove explicit no-merge policy text from executable projections.

    A suffix such as ``ETF와 통합 순위를 만들지는 말아 주세요`` names a
    product family only to prohibit a cross-section operation.  Leaving it in
    the DB clause can make the compound parser widen the requested family set.
    Exact offset carving keeps that policy auditable without granting it query
    semantics.
    """

    policy_ranges = tuple(match.span() for match in _NO_GLOBAL_POLICY.finditer(question))
    if not policy_ranges:
        return owners
    carved: list[tuple[int, int, HybridClauseOwner]] = []
    for start, end, owner in owners:
        cursor = start
        for policy_start, policy_end in policy_ranges:
            if policy_end <= start or policy_start >= end:
                continue
            if policy_start < start or policy_end > end:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "independent-result policy crosses a hybrid clause boundary",
                )
            if cursor < policy_start:
                carved.append((cursor, policy_start, owner))
            carved.append(
                (policy_start, policy_end, HybridClauseOwner.CONTROL)
            )
            cursor = policy_end
        if cursor < end:
            carved.append((cursor, end, owner))
    return carved


class HybridQueryCoordinator:
    """Preflight independent external-evidence and DB-compound clauses atomically.

    This coordinator never executes a search.  It returns one receipt only
    after both existing authorizers have produced question-bound authority.
    The public service can then call :meth:`execute_atomic`, which exposes no
    partial result if either read-only sub-execution fails.
    """

    def partition(
        self,
        question: str,
        *,
        external_span_hints: tuple[tuple[int, int], ...] = (),
    ) -> HybridClausePartition | None:
        boundaries = tuple(_INDEPENDENT_BOUNDARY.finditer(question))
        if not boundaries:
            return None
        if len(boundaries) != 1:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_BOUNDARY_AMBIGUOUS,
                "hybrid request has more than one independent-clause boundary",
            )
        if _contains_positive_global_ranking(question):
            raise HybridPreflightBlocked(
                HybridPreflightCode.INTEGRATED_RANKING_FORBIDDEN,
                "independent external and DB sections cannot share a global ranking",
            )
        if _NO_GLOBAL_MERGE.search(question) is None:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_BOUNDARY_AMBIGUOUS,
                "hybrid request does not explicitly preserve independent results",
            )

        boundary = boundaries[0]
        external_span_hints = _validate_external_span_hints(
            question,
            external_span_hints,
            boundary_start=boundary.start(),
            boundary_end=boundary.end(),
        )
        left = question[: boundary.start()]
        right_ranges = _sentence_ranges(question, boundary.end(), len(question))
        if not left.strip() or not right_ranges:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid request is missing one side of its clause boundary",
            )

        left_is_external = _has_external_clause_signal(left) or _range_has_external_hint(
            external_span_hints,
            0,
            boundary.start(),
        )
        first_right_start, first_right_end = right_ranges[0]
        first_right = question[first_right_start:first_right_end]
        right_starts_external = _has_external_clause_signal(
            first_right
        ) or _range_has_external_hint(
            external_span_hints,
            first_right_start,
            first_right_end,
        )
        if left_is_external == right_starts_external:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_BOUNDARY_AMBIGUOUS,
                "exactly one side of a hybrid boundary must start as external evidence",
            )

        owners: list[tuple[int, int, HybridClauseOwner]] = []
        if left_is_external:
            owners.append((0, boundary.start(), HybridClauseOwner.EXTERNAL_EVIDENCE))
            owners.append((boundary.start(), boundary.end(), HybridClauseOwner.CONTROL))
            for index, (start, end) in enumerate(right_ranges):
                text = question[start:end]
                is_external_tail = (
                    index > 0
                    and (
                        _has_external_clause_signal(text)
                        or _range_has_external_hint(external_span_hints, start, end)
                    )
                    and (
                        _TRAILING_EXTERNAL_PRESENTATION.search(text) is not None
                        or _range_has_external_hint(external_span_hints, start, end)
                    )
                    and _DATABASE_EXECUTION.search(text) is None
                )
                owners.append(
                    (
                        start,
                        end,
                        HybridClauseOwner.EXTERNAL_EVIDENCE
                        if is_external_tail
                        else HybridClauseOwner.DATABASE_COMPOUND,
                    )
                )
        else:
            owners.append((0, boundary.start(), HybridClauseOwner.DATABASE_COMPOUND))
            owners.append((boundary.start(), boundary.end(), HybridClauseOwner.CONTROL))
            owners.append(
                (boundary.end(), len(question), HybridClauseOwner.EXTERNAL_EVIDENCE)
            )

        owners = _carve_control_policies(question, owners)

        # Sentence matching may leave inter-sentence whitespace outside the
        # returned ranges.  Assign each gap to the following span's owner so
        # every byte remains uniquely owned and the source hashes are stable.
        normalized: list[tuple[int, int, HybridClauseOwner]] = []
        cursor = 0
        for start, end, owner in sorted(owners, key=lambda item: item[0]):
            if start > cursor:
                start = cursor
            if start != cursor or end <= start:
                raise HybridPreflightBlocked(
                    HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                    "hybrid clause ranges overlap or leave an unowned span",
                )
            normalized.append((start, end, owner))
            cursor = end
        if cursor != len(question):
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid clause ranges do not cover the complete question",
            )

        spans = tuple(
            HybridSourceSpan.from_question(
                question,
                owner=owner,
                start=start,
                end=end,
            )
            for start, end, owner in normalized
        )
        partition = HybridClausePartition(
            question=question,
            question_sha256=_sha256_text(question),
            spans=spans,
            external=HybridClauseProjection.from_spans(
                owner=HybridClauseOwner.EXTERNAL_EVIDENCE,
                spans=tuple(
                    span
                    for span in spans
                    if span.owner is HybridClauseOwner.EXTERNAL_EVIDENCE
                ),
            ),
            database=HybridClauseProjection.from_spans(
                owner=HybridClauseOwner.DATABASE_COMPOUND,
                spans=tuple(
                    span
                    for span in spans
                    if span.owner is HybridClauseOwner.DATABASE_COMPOUND
                ),
            ),
        )
        partition.validate()
        return partition

    def preflight(
        self,
        question: str,
        request_id: str,
        *,
        authorize_external: Callable[
            [HybridClauseProjection, str], ExternalKnowledgeSearchPlan
        ],
        authorize_database: Callable[
            [HybridClauseProjection, str],
            tuple[TypedCompoundPlan, CompoundAuthorizationReceipt],
        ],
        external_span_hints: tuple[tuple[int, int], ...] = (),
    ) -> HybridPreflightReceipt | None:
        partition = self.partition(
            question,
            external_span_hints=external_span_hints,
        )
        if partition is None:
            return None

        # Callbacks must convert only a genuine semantic/plan rejection into
        # ``HybridPreflightBlocked``.  Infrastructure exceptions deliberately
        # propagate so the public boundary can return 503/504 and the evaluator
        # can apply its retry policy.
        external_plan = authorize_external(partition.external, request_id)
        self._validate_external_authority(
            partition.external,
            request_id,
            external_plan,
        )

        database_plan, database_receipt = authorize_database(
            partition.database,
            request_id,
        )
        self._validate_database_authority(
            partition.database,
            request_id,
            database_plan,
            database_receipt,
        )

        external_families = {
            family
            for requirement in external_plan.composite_plan.requirements
            for family in requirement.product_families
        }
        database_families = set(database_plan.product_families)
        if not external_families or not database_families:
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid subplans must each own a concrete product family",
            )
        if external_families & database_families:
            raise HybridPreflightBlocked(
                HybridPreflightCode.FAMILY_SCOPE_OVERLAP,
                "external and independent DB clauses cannot own the same product family",
            )
        if database_plan.kind == "global_ranking":
            raise HybridPreflightBlocked(
                HybridPreflightCode.INTEGRATED_RANKING_FORBIDDEN,
                "hybrid DB section cannot carry a cross-section global ranking",
            )

        receipt = HybridPreflightReceipt(
            request_id=request_id,
            question_sha256=partition.question_sha256,
            partition=partition,
            external_plan=external_plan,
            database_plan=database_plan,
            database_receipt=database_receipt,
            external_plan_sha256=_sha256_text(external_plan.model_dump_json()),
            database_plan_sha256=_compound_plan_sha256(database_plan),
        )
        self._validate_receipt(receipt)
        return receipt

    def execute_atomic(
        self,
        receipt: HybridPreflightReceipt,
        *,
        execute_external: Callable[
            [ExternalKnowledgeSearchPlan], ExternalKnowledgeRuntimeResult
        ],
        execute_database: Callable[
            [TypedCompoundPlan, CompoundAuthorizationReceipt], CompoundExecution
        ],
    ) -> HybridAtomicExecution:
        """Return both complete sections or no result object at all."""

        self._validate_receipt(receipt)
        try:
            external = execute_external(receipt.external_plan)
        except Exception as error:
            raise HybridExecutionAborted(
                "hybrid_external_execution_failed",
                cause=error,
            ) from error
        if external.search_plan != receipt.external_plan:
            raise HybridExecutionAborted("hybrid_external_plan_binding_changed")
        if external.outcome.status not in {
            ExternalKnowledgeOutcomeStatus.SUCCESS,
            ExternalKnowledgeOutcomeStatus.NOT_FOUND,
        }:
            # Do not execute the DB half when the evidence half is incomplete.
            raise HybridExecutionAborted("hybrid_external_evidence_incomplete")

        try:
            database = execute_database(
                receipt.database_plan,
                receipt.database_receipt,
            )
        except Exception as error:
            # The verified external value is intentionally not attached to the
            # exception, so the caller cannot accidentally serialize it.
            raise HybridExecutionAborted(
                "hybrid_database_execution_failed",
                cause=error,
            ) from error
        if database.plan != receipt.database_plan:
            raise HybridExecutionAborted("hybrid_database_plan_binding_changed")
        return HybridAtomicExecution(
            preflight=receipt,
            external=external,
            database=database,
        )

    @staticmethod
    def _validate_external_authority(
        projection: HybridClauseProjection,
        request_id: str,
        plan: ExternalKnowledgeSearchPlan,
    ) -> None:
        composite = plan.composite_plan
        if (
            composite.question_id != request_id
            or composite.question != projection.question
            or not composite.execution_authorized
            or plan.execution_receipt is None
        ):
            raise HybridPreflightBlocked(
                HybridPreflightCode.EXTERNAL_AUTHORITY_MISSING,
                "external authority is not bound to the exact owned clause",
            )

    @staticmethod
    def _validate_database_authority(
        projection: HybridClauseProjection,
        request_id: str,
        plan: TypedCompoundPlan,
        receipt: CompoundAuthorizationReceipt,
    ) -> None:
        if (
            plan.question_id != request_id
            or plan.question != projection.question
            or receipt.request_id != request_id
            or receipt.question_sha256 != projection.question_sha256
            or receipt.plan_sha256 != _compound_plan_sha256(plan)
            or not receipt.authority_mac
        ):
            raise HybridPreflightBlocked(
                HybridPreflightCode.DATABASE_AUTHORITY_MISSING,
                "compound authority is not bound to the exact owned clause",
            )

    def _validate_receipt(self, receipt: HybridPreflightReceipt) -> None:
        receipt.partition.validate()
        if (
            receipt.question_sha256 != receipt.partition.question_sha256
            or receipt.external_plan_sha256
            != _sha256_text(receipt.external_plan.model_dump_json())
            or receipt.database_plan_sha256
            != _compound_plan_sha256(receipt.database_plan)
        ):
            raise HybridPreflightBlocked(
                HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE,
                "hybrid preflight receipt changed after authorization",
            )
        self._validate_external_authority(
            receipt.partition.external,
            receipt.request_id,
            receipt.external_plan,
        )
        self._validate_database_authority(
            receipt.partition.database,
            receipt.request_id,
            receipt.database_plan,
            receipt.database_receipt,
        )


@dataclass(frozen=True, slots=True)
class HybridPreflightReceipt:
    request_id: str
    question_sha256: str
    partition: HybridClausePartition
    external_plan: ExternalKnowledgeSearchPlan
    database_plan: TypedCompoundPlan
    database_receipt: CompoundAuthorizationReceipt
    external_plan_sha256: str
    database_plan_sha256: str


@dataclass(frozen=True, slots=True)
class HybridAtomicExecution:
    preflight: HybridPreflightReceipt
    external: ExternalKnowledgeRuntimeResult
    database: CompoundExecution


__all__ = [
    "HybridAtomicExecution",
    "HybridClauseOwner",
    "HybridClausePartition",
    "HybridClauseProjection",
    "HybridExecutionAborted",
    "HybridPreflightBlocked",
    "HybridPreflightCode",
    "HybridPreflightReceipt",
    "HybridQueryCoordinator",
    "HybridSourceSpan",
]
