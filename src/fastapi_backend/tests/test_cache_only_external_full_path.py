from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.external_knowledge_router import ExternalKnowledgeRouter
from finance_agent_core.agent.external_search_planning import (
    ExternalSearchPlanCompiler,
    ProvidedProductUniverseBuilder,
)
from finance_agent_core.agent.providers import (
    HyperClovaXFinalAnswerProvider,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.backend import BackendAnswerMode
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.execution.external_knowledge_runtime import ExternalKnowledgeRuntime
from finance_agent_core.execution.official_product_facts import (
    SqliteOfficialProductFactProvider,
)
from finance_agent_core.normalization import normalize_domestic_etp_row
from finance_agent_core.retrieval import external_knowledge_release as release_module
from finance_agent_core.retrieval.corpus import (
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    ExternalCorpusIntakeSpec,
    approved_corpus_manifest_bytes,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
)
from finance_agent_core.retrieval.document_dense import (
    build_approved_document_dense_artifact,
    build_document_cache_activation_receipt,
    write_approved_document_dense_artifact,
    write_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.document_discovery import (
    build_release_pinned_cache_first_discovery,
)
from finance_agent_core.retrieval.external_knowledge_release import (
    build_cache_first_external_knowledge_bootstrap,
)
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
)
from finance_agent_core.retrieval.official_connector import OfficialSourceConnector
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationStore,
    seal_temporal_relation_bundle,
    temporal_relation_bundle_bytes,
)
from finance_agent_core.storage import write_domestic_etp_database

from app.config import Settings
from app.main import create_app

_KURE_REVISION = "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
_KURE_SNAPSHOT_SHA256 = "9" * 64
_QUESTION = "오로라소재를 실제 구성종목으로 보유한 국내 ETF를 알려줘."
_SOURCE_URI = "https://www.samsungfund.com/etf/product/view.do?id=SYNTHETIC1"


class _FakeKureEmbedding:
    """Small deterministic stand-in with the immutable KURE production identity."""

    metadata = EmbeddingProviderMetadata(
        provider_kind="frozen_model",
        provider_id="fake-kure-cache-only-full-path",
        model_id="nlpai-lab/KURE-v1",
        model_revision=_KURE_REVISION,
        license_id="cc-by-nc-sa-4.0",
        dimension=8,
        pooling="cls",
    )
    artifact_gate_evidence = SimpleNamespace(
        manifest_file_sha256=_KURE_SNAPSHOT_SHA256,
        snapshot_file_manifest_sha256=_KURE_SNAPSHOT_SHA256,
    )

    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [float(value + 1) for value in hashlib.sha256(text.encode()).digest()[:8]]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)


