from __future__ import annotations

import hashlib
import json
import math
import re
import resource
import sys
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.agent import IntentRouter
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.evaluation.models import ExpectedDisposition
from finance_agent_core.evaluation.suite import load_core_evaluation_suite
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    DenseSchemaIndexManifest,
    DenseSchemaLinkerSettings,
    EmbeddingProviderMetadata,
    FeatureGatedDenseSchemaLinker,
    build_schema_field_entries,
)

_TOKEN = re.compile(r"[0-9a-z가-힣_]+")
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DenseEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldRetrievalMetrics(DenseEvaluationModel):
    scored_cases: int = Field(ge=0)
    gold_field_count: int = Field(ge=0)
    returned_field_count_at_5: int = Field(ge=0)
    exact_at_gold_cardinality: float = Field(ge=0, le=1)
    micro_precision_among_returned_at_5: float = Field(ge=0, le=1)
    fixed_k_micro_precision_at_5: float = Field(ge=0, le=1)
    micro_recall_at_3: float = Field(ge=0, le=1)
    micro_recall_at_5: float = Field(ge=0, le=1)
    micro_recall_at_10: float = Field(ge=0, le=1)
    micro_f1_at_5: float = Field(ge=0, le=1)
    full_recall_case_rate_at_5: float = Field(ge=0, le=1)
    full_recall_case_rate_at_10: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    ndcg_at_5: float = Field(ge=0, le=1)


class RetrievalLatency(DenseEvaluationModel):
    measured_cases: int = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    max_ms: float = Field(ge=0)


class SchemaPolicyMigration(DenseEvaluationModel):
    case_id: str = Field(min_length=1, max_length=128)
    product_family: ProductFamily
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    legacy_disposition: Literal["block"] = "block"
    current_disposition: Literal["execute"] = "execute"
    gold_field_ids: tuple[str, ...] = Field(min_length=1)
    reason_code: str = Field(min_length=3, max_length=128)
    review_status: Literal["pending_finance_domain_review"] = "pending_finance_domain_review"


