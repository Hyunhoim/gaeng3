from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.relations import (
    RelationIndexError,
    RelationIndexManifest,
    RelationSearchRequest,
    RelationSearchResponse,
    RelationType,
    SQLiteRelationIndex,
    build_provided_relation_index,
)
from finance_agent_core.storage.approval import DatasetApprovalError

_PROVIDED_RELATION_FAMILIES = (
    ProductFamily.BOND,
    ProductFamily.DOMESTIC_ETP,
    ProductFamily.OVERSEAS_ETP,
)


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        family: database_dir / f"{family.value}.sqlite3"
        for family in _PROVIDED_RELATION_FAMILIES
    }


def _print_model(model: object) -> None:
    print(
        json.dumps(
            model.model_dump(mode="json"),  # type: ignore[attr-defined]
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _build(arguments: argparse.Namespace) -> None:
    receipt = build_provided_relation_index(
        _database_paths(arguments.database_dir),
        arguments.output_database,
    )
    _print_model(receipt)


def _manifest(arguments: argparse.Namespace) -> None:
    _print_model(SQLiteRelationIndex(arguments.index).manifest())


def _search(arguments: argparse.Namespace) -> None:
    response = SQLiteRelationIndex(arguments.index).search(
        RelationSearchRequest(
            query=arguments.query,
            top_k=arguments.top_k,
            product_families=tuple(ProductFamily(item) for item in arguments.family),
            relation_types=tuple(RelationType(item) for item in arguments.relation_type),
            as_of_on_or_before=(
                None
                if arguments.as_of_on_or_before is None
                else date.fromisoformat(arguments.as_of_on_or_before)
            ),
        ),
        _database_paths(arguments.database_dir),
    )
    _print_model(response)


def _schema(arguments: argparse.Namespace) -> None:
    models = {
        "manifest": RelationIndexManifest,
        "request": RelationSearchRequest,
        "response": RelationSearchResponse,
    }
    print(
        json.dumps(
            models[arguments.kind].model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and inspect the non-activated relation index derived from approved product DBs."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a new immutable provided-relation index")
    build.add_argument("--database-dir", type=Path, required=True)
    build.add_argument("--output-database", type=Path, required=True)
    build.set_defaults(handler=_build)

    manifest = commands.add_parser("manifest", help="verify and print the index manifest")
    manifest.add_argument("--index", type=Path, required=True)
    manifest.set_defaults(handler=_manifest)

    search = commands.add_parser("search", help="run verified lexical relation retrieval")
    search.add_argument("--index", type=Path, required=True)
    search.add_argument("--database-dir", type=Path, required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument(
        "--family",
        action="append",
        choices=[item.value for item in _PROVIDED_RELATION_FAMILIES],
        default=[],
    )
    search.add_argument(
        "--relation-type",
        action="append",
        choices=[item.value for item in RelationType],
        default=[],
    )
    search.add_argument("--as-of-on-or-before")
    search.set_defaults(handler=_search)

    schema = commands.add_parser("schema", help="print a strict relation JSON schema")
    schema.add_argument("--kind", choices=("manifest", "request", "response"), required=True)
    schema.set_defaults(handler=_schema)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    try:
        handler(arguments)
    except (DatasetApprovalError, RelationIndexError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":  # pragma: no cover
    main()
