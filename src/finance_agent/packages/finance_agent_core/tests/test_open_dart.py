from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pydantic import ValidationError

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.retrieval.corpus_review_bridge import (
    build_external_corpus_review_worksheet,
)
from finance_agent_core.retrieval.open_dart import (
    OpenDartCollectionError,
    OpenDartCorporationCode,
    OpenDartDisclosureListQuery,
    OpenDartEntitySeed,
    OpenDartHttpResponse,
    OpenDartSelectedDisclosure,
    collect_open_dart_corp_codes,
    collect_open_dart_disclosure_list,
    collect_open_dart_original_document,
    load_open_dart_filing_whitelist,
    materialize_open_dart_pending_corpus,
    select_open_dart_disclosures,
    select_open_dart_entities,
)
from finance_agent_core.retrieval.open_dart_pending_bridge import (
    OpenDartPendingBridgeError,
    bridge_open_dart_pending_corpus,
)

COLLECTED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _zip(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _corp_code_zip() -> bytes:
    return _zip(
        {
            "CORPCODE.xml": """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00126380</corp_code><corp_name>Samsung Electronics</corp_name>
  <corp_eng_name>Samsung Electronics Co., Ltd.</corp_eng_name>
  <stock_code>005930</stock_code><modify_date>20260824</modify_date></list>
  <list><corp_code>00999999</corp_code><corp_name>Alias Target Corp</corp_name>
  <corp_eng_name></corp_eng_name><stock_code></stock_code>
  <modify_date>20260824</modify_date></list>
  <list><corp_code>00999998</corp_code><corp_name>한국전력공사</corp_name>
  <corp_eng_name>Korea Electric Power Corporation</corp_eng_name><stock_code>015760</stock_code>
  <modify_date>20260824</modify_date></list>
</result>""".encode()
        }
    )


def _disclosure_payload(page_no: int) -> bytes:
    if page_no == 1:
        receipt = "20260824000002"
        report_name = "[기재정정]사업보고서 (2026.06)"
        receipt_date = "20260824"
        correction_marker = ""
    else:
        receipt = "20260823000001"
        report_name = "사업보고서 (2026.06)"
        receipt_date = "20260823"
        correction_marker = "정"
    payload = {
        "status": "000",
        "message": "정상",
        "page_no": page_no,
        "page_count": 1,
        "total_count": 2,
        "total_page": 2,
        "list": [
            {
                "corp_code": "00126380",
                "corp_name": "Samsung Electronics",
                "stock_code": "005930",
                "corp_cls": "Y",
                "report_nm": report_name,
                "rcept_no": receipt,
                "flr_nm": "Samsung Electronics",
                "rcept_dt": receipt_date,
                "rm": correction_marker,
                "pblntf_ty": "A",
                "pblntf_detail_ty": "A001",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        endpoint: str,
        *,
        params: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> OpenDartHttpResponse:
        assert timeout_seconds > 0
        self.calls.append((endpoint, dict(params)))
        if endpoint == "/api/corpCode.xml":
            body = _corp_code_zip()
            content_type = "application/zip"
        elif endpoint == "/api/list.json":
            body = _disclosure_payload(int(params["page_no"]))
            content_type = "application/json"
        elif endpoint == "/api/document.xml":
            assert set(params) == {"rcept_no"}
            body = _zip(
                {
                    "report.xml": (
                        "<?xml version='1.0' encoding='UTF-8'?><DOCUMENT>"
                        "<TITLE>사업보고서</TITLE><P>종속회사와 사업 위험 근거 문단입니다.</P>"
                        "</DOCUMENT>"
                    ).encode()
                }
            )
            content_type = "application/zip"
        else:  # pragma: no cover - proves the fake is endpoint bounded
            raise AssertionError(endpoint)
        assert len(body) <= max_bytes
        return OpenDartHttpResponse(200, content_type, body)


def _disclosure_row(
    rcept_no: str,
    *,
    report_nm: str = "증권신고서 (지분증권)",
    rm: str = "",
    pblntf_ty: str = "C",
) -> dict[str, str]:
    return {
        "corp_code": "00126380",
        "corp_name": "Samsung Electronics",
        "stock_code": "005930",
        "corp_cls": "Y",
        "report_nm": report_nm,
        "rcept_no": rcept_no,
        "flr_nm": "Samsung Electronics",
        "rcept_dt": rcept_no[:8],
        "rm": rm,
        "pblntf_ty": pblntf_ty,
        "pblntf_detail_ty": "C001",
    }


class _SinglePageDisclosureTransport:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def get(
        self,
        endpoint: str,
        *,
        params: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> OpenDartHttpResponse:
        assert endpoint == "/api/list.json"
        assert params["last_reprt_at"] == "N"
        assert timeout_seconds > 0
        payload = {
            "status": "000",
            "message": "정상",
            "page_no": 1,
            "page_count": 100,
            "total_count": len(self.rows),
            "total_page": 1,
            "list": self.rows,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        assert len(body) <= max_bytes
        return OpenDartHttpResponse(200, "application/json", body)


def _collect_single_page_disclosures(
    rows: list[dict[str, str]],
):
    return collect_open_dart_disclosure_list(
        _SinglePageDisclosureTransport(rows),  # type: ignore[arg-type]
        OpenDartDisclosureListQuery(
            corp_code="00126380",
            bgn_de=date(2026, 1, 1),
            end_de=date(2026, 8, 24),
        ),
        collected_at_utc=COLLECTED_AT,
    )


def test_bounded_pipeline_selects_terminal_correction_and_preserves_pending_boundary(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    corp_codes = collect_open_dart_corp_codes(transport, collected_at_utc=COLLECTED_AT)
    selection = select_open_dart_entities(
        corp_codes.snapshot,
        (
            OpenDartEntitySeed(
                entity_id="provided:alias-target",
                legal_name="Unknown Name",
                reviewed_aliases=("Alias Target Corp",),
                seed_source="provided_db",
            ),
            OpenDartEntitySeed(
                entity_id="provided:samsung",
                legal_name="A deliberately different name",
                stock_code="005930",
                seed_source="provided_db",
            ),
        ),
    )
    assert [item.match_method for item in selection.bindings] == [
        "reviewed_alias_exact",
        "stock_code_exact",
    ]

    query = OpenDartDisclosureListQuery(
        corp_code="00126380",
        bgn_de=date(2026, 1, 1),
        end_de=date(2026, 8, 24),
        page_count=1,
    )
    disclosure_list = collect_open_dart_disclosure_list(
        transport,
        query,
        collected_at_utc=COLLECTED_AT,
    )
    list_calls = [item for item in transport.calls if item[0] == "/api/list.json"]
    assert [item[1]["page_no"] for item in list_calls] == ["1", "2"]
    assert all(item[1]["bgn_de"] == "20260101" for item in list_calls)
    assert all(item[1]["end_de"] == "20260824" for item in list_calls)
    assert all(item[1]["last_reprt_at"] == "N" for item in list_calls)
    assert [item.rm for item in disclosure_list.snapshot.disclosures] == ["", "정"]

    policy = load_open_dart_filing_whitelist()
    selected = select_open_dart_disclosures((disclosure_list.snapshot,), policy)
    assert len(selected) == 1
    assert selected[0].disclosure.rcept_no == "20260824000002"
    assert selected[0].disclosure.report_nm.startswith("[기재정정]")
    assert selected[0].capabilities == (
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    )
    document = collect_open_dart_original_document(
        transport,
        selected[0],
        collected_at_utc=COLLECTED_AT,
    )
    output = tmp_path / "open-dart-pending"
    result = materialize_open_dart_pending_corpus(
        output,
        corp_codes=corp_codes,
        entity_selection=selection,
        disclosure_lists=(disclosure_list,),
        selected_disclosures=selected,
        documents=(document,),
        policy=policy,
    )

    assert result.manifest.status == "not_authorized_for_retrieval_or_release"
    assert result.manifest.relation_claim_count == 0
    assert result.manifest.relation_claim_status == "not_extracted_pending_independent_review"
    assert result.manifest.selected_disclosures == selected
    assert len(result.manifest_sha256) == 64
    assert (output / "raw/corp-code/corpCode.zip").read_bytes() == corp_codes.archive_bytes
    assert (output / "snapshots/20260824000002.txt").read_bytes() == (
        document.normalized_snapshot_bytes
    )
    manifest_data = (output / "open-dart-pending-manifest.json").read_bytes()
    assert b"crtfc_key" not in manifest_data

    bridged_root = tmp_path / "open-dart-review-queue"
    bridged = bridge_open_dart_pending_corpus(
        output,
        bridged_root,
        registry=load_pending_intake_source_registry(),
        policy=policy,
    )
    assert bridged.manifest.status == "not_authorized_for_retrieval_or_release"
    assert bridged.receipt.status == "not_authorized_for_retrieval_or_release"
    assert bridged.receipt.open_dart_manifest_sha256 == result.manifest_sha256
    assert bridged.receipt.pending_manifest_sha256 == bridged.manifest_sha256
    assert bridged.manifest.documents[0].raw_media_type == "application/zip"
    assert (bridged_root / "pending-manifest.json").is_file()
    assert (bridged_root / "open-dart-bridge-receipt.json").is_file()
    assert (bridged_root / "provenance/open-dart-pending-manifest.json").read_bytes() == (
        manifest_data
    )
    worksheet = build_external_corpus_review_worksheet(
        bridged_root,
        registry=load_pending_intake_source_registry(),
    )
    assert worksheet.status == "requires_external_reviews_and_rights"
    assert [item.document_id for item in worksheet.documents] == [
        "open-dart-20260824000002"
    ]
    assert worksheet.documents[0].title.startswith("Samsung Electronics — ")
    assert worksheet.documents[0].publisher == "금융감독원"

    with pytest.raises(OpenDartCollectionError, match="already exists"):
        materialize_open_dart_pending_corpus(
            output,
            corp_codes=corp_codes,
            entity_selection=selection,
            disclosure_lists=(disclosure_list,),
            selected_disclosures=selected,
            documents=(document,),
            policy=policy,
        )


def test_entity_selection_normalizes_only_explicit_legal_designators() -> None:
    transport = _Transport()
    corp_codes = collect_open_dart_corp_codes(transport, collected_at_utc=COLLECTED_AT)

    selection = select_open_dart_entities(
        corp_codes.snapshot,
        (
            OpenDartEntitySeed(
                entity_id="provided:kepco",
                legal_name="한국전력공사(주)",
                seed_source="provided_db",
            ),
        ),
    )

    assert selection.unmatched_entity_ids == ()
    assert selection.ambiguous_entity_ids == ()
    assert selection.bindings[0].corp_name == "한국전력공사"
    assert selection.bindings[0].match_method == "legal_designator_exact"


def test_selector_resolves_original_to_terminal_correction() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row("20260820000001", rm="연정"),
            _disclosure_row(
                "20260821000001",
                report_nm="[기재정정]증권신고서 (지분증권)",
            ),
        ]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
    )

    assert [item.disclosure.rcept_no for item in selected] == ["20260821000001"]
    assert all("정" not in item.disclosure.rm for item in selected)


def test_selector_resolves_multiple_corrections_to_latest_terminal() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row("20260820000001", rm="정"),
            _disclosure_row(
                "20260821000001",
                report_nm="[기재정정]증권신고서 (지분증권)",
                rm="정",
            ),
            _disclosure_row(
                "20260822000001",
                report_nm="[첨부정정]증권신고서 (지분증권)",
            ),
        ]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
    )

    assert [item.disclosure.rcept_no for item in selected] == ["20260822000001"]


def test_selector_accepts_official_issuance_condition_terminal() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row(
                "20260820000001",
                report_nm="증권신고서 (채무증권)",
                rm="정",
            ),
            _disclosure_row(
                "20260821000001",
                report_nm="[기재정정]증권신고서 (채무증권)",
                rm="정",
            ),
            _disclosure_row(
                "20260822000001",
                report_nm="[발행조건확정]증권신고서 (채무증권)",
            ),
        ]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
    )

    assert [item.disclosure.rcept_no for item in selected] == ["20260822000001"]


def test_selector_does_not_accept_a_report_name_by_infix_only() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row(
                "20260820000001",
                report_nm="해외증권거래소등에신고한사업보고서등의국내신고",
                pblntf_ty="",
            )
        ]
    )

    assert (
        select_open_dart_disclosures(
            (disclosure_list.snapshot,),
            load_open_dart_filing_whitelist(),
        )
        == ()
    )


