from __future__ import annotations

import argparse
import sys
from pathlib import Path

from finance_agent_core.ontology import (
    REQUIRED_ONTOLOGY_FILENAMES,
    render_ontology_bundle,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
DEFAULT_ONTOLOGY_ROOT = REPOSITORY_ROOT / "ontology"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or validate the official Turtle ontology bundle."
    )
    parser.add_argument("--check", action="store_true", help="validate without writing")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ONTOLOGY_ROOT)
    return parser.parse_args(argv)


def _syntax_errors(bundle: dict[str, str]) -> list[str]:
    try:
        from rdflib import Graph
    except ImportError:
        return ["rdflib is required; install finance_agent/requirements/dev.txt"]

    errors: list[str] = []
    for filename, content in bundle.items():
        try:
            Graph().parse(data=content, format="turtle")
        except Exception as error:  # noqa: BLE001 - report parser diagnostics at CLI boundary
            errors.append(f"{filename}: invalid Turtle: {error}")
    return errors


def _drift_errors(output_dir: Path, expected: dict[str, str]) -> list[str]:
    errors: list[str] = []
    actual_names = (
        {path.name for path in output_dir.glob("*.ttl")}
        if output_dir.is_dir()
        else set()
    )
    if actual_names != REQUIRED_ONTOLOGY_FILENAMES:
        errors.append(
            "ontology filenames differ: "
            f"expected={sorted(REQUIRED_ONTOLOGY_FILENAMES)}, actual={sorted(actual_names)}"
        )
    for filename, expected_content in expected.items():
        path = output_dir / filename
        if not path.is_file():
            continue
        if path.read_text(encoding="utf-8") != expected_content:
            errors.append(f"{filename}: differs from field_registry.yaml")
    return errors


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(argv)
    bundle = render_ontology_bundle()
    syntax_errors = _syntax_errors(bundle)
    if syntax_errors:
        for error in syntax_errors:
            print(error, file=sys.stderr)
        return 1

    if arguments.check:
        errors = _drift_errors(arguments.output_dir, bundle)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(
            f"Ontology checks passed: {len(bundle)} Turtle files match field_registry.yaml."
        )
        return 0

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in bundle.items():
        (arguments.output_dir / filename).write_text(content, encoding="utf-8")
    print(
        f"Ontology synchronized: {len(bundle)} Turtle files in {arguments.output_dir}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
