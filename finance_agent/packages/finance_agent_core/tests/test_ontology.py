from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from finance_agent_core.config import load_field_registry
from finance_agent_core.ontology import (
    ONTOLOGY_DOMAINS,
    REQUIRED_ONTOLOGY_FILENAMES,
    render_ontology_bundle,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ONTOLOGY_ROOT = REPOSITORY_ROOT / "ontology"


def test_official_ontology_bundle_has_exact_required_filenames() -> None:
    bundle = render_ontology_bundle()

    assert set(bundle) == REQUIRED_ONTOLOGY_FILENAMES


def test_official_ontology_bundle_is_valid_turtle() -> None:
    for content in render_ontology_bundle().values():
        graph = Graph()
        graph.parse(data=content, format="turtle")
        assert len(graph) > 0


def test_checked_in_ontology_matches_every_registry_field() -> None:
    registry = load_field_registry()
    expected = render_ontology_bundle(registry)

    assert {path.name for path in ONTOLOGY_ROOT.glob("*.ttl")} == REQUIRED_ONTOLOGY_FILENAMES
    for filename, content in expected.items():
        assert (ONTOLOGY_ROOT / filename).read_text(encoding="utf-8") == content
    for domain in ONTOLOGY_DOMAINS:
        expected_fields = {
            name
            for name, definition in registry.fields.items()
            if domain.dataset in definition.datasets
        }
        assert len(expected_fields) > 0
        assert all(
            f"{domain.prefix}:field_{name}\n" in expected[domain.filename]
            for name in expected_fields
        )