def test_selector_keeps_only_latest_terminal_filing_per_corporation_scope() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row(
                "20260331000001",
                report_nm="사업보고서 (2025.12)",
                pblntf_ty="A",
            ),
            _disclosure_row(
                "20260814000001",
                report_nm="반기보고서 (2026.06)",
                pblntf_ty="A",
            ),
            _disclosure_row(
                "20260820000001",
                report_nm="증권신고서 (지분증권)",
            ),
        ]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
    )

    assert [item.disclosure.rcept_no for item in selected] == [
        "20260814000001",
        "20260820000001",
    ]
    assert {item.disclosure.pblntf_ty for item in selected} == {"A", "C"}


@pytest.mark.parametrize(
    ("report_nm", "rm"),
    [
        ("증권신고서 (지분증권)", "유철"),
        ("[철회신고서]증권신고서 (지분증권)", ""),
        ("[철회간주안내]증권신고서 (지분증권)", ""),
    ],
)
def test_selector_rejects_withdrawn_filings(report_nm: str, rm: str) -> None:
    disclosure_list = _collect_single_page_disclosures(
        [_disclosure_row("20260820000001", report_nm=report_nm, rm=rm)]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
    )

    assert selected == ()


def test_selector_fails_closed_when_terminal_correction_is_missing() -> None:
    disclosure_list = _collect_single_page_disclosures([_disclosure_row("20260820000001", rm="정")])

    with pytest.raises(OpenDartCollectionError, match="no terminal correction"):
        select_open_dart_disclosures(
            (disclosure_list.snapshot,),
            load_open_dart_filing_whitelist(),
        )


