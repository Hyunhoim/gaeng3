from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingSnapshotManifestV2,
    create_schema_embedding_snapshot_manifest,
    write_schema_embedding_snapshot_manifest,
)
from finance_agent_core.evaluation.schema_embedding_external_v2 import (
    ArtifactBoundSchemaCandidateProvider,
    CandidateLockError,
    ExternalBlindPredictionReceipt,
    ExternalBlindPrivateAnswerKey,
    ExternalBundleUnavailableError,
    run_and_freeze_question_only_predictions,
    score_revealed_bundle_files,
)
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    EmbeddingProviderMetadata,
    SchemaFieldCandidate,
    build_schema_field_entries,
)

_IMPLEMENTATION_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_IMAGE_REFERENCE = "registry.example/schema-blind@sha256:" + "a" * 64
_TRACKED_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[3]
    / "evaluation"
    / "protocols"
    / "schema-embedding-external-blind-v2.protocol.json"
)


def _sha256(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode()
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: object) -> bytes:
    return f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n".encode()


def _labels() -> list[tuple[str, str]]:
    intents = [
        *(["search"] * 24),
        *(["detail"] * 12),
        *(["compare"] * 16),
        *(["aggregate"] * 12),
        *(["explain"] * 12),
        *(["clarify"] * 12),
        *(["unsupported"] * 12),
    ]
    families = [
        *(["overseas_etp"] * 25),
        *(["domestic_etp"] * 25),
        *(["bond"] * 25),
        *(["fund"] * 25),
    ]
    return list(zip(families, intents, strict=True))


def _question_payload() -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "suite_id": "external-blind-v1-100",
        "status": "question_only_without_gold_labels",
        "cases": [
            {
                "id": f"external-blind-v1-{index:03d}",
                # Markers exist only in this unit fixture.  The phase-1 schema
                # itself exposes no separate family, intent, or rationale field.
                "question": (
                    f"단위 테스트 질문 {index:03d} family-{family} intent-{intent} "
                    "상품명에 alpha 포함"
                ),
            }
            for index, (family, intent) in enumerate(_labels(), start=1)
        ],
    }


def _answer_payload() -> dict[str, object]:
    cases = []
    for index, (family, intent) in enumerate(_labels(), start=1):
        case_id = f"external-blind-v1-{index:03d}"
        disposition = (
            "clarify"
            if intent == "clarify"
            else "unsupported"
            if intent == "unsupported"
            else "execute"
        )
        plan = None
        plan_intent = None
        candidate_count = None
        if disposition == "execute":
            plan_intent = "search"
            candidate_count = 0
            plan = {
                "schema_version": "1.0",
                "question_id": case_id,
                "intent": "search",
                "product_families": [family],
                "constraints": [
                    {
                        "field": "product_name",
                        "operator": "contains",
                        "value": "alpha",
                        "unit": "none",
                        "strength": "locked",
                    }
                ],
                "ranking": [],
                "projection": ["product_name"],
                "limit": 5,
                "intent_payload": {
                    "comparison_fields": [],
                    "group_by": [],
                    "aggregations": [],
                    "explain_product_ids": [],
                },
                "ambiguities": [],
                "unsupported_conditions": [],
            }
        cases.append(
            {
                "id": case_id,
                "expected_product_family": family,
                "expected_interaction_intent": intent,
                "expected_disposition": disposition,
                "expected_query_plan_intent": plan_intent,
                "expected_query_plan": plan,
                "gold_schema_field_ids": (["product_name"] if disposition == "execute" else []),
                "expected_candidate_count": candidate_count,
                "expected_product_ids": [],
                "required_answer_checks": [],
                "rationale": ("두 단계 blind 계약의 단위 테스트를 위한 비공개 정답입니다."),
            }
        )
    return {
        "schema_version": "2.0",
        "suite_id": "external-blind-v1-100",
        "status": "private_labels_and_gold_before_reveal",
        "reviewer_role": "financial_domain",
        "database_sha256_by_family": {
            family.value: _sha256(f"{family.value}-database") for family in ProductFamily
        },
        "cases": cases,
    }


def _write_question_answer_bundle(root: Path) -> tuple[Path, Path]:
    question_raw = _json_bytes(_question_payload())
    answer_raw = _json_bytes(_answer_payload())
    question_path = root / "questions.json"
    answer_path = root / "answers.json"
    question_path.write_bytes(question_raw)
    answer_path.write_bytes(answer_raw)
    return question_path, answer_path


def _write_commitment(
    root: Path,
    *,
    question_path: Path,
    answer_path: Path,
    protocol_sha256: str,
    reference_corpus_sha256: str,
    near_duplicate_report_sha256: str,
) -> Path:
    commitment = {
        "schema_version": "2.0",
        "protocol_id": "schema-embedding-external-blind-v2",
        "suite_id": "external-blind-v1-100",
        "status": "sealed_external_before_reveal",
        "author_role": "financial_domain",
        "implementation_commit": _IMPLEMENTATION_COMMIT,
        "questions_sha256": _sha256(question_path.read_bytes()),
        "answers_sha256": _sha256(answer_path.read_bytes()),
        "question_count": 100,
        "created_at_utc": "2026-08-13T00:00:00Z",
        "protocol_sha256": protocol_sha256,
        "reference_corpus_sha256": reference_corpus_sha256,
        "near_duplicate_max_similarity": 0.84,
        "near_duplicate_report_sha256": near_duplicate_report_sha256,
    }
    commitment_path = root / "commitment.json"
    commitment_path.write_bytes(_json_bytes(commitment))
    return commitment_path


