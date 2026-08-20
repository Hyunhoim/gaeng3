from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.relations import (
    RelationIndexError,
    RelationSearchRequest,
    RelationType,
    SQLiteRelationIndex,
    VerifiedProductDatabase,
    build_provided_relation_index,
)
from finance_agent_core.storage import write_domestic_etp_database
from finance_agent_core.storage.approval import sha256_file
from finance_agent_core.storage.identity_cache import load_product_identities


class SyntheticDatabaseVerifier:
    """Test-only verifier; production uses ApprovedProductDatabaseVerifier."""

    def __init__(self, approval_manifest_sha256: str = "f" * 64) -> None:
        self._approval_manifest_sha256 = approval_manifest_sha256
        self.calls: list[tuple[ProductFamily, Path]] = []

    @property
    def approval_manifest_sha256(self) -> str:
        return self._approval_manifest_sha256

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase:
        resolved = Path(path).resolve(strict=True)
        manifest, identities = load_product_identities(resolved)
        if manifest.dataset != product_family.value:
            raise RelationIndexError("synthetic verifier family mismatch")
        self.calls.append((product_family, resolved))
        return VerifiedProductDatabase(
            product_family=product_family,
            path=resolved,
            manifest=manifest,
            database_sha256=sha256_file(resolved),
            identities=identities,
        )


@pytest.fixture
def relation_index(
    tmp_path: Path,
    domestic_sample_database,
) -> tuple[Path, Path, SyntheticDatabaseVerifier]:
    product_database, _, _ = domestic_sample_database
    index_path = tmp_path / "relations.sqlite3"
    verifier = SyntheticDatabaseVerifier()
    build_provided_relation_index(
        {ProductFamily.DOMESTIC_ETP: product_database},
        index_path,
        verifier=verifier,
    )
    return index_path, product_database, verifier


@pytest.fixture
def overlapping_relation_index(
    tmp_path: Path,
    domestic_sample_database,
) -> tuple[Path, Path, SyntheticDatabaseVerifier]:
    _, records, manifest = domestic_sample_database
    managers = (
        "Test Capital",
        "TEST CAPITAL",
        "Test Capital Holdings",
        "Other Capital",
        "S&P 500",
        "한국운용",
        "테스트 운용 그룹",
    )
    updated_records = []
    for record, manager in zip(records, managers, strict=True):
        updated_records.append(
            record.model_copy(
                update={
                    "manager": manager,
                    "source_values": {
                        **record.source_values,
                        "cu_fund_mgmt_co": manager,
                    },
                }
            )
        )
    product_database = tmp_path / "overlapping-domestic-etp.sqlite3"
    write_domestic_etp_database(product_database, updated_records, manifest)
    index_path = tmp_path / "overlapping-relations.sqlite3"
    verifier = SyntheticDatabaseVerifier()
    build_provided_relation_index(
        {ProductFamily.DOMESTIC_ETP: product_database},
        index_path,
        verifier=verifier,
    )
    return index_path, product_database, verifier


def test_builds_deterministic_provided_relation_index(
    tmp_path: Path,
    domestic_sample_database,
) -> None:
    product_database, records, _ = domestic_sample_database
    first = tmp_path / "relations-1.sqlite3"
    second = tmp_path / "relations-2.sqlite3"
    verifier = SyntheticDatabaseVerifier()

    first_receipt = build_provided_relation_index(
        {"domestic_etp": product_database},
        first,
        verifier=verifier,
    )
    second_receipt = build_provided_relation_index(
        {ProductFamily.DOMESTIC_ETP: product_database},
        second,
        verifier=verifier,
    )

    assert first_receipt == second_receipt
    assert first_receipt.relation_count == len(records) * 3
    assert first_receipt.database_sha256 == sha256_file(first)
    assert first.stat().st_mode & 0o222 == 0
    assert SQLiteRelationIndex(first).manifest().status == "verified_not_agent_activated"


def test_searches_manager_and_rehydrates_official_product_identity(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index
    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(
            query="테스트운용",
            top_k=3,
            product_families=(ProductFamily.DOMESTIC_ETP,),
            relation_types=(RelationType.MANAGED_BY,),
        ),
        {ProductFamily.DOMESTIC_ETP: product_database},
        verifier=verifier,
    )

    assert response.status == "found"
    assert len(response.evidence) == 3
    assert all(item.relation_type is RelationType.MANAGED_BY for item in response.evidence)
    assert [item.product_name for item in response.evidence] == [
        "국내 테스트 A000002",
        "국내 테스트 A000003",
        "국내 테스트 A000004",
    ]
    assert all(item.source_columns == ("cu_fund_mgmt_co",) for item in response.evidence)
    assert all(item.as_of.isoformat() == "2026-06-15" for item in response.evidence)
    assert all(item.approval_manifest_sha256 == "f" * 64 for item in response.evidence)


@pytest.mark.parametrize(
    ("query", "relation_type", "canonical_field"),
    [
        ("주식", RelationType.CLASSIFIED_AS_ASSET, "asset_type"),
        ("미국", RelationType.INVESTS_IN_REGION, "investment_region"),
    ],
)
def test_searches_other_provided_relation_fields(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
    query: str,
    relation_type: RelationType,
    canonical_field: str,
) -> None:
    index_path, product_database, verifier = relation_index

    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(
            query=query,
            top_k=50,
            relation_types=(relation_type,),
        ),
        {"domestic_etp": product_database},
        verifier=verifier,
    )

    assert response.status == "found"
    assert len(response.evidence) == 7
    assert all(item.canonical_field == canonical_field for item in response.evidence)


def test_returns_not_found_without_fabricating_relations(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index

    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(query="존재하지않는관계어"),
        {"domestic_etp": product_database},
        verifier=verifier,
    )

    assert response.status == "not_found"
    assert response.evidence == ()