def test_bulk_selector_skips_only_unresolved_correction_lineage() -> None:
    disclosure_list = _collect_single_page_disclosures(
        [
            _disclosure_row("20260820000001", rm="정"),
            _disclosure_row(
                "20260821000001",
                report_nm="반기보고서 (2026.06)",
                pblntf_ty="A",
            ),
        ]
    )

    selected = select_open_dart_disclosures(
        (disclosure_list.snapshot,),
        load_open_dart_filing_whitelist(),
        skip_unresolved_correction_lineages=True,
    )

    assert [item.disclosure.rcept_no for item in selected] == ["20260821000001"]


def test_manifest_rejects_selector_and_document_bypass(tmp_path: Path) -> None:
    transport = _Transport()
    corp_codes = collect_open_dart_corp_codes(transport, collected_at_utc=COLLECTED_AT)
    entity_selection = select_open_dart_entities(
        corp_codes.snapshot,
        (
            OpenDartEntitySeed(
                entity_id="provided:samsung",
                legal_name="Samsung Electronics",
                stock_code="005930",
                seed_source="provided_db",
            ),
        ),
    )
    disclosure_list = collect_open_dart_disclosure_list(
        transport,
        OpenDartDisclosureListQuery(
            corp_code="00126380",
            bgn_de=date(2026, 1, 1),
            end_de=date(2026, 8, 24),
            page_count=1,
        ),
        collected_at_utc=COLLECTED_AT,
    )
    policy = load_open_dart_filing_whitelist()
    selected = select_open_dart_disclosures((disclosure_list.snapshot,), policy)

    with pytest.raises(OpenDartCollectionError, match="exact selector output"):
        materialize_open_dart_pending_corpus(
            tmp_path / "missing-selection",
            corp_codes=corp_codes,
            entity_selection=entity_selection,
            disclosure_lists=(disclosure_list,),
            selected_disclosures=(),
            documents=(),
            policy=policy,
        )

    superseded = next(item for item in disclosure_list.snapshot.disclosures if "정" in item.rm)
    bypass_candidate = OpenDartSelectedDisclosure(
        disclosure=superseded,
        capabilities=selected[0].capabilities,
    )
    bypass_document = collect_open_dart_original_document(
        transport,
        bypass_candidate,
        collected_at_utc=COLLECTED_AT,
    )
    with pytest.raises(OpenDartCollectionError, match="was not selected"):
        materialize_open_dart_pending_corpus(
            tmp_path / "document-bypass",
            corp_codes=corp_codes,
            entity_selection=entity_selection,
            disclosure_lists=(disclosure_list,),
            selected_disclosures=selected,
            documents=(bypass_document,),
            policy=policy,
        )