def _write_model_manifest(root: Path, alias: str) -> tuple[Path, Path]:
    snapshot = root / f"{alias}-snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_bytes(f"{alias}-config".encode())
    (snapshot / "model.safetensors").write_bytes(f"{alias}-weights".encode())
    (snapshot / "tokenizer.json").write_bytes(f"{alias}-tokenizer".encode())
    manifest = create_schema_embedding_snapshot_manifest(snapshot, alias=alias)
    path = root / f"{alias}-manifest.json"
    write_schema_embedding_snapshot_manifest(manifest, path)
    return path, snapshot


def _fixture_protocol(
    bge_manifest_path: Path,
    kure_manifest_path: Path,
):
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    tracked = module.load_external_blind_v2_protocol(_TRACKED_PROTOCOL_PATH)

    def candidate(path: Path, priority: int):
        manifest = SchemaEmbeddingSnapshotManifestV2.model_validate_json(path.read_bytes())
        return module.ProtocolModelCandidate(
            priority=priority,
            alias=manifest.candidate.alias,
            repository=manifest.candidate.model_id,
            revision=manifest.candidate.revision,
            snapshot_manifest_path=f"unit/{manifest.candidate.alias}.json",
            snapshot_manifest_file_sha256=_sha256(path.read_bytes()),
            snapshot_file_manifest_sha256=manifest.snapshot_file_manifest_sha256,
            weights_file_manifest_sha256=(
                manifest.category_digests["weights"].file_manifest_sha256
            ),
            tokenizer_file_manifest_sha256=(
                manifest.category_digests["tokenizer"].file_manifest_sha256
            ),
            config_file_manifest_sha256=(manifest.category_digests["config"].file_manifest_sha256),
            other_file_manifest_sha256=(manifest.category_digests["other"].file_manifest_sha256),
        )

    contract = module.ExternalBlindV2ProtocolLock(
        schema_version="2.0",
        protocol_id="schema-embedding-external-blind-v2",
        candidate_lock=(
            module.ProtocolLexicalCandidate(
                priority=0,
                alias="lexical",
                algorithm="server_build_lexical_hints_v1",
                role="frozen_baseline",
            ),
            candidate(bge_manifest_path, 1),
            candidate(kure_manifest_path, 2),
        ),
        reference_corpus=tracked.contract.reference_corpus,
    )
    return module._LoadedProtocol(contract=contract, raw_sha256=tracked.raw_sha256)


def _intent(question: str) -> InteractionIntent:
    return InteractionIntent(question.split("intent-", 1)[1].split(" ", 1)[0])


def _family(question: str) -> ProductFamily:
    return ProductFamily(question.split("family-", 1)[1].split(" ", 1)[0])


class _FixtureRouter:
    """Mechanics-only test double installed in place of the fixed Router class."""

    def route(self, question: str, request_id: str) -> RouteDecision:
        intent = _intent(question)
        family = _family(question)
        disposition = (
            RouteDisposition.CLARIFY
            if intent is InteractionIntent.CLARIFY
            else RouteDisposition.UNSUPPORTED
            if intent is InteractionIntent.UNSUPPORTED
            else RouteDisposition.EXECUTE
        )
        return RouteDecision(
            draft=MinimalQueryDraft(
                request_id=request_id,
                question=question,
                intent=intent,
                product_families=[family],
                product_mentions=[],
                requested_limit=5,
            ),
            disposition=disposition,
            reason_code=f"fixture_{disposition.value}",
            reason="독립 blind 메커니즘 단위 테스트용 고정 경로입니다.",
            query_plan_intent=(Intent.SEARCH if disposition is RouteDisposition.EXECUTE else None),
            capability_matrix_version="1.0",
        )


class _MisroutesOneGoldControlRouter(_FixtureRouter):
    """Makes one gold CLARIFY case executable without seeing the answer key."""

    def route(self, question: str, request_id: str) -> RouteDecision:
        decision = super().route(question, request_id)
        if request_id != "external-blind-v1-077":
            return decision
        return RouteDecision(
            draft=decision.draft.model_copy(update={"intent": InteractionIntent.SEARCH}),
            disposition=RouteDisposition.EXECUTE,
            reason_code="fixture_false_execute",
            reason=("Dense 미실행 평가가 실제 gold control을 사용하는지 검증합니다."),
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version="1.0",
        )


def _vector(key: str, dimension: int = 64) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{key}:{counter}".encode()).digest()
        values.extend(1.0 if byte & 1 else -1.0 for byte in digest)
        counter += 1
    return values[:dimension]


class _FixtureCpuProvider:
    def __init__(self, alias: str, evidence) -> None:
        candidate = evidence.candidate
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id=f"fixture-{alias}",
            model_id=candidate.model_id,
            model_revision=candidate.revision,
            license_id="mit-test-only",
            dimension=64,
            pooling="mean",
        )
        self.artifact_gate_evidence = evidence

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _vector(text)


class _PredictableIndex:
    def __init__(self, trusted: DenseSchemaIndex) -> None:
        self.manifest = trusted.manifest
        self.provider = trusted.provider
        self.calls: list[tuple[str, ProductFamily]] = []

    def search(
        self,
        question: str,
        family: ProductFamily,
        *,
        top_k: int,
    ) -> list[SchemaFieldCandidate]:
        del top_k
        self.calls.append((question, family))
        control = _intent(question) in {
            InteractionIntent.CLARIFY,
            InteractionIntent.UNSUPPORTED,
        }
        scores = (0.2, 0.19) if control else (0.9, 0.5)
        return [
            SchemaFieldCandidate(
                product_family=family,
                field_id="product_name",
                score=scores[0],
                rank=1,
            ),
            SchemaFieldCandidate(
                product_family=family,
                field_id="product_id",
                score=scores[1],
                rank=2,
            ),
        ]


