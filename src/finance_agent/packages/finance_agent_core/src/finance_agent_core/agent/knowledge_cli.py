from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from finance_agent_core.agent.knowledge_service import (
    KnowledgeAgent,
    KnowledgeAgentResult,
    KnowledgeServiceError,
)
from finance_agent_core.answering.claims import KnowledgeAnswerDraft
from finance_agent_core.contracts.knowledge import KnowledgeQueryPlan
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.release import KnowledgeRetrievalRelease
from finance_agent_core.retrieval.relations import RelationIndexError

_MAX_CONTRACT_BYTES = 2 * 1024 * 1024
_RELATION_FAMILIES = (
    ProductFamily.BOND,
    ProductFamily.DOMESTIC_ETP,
    ProductFamily.OVERSEAS_ETP,
)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_contract(path: Path, label: str) -> bytes:
    """Read one bounded, non-symlink JSON contract from a stable descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if before.st_size <= 0 or before.st_size > _MAX_CONTRACT_BYTES:
            raise ValueError(f"{label} size is outside the allowed range")
        chunks: list[bytes] = []
        remaining = _MAX_CONTRACT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or len(data) != before.st_size:
            raise ValueError(f"{label} changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def _load_model(path: Path, label: str, model: type[BaseModel]) -> BaseModel:
    data = _read_contract(path, label)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"{label} contains a duplicate JSON key")
            payload[key] = value
        return payload

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains a non-finite JSON constant: {value}")

    try:
        decoded = data.decode("utf-8")
        payload = json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must use UTF-8") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain one JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise ValueError(f"{label} violates its strict schema") from error


def _print_json(payload: object) -> None:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {family: database_dir / f"{family.value}.sqlite3" for family in _RELATION_FAMILIES}


def _execute(arguments: argparse.Namespace) -> None:
    plan = _load_model(arguments.plan, "knowledge plan", KnowledgeQueryPlan)
    release = _load_model(arguments.release, "knowledge release", KnowledgeRetrievalRelease)
    assert isinstance(plan, KnowledgeQueryPlan)
    assert isinstance(release, KnowledgeRetrievalRelease)

    if release.relation is not None:
        if arguments.relation_index is None or arguments.database_dir is None:
            raise ValueError("relation release requires --relation-index and --database-dir")
        relation_database_paths = _database_paths(arguments.database_dir)
    else:
        if arguments.relation_index is not None or arguments.database_dir is not None:
            raise ValueError("relation paths were supplied for a release without relations")
        relation_database_paths = None

    if release.document is not None and arguments.document_index is None:
        raise ValueError("document release requires --document-index")
    if release.document is None and arguments.document_index is not None:
        raise ValueError("document path was supplied for a release without documents")

    result = KnowledgeAgent(
        release=release,
        relation_index_path=arguments.relation_index,
        relation_database_paths=relation_database_paths,
        document_index_path=arguments.document_index,
    ).execute(plan)
    _print_json(result)


def _schema(arguments: argparse.Namespace) -> None:
    models: dict[str, type[BaseModel]] = {
        "plan": KnowledgeQueryPlan,
        "release": KnowledgeRetrievalRelease,
        "result": KnowledgeAgentResult,
        "answer-draft": KnowledgeAnswerDraft,
    }
    _print_json(models[arguments.kind].model_json_schema())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Execute the internal, non-activated P0-7 knowledge contract without an LLM.")
    )
    commands = parser.add_subparsers(dest="command", required=True)

    execute = commands.add_parser("execute", help="run one server-owned knowledge plan")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--release", type=Path, required=True)
    execute.add_argument("--relation-index", type=Path)
    execute.add_argument("--database-dir", type=Path)
    execute.add_argument("--document-index", type=Path)
    execute.set_defaults(handler=_execute)

    schema = commands.add_parser("schema", help="print a strict P0-7 JSON schema")
    schema.add_argument(
        "--kind",
        choices=("plan", "release", "result", "answer-draft"),
        required=True,
    )
    schema.set_defaults(handler=_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    try:
        handler(arguments)
    except (KnowledgeServiceError, RelationIndexError, ValueError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":  # pragma: no cover
    main()
