from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from finance_agent_core.retrieval.open_dart import (
    OPEN_DART_EVIDENCE_CUTOFF,
    MaterializedOpenDartPendingCorpus,
    OpenDartCollectionError,
    OpenDartDisclosureListQuery,
    OpenDartEntityScopeReference,
    OpenDartEntitySeed,
    OpenDartEntitySelectionReport,
    OpenDartOriginalDocumentUnavailable,
    OpenDartTransport,
    collect_open_dart_corp_codes,
    collect_open_dart_disclosure_list,
    collect_open_dart_original_document,
    load_open_dart_filing_whitelist,
    materialize_open_dart_pending_corpus,
    select_open_dart_disclosures,
    select_open_dart_entities,
)
from finance_agent_core.retrieval.open_dart_scope import (
    OpenDartProvidedRelationScope,
    OpenDartScopeError,
    build_open_dart_scope_from_provided_relations,
)
from finance_agent_core.retrieval.open_dart_transport import (
    UrllibOpenDartTransport,
    load_open_dart_api_key,
)

_MAX_SEED_FILE_BYTES = 2 * 1024 * 1024
_MAX_ENTITY_SEEDS = 500
_MAX_SELECTED_DOCUMENTS = 1000
_ReviewedSeedKind = Literal["open_dart_reviewed_entity_seeds"]