def _prepare_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    router_type: type[_FixtureRouter] = _FixtureRouter,
    authorization_issued_at_utc: str = "2026-08-13T00:30:00Z",
    prediction_created_at_utc: str = "2026-08-13T01:00:00Z",
):
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    question_path, answer_path = _write_question_answer_bundle(root)
    bge_manifest, bge_snapshot = _write_model_manifest(root, "bge-m3")
    kure_manifest, kure_snapshot = _write_model_manifest(root, "kure-v1")
    protocol = _fixture_protocol(bge_manifest, kure_manifest)
    monkeypatch.setattr(module, "load_external_blind_v2_protocol", lambda _path: protocol)
    questions = module.ExternalBlindQuestionOnlySet.model_validate_json(question_path.read_bytes())
    reference_report = module.build_external_blind_v2_reference_report(
        questions,
        raw_questions_sha256=_sha256(question_path.read_bytes()),
        protocol=protocol,
    )
    reference_report_path = root / "near-duplicate-report.json"
    reference_report_path.write_text(
        f"{reference_report.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    commitment_path = _write_commitment(
        root,
        question_path=question_path,
        answer_path=answer_path,
        protocol_sha256=protocol.raw_sha256,
        reference_corpus_sha256=reference_report.reference_corpus_sha256,
        near_duplicate_report_sha256=_sha256(reference_report_path.read_bytes()),
    )
    artifacts = module.load_candidate_artifact_lock(
        protocol=protocol,
        bge_manifest_path=bge_manifest,
        bge_snapshot_dir=bge_snapshot,
        kure_manifest_path=kure_manifest,
        kure_snapshot_dir=kure_snapshot,
    )
    monkeypatch.setattr(module, "IntentRouter", router_type)
    monkeypatch.setattr(module, "VerifiedSentenceTransformerCpuProvider", _FixtureCpuProvider)
    providers = {}
    indexes = {}
    for loaded in artifacts:
        alias = loaded.manifest.candidate.alias
        provider = _FixtureCpuProvider(alias, loaded.gate_evidence)
        base = DenseSchemaIndex.build(build_schema_field_entries(), provider)
        index = _PredictableIndex(base)
        indexes[alias] = index
    monkeypatch.setattr(module, "DenseSchemaIndex", _PredictableIndex)
    for loaded in artifacts:
        alias = loaded.manifest.candidate.alias
        index = indexes[alias]
        providers[alias] = ArtifactBoundSchemaCandidateProvider(
            loaded,
            index,  # type: ignore[arg-type]
        )
    monkeypatch.setattr(
        module,
        "_load_and_build_official_candidates",
        lambda *_args, **_kwargs: providers,
    )
    authorization = {
        "schema_version": "1.0",
        "status": "authorized_by_independent_evaluator",
        "evaluator_role": "independent_external_evaluator",
        "implementation_commit": _IMPLEMENTATION_COMMIT,
        "image_reference": _IMAGE_REFERENCE,
        "platform": "linux/amd64",
        "clean_source_tree": True,
        "questions_sha256": _sha256(question_path.read_bytes()),
        "protocol_sha256": protocol.raw_sha256,
        "reference_corpus_sha256": reference_report.reference_corpus_sha256,
        "near_duplicate_max_similarity": 0.84,
        "near_duplicate_report_sha256": _sha256(reference_report_path.read_bytes()),
        "bge_manifest_sha256": _sha256(bge_manifest.read_bytes()),
        "kure_manifest_sha256": _sha256(kure_manifest.read_bytes()),
        "issued_at_utc": authorization_issued_at_utc,
        "external_authorization_receipt_sha256": "c" * 64,
    }
    authorization_path = root / "execution-authorization.json"
    authorization_path.write_bytes(_json_bytes(authorization))
    prediction_path = root / "predictions.json"
    artifact = run_and_freeze_question_only_predictions(
        question_path=question_path,
        commitment_path=commitment_path,
        execution_authorization_path=authorization_path,
        protocol_path=_TRACKED_PROTOCOL_PATH,
        near_duplicate_report_path=reference_report_path,
        bge_manifest_path=bge_manifest,
        bge_snapshot_dir=bge_snapshot,
        kure_manifest_path=kure_manifest,
        kure_snapshot_dir=kure_snapshot,
        output_path=prediction_path,
        implementation_commit=_IMPLEMENTATION_COMMIT,
        created_at_utc=prediction_created_at_utc,
        bge_trusted_cache_root=root,
        kure_trusted_cache_root=root,
    )
    return {
        "artifact": artifact,
        "indexes": indexes,
        "prediction": prediction_path,
        "questions": question_path,
        "answers": answer_path,
        "commitment": commitment_path,
        "authorization": authorization_path,
        "protocol": _TRACKED_PROTOCOL_PATH,
        "near_duplicate_report": reference_report_path,
        "bge_manifest": bge_manifest,
        "bge_snapshot": bge_snapshot,
        "kure_manifest": kure_manifest,
        "kure_snapshot": kure_snapshot,
        "providers": providers,
    }