def test_full_entity_match_does_not_fill_top_k_with_overlapping_names(
    overlapping_relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = overlapping_relation_index

    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(
            query="  test   capital  ",
            top_k=50,
            relation_types=(RelationType.MANAGED_BY,),
        ),
        {ProductFamily.DOMESTIC_ETP: product_database},
        verifier=verifier,
    )

    assert response.status == "found"
    assert len(response.evidence) == 2
    assert {item.entity_label for item in response.evidence} == {
        "Test Capital",
        "TEST CAPITAL",
    }


def test_partial_entity_token_is_not_found_even_when_fts_has_candidates(
    overlapping_relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = overlapping_relation_index

    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(
            query="Test",
            top_k=50,
            relation_types=(RelationType.MANAGED_BY,),
        ),
        {ProductFamily.DOMESTIC_ETP: product_database},
        verifier=verifier,
    )

    assert response.status == "not_found"
    assert response.evidence == ()


@pytest.mark.parametrize(
    ("query", "expected_label"),
    [
        ("Ｓ　＆　Ｐ　５００", "S&P 500"),
        ("한 국 운용", "한국운용"),
    ],
)
def test_full_entity_match_preserves_safe_korean_and_english_normalization(
    overlapping_relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
    query: str,
    expected_label: str,
) -> None:
    index_path, product_database, verifier = overlapping_relation_index

    response = SQLiteRelationIndex(index_path).search(
        RelationSearchRequest(
            query=query,
            top_k=50,
            relation_types=(RelationType.MANAGED_BY,),
        ),
        {ProductFamily.DOMESTIC_ETP: product_database},
        verifier=verifier,
    )

    assert response.status == "found"
    assert [item.entity_label for item in response.evidence] == [expected_label]


def test_rejects_runtime_product_database_drift(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index
    with sqlite3.connect(product_database) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ? WHERE product_id = ?",
            ("변경된 이름", "KR7000000002"),
        )

    with pytest.raises(RelationIndexError, match="differs from relation index binding"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(query="테스트운용"),
            {"domestic_etp": product_database},
            verifier=verifier,
        )


def test_rejects_relation_index_product_id_tampering(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index
    os.chmod(index_path, 0o644)
    with sqlite3.connect(index_path) as connection:
        relation_id = connection.execute(
            """
            SELECT relation_id FROM product_relations
            WHERE relation_type = 'managed_by'
            ORDER BY product_id LIMIT 1
            """
        ).fetchone()[0]
        connection.execute(
            "UPDATE product_relations SET product_id = 'MISSING' WHERE relation_id = ?",
            (relation_id,),
        )

    with pytest.raises(RelationIndexError, match="relation ID differs"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(
                query="테스트운용",
                relation_types=(RelationType.MANAGED_BY,),
            ),
            {"domestic_etp": product_database},
            verifier=verifier,
        )


def test_rejects_candidate_missing_from_verified_identity_snapshot(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, _ = relation_index

    class MissingIdentityVerifier(SyntheticDatabaseVerifier):
        def verify(
            self,
            product_family: ProductFamily,
            path: str | Path,
        ) -> VerifiedProductDatabase:
            snapshot = super().verify(product_family, path)
            return VerifiedProductDatabase(
                product_family=snapshot.product_family,
                path=snapshot.path,
                manifest=snapshot.manifest,
                database_sha256=snapshot.database_sha256,
                identities=tuple(
                    item for item in snapshot.identities if item.product_id != "KR7000000002"
                ),
            )

    with pytest.raises(RelationIndexError, match="failed official product re-verification"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(
                query="테스트운용",
                relation_types=(RelationType.MANAGED_BY,),
            ),
            {"domestic_etp": product_database},
            verifier=MissingIdentityVerifier(),
        )


def test_rejects_runtime_approval_manifest_mismatch(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, _ = relation_index

    with pytest.raises(RelationIndexError, match="approval manifest differs"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(query="테스트운용"),
            {"domestic_etp": product_database},
            verifier=SyntheticDatabaseVerifier("e" * 64),
        )


def test_rejects_database_family_set_mismatch(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
    sample_database,
) -> None:
    index_path, _, verifier = relation_index
    overseas_database, _, _ = sample_database

    with pytest.raises(RelationIndexError, match="families differ"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(query="테스트운용"),
            {"overseas_etp": overseas_database},
            verifier=verifier,
        )


def test_never_overwrites_existing_relation_index(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index

    with pytest.raises(RelationIndexError, match="already exists"):
        build_provided_relation_index(
            {"domestic_etp": product_database},
            index_path,
            verifier=verifier,
        )


def test_rejects_fund_relation_extraction_before_source_contract(
    tmp_path: Path,
) -> None:
    with pytest.raises(RelationIndexError, match="not enabled for fund"):
        build_provided_relation_index(
            {"fund": tmp_path / "fund.sqlite3"},
            tmp_path / "relations.sqlite3",
            verifier=SyntheticDatabaseVerifier(),
        )


def test_rejects_fund_relation_search_before_source_contract() -> None:
    with pytest.raises(ValueError, match="fund relation search is not enabled"):
        RelationSearchRequest(query="공모펀드", product_families=(ProductFamily.FUND,))


def test_rejects_non_searchable_query_tokens(
    relation_index: tuple[Path, Path, SyntheticDatabaseVerifier],
) -> None:
    index_path, product_database, verifier = relation_index

    with pytest.raises(ValueError, match="no searchable tokens"):
        SQLiteRelationIndex(index_path).search(
            RelationSearchRequest(query="!!!"),
            {"domestic_etp": product_database},
            verifier=verifier,
        )