class SchemaPolicyMigrationSuite(DenseEvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: Literal["schema-linker-policy-migrations-v1"] = "schema-linker-policy-migrations-v1"
    status: Literal["developer_authored_pending_finance_domain_review"] = (
        "developer_authored_pending_finance_domain_review"
    )
    base_suite_sha256_by_family: dict[str, str]
    migrations: tuple[SchemaPolicyMigration, ...]


class SchemaSafetyMetrics(DenseEvaluationModel):
    legacy_suite_blocked_cases: int = Field(ge=0)
    versioned_policy_migration_count: int = Field(ge=0)
    policy_migration_review_status: Literal["developer_authored_pending_finance_domain_review"] = (
        "developer_authored_pending_finance_domain_review"
    )
    current_policy_blocked_cases: int = Field(ge=0)
    blocked_cases_without_dense_query: int = Field(ge=0)
    blocked_no_call_rate: float = Field(ge=0, le=1)
    current_policy_execute_cases: int = Field(ge=0)
    router_only_false_positive_count: int = Field(ge=0)
    router_only_false_negative_count: int = Field(ge=0)
    pre_dense_gate_false_positive_count: int = Field(ge=0)
    pre_dense_gate_false_negative_count: int = Field(ge=0)
    route_family_mismatch_count: int = Field(ge=0)
    out_of_registry_candidate_count: int = Field(ge=0)
    out_of_family_candidate_count: int = Field(ge=0)
    production_feature_enabled: Literal[False] = False
    production_probe_status: Literal["disabled"] = "disabled"
    production_probe_provider_query_calls: Literal[0] = 0


class SchemaRuntimeMetrics(DenseEvaluationModel):
    percentile_method: Literal["linear_interpolation"] = "linear_interpolation"
    index_build_ms: float = Field(ge=0)
    index_build_peak_rss_delta_kib: int = Field(ge=0)
    process_peak_rss_kib: int = Field(ge=0)
    theoretical_raw_float64_vector_payload_bytes: int = Field(ge=0)
    lexical_latency: RetrievalLatency
    dense_latency: RetrievalLatency
    schema_link_stage_total_latency: RetrievalLatency


class MissedFieldRecovery(DenseEvaluationModel):
    lexical_missed_fields_at_5: int = Field(ge=0)
    dense_recovered_fields_at_5: int = Field(ge=0)
    dense_recovery_rate_at_5: float = Field(ge=0, le=1)


class DenseSchemaDecision(DenseEvaluationModel):
    evidence_status: Literal["insufficient_evidence"] = "insufficient_evidence"
    real_embedding_required: Literal[True] = True
    production_adoption: Literal["rejected_for_now"] = "rejected_for_now"
    product_semantic_search: Literal["deferred"] = "deferred"
    abstention_policy_status: Literal["not_calibrated"] = "not_calibrated"
    activation_blocker: Literal["real_embedding_and_ood_abstention_thresholds_required"] = (
        "real_embedding_and_ood_abstention_thresholds_required"
    )
    reason: str


class DenseSchemaEvaluationReport(DenseEvaluationModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: Literal["dense-schema-linker-offline-component-v1"] = (
        "dense-schema-linker-offline-component-v1"
    )
    status: Literal["public_offline_component_not_blind"] = "public_offline_component_not_blind"
    evaluation_scope: Literal["schema_field_linking_with_gold_product_family"] = (
        "schema_field_linking_with_gold_product_family"
    )
    routing_quality_included: Literal[False] = False
    suite_case_count: int = Field(ge=1)
    suite_sha256_by_family: dict[str, str]
    policy_migration_suite_id: str
    policy_migration_suite_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_migration_suite_status: Literal["developer_authored_pending_finance_domain_review"] = (
        "developer_authored_pending_finance_domain_review"
    )
    index_manifest: DenseSchemaIndexManifest
    provider_document_calls: int = Field(ge=0)
    provider_query_calls: int = Field(ge=0)
    lexical: FieldRetrievalMetrics
    fake_dense: FieldRetrievalMetrics
    hybrid: FieldRetrievalMetrics
    missed_field_recovery: MissedFieldRecovery
    safety: SchemaSafetyMetrics
    runtime: SchemaRuntimeMetrics
    decision: DenseSchemaDecision


@dataclass(frozen=True)
class _EvaluationCase:
    case_id: str
    family: ProductFamily
    question: str
    legacy_expected_disposition: ExpectedDisposition
    current_expected_disposition: ExpectedDisposition
    gold_fields: tuple[str, ...]


@dataclass(frozen=True)
class _Observation:
    gold: frozenset[str]
    lexical: tuple[str, ...]
    dense: tuple[str, ...]
    hybrid: tuple[str, ...]


@dataclass(frozen=True)
class _LoadedPolicyMigrationSuite:
    suite: SchemaPolicyMigrationSuite
    suite_sha256: str


class FakeHashEmbeddingProvider:
    """Dependency-free fake used only to exercise the Dense contract.

    It hashes lexical tokens and character n-grams.  It is not a trained model,
    does not provide semantic generalization, and must never be interpreted as
    evidence that a real Dense retriever improves quality.
    """

    def __init__(self, dimension: int = 256) -> None:
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="fake_contract",
            provider_id="fake_hash_embedding_v1",
            model_id="fake/hash-char-ngram",
            model_revision="contract-v1",
            license_id="test-only",
            dimension=dimension,
            pooling="signed-hash-bag",
        )
        self.document_calls = 0
        self.document_text_count = 0
        self.query_calls = 0

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    def _embed(self, text: str) -> list[float]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        features: list[tuple[str, float]] = []
        for token in _TOKEN.findall(normalized):
            features.append((f"w:{token}", 2.0))
            compact = token.replace("_", "")
            for size in (2, 3):
                features.extend(
                    (f"c{size}:{compact[index : index + size]}", 1.0)
                    for index in range(max(0, len(compact) - size + 1))
                )
        vector = [0.0] * self.metadata.dimension
        for feature, weight in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.metadata.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        if not any(vector):
            vector[0] = 1.0
        return vector

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.document_text_count += len(texts)
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._embed(text)


def _rss_kib() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024) if sys.platform == "darwin" else int(peak)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _latency(values: Sequence[float]) -> RetrievalLatency:
    return RetrievalLatency(
        measured_cases=len(values),
        p50_ms=_percentile(values, 0.5),
        p95_ms=_percentile(values, 0.95),
        max_ms=round(max(values, default=0.0), 6),
    )


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_policy_migration_suite() -> _LoadedPolicyMigrationSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "schema_linker_policy_migrations_v1.json"
    )
    raw = resource.read_bytes()
    return _LoadedPolicyMigrationSuite(
        suite=SchemaPolicyMigrationSuite.model_validate_json(raw),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _lexical_result(
    question: str,
    family: ProductFamily,
) -> tuple[tuple[str, ...], bool]:
    hints = build_lexical_hints(
        question,
        family.value,
        force_product_family_hint=True,
    )
    values = [
        *(item["field"] for item in hints["required_constraints"]),
        *(item["field"] for item in hints["required_rankings"]),
    ]
    registry = load_field_registry()
    fields = _ordered_unique(
        [
            value
            for value in values
            if value in registry.fields and family.value in registry.fields[value].datasets
        ]
    )
    blocked = bool(hints["ambiguity_spans"] or hints["unsupported_spans"])
    return fields, blocked


def _rrf(lexical: Sequence[str], dense: Sequence[str], top_k: int = 10) -> tuple[str, ...]:
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for ranking in (lexical, dense):
        for rank, field_id in enumerate(ranking, start=1):
            scores[field_id] = scores.get(field_id, 0.0) + 1.0 / (60 + rank)
            best_rank[field_id] = min(best_rank.get(field_id, rank), rank)
    ordered = sorted(scores, key=lambda field: (-scores[field], best_rank[field], field))
    return tuple(ordered[:top_k])


def _dcg(predicted: Sequence[str], gold: frozenset[str], k: int) -> float:
    return sum(
        (1.0 / math.log2(rank + 1)) if field_id in gold else 0.0
        for rank, field_id in enumerate(predicted[:k], start=1)
    )


def _retrieval_metrics(
    observations: Sequence[_Observation],
    attribute: Literal["lexical", "dense", "hybrid"],
) -> FieldRetrievalMetrics:
    total_gold = sum(len(item.gold) for item in observations)
    predictions = [getattr(item, attribute) for item in observations]
    returned_at_5 = sum(len(predicted[:5]) for predicted in predictions)
    pairs = list(zip(observations, predictions, strict=True))
    hits_at_3 = sum(len(item.gold & set(predicted[:3])) for item, predicted in pairs)
    hits_at_5 = sum(len(item.gold & set(predicted[:5])) for item, predicted in pairs)
    hits_at_10 = sum(len(item.gold & set(predicted[:10])) for item, predicted in pairs)
    precision = hits_at_5 / returned_at_5 if returned_at_5 else 0.0
    fixed_k_precision = hits_at_5 / (len(observations) * 5) if observations else 0.0
    recall_5 = hits_at_5 / total_gold if total_gold else 0.0
    exact = sum(
        len(predicted) >= len(item.gold) and set(predicted[: len(item.gold)]) == item.gold
        for item, predicted in zip(observations, predictions, strict=True)
    )
    reciprocal_ranks = []
    ndcg_values = []
    for item, predicted in zip(observations, predictions, strict=True):
        first = next(
            (rank for rank, field_id in enumerate(predicted, start=1) if field_id in item.gold),
            None,
        )
        reciprocal_ranks.append(0.0 if first is None else 1.0 / first)
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(5, len(item.gold)) + 1))
        ndcg_values.append(0.0 if ideal == 0 else _dcg(predicted, item.gold, 5) / ideal)
    case_count = len(observations)
    return FieldRetrievalMetrics(
        scored_cases=case_count,
        gold_field_count=total_gold,
        returned_field_count_at_5=returned_at_5,
        exact_at_gold_cardinality=round(exact / case_count, 6) if case_count else 0.0,
        micro_precision_among_returned_at_5=round(precision, 6),
        fixed_k_micro_precision_at_5=round(fixed_k_precision, 6),
        micro_recall_at_3=round(hits_at_3 / total_gold, 6) if total_gold else 0.0,
        micro_recall_at_5=round(recall_5, 6),
        micro_recall_at_10=round(hits_at_10 / total_gold, 6) if total_gold else 0.0,
        micro_f1_at_5=(
            round(2 * precision * recall_5 / (precision + recall_5), 6)
            if precision + recall_5
            else 0.0
        ),
        full_recall_case_rate_at_5=(
            round(
                sum(item.gold <= set(predicted[:5]) for item, predicted in pairs) / case_count,
                6,
            )
            if case_count
            else 0.0
        ),
        full_recall_case_rate_at_10=(
            round(
                sum(item.gold <= set(predicted[:10]) for item, predicted in pairs) / case_count,
                6,
            )
            if case_count
            else 0.0
        ),
        mrr=round(sum(reciprocal_ranks) / case_count, 6) if case_count else 0.0,
        ndcg_at_5=round(sum(ndcg_values) / case_count, 6) if case_count else 0.0,
    )


