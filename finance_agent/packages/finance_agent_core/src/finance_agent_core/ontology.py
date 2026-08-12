from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from finance_agent_core.config import FieldRegistry, load_field_registry

COMMON_IRI = "https://gaeng3.ai/ontology/common"
COMMON_NAMESPACE = f"{COMMON_IRI}#"
ONTOLOGY_RENDERER_VERSION = "registry-derived-turtle-v1"


@dataclass(frozen=True, slots=True)
class OntologyDomain:
    dataset: str
    filename: str
    prefix: str
    ontology_iri: str
    product_class: str
    product_label: str
    family_label: str


ONTOLOGY_DOMAINS = (
    OntologyDomain(
        dataset="bond",
        filename="bond_kr.ttl",
        prefix="bond",
        ontology_iri="https://gaeng3.ai/ontology/bond_kr",
        product_class="KoreanBondProduct",
        product_label="국내채권 상품",
        family_label="국내채권",
    ),
    OntologyDomain(
        dataset="domestic_etp",
        filename="etf_kr.ttl",
        prefix="etfkr",
        ontology_iri="https://gaeng3.ai/ontology/etf_kr",
        product_class="KoreanETPProduct",
        product_label="국내 ETF·ETN 상품",
        family_label="국내 ETF·ETN",
    ),
    OntologyDomain(
        dataset="overseas_etp",
        filename="etf_gl.ttl",
        prefix="etfgl",
        ontology_iri="https://gaeng3.ai/ontology/etf_gl",
        product_class="GlobalETPProduct",
        product_label="해외 ETF·ETN 상품",
        family_label="해외 ETF·ETN",
    ),
    OntologyDomain(
        dataset="fund",
        filename="fund_pub.ttl",
        prefix="fund",
        ontology_iri="https://gaeng3.ai/ontology/fund_pub",
        product_class="PublicFundProduct",
        product_label="공모펀드 상품 클래스",
        family_label="공모펀드",
    ),
)

REQUIRED_ONTOLOGY_FILENAMES = {
    "common.ttl",
    *(domain.filename for domain in ONTOLOGY_DOMAINS),
}


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ko(value: str) -> str:
    return f"{_literal(value)}@ko"