class OpenDartReviewedEntitySeeds(BaseModel):
    """Human-reviewed, non-authoritative entity seeds for offline collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    artifact_kind: _ReviewedSeedKind = "open_dart_reviewed_entity_seeds"
    review_status: Literal["reviewed_for_open_dart_collection"]
    evidence_cutoff: date
    seeds: tuple[OpenDartEntitySeed, ...] = Field(
        min_length=1,
        max_length=_MAX_ENTITY_SEEDS,
    )

    @model_validator(mode="after")
    def validate_reviewed_seeds(self) -> OpenDartReviewedEntitySeeds:
        if self.evidence_cutoff != OPEN_DART_EVIDENCE_CUTOFF:
            raise ValueError("Open DART entity seeds use an unexpected evidence cutoff")
        entity_ids = [item.entity_id for item in self.seeds]
        if entity_ids != sorted(entity_ids) or len(entity_ids) != len(set(entity_ids)):
            raise ValueError("Open DART entity seeds must be unique and sorted")
        return self


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_reviewed_seed_file(path: Path) -> bytes:
    if path.is_symlink():
        raise OpenDartCollectionError("Open DART entity seed file must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OpenDartCollectionError("Open DART entity seed file is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OpenDartCollectionError("Open DART entity seeds must use a regular file")
        if before.st_mode & 0o022:
            raise OpenDartCollectionError(
                "Open DART entity seed file must not be group/world writable"
            )
        if not 0 < before.st_size <= _MAX_SEED_FILE_BYTES:
            raise OpenDartCollectionError("Open DART entity seed file size is outside the bound")
        chunks: list[bytes] = []
        remaining = _MAX_SEED_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or len(data) != before.st_size:
            raise OpenDartCollectionError("Open DART entity seed file changed while being read")
        return data
    finally:
        os.close(descriptor)


def load_reviewed_open_dart_entity_seeds(path: str | Path) -> OpenDartReviewedEntitySeeds:
    data = _read_reviewed_seed_file(Path(path))

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise OpenDartCollectionError("Open DART entity seed JSON contains duplicate keys")
            payload[key] = value
        return payload

    def reject_constant(_value: str) -> None:
        raise OpenDartCollectionError("Open DART entity seed JSON contains a non-finite number")

    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        return OpenDartReviewedEntitySeeds.model_validate(payload)
    except UnicodeDecodeError:
        raise OpenDartCollectionError("Open DART entity seed file must use UTF-8") from None
    except json.JSONDecodeError:
        raise OpenDartCollectionError("Open DART entity seed file is not valid JSON") from None
    except ValidationError:
        raise OpenDartCollectionError("Open DART entity seed file violates its schema") from None


def collect_open_dart_pending_from_seeds(
    *,
    reviewed_seeds: OpenDartReviewedEntitySeeds,
    transport: OpenDartTransport,
    output_root: str | Path,
    begin_date: date,
    collected_at_utc: datetime,
    max_documents: int = 500,
) -> MaterializedOpenDartPendingCorpus:
    """Run the existing collector and stop at its non-authoritative pending boundary."""

    return _collect_open_dart_pending_from_entities(
        entity_seeds=reviewed_seeds.seeds,
        transport=transport,
        output_root=output_root,
        begin_date=begin_date,
        collected_at_utc=collected_at_utc,
        max_documents=max_documents,
        require_all_entities=True,
    )


def _scope_reference(
    scope: OpenDartProvidedRelationScope,
    selection: OpenDartEntitySelectionReport,
) -> OpenDartEntityScopeReference:
    return OpenDartEntityScopeReference(
        source_relation_index_sha256=scope.source_relation_index_sha256,
        source_relation_manifest_sha256=scope.source_relation_manifest_sha256,
        source_relation_set_sha256=scope.source_relation_set_sha256,
        scope_set_sha256=scope.scope_set_sha256,
        seed_set_sha256=selection.seed_set_sha256,
        source_relation_count=scope.source_relation_count,
        eligible_relation_count=scope.eligible_relation_count,
        eligible_entity_count=scope.eligible_entity_count,
        selected_entity_count=len(scope.seeds),
        selected_product_count=scope.selected_product_count,
        managed_by_review_required_count=scope.managed_by_review_required_count,
        truncated=scope.truncated,
    )


def _collect_open_dart_pending_from_entities(
    *,
    entity_seeds: tuple[OpenDartEntitySeed, ...],
    transport: OpenDartTransport,
    output_root: str | Path,
    begin_date: date,
    collected_at_utc: datetime,
    max_documents: int,
    require_all_entities: bool,
    provided_scope: OpenDartProvidedRelationScope | None = None,
) -> MaterializedOpenDartPendingCorpus:
    """Collect exact-bound entities while preserving unresolved coverage evidence."""

    if begin_date > OPEN_DART_EVIDENCE_CUTOFF:
        raise OpenDartCollectionError("Open DART begin date follows the fixed evidence cutoff")
    if not 1 <= max_documents <= _MAX_SELECTED_DOCUMENTS:
        raise OpenDartCollectionError("Open DART document budget is outside the safety bound")
    if collected_at_utc.tzinfo is None or collected_at_utc.utcoffset() != UTC.utcoffset(
        collected_at_utc
    ):
        raise OpenDartCollectionError("Open DART collection timestamp must use UTC")

    corp_codes = collect_open_dart_corp_codes(
        transport,
        collected_at_utc=collected_at_utc,
    )
    entity_selection = select_open_dart_entities(corp_codes.snapshot, entity_seeds)
    if require_all_entities and (
        entity_selection.unmatched_entity_ids or entity_selection.ambiguous_entity_ids
    ):
        raise OpenDartCollectionError(
            "Open DART reviewed entities did not resolve uniquely; collection was not materialized"
        )
    if not entity_selection.bindings:
        raise OpenDartCollectionError(
            "Open DART entity scope produced no exact corporation binding"
        )
    corporation_codes = tuple(sorted({item.corp_code for item in entity_selection.bindings}))
    disclosure_lists = tuple(
        collect_open_dart_disclosure_list(
            transport,
            OpenDartDisclosureListQuery(
                corp_code=corp_code,
                bgn_de=begin_date,
                end_de=OPEN_DART_EVIDENCE_CUTOFF,
                page_count=100,
            ),
            collected_at_utc=collected_at_utc,
        )
        for corp_code in corporation_codes
    )
    policy = load_open_dart_filing_whitelist()
    selected = select_open_dart_disclosures(
        tuple(item.snapshot for item in disclosure_lists),
        policy,
        skip_unresolved_correction_lineages=not require_all_entities,
    )
    if not selected:
        raise OpenDartCollectionError("Open DART whitelist selected no documents")
    if len(selected) > max_documents:
        raise OpenDartCollectionError("Open DART whitelist exceeded the document budget")
    documents = []
    unavailable_receipts: list[str] = []
    for item in selected:
        try:
            documents.append(
                collect_open_dart_original_document(
                    transport,
                    item,
                    collected_at_utc=collected_at_utc,
                )
            )
        except OpenDartOriginalDocumentUnavailable:
            if require_all_entities:
                raise
            unavailable_receipts.append(item.disclosure.rcept_no)
    return materialize_open_dart_pending_corpus(
        output_root,
        corp_codes=corp_codes,
        entity_selection=entity_selection,
        disclosure_lists=disclosure_lists,
        selected_disclosures=selected,
        documents=tuple(documents),
        policy=policy,
        entity_scope=(
            None if provided_scope is None else _scope_reference(provided_scope, entity_selection)
        ),
        unavailable_original_document_receipts=tuple(sorted(unavailable_receipts)),
    )


def collect_open_dart_pending_from_provided_relations(
    *,
    relation_database: str | Path,
    transport: OpenDartTransport,
    output_root: str | Path,
    begin_date: date,
    collected_at_utc: datetime,
    max_entities: int = 150,
    max_documents: int = 500,
) -> tuple[MaterializedOpenDartPendingCorpus, OpenDartProvidedRelationScope]:
    """Collect the highest-coverage, exact-bound issuers from the provided DB.

    ``managed_by`` brand surfaces are intentionally excluded by the scope
    builder. Unmatched or ambiguous issuer names remain recorded in the
    collection manifest but do not suppress other exact bindings.
    """

    policy = load_open_dart_filing_whitelist()
    conservative_entity_limit = max_documents // policy.max_documents_per_corporation
    if conservative_entity_limit < 1:
        raise OpenDartCollectionError("Open DART document budget cannot admit one corporation")
    scope = build_open_dart_scope_from_provided_relations(
        relation_database,
        max_entities=min(max_entities, conservative_entity_limit),
    )
    result = _collect_open_dart_pending_from_entities(
        entity_seeds=scope.seeds,
        transport=transport,
        output_root=output_root,
        begin_date=begin_date,
        collected_at_utc=collected_at_utc,
        max_documents=max_documents,
        require_all_entities=False,
        provided_scope=scope,
    )
    return result, scope


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect reviewed entities from Open DART into a key-free, non-authoritative "
            "pending corpus ending 2026-08-24"
        )
    )
    parser.add_argument("--api-key-file", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--entity-seeds", type=Path)
    scope.add_argument("--provided-relation-db", type=Path)
    parser.add_argument("--begin-date", type=_iso_date, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-documents", type=int, default=500)
    parser.add_argument("--max-entities", type=int, default=150)
    parser.add_argument("--request-budget", type=int, default=2000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        key = load_open_dart_api_key(arguments.api_key_file)
        transport = UrllibOpenDartTransport(
            key,
            max_requests=arguments.request_budget,
        )
        del key
        scope_result: OpenDartProvidedRelationScope | None = None
        if arguments.provided_relation_db is not None:
            result, scope_result = collect_open_dart_pending_from_provided_relations(
                relation_database=arguments.provided_relation_db,
                transport=transport,
                output_root=arguments.output_root,
                begin_date=arguments.begin_date,
                collected_at_utc=datetime.now(UTC),
                max_entities=arguments.max_entities,
                max_documents=arguments.max_documents,
            )
        else:
            reviewed = load_reviewed_open_dart_entity_seeds(arguments.entity_seeds)
            result = collect_open_dart_pending_from_seeds(
                reviewed_seeds=reviewed,
                transport=transport,
                output_root=arguments.output_root,
                begin_date=arguments.begin_date,
                collected_at_utc=datetime.now(UTC),
                max_documents=arguments.max_documents,
            )
    except (OpenDartCollectionError, OpenDartScopeError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(
        json.dumps(
            {
                "chunk_count": result.manifest.chunk_count,
                "collection_id": result.manifest.collection_id,
                "document_count": result.manifest.document_count,
                "evidence_cutoff": result.manifest.evidence_cutoff.isoformat(),
                "manifest_sha256": result.manifest_sha256,
                "status": result.manifest.status,
                "entity_binding_count": len(result.manifest.entity_selection.bindings),
                "unmatched_entity_count": len(
                    result.manifest.entity_selection.unmatched_entity_ids
                ),
                "ambiguous_entity_count": len(
                    result.manifest.entity_selection.ambiguous_entity_ids
                ),
                "provided_relation_scope_sha256": (
                    None if scope_result is None else scope_result.canonical_sha256
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "OpenDartReviewedEntitySeeds",
    "build_parser",
    "collect_open_dart_pending_from_seeds",
    "collect_open_dart_pending_from_provided_relations",
    "load_reviewed_open_dart_entity_seeds",
    "main",
]