def _load_cases() -> tuple[
    list[_EvaluationCase],
    dict[str, str],
    _LoadedPolicyMigrationSuite,
]:
    cases: list[_EvaluationCase] = []
    suite_hashes: dict[str, str] = {}
    loaded_migrations = _load_policy_migration_suite()
    migration_by_key = {
        (item.product_family, item.case_id): item for item in loaded_migrations.suite.migrations
    }
    if len(migration_by_key) != len(loaded_migrations.suite.migrations):
        raise RuntimeError("schema policy migration keys must be unique")
    consumed_migrations: set[tuple[ProductFamily, str]] = set()
    registry = load_field_registry()
    for family in ProductFamily:
        loaded = load_core_evaluation_suite(family.value)
        suite_hashes[family.value] = loaded.suite_sha256
        pinned_base_sha = loaded_migrations.suite.base_suite_sha256_by_family.get(family.value)
        if any(item.product_family is family for item in loaded_migrations.suite.migrations):
            if pinned_base_sha != loaded.suite_sha256:
                raise RuntimeError(f"{family.value} policy migration base suite SHA-256 differs")
        for case in loaded.suite.cases:
            gold_fields = _ordered_unique(
                [
                    *(constraint.field for constraint in case.constraints),
                    *(ranking.field for ranking in case.ranking),
                ]
            )
            migration = migration_by_key.get((family, case.id))
            current_disposition = case.disposition
            if migration is not None:
                if case.disposition is not ExpectedDisposition.BLOCK:
                    raise RuntimeError("current policy migration must start from a blocked case")
                if _sha256_text(case.question) != migration.question_sha256:
                    raise RuntimeError("schema policy migration question SHA-256 differs")
                invalid_fields = [
                    field_id
                    for field_id in migration.gold_field_ids
                    if field_id not in registry.fields
                    or family.value not in registry.fields[field_id].datasets
                ]
                if invalid_fields:
                    raise RuntimeError("schema policy migration uses invalid canonical field IDs")
                current_disposition = ExpectedDisposition.EXECUTE
                gold_fields = migration.gold_field_ids
                consumed_migrations.add((family, case.id))
            cases.append(
                _EvaluationCase(
                    case_id=case.id,
                    family=family,
                    question=case.question,
                    legacy_expected_disposition=case.disposition,
                    current_expected_disposition=current_disposition,
                    gold_fields=gold_fields,
                )
            )
    if consumed_migrations != set(migration_by_key):
        raise RuntimeError("schema policy migration case is absent from the pinned base suite")
    return cases, suite_hashes, loaded_migrations