def test_cutoff_and_page_size_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="approved cutoff"):
        OpenDartDisclosureListQuery(
            corp_code="00126380",
            bgn_de=date(2026, 1, 1),
            end_de=date(2026, 8, 25),
        )
    with pytest.raises(ValidationError):
        OpenDartDisclosureListQuery(
            corp_code="00126380",
            bgn_de=date(2026, 1, 1),
            end_de=date(2026, 8, 24),
            page_count=101,
        )


def test_official_alphanumeric_stock_code_is_accepted() -> None:
    record = OpenDartCorporationCode(
        corp_code="00123456",
        corp_name="알파테스트",
        stock_code="0001A0",
        modify_date=date(2026, 8, 24),
    )

    assert record.stock_code == "0001A0"


class _UnsafeCorpCodeTransport:
    def get(self, *_args: object, **_kwargs: object) -> OpenDartHttpResponse:
        return OpenDartHttpResponse(
            200,
            "application/zip",
            _zip({"../CORPCODE.xml": b"<result/>"}),
        )


def test_zip_path_traversal_is_rejected() -> None:
    with pytest.raises(OpenDartCollectionError, match="unsafe member"):
        collect_open_dart_corp_codes(
            _UnsafeCorpCodeTransport(),  # type: ignore[arg-type]
            collected_at_utc=COLLECTED_AT,
        )