def _write_receipt(
    run: dict[str, object],
    *,
    prediction_sha: str | None = None,
    recorded_at_utc: str = "2026-08-13T01:01:00Z",
) -> Path:
    artifact = run["artifact"]
    prediction = run["prediction"]
    assert isinstance(prediction, Path)
    receipt = ExternalBlindPredictionReceipt(
        status="prediction_hash_recorded_externally_before_answer_reveal",
        evaluator_role="independent_external_evaluator",
        prediction_artifact_sha256=prediction_sha or _sha256(prediction.read_bytes()),
        questions_sha256=artifact.lock.questions_sha256,  # type: ignore[union-attr]
        implementation_commit=_IMPLEMENTATION_COMMIT,
        image_reference=_IMAGE_REFERENCE,
        recorded_at_utc=recorded_at_utc,
        external_locator="append-only://independent-evaluator/schema-blind/receipt-001",
    )
    path = prediction.parent / "prediction-receipt.json"
    path.write_text(f"{receipt.model_dump_json(indent=2)}\n", encoding="utf-8")
    return path


def test_phase_one_schema_rejects_family_intent_and_author_note() -> None:
    from finance_agent_core.evaluation.schema_embedding_external_v2 import (
        ExternalBlindQuestionOnlySet,
    )

    payload = _question_payload()
    payload["cases"][0]["product_family"] = "bond"  # type: ignore[index]
    payload["cases"][0]["intent"] = "search"  # type: ignore[index]
    payload["cases"][0]["author_note"] = "hidden rationale"  # type: ignore[index]

    with pytest.raises(ValueError, match="extra"):
        ExternalBlindQuestionOnlySet.model_validate(payload)


def test_private_answer_requires_explicit_gold_for_execute_and_empty_gold_for_controls() -> None:
    payload = _answer_payload()
    payload["cases"][0]["gold_schema_field_ids"] = []  # type: ignore[index]

    with pytest.raises(ValueError, match="explicit schema-field gold"):
        ExternalBlindPrivateAnswerKey.model_validate(payload)

    payload = _answer_payload()
    payload["cases"][76]["gold_schema_field_ids"] = ["product_name"]  # type: ignore[index]

    with pytest.raises(ValueError, match="control private answers"):
        ExternalBlindPrivateAnswerKey.model_validate(payload)


def test_private_answer_rejects_unknown_and_cross_family_gold_fields() -> None:
    payload = _answer_payload()
    payload["cases"][0]["gold_schema_field_ids"] = ["not_a_registry_field"]  # type: ignore[index]

    with pytest.raises(ValueError, match="unknown schema-field gold ID"):
        ExternalBlindPrivateAnswerKey.model_validate(payload)

    payload = _answer_payload()
    payload["cases"][0]["gold_schema_field_ids"] = [  # type: ignore[index]
        "product_name",
        "product_name",
    ]
    with pytest.raises(ValueError, match="must be unique"):
        ExternalBlindPrivateAnswerKey.model_validate(payload)

    payload = _answer_payload()
    first_case = payload["cases"][0]  # type: ignore[index]
    family = first_case["expected_product_family"]
    registry = load_field_registry()
    cross_family_field = next(
        field_id for field_id, field in registry.fields.items() if family not in field.datasets
    )
    first_case["gold_schema_field_ids"] = [cross_family_field]

    with pytest.raises(ValueError, match="outside the expected product family"):
        ExternalBlindPrivateAnswerKey.model_validate(payload)


def test_field_scoring_uses_explicit_gold_not_fields_in_query_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    run = _prepare_run(tmp_path, monkeypatch)
    payload = _answer_payload()
    for case in payload["cases"]:  # type: ignore[index]
        if case["expected_disposition"] == "execute":
            case["gold_schema_field_ids"] = ["product_id"]
    answers = ExternalBlindPrivateAnswerKey.model_validate(payload)

    outcomes = module._field_outcomes(run["artifact"], answers, "lexical")

    assert len(outcomes) == 76
    assert not any(item.exact for item in outcomes)


def test_official_runner_exposes_no_provider_or_index_injection() -> None:
    parameters = inspect.signature(run_and_freeze_question_only_predictions).parameters

    assert "providers" not in parameters
    assert "provider" not in parameters
    assert "indexes" not in parameters
    assert "index" not in parameters
    assert parameters["bge_trusted_cache_root"].default is inspect.Parameter.empty
    assert parameters["kure_trusted_cache_root"].default is inspect.Parameter.empty


def test_tracked_protocol_binds_complete_public_model_selection_question_inventory() -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    protocol = module.load_external_blind_v2_protocol(_TRACKED_PROTOCOL_PATH)
    reference = protocol.contract.reference_corpus

    assert reference.source_evaluation_id == "schema-embedding-cpu-public-v1"
    assert reference.source_manifest_resource_name == "schema_embedding_cpu_public_v1.json"
    assert reference.source_manifest_file_sha256 == (
        "1cfb2f5a1aeb9faffa1acbc2a6ad0966f05796e33248fd78e0292609031479db"
    )
    assert reference.policy_migration_resource_name == ("schema_linker_policy_migrations_v1.json")
    assert reference.policy_migration_file_sha256 == (
        "82613954ce1734f34f51f1254d7d8e65d34c966b5048984b9a535d1c45ad5405"
    )
    assert tuple(item.resource_name for item in reference.suites) == (
        "bond_core_50.json",
        "domestic_etp_core_50.json",
        "overseas_etp_core_50.json",
        "fund_core_50.json",
    )
    loaded = module._load_reference_corpus(protocol)
    assert len(loaded.questions) == reference.reference_question_count == 200
    assert loaded.corpus_sha256 == reference.reference_corpus_sha256