def _decimal(value: float) -> str:
    normalized = format(Decimal(str(value)).normalize(), "f")
    return f'"{normalized}"^^xsd:decimal'


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _json_literal(value: object) -> str:
    return _literal(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _statement(subject: str, predicates: list[tuple[str, str]]) -> str:
    lines = [subject]
    for index, (predicate, value) in enumerate(predicates):
        suffix = " ." if index == len(predicates) - 1 else " ;"
        lines.append(f"    {predicate} {value}{suffix}")
    return "\n".join(lines)


def _render_common(registry: FieldRegistry) -> str:
    datatype_properties = {
        "registryVersion": "registry version",
        "registryName": "canonical registry name",
        "sourceId": "official source identifier",
        "sourceLabel": "official source label",
        "logicalGrain": "logical row grain",
        "snapshotDate": "source snapshot date",
        "rowCount": "raw row count",
        "logicalRowCount": "logical product count",
        "quarantinedRows": "quarantined row count",
        "executionEnabled": "execution enabled",
        "primaryKey": "logical primary key column",
        "rawPrimaryKey": "raw primary key column",
        "rawFilePattern": "raw workbook pattern",
        "schemaFilePattern": "schema workbook pattern",
        "staticAsOfColumn": "static as-of source column",
        "dynamicAsOfColumn": "dynamic as-of source column",
        "canonicalName": "canonical field name",
        "valueType": "canonical value type",
        "unit": "canonical unit",
        "quality": "field quality status",
        "coveragePct": "non-missing coverage percentage",
        "queryable": "query filtering capability",
        "selectable": "answer selection capability",
        "sortable": "sorting capability",
        "aggregatable": "aggregation capability",
        "comparable": "comparison capability",
        "comparisonMode": "comparison calculation mode",
        "comparisonScope": "comparison compatibility scope",
        "allowedOperator": "allowed QueryPlan operator",
        "enumValue": "allowed enum value",
        "asOfBasis": "field as-of basis",
        "sourceDataset": "source dataset registry name",
        "sourceColumn": "source workbook column",
        "sourceTransform": "normalization transform",
        "constantValue": "constant source value",
        "valueMapJson": "source value map encoded as JSON",
        "sentinelValuesJson": "sentinel quality map encoded as JSON",
        "notes": "registry notes",
    }
    blocks = [
        "@prefix ga: <https://gaeng3.ai/ontology/common#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        _statement(
            f"<{COMMON_IRI}>",
            [
                ("a", "owl:Ontology"),
                ("rdfs:label", _ko("금융상품 Agent 공통 Ontology")),
                ("ga:registryVersion", _literal(registry.schema_version)),
            ],
        ),
        "",
    ]
    for name, label in (
        ("FinancialProduct", "financial product"),
        ("ProductFamily", "product family"),
        ("Dataset", "source dataset"),
        ("CanonicalField", "canonical field"),
    ):
        blocks.extend(
            [
                _statement(
                    f"ga:{name}",
                    [("a", "owl:Class"), ("rdfs:label", _literal(label))],
                ),
                "",
            ]
        )
    for name, (domain, range_) in {
        "describedByDataset": ("ga:ProductFamily", "ga:Dataset"),
        "hasCanonicalField": ("ga:ProductFamily", "ga:CanonicalField"),
        "forFamily": ("ga:CanonicalField", "ga:ProductFamily"),
    }.items():
        blocks.extend(
            [
                _statement(
                    f"ga:{name}",
                    [
                        ("a", "owl:ObjectProperty"),
                        ("rdfs:domain", domain),
                        ("rdfs:range", range_),
                    ],
                ),
                "",
            ]
        )
    for name, label in datatype_properties.items():
        blocks.extend(
            [
                _statement(
                    f"ga:{name}",
                    [
                        ("a", "owl:DatatypeProperty"),
                        ("rdfs:label", _literal(label)),
                    ],
                ),
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def _render_domain(registry: FieldRegistry, domain: OntologyDomain) -> str:
    dataset = registry.require_dataset(domain.dataset)
    namespace = f"{domain.ontology_iri}#"
    prefix = domain.prefix
    family = f"{prefix}:Family"
    dataset_node = f"{prefix}:Dataset"
    field_names = [
        name
        for name, definition in registry.fields.items()
        if domain.dataset in definition.datasets
    ]
    blocks = [
        f"@prefix {prefix}: <{namespace}> .",
        "@prefix ga: <https://gaeng3.ai/ontology/common#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        _statement(
            f"<{domain.ontology_iri}>",
            [
                ("a", "owl:Ontology"),
                ("owl:imports", f"<{COMMON_IRI}>"),
                ("rdfs:label", _ko(f"{domain.family_label} Ontology")),
                ("ga:registryVersion", _literal(registry.schema_version)),
            ],
        ),
        "",
        _statement(
            f"{prefix}:{domain.product_class}",
            [
                ("a", "owl:Class"),
                ("rdfs:subClassOf", "ga:FinancialProduct"),
                ("rdfs:label", _ko(domain.product_label)),
            ],
        ),
        "",
        _statement(
            family,
            [
                ("a", "ga:ProductFamily"),
                ("rdfs:label", _ko(domain.family_label)),
                ("ga:registryName", _literal(domain.dataset)),
                ("ga:describedByDataset", dataset_node),
                *[("ga:hasCanonicalField", f"{prefix}:field_{name}") for name in field_names],
            ],
        ),
        "",
        _statement(
            dataset_node,
            [
                ("a", "ga:Dataset"),
                ("ga:registryName", _literal(domain.dataset)),
                ("ga:sourceId", _literal(dataset.source_id)),
                ("ga:sourceLabel", _ko(dataset.source_label)),
                ("ga:logicalGrain", _ko(dataset.logical_grain)),
                ("ga:snapshotDate", f'"{dataset.snapshot_date.isoformat()}"^^xsd:date'),
                ("ga:rowCount", str(dataset.row_count)),
                *(
                    [("ga:logicalRowCount", str(dataset.logical_row_count))]
                    if dataset.logical_row_count is not None
                    else []
                ),
                ("ga:quarantinedRows", str(dataset.quarantined_rows)),
                ("ga:executionEnabled", _boolean(dataset.execution_enabled)),
                *[("ga:primaryKey", _literal(value)) for value in dataset.primary_key],
                *[("ga:rawPrimaryKey", _literal(value)) for value in dataset.raw_primary_key],
                ("ga:rawFilePattern", _literal(dataset.provenance.raw_file_pattern)),
                ("ga:schemaFilePattern", _literal(dataset.provenance.schema_file_pattern)),
                ("ga:staticAsOfColumn", _literal(dataset.provenance.static_as_of_column)),
                ("ga:dynamicAsOfColumn", _literal(dataset.provenance.dynamic_as_of_column)),
                ("ga:notes", _ko(dataset.notes)),
            ],
        ),
        "",
    ]
    for name in field_names:
        field = registry.require_field(name, [domain.dataset])
        predicates = [
            ("a", "ga:CanonicalField"),
            ("ga:forFamily", family),
            ("ga:canonicalName", _literal(name)),
            ("rdfs:label", _ko(field.label)),
            *[("skos:altLabel", _ko(alias)) for alias in field.aliases],
            ("ga:valueType", _literal(field.value_type.value)),
            ("ga:unit", _literal(field.unit)),
            ("ga:quality", _literal(field.quality.value)),
            ("ga:coveragePct", _decimal(field.coverage_pct)),
            ("ga:queryable", _boolean(field.queryable)),
            ("ga:selectable", _boolean(field.selectable)),
            ("ga:sortable", _boolean(field.sortable)),
            ("ga:aggregatable", _boolean(field.aggregatable)),
            ("ga:comparable", _boolean(field.comparable)),
            ("ga:comparisonMode", _literal(field.comparison_mode.value)),
            ("ga:comparisonScope", _literal(field.comparison_scope)),
            *[("ga:allowedOperator", _literal(operator)) for operator in field.allowed_operators],
            *[("ga:enumValue", _literal(value)) for value in field.enum_values],
            ("ga:asOfBasis", _literal(field.as_of_basis.value)),
            ("ga:sourceDataset", _literal(field.source.dataset)),
            *[("ga:sourceColumn", _literal(column)) for column in field.source.columns],
            ("ga:sourceTransform", _literal(field.source.transform.value)),
        ]
        if field.source.constant_value is not None:
            predicates.append(("ga:constantValue", _literal(field.source.constant_value)))
        if field.source.value_map:
            predicates.append(("ga:valueMapJson", _json_literal(field.source.value_map)))
        if field.sentinel_values:
            predicates.append(("ga:sentinelValuesJson", _json_literal(field.sentinel_values)))
        predicates.append(("ga:notes", _ko(field.notes)))
        blocks.extend(
            [
                _statement(f"{prefix}:field_{name}", predicates),
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def render_ontology_bundle(
    registry: FieldRegistry | None = None,
) -> dict[str, str]:
    """Render the five official Turtle files from the frozen field registry."""

    resolved = registry or load_field_registry()
    bundle = {"common.ttl": _render_common(resolved)}
    bundle.update(
        {domain.filename: _render_domain(resolved, domain) for domain in ONTOLOGY_DOMAINS}
    )
    return bundle


def ontology_bundle_sha256(registry: FieldRegistry | None = None) -> str:
    """Return the one canonical Ontology identity used by release and authority."""

    bundle = render_ontology_bundle(registry)
    payload = [{"filename": filename, "content": bundle[filename]} for filename in sorted(bundle)]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