def run_dense_schema_linker_evaluation(
    provider: FakeHashEmbeddingProvider | None = None,
) -> DenseSchemaEvaluationReport:
    fake = provider or FakeHashEmbeddingProvider()
    entries = build_schema_field_entries()
    rss_before_index = _rss_kib()
    build_started = time.perf_counter()
    index = DenseSchemaIndex.build(entries, fake)
    index_build_ms = (time.perf_counter() - build_started) * 1000
    rss_after_index = _rss_kib()

    feature_calls_before = fake.query_calls
    feature_probe = FeatureGatedDenseSchemaLinker(
        index,
        DenseSchemaLinkerSettings(enabled=False),
    ).link("해외 ETF 총보수", ProductFamily.OVERSEAS_ETP)
    feature_probe_calls = fake.query_calls - feature_calls_before
    if feature_probe.status != "disabled" or feature_probe_calls:
        raise RuntimeError("disabled Dense schema feature reached the embedding provider")

    cases, suite_hashes, loaded_migrations = _load_cases()
    router = IntentRouter()
    registry = load_field_registry()
    observations: list[_Observation] = []
    lexical_latencies: list[float] = []
    dense_latencies: list[float] = []
    hybrid_latencies: list[float] = []
    legacy_blocked = 0
    current_blocked = 0
    blocked_without_call = 0
    current_execute = 0
    router_only_false_positive = 0
    router_only_false_negative = 0
    pre_dense_false_positive = 0
    pre_dense_false_negative = 0
    route_family_mismatch = 0
    invalid_registry = 0
    invalid_family = 0

    for case in cases:
        legacy_blocked += int(case.legacy_expected_disposition is ExpectedDisposition.BLOCK)
        is_expected_block = case.current_expected_disposition is ExpectedDisposition.BLOCK
        current_blocked += int(is_expected_block)
        current_execute += int(not is_expected_block)
        calls_before = fake.query_calls
        route = router.route(case.question, f"dense-shadow-{case.case_id}")
        actual_execute = route.disposition is RouteDisposition.EXECUTE
        router_only_false_positive += int(is_expected_block and actual_execute)
        router_only_false_negative += int(not is_expected_block and not actual_execute)
        family_matches = case.family in route.draft.product_families
        route_family_mismatch += int(actual_execute and not family_matches)

        lexical_fields: tuple[str, ...]
        dense_fields: tuple[str, ...] = ()
        hybrid_fields: tuple[str, ...] = ()
        lexical_started = time.perf_counter()
        lexical_fields, lexical_blocked = _lexical_result(case.question, case.family)
        lexical_ms = (time.perf_counter() - lexical_started) * 1000
        effective_dense_execution = actual_execute and family_matches and not lexical_blocked
        # Field-linking quality is isolated with the suite's frozen family.  Current
        # production gate disagreements stay visible in the safety metrics, while
        # they do not become false Dense misses.  Current-policy control cases are
        # never force-executed.
        offline_schema_execution = not is_expected_block or effective_dense_execution
        if offline_schema_execution:
            lexical_latencies.append(lexical_ms)
            dense_started = time.perf_counter()
            candidates = index.search(case.question, case.family, top_k=10)
            dense_ms = (time.perf_counter() - dense_started) * 1000
            dense_fields = tuple(candidate.field_id for candidate in candidates)

            fusion_started = time.perf_counter()
            hybrid_fields = _rrf(lexical_fields, dense_fields, top_k=10)
            fusion_ms = (time.perf_counter() - fusion_started) * 1000
            dense_latencies.append(dense_ms)
            hybrid_latencies.append(lexical_ms + dense_ms + fusion_ms)

            invalid_registry += sum(field_id not in registry.fields for field_id in dense_fields)
            invalid_family += sum(
                field_id not in registry.fields
                or case.family.value not in registry.fields[field_id].datasets
                for field_id in dense_fields
            )
        pre_dense_false_positive += int(is_expected_block and effective_dense_execution)
        pre_dense_false_negative += int(not is_expected_block and not effective_dense_execution)
        if is_expected_block and fake.query_calls == calls_before:
            blocked_without_call += 1
        if not is_expected_block and case.gold_fields:
            observations.append(
                _Observation(
                    gold=frozenset(case.gold_fields),
                    lexical=lexical_fields,
                    dense=dense_fields,
                    hybrid=hybrid_fields,
                )
            )

    lexical_metrics = _retrieval_metrics(observations, "lexical")
    dense_metrics = _retrieval_metrics(observations, "dense")
    hybrid_metrics = _retrieval_metrics(observations, "hybrid")
    lexical_missed = sum(len(item.gold - set(item.lexical[:5])) for item in observations)
    dense_recovered = sum(
        len((item.gold - set(item.lexical[:5])) & set(item.dense[:5])) for item in observations
    )
    return DenseSchemaEvaluationReport(
        suite_case_count=len(cases),
        suite_sha256_by_family=suite_hashes,
        policy_migration_suite_id=loaded_migrations.suite.suite_id,
        policy_migration_suite_sha256=loaded_migrations.suite_sha256,
        policy_migration_suite_status=loaded_migrations.suite.status,
        index_manifest=index.manifest,
        provider_document_calls=fake.document_calls,
        provider_query_calls=fake.query_calls,
        lexical=lexical_metrics,
        fake_dense=dense_metrics,
        hybrid=hybrid_metrics,
        missed_field_recovery=MissedFieldRecovery(
            lexical_missed_fields_at_5=lexical_missed,
            dense_recovered_fields_at_5=dense_recovered,
            dense_recovery_rate_at_5=(
                round(dense_recovered / lexical_missed, 6) if lexical_missed else 0.0
            ),
        ),
        safety=SchemaSafetyMetrics(
            legacy_suite_blocked_cases=legacy_blocked,
            versioned_policy_migration_count=len(loaded_migrations.suite.migrations),
            policy_migration_review_status=loaded_migrations.suite.status,
            current_policy_blocked_cases=current_blocked,
            blocked_cases_without_dense_query=blocked_without_call,
            blocked_no_call_rate=(
                round(blocked_without_call / current_blocked, 6) if current_blocked else 1.0
            ),
            current_policy_execute_cases=current_execute,
            router_only_false_positive_count=router_only_false_positive,
            router_only_false_negative_count=router_only_false_negative,
            pre_dense_gate_false_positive_count=pre_dense_false_positive,
            pre_dense_gate_false_negative_count=pre_dense_false_negative,
            route_family_mismatch_count=route_family_mismatch,
            out_of_registry_candidate_count=invalid_registry,
            out_of_family_candidate_count=invalid_family,
            production_feature_enabled=False,
            production_probe_status="disabled",
            production_probe_provider_query_calls=0,
        ),
        runtime=SchemaRuntimeMetrics(
            index_build_ms=round(index_build_ms, 6),
            index_build_peak_rss_delta_kib=max(0, rss_after_index - rss_before_index),
            process_peak_rss_kib=_rss_kib(),
            theoretical_raw_float64_vector_payload_bytes=(
                index.manifest.vector_count * index.manifest.provider.dimension * 8
            ),
            lexical_latency=_latency(lexical_latencies),
            dense_latency=_latency(dense_latencies),
            schema_link_stage_total_latency=_latency(hybrid_latencies),
        ),
        decision=DenseSchemaDecision(
            reason=(
                "fake hash embedding은 배선·manifest·안전 gate만 검증하며 학습된 "
                "의미 표현이 아니므로 Dense 품질 개선이나 상품 의미 검색 채택의 "
                "근거가 될 수 없습니다."
            )
        ),
    )


def report_fingerprint(report: DenseSchemaEvaluationReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "DenseSchemaEvaluationReport",
    "FakeHashEmbeddingProvider",
    "FieldRetrievalMetrics",
    "report_fingerprint",
    "run_dense_schema_linker_evaluation",
]