def test_cli_run_parser_requires_exact_snapshot_and_cache_inputs(tmp_path: Path) -> None:
    from finance_agent_core.evaluation.schema_embedding_external_v2_cli import build_parser

    arguments = build_parser().parse_args(
        [
            "run",
            "--questions",
            str(tmp_path / "questions.json"),
            "--commitment",
            str(tmp_path / "commitment.json"),
            "--execution-authorization",
            str(tmp_path / "authorization.json"),
            "--protocol",
            str(_TRACKED_PROTOCOL_PATH),
            "--near-duplicate-report",
            str(tmp_path / "near-duplicate-report.json"),
            "--bge-manifest",
            str(tmp_path / "bge.json"),
            "--bge-snapshot",
            str(tmp_path / "bge"),
            "--bge-cache-root",
            str(tmp_path / "cache"),
            "--kure-manifest",
            str(tmp_path / "kure.json"),
            "--kure-snapshot",
            str(tmp_path / "kure"),
            "--kure-cache-root",
            str(tmp_path / "cache"),
            "--implementation-commit",
            _IMPLEMENTATION_COMMIT,
            "--created-at-utc",
            "2026-08-13T01:00:00Z",
            "--output",
            str(tmp_path / "predictions.json"),
        ]
    )

    assert arguments.command == "run"
    assert arguments.bge_cache_root == tmp_path / "cache"
    assert arguments.kure_cache_root == tmp_path / "cache"


@pytest.mark.parametrize(
    "near_duplicate",
    [
        "매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "매수 가능한 국내채권을 매수수익률이 높은 순서로 5개 보여줘",
    ],
)
def test_public_exact_copy_and_light_paraphrase_fail_before_provider_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    near_duplicate: str,
) -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    run = _prepare_run(tmp_path, monkeypatch)
    run["prediction"].unlink()  # type: ignore[union-attr]
    question_path = run["questions"]
    assert isinstance(question_path, Path)
    payload = json.loads(question_path.read_text(encoding="utf-8"))
    payload["cases"][0]["question"] = near_duplicate
    question_path.write_bytes(_json_bytes(payload))
    provider_load_count = 0

    def forbidden_provider_load(*_args, **_kwargs):
        nonlocal provider_load_count
        provider_load_count += 1
        raise AssertionError("provider loading must remain unreachable")

    monkeypatch.setattr(module, "_load_and_build_official_candidates", forbidden_provider_load)

    with pytest.raises(CandidateLockError, match="too similar"):
        run_and_freeze_question_only_predictions(
            question_path=question_path,
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            execution_authorization_path=run["authorization"],  # type: ignore[arg-type]
            protocol_path=run["protocol"],  # type: ignore[arg-type]
            near_duplicate_report_path=run["near_duplicate_report"],  # type: ignore[arg-type]
            bge_manifest_path=run["bge_manifest"],  # type: ignore[arg-type]
            bge_snapshot_dir=run["bge_snapshot"],  # type: ignore[arg-type]
            kure_manifest_path=run["kure_manifest"],  # type: ignore[arg-type]
            kure_snapshot_dir=run["kure_snapshot"],  # type: ignore[arg-type]
            output_path=run["prediction"],  # type: ignore[arg-type]
            implementation_commit=_IMPLEMENTATION_COMMIT,
            created_at_utc="2026-08-13T01:00:00Z",
            bge_trusted_cache_root=tmp_path,
            kure_trusted_cache_root=tmp_path,
        )

    assert provider_load_count == 0
    assert not run["prediction"].exists()  # type: ignore[union-attr]


def test_cli_run_refuses_missing_external_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from finance_agent_core.evaluation.schema_embedding_external_v2_cli import main

    exit_code = main(
        [
            "run",
            "--questions",
            str(tmp_path / "questions.json"),
            "--commitment",
            str(tmp_path / "commitment.json"),
            "--execution-authorization",
            str(tmp_path / "authorization.json"),
            "--protocol",
            str(_TRACKED_PROTOCOL_PATH),
            "--near-duplicate-report",
            str(tmp_path / "near-duplicate-report.json"),
            "--bge-manifest",
            str(tmp_path / "bge.json"),
            "--bge-snapshot",
            str(tmp_path / "bge"),
            "--bge-cache-root",
            str(tmp_path / "cache"),
            "--kure-manifest",
            str(tmp_path / "kure.json"),
            "--kure-snapshot",
            str(tmp_path / "kure"),
            "--kure-cache-root",
            str(tmp_path / "cache"),
            "--implementation-commit",
            _IMPLEMENTATION_COMMIT,
            "--created-at-utc",
            "2026-08-13T01:00:00Z",
            "--output",
            str(tmp_path / "predictions.json"),
        ]
    )

    assert exit_code == 2
    assert "run refused" in capsys.readouterr().err
    assert not (tmp_path / "predictions.json").exists()