class _NoNetworkTransport:
    calls = 0

    def fetch(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("cache-only E2E attempted an external network fetch")


class _FakeHcxTransport:
    """HCX-shaped Intent/Semantic Resolver/final generation without a remote call."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.operations.append(request.operation)
        if request.operation == "intent_analysis":
            content = {
                "schema_version": "1.0",
                "semantic_frame_version": "2.0",
                "intent": "search",
                "product_families": ["domestic_etp"],
                "evidence_spans": ["오로라소재", "실제 구성종목으로 보유한", "국내 ETF"],
                "needs_clarification": False,
                "knowledge_requirements": [
                    {
                        "capability": "portfolio_holdings",
                        "subject_span": "오로라소재",
                        "relation_span": "실제 구성종목으로 보유한",
                        "search_aliases": [],
                    }
                ],
                "execution_clauses": [
                    {
                        "sequence": 1,
                        "role": "relation",
                        "text_span": "오로라소재를 실제 구성종목으로 보유한",
                        "product_families": ["domestic_etp"],
                    },
                    {
                        "sequence": 2,
                        "role": "scope",
                        "text_span": "국내 ETF",
                        "product_families": ["domestic_etp"],
                    },
                ],
            }
        elif request.operation == "semantic_resolver":
            content = {
                "schema_version": "1.0",
                "aliases": [
                    {
                        "subject_span": "오로라소재",
                        "search_aliases": ["Aurora Materials"],
                    }
                ],
            }
        elif request.operation == "final_answer":
            properties = request.response_schema["properties"]
            content = {
                "schema_version": "1.0",
                "lead": properties["lead"]["enum"][0],
                "answer_body_sha256": properties["answer_body_sha256"]["enum"][0],
            }
        else:  # pragma: no cover - a new provider operation must be reviewed explicitly
            raise AssertionError(f"unexpected HCX operation: {request.operation}")
        return {
            "status_code": 200,
            "content": json.dumps(content, ensure_ascii=False),
            "request_id": f"fake-hcx-cache-only-{len(self.operations):03d}",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }


def _domestic_database(tmp_path: Path) -> Path:
    records = []
    for row, name, ticker in (
        (2, "KODEX 미래산업", "A000002"),
        (3, "KODEX 평온채권", "A000003"),
    ):
        records.append(
            normalize_domestic_etp_row(
                source_row=row,
                present_source_fields=65,
                source_snapshot_date=date(2026, 8, 24),
                values={
                    "pd_exg_mkt_cd": "EXG_MKT_NO_001",
                    "pd_itm_no": f"KR7{row:09d}",
                    "pd_itm_no_ma": ticker,
                    "pd_grp_no": "ETF",
                    "pd_nm": name,
                    "pd_abrv_nm": name,
                    "pd_sale_yn": "1",
                    "pd_tr_yn": "0",
                    "pd_pen_tr_yn": "Y",
                    "pd_risk_nm": "높은위험(2등급)",
                    "pd_curr_cd": "CURR_CD_KRW",
                    "wu_inv_ast_type": "주식",
                    "wu_inv_rgn": "한국",
                    "wu_core_yn": "N",
                    "cu_fund_mgmt_co": "삼성자산운용",
                    "cu_strtegy": "실물복제",
                    "cu_lev_fector": "1",
                    "cu_charge_rt": "0.4",
                    "du_last_aum": "100000000",
                    "du_clpr": "10000",
                    "du_er_1d": "1",
                    "du_er_1m": str(10 + row),
                    "du_er_3m": "2",
                    "du_er_6m": "3",
                    "du_er_1y": "4",
                    "du_er_ytd": "5",
                    "du_val_1d": "1000000",
                    "wu_upt_dt": "20260822",
                    "du_upt_dt": "20260822",
                },
            )
        )
    path = tmp_path / "domestic-etp.sqlite3"
    write_domestic_etp_database(
        path,
        records,
        DatabaseManifest(
            dataset="domestic_etp",
            registry_schema_version="1.4",
            source_file_name="synthetic_cache_only_domestic_etp.xlsx",
            source_file_sha256="a" * 64,
            source_file_size_bytes=1234,
            source_snapshot_date=date(2026, 8, 24),
            total_rows=len(records),
            searchable_rows=len(records),
            quarantined_rows=0,
        ),
    )
    return path


def _coverage(corpus_sha256: str, dense_sha256: str) -> ApprovedKnowledgeCoverageManifest:
    entries = []
    for capability in KnowledgeCapability:
        enabled = capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        entries.append(
            KnowledgeCoverageEntry(
                capability=capability,
                status="approved" if enabled else "not_configured",
                runtime_execution_enabled=enabled,
                allowed_source_kinds=(
                    ("provided", "external_approved") if enabled else ("provided",)
                ),
                corpus_sha256=corpus_sha256 if enabled else None,
                index_sha256=dense_sha256 if enabled else None,
            )
        )
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="synthetic-cache-only-e2e",
        dataset_release_id="synthetic-20260824",
        capabilities=tuple(entries),
    )


def _approved_cache_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "approved-corpus"
    (root / "documents").mkdir(parents=True)
    text = (
        "2026-08-20 기준 공식 구성종목 표. "
        "KODEX 미래산업은 오로라소재를 실제 구성종목으로 보유합니다. "
        "제공 DB에 없는 KODEX 외부전용도 오로라소재를 보유합니다."
    )
    (root / "documents/holdings.txt").write_text(text, encoding="utf-8")
    reviewed_at = datetime(2026, 8, 21, tzinfo=UTC)
    manifest = seal_approved_corpus_manifest(
        ExternalCorpusIntakeSpec(
            corpus_id="synthetic-cache-only-e2e",
            reviews=(
                CorpusReview(
                    reviewer_role="data_rights",
                    reviewer_id="synthetic-rights-reviewer",
                    reviewed_at_utc=reviewed_at,
                    note="합성 fixture 저장·검색 권한 검토",
                ),
                CorpusReview(
                    reviewer_role="finance_domain",
                    reviewer_id="synthetic-finance-reviewer",
                    reviewed_at_utc=reviewed_at,
                    note="합성 구성종목 관계와 시점 검토",
                ),
            ),
            documents=(
                CorpusDocumentSpec(
                    document_id="synthetic-holdings-document",
                    relative_path="documents/holdings.txt",
                    title="KODEX 미래산업 공식 구성종목",
                    publisher="삼성자산운용",
                    source_registry_id="samsungfund-kodex-product",
                    source_uri=_SOURCE_URI,
                    capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                    collected_at_utc=datetime(2026, 8, 20, tzinfo=UTC),
                    published_at=date(2026, 8, 20),
                    as_of=date(2026, 8, 20),
                    language="ko",
                    media_type="text/plain",
                    purposes=("holdings",),
                    license=CorpusLicenseApproval(
                        license_id="synthetic-test-approval",
                        license_uri="https://www.samsungfund.com/",
                        storage_allowed=True,
                        retrieval_allowed=True,
                        competition_use_allowed=True,
                        deployment_bundle_allowed=True,
                        attribution_text="합성 테스트용 승인",
                    ),
                ),
            ),
        ),
        root,
    )
    manifest_path = root / "approved-manifest.json"
    manifest_path.write_bytes(approved_corpus_manifest_bytes(manifest))
    corpus = verify_approved_corpus(manifest_path, root)
    embedding = _FakeKureEmbedding()
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=embedding,
        embedding_snapshot_manifest_sha256=_KURE_SNAPSHOT_SHA256,
    )
    registry = load_official_source_registry()
    activation = build_document_cache_activation_receipt(
        corpus,
        dense,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=1.0,
        minimum_dense_margin=2.0,
        top_k=1,
    )
    dense_path = tmp_path / "approved-document-dense.json"
    activation_path = tmp_path / "approved-document-activation.json"
    dense_file_sha256 = write_approved_document_dense_artifact(dense, dense_path)
    activation_file_sha256 = write_document_cache_activation_receipt(
        activation,
        activation_path,
    )
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus.manifest_sha256,
        registry=registry,
    )
    relation_path = tmp_path / "approved-relations.json"
    relation_bytes = temporal_relation_bundle_bytes(relation_bundle)
    relation_path.write_bytes(relation_bytes)
    relation_file_sha256 = hashlib.sha256(relation_bytes).hexdigest()
    coverage = _coverage(corpus.manifest_sha256, dense.canonical_sha256)
    monkeypatch.setattr(
        release_module,
        "load_approved_knowledge_coverage_manifest",
        lambda: coverage,
    )
    bootstrap = build_cache_first_external_knowledge_bootstrap(
        approved_corpus_manifest_path=manifest_path,
        approved_corpus_root=root,
        expected_approved_corpus_manifest_sha256=corpus.manifest_sha256,
        document_dense_artifact_path=dense_path,
        expected_document_dense_file_sha256=dense_file_sha256,
        activation_receipt_path=activation_path,
        expected_activation_receipt_file_sha256=activation_file_sha256,
        relation_bundle_path=relation_path,
        expected_relation_bundle_file_sha256=relation_file_sha256,
        retrieval_mode="approved_corpus_cache_only",
    )
    discovery = build_release_pinned_cache_first_discovery(
        activation_receipt_path=activation_path,
        expected_activation_receipt_sha256=activation_file_sha256,
        document_dense_artifact_path=dense_path,
        expected_document_dense_sha256=dense_file_sha256,
        expected_corpus_manifest_sha256=corpus.manifest_sha256,
        expected_document_bm25_sha256=activation.document_bm25_sha256,
        expected_embedding_model_revision=_KURE_REVISION,
        embedding=embedding,
        fallback=None,
        registry=registry,
        approved_corpus_manifest_path=manifest_path,
        approved_corpus_root=root,
        cache_only_coverage_manifest=coverage,
    )
    return bootstrap, discovery, registry, coverage, embedding, activation


def test_fastapi_cache_only_external_full_path_uses_db_intersection_and_hcx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _domestic_database(tmp_path)
    bootstrap, discovery, registry, coverage, embedding, activation = (
        _approved_cache_release(tmp_path, monkeypatch)
    )
    transport = _NoNetworkTransport()
    store = TemporalRelationStore(bootstrap.relation_bundle, registry)
    acquisition = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=discovery,
        connector=OfficialSourceConnector(transport, registry),
    )
    paths = {ProductFamily.DOMESTIC_ETP: database}
    runtime = ExternalKnowledgeRuntime(
        acquisition=acquisition,
        product_fact_provider=SqliteOfficialProductFactProvider(
            paths,
            verify_approved_databases=False,
        ),
        release_artifact=bootstrap.artifact,
        release_artifact_file_sha256=bootstrap.artifact_file_sha256,
    )
    hcx = _FakeHcxTransport()
    hcx_settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
    agent = RoutedFinanceAgent(
        paths,
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(
            hcx_settings,
            hcx,
            require_execution_structure=True,
        ),
        official_answer_provider=HyperClovaXFinalAnswerProvider(hcx_settings, hcx),
        require_official_hclx=True,
        external_knowledge_router=ExternalKnowledgeRouter(
            coverage=_coverage(
                bootstrap.artifact.corpus_manifest_sha256,
                bootstrap.artifact.document_dense_sha256,
            )
        ),
        external_knowledge_runtime=runtime,
        external_universe_builder=ProvidedProductUniverseBuilder(
            paths,
            verify_approved_databases=False,
        ),
        external_search_plan_compiler=ExternalSearchPlanCompiler(registry),
    )
    application = create_app(
        settings=Settings(OFFICIAL_ANSWER_TIMEOUT_SECONDS=5),
        agent=agent,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": "CACHE-ONLY-E2E", "question": _QUESTION},
        )

    body = OfficialAnswerResponse.model_validate(response.json())
    trace = json.loads(body.think_trace)
    context = json.loads(body.retrieved_context)
    assert response.status_code == 200, response.text
    assert trace["status"] == "success"
    assert trace["answer_mode"] == BackendAnswerMode.LLM_GROUNDED.value
    assert trace["filters"] == [
        {
            "requirement_id": trace["filters"][0]["requirement_id"],
            "capability": "portfolio_holdings",
            "predicate": "holds_company",
            "subject_evidence_span": "오로라소재",
            "relation_evidence_span": "실제 구성종목으로 보유한",
            "product_families": ["domestic_etp"],
        }
    ]
    assert "provided_db_candidate_freeze" in trace["execution_steps"]
    assert "verified_external_db_intersection" in trace["execution_steps"]
    assert [item["product_id"] for item in context["evidence"]["products"]] == ["KR7000000002"]
    assert context["evidence"]["documents"][0]["document_id"] == "synthetic-holdings-document"
    assert context["evidence"]["documents"][0]["source_uri"] == _SOURCE_URI
    assert context["evidence"]["documents"][0]["as_of"] == "2026-08-20"
    assert "오로라소재" in context["evidence"]["documents"][0]["text"]
    assert discovery.cache_queries >= 1
    assert discovery.cache_hits >= 1
    assert discovery.cache_evidence_hits >= 1
    assert discovery.live_fallback_queries == 0
    assert embedding.document_calls == 1
    assert embedding.query_calls >= 1
    assert bootstrap.artifact.retrieval_mode == "approved_corpus_cache_only"
    assert bootstrap.artifact.coverage_manifest_sha256 == coverage.canonical_sha256
    assert bootstrap.artifact.document_bm25_sha256 == activation.document_bm25_sha256
    assert transport.calls == 0
    assert hcx.operations[0] == "intent_analysis"
    assert hcx.operations[-1] == "final_answer"
