from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval import open_dart_cli
from finance_agent_core.retrieval.open_dart import (
    OpenDartCollectionError,
    OpenDartEntitySeed,
    OpenDartHttpResponse,
)
from finance_agent_core.retrieval.open_dart_cli import (
    OpenDartReviewedEntitySeeds,
    _collect_open_dart_pending_from_entities,
    collect_open_dart_pending_from_seeds,
    load_reviewed_open_dart_entity_seeds,
)
from finance_agent_core.retrieval.open_dart_scope import (
    OpenDartProductFamilyCoverage,
    OpenDartProvidedRelationScope,
    OpenDartScopedEntity,
)
from finance_agent_core.retrieval.open_dart_transport import (
    UrllibOpenDartTransport,
    _NoRedirectHandler,
    load_open_dart_api_key,
)

_KEY = "A1" * 20
_COLLECTED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _zip(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _corp_code_zip() -> bytes:
    return _zip(
        {
            "CORPCODE.xml": b"""<?xml version="1.0" encoding="UTF-8"?>
<result><list><corp_code>00126380</corp_code><corp_name>Samsung Electronics</corp_name>
<corp_eng_name>Samsung Electronics Co., Ltd.</corp_eng_name>
<stock_code>005930</stock_code><modify_date>20260824</modify_date></list></result>"""
        }
    )


def _reviewed_seeds() -> OpenDartReviewedEntitySeeds:
    return OpenDartReviewedEntitySeeds.model_validate(
        {
            "schema_version": "1.0",
            "artifact_kind": "open_dart_reviewed_entity_seeds",
            "review_status": "reviewed_for_open_dart_collection",
            "evidence_cutoff": "2026-08-24",
            "seeds": [
                {
                    "entity_id": "provided:samsung",
                    "legal_name": "Samsung Electronics",
                    "stock_code": "005930",
                    "reviewed_aliases": [],
                    "seed_source": "provided_db",
                }
            ],
        }
    )


def _provided_scope_with_unmatched_seed() -> OpenDartProvidedRelationScope:
    product_hash = "a" * 64
    entities = (
        OpenDartScopedEntity(
            entity_id="odscope-22222222222222222222222222222222",
            legal_name="Missing Corporation",
            normalized_name="missing corporation",
            source_relation_entity_id="source-missing",
            relation_types=("issued_by",),
            family_coverage=(
                OpenDartProductFamilyCoverage(
                    product_family=ProductFamily.BOND,
                    relation_count=1,
                    product_count=1,
                    product_id_set_sha256=product_hash,
                ),
            ),
            relation_count=1,
            product_count=1,
            product_id_set_sha256=product_hash,
        ),
        OpenDartScopedEntity(
            entity_id="odscope-11111111111111111111111111111111",
            legal_name="Samsung Electronics",
            normalized_name="samsung electronics",
            source_relation_entity_id="source-samsung",
            relation_types=("issued_by",),
            family_coverage=(
                OpenDartProductFamilyCoverage(
                    product_family=ProductFamily.BOND,
                    relation_count=1,
                    product_count=1,
                    product_id_set_sha256=product_hash,
                ),
            ),
            relation_count=1,
            product_count=1,
            product_id_set_sha256=product_hash,
        ),
    )
    seeds = tuple(
        sorted(
            (
                OpenDartEntitySeed(
                    entity_id=item.entity_id,
                    legal_name=item.legal_name,
                    seed_source="provided_db",
                )
                for item in entities
            ),
            key=lambda item: item.entity_id,
        )
    )
    import hashlib

    canonical = (
        json.dumps(
            [item.model_dump(mode="json") for item in entities],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return OpenDartProvidedRelationScope(
        source_relation_index_sha256="b" * 64,
        source_relation_manifest_sha256="c" * 64,
        source_relation_set_sha256="d" * 64,
        source_relation_count=2,
        eligible_relation_count=2,
        eligible_entity_count=2,
        eligible_product_count=2,
        managed_by_review_required_count=0,
        max_entities=2,
        truncated=False,
        selected_relation_count=2,
        selected_product_count=2,
        entities=entities,
        seeds=seeds,
        scope_set_sha256=hashlib.sha256(canonical).hexdigest(),
    )


class _PipelineTransport:
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
        del timeout_seconds
        self.calls.append((endpoint, dict(params)))
        if endpoint == "/api/corpCode.xml":
            body = _corp_code_zip()
            content_type = "application/zip"
        elif endpoint == "/api/list.json":
            body = json.dumps(
                {
                    "status": "000",
                    "message": "normal",
                    "page_no": 1,
                    "page_count": 100,
                    "total_count": 1,
                    "total_page": 1,
                    "list": [
                        {
                            "corp_code": "00126380",
                            "corp_name": "Samsung Electronics",
                            "stock_code": "005930",
                            "corp_cls": "Y",
                            "report_nm": "사업보고서 (2026.06)",
                            "rcept_no": "20260824000001",
                            "flr_nm": "Samsung Electronics",
                            "rcept_dt": "20260824",
                            "rm": "",
                            "pblntf_ty": "A",
                            "pblntf_detail_ty": "A001",
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode()
            content_type = "application/json"
        elif endpoint == "/api/document.xml":
            body = _zip(
                {
                    "report.xml": (
                        "<?xml version='1.0' encoding='UTF-8'?><DOCUMENT>"
                        "<P>종속회사와 사업 위험에 관한 회사 제출 공시 근거입니다.</P>"
                        "</DOCUMENT>"
                    ).encode()
                }
            )
            content_type = "application/zip"
        else:  # pragma: no cover
            raise AssertionError(endpoint)
        assert len(body) <= max_bytes
        return OpenDartHttpResponse(200, content_type, body)


class _Response:
    def __init__(self, body: bytes, url: str, *, status: int = 200) -> None:
        self.status = status
        self._body = body
        self._url = url
        self.closed = False
        self.read_limit: int | None = None
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self._body[:limit]

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, _Response) and not outcome._url:
            outcome._url = request.full_url
        return outcome


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_api_key_file_accepts_only_owner_private_regular_file(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "opendart-api-key"
    path.write_text(_KEY, encoding="ascii")
    path.chmod(mode)

    assert load_open_dart_api_key(path) == _KEY


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (_KEY, 0o640),
        ("A" * 39, 0o600),
        ("A" * 39 + "!", 0o600),
        ("A" * 40 + "\n", 0o600),
    ],
)
def test_api_key_file_rejects_unsafe_mode_or_shape_without_leak(
    tmp_path: Path,
    value: str,
    mode: int,
) -> None:
    path = tmp_path / "opendart-api-key"
    path.write_text(value, encoding="ascii")
    path.chmod(mode)

    with pytest.raises(OpenDartCollectionError) as caught:
        load_open_dart_api_key(path)

    assert value not in str(caught.value)


def test_api_key_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "key"
    target.write_text(_KEY, encoding="ascii")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(OpenDartCollectionError, match="symbolic link"):
        load_open_dart_api_key(link)


def test_transport_uses_fixed_https_endpoint_and_query_only_credential() -> None:
    response = _Response(b'{"status":"013"}', "")
    opener = _Opener([response])
    transport = UrllibOpenDartTransport(
        _KEY,
        opener=opener,
        retry_delays=(),
        sleeper=lambda _delay: None,
    )

    result = transport.get(
        "/api/list.json",
        params={
            "bgn_de": "20260101",
            "corp_code": "00126380",
            "end_de": "20260824",
            "last_reprt_at": "N",
            "page_count": "100",
            "page_no": "1",
        },
        timeout_seconds=20.0,
        max_bytes=4096,
    )

    request = opener.requests[0]
    parsed = urllib.parse.urlsplit(request.full_url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        "opendart.fss.or.kr",
        "/api/list.json",
    )
    query = urllib.parse.parse_qs(parsed.query)
    assert query["crtfc_key"] == [_KEY]
    assert query["corp_code"] == ["00126380"]
    assert _KEY not in "\n".join(f"{key}:{value}" for key, value in request.header_items())
    assert result.body == b'{"status":"013"}'
    assert response.read_limit == 4097
    assert response.closed is True
    assert _KEY not in repr(transport)


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("/api/other.json", {}),
        ("https://attacker.invalid/api/list.json", {}),
        ("/api/corpCode.xml", {"crtfc_key": _KEY}),
        ("/api/document.xml", {"rcept_no": "20260824000001", "extra": "1"}),
    ],
)
def test_transport_rejects_endpoint_or_parameter_escape(
    endpoint: str,
    params: dict[str, str],
) -> None:
    transport = UrllibOpenDartTransport(_KEY, opener=_Opener([]), retry_delays=())

    with pytest.raises(OpenDartCollectionError):
        transport.get(endpoint, params=params, timeout_seconds=1.0, max_bytes=1024)


def test_transport_retries_transient_status_with_bounded_backoff() -> None:
    error_body = _Response(b"DO_NOT_READ_HTTP_ERROR_BODY", "")
    error = urllib.error.HTTPError(
        "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=" + _KEY,
        429,
        "DO_NOT_LOG_REMOTE_MESSAGE",
        Message(),
        error_body,
    )
    response = _Response(b"ok", "")
    opener = _Opener([error, response])
    delays: list[float] = []
    transport = UrllibOpenDartTransport(
        _KEY,
        opener=opener,
        retry_delays=(0.25,),
        sleeper=delays.append,
    )

    result = transport.get(
        "/api/corpCode.xml",
        params={},
        timeout_seconds=1.0,
        max_bytes=2,
    )

    assert result.body == b"ok"
    assert len(opener.requests) == 2
    assert delays == [0.25]
    assert error_body.closed is True


def test_transport_fails_closed_on_redirect_and_oversized_body() -> None:
    redirect_body = _Response(b"DO_NOT_READ_REDIRECT_BODY", "")
    redirect = urllib.error.HTTPError(
        "https://attacker.invalid/steal?secret=" + _KEY,
        302,
        "DO_NOT_LOG_REDIRECT",
        Message(),
        redirect_body,
    )
    redirecting = UrllibOpenDartTransport(_KEY, opener=_Opener([redirect]), retry_delays=())
    with pytest.raises(OpenDartCollectionError) as caught:
        redirecting.get(
            "/api/corpCode.xml",
            params={},
            timeout_seconds=1.0,
            max_bytes=1024,
        )
    assert _KEY not in str(caught.value)
    assert "DO_NOT_LOG" not in str(caught.value)
    assert redirect_body.closed is True

    oversized_response = _Response(b"abc", "")
    oversized = UrllibOpenDartTransport(
        _KEY,
        opener=_Opener([oversized_response]),
        retry_delays=(),
    )
    with pytest.raises(OpenDartCollectionError, match="size"):
        oversized.get(
            "/api/corpCode.xml",
            params={},
            timeout_seconds=1.0,
            max_bytes=2,
        )


def test_default_redirect_handler_never_forwards_key() -> None:
    request = urllib.request.Request(
        "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=" + _KEY
    )

    redirected = _NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        "https://attacker.invalid/steal",
    )

    assert redirected is None


def test_seed_loader_rejects_duplicate_keys_and_writable_contract(tmp_path: Path) -> None:
    path = tmp_path / "seeds.json"
    path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(OpenDartCollectionError, match="duplicate"):
        load_reviewed_open_dart_entity_seeds(path)

    path.write_text(json.dumps(_reviewed_seeds().model_dump(mode="json")), encoding="utf-8")
    path.chmod(0o622)
    with pytest.raises(OpenDartCollectionError, match="writable"):
        load_reviewed_open_dart_entity_seeds(path)


def test_collection_cli_pipeline_materializes_key_free_pending_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key_path = tmp_path / "opendart-api-key"
    key_path.write_text(_KEY, encoding="ascii")
    key_path.chmod(0o600)
    seed_path = tmp_path / "seeds.json"
    seed_path.write_text(
        json.dumps(_reviewed_seeds().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    seed_path.chmod(0o600)
    output = tmp_path / "pending"
    transport = _PipelineTransport()
    monkeypatch.setattr(
        open_dart_cli,
        "UrllibOpenDartTransport",
        lambda _key, max_requests: transport,
    )

    assert (
        open_dart_cli.main(
            [
                "--api-key-file",
                str(key_path),
                "--entity-seeds",
                str(seed_path),
                "--begin-date",
                "2026-01-01",
                "--output-root",
                str(output),
            ]
        )
        == 0
    )

    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "not_authorized_for_retrieval_or_release"
    assert summary["evidence_cutoff"] == "2026-08-24"
    assert summary["document_count"] == 1
    assert [item[0] for item in transport.calls] == [
        "/api/corpCode.xml",
        "/api/list.json",
        "/api/document.xml",
    ]
    assert transport.calls[1][1]["end_de"] == "20260824"
    manifest_bytes = (output / "open-dart-pending-manifest.json").read_bytes()
    assert _KEY.encode() not in manifest_bytes


def test_provided_scope_collects_exact_bindings_and_records_unmatched(
    tmp_path: Path,
) -> None:
    scope = _provided_scope_with_unmatched_seed()

    result = _collect_open_dart_pending_from_entities(
        entity_seeds=scope.seeds,
        transport=_PipelineTransport(),
        output_root=tmp_path / "provided-scope-pending",
        begin_date=date(2026, 1, 1),
        collected_at_utc=_COLLECTED_AT,
        max_documents=5,
        require_all_entities=False,
        provided_scope=scope,
    )

    assert len(result.manifest.entity_selection.bindings) == 1
    assert result.manifest.entity_selection.unmatched_entity_ids == (
        "odscope-22222222222222222222222222222222",
    )
    assert result.manifest.entity_scope is not None
    assert result.manifest.entity_scope.source_relation_index_sha256 == "b" * 64
    assert result.manifest.entity_scope.seed_set_sha256 == (
        result.manifest.entity_selection.seed_set_sha256
    )


def test_collection_stops_before_materialization_when_entity_is_not_unique(tmp_path: Path) -> None:
    seeds = OpenDartReviewedEntitySeeds.model_validate(
        {
            **_reviewed_seeds().model_dump(mode="json"),
            "seeds": [
                {
                    "entity_id": "provided:missing",
                    "legal_name": "Missing Company",
                    "reviewed_aliases": [],
                    "seed_source": "provided_db",
                }
            ],
        }
    )
    output = tmp_path / "must-not-exist"

    with pytest.raises(OpenDartCollectionError, match="did not resolve uniquely"):
        collect_open_dart_pending_from_seeds(
            reviewed_seeds=seeds,
            transport=_PipelineTransport(),
            output_root=output,
            begin_date=date(2026, 1, 1),
            collected_at_utc=_COLLECTED_AT,
        )

    assert not output.exists()