def test_official_factory_loads_both_models_before_building_canonical_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    bge_manifest, bge_snapshot = _write_model_manifest(tmp_path, "bge-m3")
    kure_manifest, kure_snapshot = _write_model_manifest(tmp_path, "kure-v1")
    protocol = _fixture_protocol(bge_manifest, kure_manifest)
    artifacts = module.load_candidate_artifact_lock(
        protocol=protocol,
        bge_manifest_path=bge_manifest,
        bge_snapshot_dir=bge_snapshot,
        kure_manifest_path=kure_manifest,
        kure_snapshot_dir=kure_snapshot,
        bge_trusted_cache_root=tmp_path,
        kure_trusted_cache_root=tmp_path,
    )
    events: list[str] = []

    class _RecordingProvider(_FixtureCpuProvider):
        def __init__(self, alias: str, evidence) -> None:
            super().__init__(alias, evidence)
            self.alias = alias

        def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            events.append(f"index:{self.alias}")
            return super().embed_documents(texts)

    def fake_loader(*, alias: str, **_kwargs):
        events.append(f"load:{alias}")
        evidence = next(
            item.gate_evidence for item in artifacts if item.manifest.candidate.alias == alias
        )
        return _RecordingProvider(alias, evidence)

    monkeypatch.setattr(module, "VerifiedSentenceTransformerCpuProvider", _RecordingProvider)
    monkeypatch.setattr(module, "load_verified_schema_embedding_cpu_provider", fake_loader)
    candidates = module._load_and_build_official_candidates(
        artifacts,
        bge_manifest_path=bge_manifest,
        bge_snapshot_dir=bge_snapshot,
        bge_trusted_cache_root=tmp_path,
        kure_manifest_path=kure_manifest,
        kure_snapshot_dir=kure_snapshot,
        kure_trusted_cache_root=tmp_path,
    )

    assert tuple(candidates) == ("bge-m3", "kure-v1")
    assert events == ["load:bge-m3", "load:kure-v1", "index:bge-m3", "index:kure-v1"]