def test_open_dart_review_bridge_rejects_tampered_source_bytes(tmp_path: Path) -> None:
    transport = _Transport()
    corp_codes = collect_open_dart_corp_codes(transport, collected_at_utc=COLLECTED_AT)
    selection = select_open_dart_entities(
        corp_codes.snapshot,
        (
            OpenDartEntitySeed(
                entity_id="provided:samsung",
                legal_name="Samsung Electronics",
                stock_code="005930",
                seed_source="provided_db",
            ),
        ),
    )
    disclosure_list = collect_open_dart_disclosure_list(
        transport,
        OpenDartDisclosureListQuery(
            corp_code="00126380",
            bgn_de=date(2026, 1, 1),
            end_de=date(2026, 8, 24),
            page_count=1,
        ),
        collected_at_utc=COLLECTED_AT,
    )
    policy = load_open_dart_filing_whitelist()
    selected = select_open_dart_disclosures((disclosure_list.snapshot,), policy)
    document = collect_open_dart_original_document(
        transport,
        selected[0],
        collected_at_utc=COLLECTED_AT,
    )
    source_root = tmp_path / "open-dart-tampered"
    materialize_open_dart_pending_corpus(
        source_root,
        corp_codes=corp_codes,
        entity_selection=selection,
        disclosure_lists=(disclosure_list,),
        selected_disclosures=selected,
        documents=(document,),
        policy=policy,
    )
    snapshot = source_root / document.snapshot.normalized_snapshot.relative_path
    snapshot.write_text("tampered", encoding="utf-8")

    with pytest.raises(OpenDartPendingBridgeError, match="size differs|SHA-256 differs"):
        bridge_open_dart_pending_corpus(
            source_root,
            tmp_path / "must-not-exist",
            registry=load_pending_intake_source_registry(),
            policy=policy,
        )