def test_missing_external_authorization_fails_before_prediction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    run["prediction"].unlink()  # type: ignore[union-attr]
    run["authorization"].unlink()  # type: ignore[union-attr]

    with pytest.raises(ExternalBundleUnavailableError, match="authorization"):
        run_and_freeze_question_only_predictions(
            question_path=run["questions"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            execution_authorization_path=run["authorization"],  # type: ignore[arg-type]
            protocol_path=run["protocol"],  # type: ignore[arg-type]
            near_duplicate_report_path=run["near_duplicate_report"],  # type: ignore[arg-type]
            bge_manifest_path=run["bge_manifest"],  # type: ignore[arg-type]
            bge_snapshot_dir=run["bge_snapshot"],  # type: ignore[arg-type]
            kure_manifest_path=run["kure_manifest"],  # type: ignore[arg-type]
            kure_snapshot_dir=run["kure_snapshot"],  # type: ignore[arg-type]
            output_path=run["prediction"],  # type: ignore[arg-type]
            implementation_commit=_IMPLEMENTATION_COMMIT,
            created_at_utc="2026-08-13T01:00:00Z",
            bge_trusted_cache_root=tmp_path,
            kure_trusted_cache_root=tmp_path,
        )
    assert not run["prediction"].exists()  # type: ignore[union-attr]


@pytest.mark.parametrize("alias", ["bge-m3", "kure-v1"])
def test_snapshot_tamper_fails_before_new_prediction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    run["prediction"].unlink()  # type: ignore[union-attr]
    snapshot = run["bge_snapshot" if alias == "bge-m3" else "kure_snapshot"]
    (snapshot / "model.safetensors").write_bytes(b"tampered")  # type: ignore[operator]

    with pytest.raises(CandidateLockError, match="snapshot bytes failed"):
        run_and_freeze_question_only_predictions(
            question_path=run["questions"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            execution_authorization_path=run["authorization"],  # type: ignore[arg-type]
            protocol_path=run["protocol"],  # type: ignore[arg-type]
            near_duplicate_report_path=run["near_duplicate_report"],  # type: ignore[arg-type]
            bge_manifest_path=run["bge_manifest"],  # type: ignore[arg-type]
            bge_snapshot_dir=run["bge_snapshot"],  # type: ignore[arg-type]
            kure_manifest_path=run["kure_manifest"],  # type: ignore[arg-type]
            kure_snapshot_dir=run["kure_snapshot"],  # type: ignore[arg-type]
            output_path=run["prediction"],  # type: ignore[arg-type]
            implementation_commit=_IMPLEMENTATION_COMMIT,
            created_at_utc="2026-08-13T01:00:00Z",
            bge_trusted_cache_root=tmp_path,
            kure_trusted_cache_root=tmp_path,
        )


def test_self_consistent_regenerated_manifest_cannot_replace_protocol_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    run = _prepare_run(tmp_path, monkeypatch)
    run["prediction"].unlink()  # type: ignore[union-attr]
    snapshot = run["bge_snapshot"]
    manifest_path = run["bge_manifest"]
    assert isinstance(snapshot, Path)
    assert isinstance(manifest_path, Path)
    (snapshot / "model.safetensors").write_bytes(b"replacement-weights")
    manifest_path.unlink()
    write_schema_embedding_snapshot_manifest(
        create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3"),
        manifest_path,
    )
    provider_load_count = 0

    def forbidden_provider_load(*_args, **_kwargs):
        nonlocal provider_load_count
        provider_load_count += 1
        raise AssertionError("provider loading must remain unreachable")

    monkeypatch.setattr(module, "_load_and_build_official_candidates", forbidden_provider_load)

    with pytest.raises(CandidateLockError, match="exact tracked protocol snapshot"):
        run_and_freeze_question_only_predictions(
            question_path=run["questions"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            execution_authorization_path=run["authorization"],  # type: ignore[arg-type]
            protocol_path=run["protocol"],  # type: ignore[arg-type]
            near_duplicate_report_path=run["near_duplicate_report"],  # type: ignore[arg-type]
            bge_manifest_path=manifest_path,
            bge_snapshot_dir=snapshot,
            kure_manifest_path=run["kure_manifest"],  # type: ignore[arg-type]
            kure_snapshot_dir=run["kure_snapshot"],  # type: ignore[arg-type]
            output_path=run["prediction"],  # type: ignore[arg-type]
            implementation_commit=_IMPLEMENTATION_COMMIT,
            created_at_utc="2026-08-13T01:00:00Z",
            bge_trusted_cache_root=tmp_path,
            kure_trusted_cache_root=tmp_path,
        )

    assert provider_load_count == 0
    assert not run["prediction"].exists()  # type: ignore[union-attr]


def test_oversized_question_bundle_fails_before_provider_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_agent_core.evaluation import schema_embedding_external_v2 as module

    question_path = tmp_path / "oversized-questions.json"
    question_path.write_bytes(b"x" * (module._MAX_QUESTION_BUNDLE_BYTES + 1))
    provider_load_count = 0

    def forbidden_provider_load(*_args, **_kwargs):
        nonlocal provider_load_count
        provider_load_count += 1
        raise AssertionError("provider loading must remain unreachable")

    monkeypatch.setattr(module, "_load_and_build_official_candidates", forbidden_provider_load)

    with pytest.raises(ExternalBundleUnavailableError, match="safety limit"):
        run_and_freeze_question_only_predictions(
            question_path=question_path,
            commitment_path=tmp_path / "missing-commitment.json",
            execution_authorization_path=tmp_path / "missing-authorization.json",
            protocol_path=_TRACKED_PROTOCOL_PATH,
            near_duplicate_report_path=tmp_path / "missing-reference-report.json",
            bge_manifest_path=tmp_path / "missing-bge.json",
            bge_snapshot_dir=tmp_path / "missing-bge",
            kure_manifest_path=tmp_path / "missing-kure.json",
            kure_snapshot_dir=tmp_path / "missing-kure",
            output_path=tmp_path / "predictions.json",
            implementation_commit=_IMPLEMENTATION_COMMIT,
            created_at_utc="2026-08-13T01:00:00Z",
            bge_trusted_cache_root=tmp_path,
            kure_trusted_cache_root=tmp_path,
        )

    assert provider_load_count == 0


def test_question_only_run_uses_fixed_router_and_global_ood_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    artifact = run["artifact"]

    assert artifact.status == "local_predictions_awaiting_external_receipt"
    assert artifact.answer_key_opened is False
    assert artifact.predicted_non_execute_operational_dense_call_count == 0
    assert artifact.lock.image_reference == _IMAGE_REFERENCE
    control = [
        item for item in artifact.cases if item.route_disposition is not RouteDisposition.EXECUTE
    ]
    assert len(control) == 24
    assert all(not model.operational_dense_called for item in control for model in item.models)
    assert all(
        model.offline_ood_probe.family_source
        == "all_four_approved_families_without_hidden_label_access"
        for item in artifact.cases
        for model in item.models
    )
    # Every model probes four families for all 100 cases; only the 76 executable
    # routes receive one additional operational call.
    assert all(len(index.calls) == 476 for index in run["indexes"].values())


def test_scoring_requires_matching_external_prediction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    bad_receipt = _write_receipt(run, prediction_sha="f" * 64)
    output = tmp_path / "score.json"

    with pytest.raises(ExternalBundleUnavailableError, match="receipt differs"):
        score_revealed_bundle_files(
            prediction_path=run["prediction"],  # type: ignore[arg-type]
            question_path=run["questions"],  # type: ignore[arg-type]
            answer_path=run["answers"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            prediction_receipt_path=bad_receipt,
            output_path=output,
            scored_at_utc="2026-08-13T02:00:00Z",
        )
    assert not output.exists()


def test_pre_inference_chronology_rejects_authorization_before_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ExternalBundleUnavailableError, match="commitment.*authorization"):
        _prepare_run(
            tmp_path,
            monkeypatch,
            authorization_issued_at_utc="2026-08-12T23:59:59Z",
        )

    assert not (tmp_path / "predictions.json").exists()


def test_scoring_chronology_rejects_receipt_before_prediction_and_score_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    early_receipt = _write_receipt(run, recorded_at_utc="2026-08-13T00:59:59Z")

    with pytest.raises(ExternalBundleUnavailableError, match="prediction.*receipt"):
        score_revealed_bundle_files(
            prediction_path=run["prediction"],  # type: ignore[arg-type]
            question_path=run["questions"],  # type: ignore[arg-type]
            answer_path=run["answers"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            prediction_receipt_path=early_receipt,
            output_path=tmp_path / "early-receipt-score.json",
            scored_at_utc="2026-08-13T02:00:00Z",
        )

    early_receipt.unlink()
    receipt = _write_receipt(run, recorded_at_utc="2026-08-13T01:30:00Z")
    with pytest.raises(ExternalBundleUnavailableError, match="receipt.*score"):
        score_revealed_bundle_files(
            prediction_path=run["prediction"],  # type: ignore[arg-type]
            question_path=run["questions"],  # type: ignore[arg-type]
            answer_path=run["answers"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            prediction_receipt_path=receipt,
            output_path=tmp_path / "early-score.json",
            scored_at_utc="2026-08-13T01:29:59Z",
        )


def test_scoring_detects_question_tamper_and_does_not_publish_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    receipt = _write_receipt(run)
    question_path = run["questions"]
    assert isinstance(question_path, Path)
    tampered = json.loads(question_path.read_text(encoding="utf-8"))
    tampered["cases"][0]["question"] += " tampered"
    question_path.write_bytes(_json_bytes(tampered))
    output_path = tmp_path / "tampered-score.json"

    with pytest.raises(ExternalBundleUnavailableError, match="differs"):
        score_revealed_bundle_files(
            prediction_path=run["prediction"],  # type: ignore[arg-type]
            question_path=question_path,
            answer_path=run["answers"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            prediction_receipt_path=receipt,
            output_path=output_path,
            scored_at_utc="2026-08-13T02:00:00Z",
        )

    assert not output_path.exists()


def test_score_output_collision_never_overwrites_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    receipt = _write_receipt(run)
    output_path = tmp_path / "existing-score.json"
    output_path.write_text("caller-owned", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        score_revealed_bundle_files(
            prediction_path=run["prediction"],  # type: ignore[arg-type]
            question_path=run["questions"],  # type: ignore[arg-type]
            answer_path=run["answers"],  # type: ignore[arg-type]
            commitment_path=run["commitment"],  # type: ignore[arg-type]
            prediction_receipt_path=receipt,
            output_path=output_path,
            scored_at_utc="2026-08-13T02:00:00Z",
        )

    assert output_path.read_text(encoding="utf-8") == "caller-owned"


def test_revealed_scoring_covers_router_fields_bootstrap_and_ood(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(tmp_path, monkeypatch)
    receipt = _write_receipt(run)
    output = tmp_path / "score.json"

    report = score_revealed_bundle_files(
        prediction_path=run["prediction"],  # type: ignore[arg-type]
        question_path=run["questions"],  # type: ignore[arg-type]
        answer_path=run["answers"],  # type: ignore[arg-type]
        commitment_path=run["commitment"],  # type: ignore[arg-type]
        prediction_receipt_path=receipt,
        output_path=output,
        scored_at_utc="2026-08-13T02:00:00Z",
    )

    assert output.is_file()
    assert report.routing.disposition_accuracy == 1
    assert report.routing.family_scored_case_count == 76
    assert report.routing.family_exact_count == 76
    assert report.routing.family_accuracy == 1
    assert all(
        item.family_exact is None
        for item in report.routing_cases
        if item.expected_disposition is not RouteDisposition.EXECUTE
    )
    assert report.routing.interaction_intent_accuracy == 1
    assert report.routing.control_operational_dense_call_count == 0
    assert report.routing.control_no_operational_dense_case_count == 24
    assert report.routing.control_no_operational_dense_rate == 1
    assert report.routing.control_operational_dense_gate_passed is True
    assert [item.candidate for item in report.field_scores] == [
        "lexical",
        "bge-m3",
        "kure-v1",
    ]
    assert all(item.scored_case_count == 76 for item in report.field_scores)
    assert len(report.paired_bootstrap) == 3
    assert all(item.test_gate_passed for item in report.ood_thresholds)


def test_scoring_measures_dense_calls_against_revealed_gold_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _prepare_run(
        tmp_path,
        monkeypatch,
        router_type=_MisroutesOneGoldControlRouter,
    )
    artifact = run["artifact"]
    assert artifact.predicted_non_execute_operational_dense_call_count == 0
    receipt = _write_receipt(run)

    report = score_revealed_bundle_files(
        prediction_path=run["prediction"],  # type: ignore[arg-type]
        question_path=run["questions"],  # type: ignore[arg-type]
        answer_path=run["answers"],  # type: ignore[arg-type]
        commitment_path=run["commitment"],  # type: ignore[arg-type]
        prediction_receipt_path=receipt,
        output_path=tmp_path / "misroute-score.json",
        scored_at_utc="2026-08-13T02:00:00Z",
    )

    assert report.routing.control_case_count == 24
    assert report.routing.control_operational_dense_call_count == 2
    assert report.routing.control_no_operational_dense_case_count == 23
    assert report.routing.control_no_operational_dense_rate == 0.958333
    assert report.routing.control_operational_dense_gate_passed is False


def test_score_cli_prints_gold_control_dense_safety_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from finance_agent_core.evaluation.schema_embedding_external_v2_cli import main

    run = _prepare_run(tmp_path, monkeypatch)
    receipt = _write_receipt(run)

    exit_code = main(
        [
            "score",
            "--predictions",
            str(run["prediction"]),
            "--questions",
            str(run["questions"]),
            "--answers",
            str(run["answers"]),
            "--commitment",
            str(run["commitment"]),
            "--prediction-receipt",
            str(receipt),
            "--scored-at-utc",
            "2026-08-13T02:00:00Z",
            "--output",
            str(tmp_path / "cli-score.json"),
        ]
    )

    assert exit_code == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["gold_control_dense_safety"] == {
        "control_case_count": 24,
        "operational_dense_provider_call_count": 0,
        "no_operational_dense_case_rate": 1.0,
        "gate_passed": True,
    }


def test_routing_score_contract_rejects_inconsistent_control_dense_counts() -> None:
    from finance_agent_core.evaluation.schema_embedding_external_v2 import (
        RoutingScoreSummary,
    )

    with pytest.raises(ValueError, match="per-case model calls"):
        RoutingScoreSummary(
            case_count=100,
            disposition_exact_count=99,
            disposition_accuracy=0.99,
            family_scored_case_count=76,
            family_exact_count=76,
            family_accuracy=1,
            interaction_intent_exact_count=99,
            interaction_intent_accuracy=0.99,
            query_plan_intent_exact_count=99,
            query_plan_intent_accuracy=0.99,
            control_case_count=24,
            control_operational_dense_call_count=0,
            control_no_operational_dense_case_count=23,
            control_no_operational_dense_rate=0.958333,
            control_operational_dense_gate_passed=False,
        )
