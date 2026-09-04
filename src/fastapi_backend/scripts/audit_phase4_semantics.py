from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from finance_agent_core.agent.compound_query import TypedCompoundQueryEngine
from finance_agent_core.agent.document_query import (
    TypedDocumentQueryEngine,
    canonical_relation_entity,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage import RecordSnapshotCache

COMPOUND_TYPES = frozenset(
    {
        "anchor_relative_search",
        "bounded_single_ranking",
        "cohort_aggregate_and_representatives",
        "cross_family_parallel_search",
        "cross_family_two_stage",
        "named_pair_compare",
        "two_stage_derived_shortlist",
    }
)
DOCUMENT_TYPES = frozenset(
    {
        "dated_theme_history_cross_family",
        "document_relation_to_db",
        "multi_entity_document_screen_risk",
        "portfolio_overlap_reverse_search",
        "verified_empty_document_intersection",
    }
)

_EXPLICIT_LIMIT_PATTERN = re.compile(
    r"(?:top\s*\d+|상위\s*\d+|최대\s*\d+|\d+\s*개)",
    re.IGNORECASE,
)


def _gold_contract_warnings(case: dict[str, Any], question: str) -> list[dict[str, Any]]:
    """Report score-contract conventions that do not change the source answer.

    A verified empty document intersection is a valid evaluated outcome, but an empty
    ``IN`` filter is not a valid executable QueryPlan.  Several Phase 4 Gold cases also
    carry a default limit even though the user did not request a result count.  Keep
    those conventions visible instead of silently counting them as Agent errors.
    """

    if case.get("question_type") != "verified_empty_document_intersection":
        return []
    subqueries = tuple(case.get("expected_subqueries") or ())
    if not subqueries:
        return []
    verified_empty = all(
        int(item.get("candidate_count") or 0) == 0
        and not item.get("top_product_ids")
        for item in subqueries
    )
    if not verified_empty:
        return []

    warnings: list[dict[str, Any]] = []
    empty_identity_filters = [
        {
            "family": item.get("product_family"),
            "field": condition.get("field"),
        }
        for item in subqueries
        for condition in item.get("filters", ())
        if condition.get("operator") == "in" and condition.get("value") == []
    ]
    if empty_identity_filters:
        warnings.append(
            {
                "id": str(case["id"]),
                "kind": "non_executable_empty_identity_filter",
                "impact": "queryplan_contract_only",
                "details": empty_identity_filters,
            }
        )
    if not _EXPLICIT_LIMIT_PATTERN.search(question):
        limits = sorted({int(item["limit"]) for item in subqueries if item.get("limit")})
        if limits:
            warnings.append(
                {
                    "id": str(case["id"]),
                    "kind": "implicit_limit_on_verified_empty_result",
                    "impact": "queryplan_contract_only",
                    "limits": limits,
                }
            )
    return warnings


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily.BOND: database_dir / "bond.sqlite3",
        ProductFamily.DOMESTIC_ETP: database_dir / "domestic_etp.sqlite3",
        ProductFamily.OVERSEAS_ETP: database_dir / "overseas_etp.sqlite3",
        ProductFamily.FUND: database_dir / "fund.sqlite3",
    }


def _execution_products(execution: Any) -> list[list[str]]:
    return [
        [str(product.product_id) for product in family.products]
        for family in execution.families
    ]


def _expected_products(case: dict[str, Any]) -> list[list[str]]:
    return [
        [str(value) for value in subquery["top_product_ids"]]
        for subquery in case["expected_subqueries"]
    ]


def _expected_candidates(case: dict[str, Any]) -> list[int]:
    return [int(subquery["candidate_count"]) for subquery in case["expected_subqueries"]]


def _actual_candidates(execution: Any) -> list[int]:
    return [int(family.candidate_count) for family in execution.families]


def _relation_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    task = case.get("external_task") or {}
    return {
        str(item["profile_key"]): item
        for item in task.get("verified_relation_rows", ())
        if bool(item.get("matched"))
    }


def _selection_map(execution: Any) -> dict[str, Any]:
    return {item.profile.profile_key: item for item in execution.selections}


def _holding_map(selection: Any) -> dict[str, float]:
    return {item.name: float(item.weight_pct) for item in selection.matched_holdings}


def _same_holdings(expected: dict[str, Any], selection: Any) -> bool:
    actual = _holding_map(selection)
    wanted = {str(key): float(value) for key, value in expected.items()}
    actual_by_entity = {canonical_relation_entity(key): value for key, value in actual.items()}
    wanted_by_entity = {canonical_relation_entity(key): value for key, value in wanted.items()}
    return set(actual_by_entity) == set(wanted_by_entity) and all(
        abs(actual_by_entity[key] - wanted_by_entity[key]) <= 1e-8
        for key in wanted_by_entity
    )


def _normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


def _audit_gold_relation_case(
    case: dict[str, Any],
    *,
    engine: TypedDocumentQueryEngine,
) -> list[dict[str, Any]]:
    task = case.get("external_task") or {}
    if not task:
        return []
    question = str(case["_question"])
    profiles = {item.profile_key: item for item in engine.index.profiles}
    predicate = task.get("predicate") or {}
    entities = tuple(str(item) for item in predicate.get("entities", ()))
    normalized_entities = tuple(canonical_relation_entity(item) for item in entities)
    require_all = predicate.get("quantifier") == "all"
    minimum = predicate.get("minimum_sum_weight_pct")
    failures: list[dict[str, Any]] = []
    if case["question_type"] == "portfolio_overlap_reverse_search":
        seed_text = question[: question.find("그 공통 기업")]
        seed_profiles = engine.index.mentioned_profiles(seed_text)
        common = (
            {item.canonical_name for item in seed_profiles[0].holdings}
            if seed_profiles
            else set()
        )
        for profile in seed_profiles[1:]:
            common &= {item.canonical_name for item in profile.holdings}
        if common != set(normalized_entities):
            failures.append(
                {
                    "reason": "derived_overlap_predicate_mismatch",
                    "expected_entities": sorted(normalized_entities),
                    "actual_entities": sorted(common),
                }
            )
    elif any(_normalize_name(entity) not in _normalize_name(question) for entity in entities):
        failures.append({"reason": "predicate_entity_absent_from_question"})
    for row in task.get("verified_relation_rows", ()):
        profile_key = str(row["profile_key"])
        profile = profiles.get(profile_key)
        if profile is None:
            failures.append({"reason": "profile_missing_from_official_corpus", "profile": profile_key})
            continue
        resolved = engine._resolve_product(profile_key)
        if resolved is None or resolved[0].value != row["family"] or resolved[1] != row["product_id"]:
            failures.append({"reason": "official_profile_db_identity_mismatch", "profile": profile_key})
        holding_by_name = {item.canonical_name: item for item in profile.holdings}
        matched = tuple(
            holding_by_name[item] for item in normalized_entities if item in holding_by_name
        )
        accepted = len(matched) == len(normalized_entities) if require_all else bool(matched)
        if minimum is not None:
            accepted = accepted and sum(item.weight_pct for item in matched) >= float(minimum)
        expected_holdings = {
            str(key): float(value) for key, value in row.get("matched_holdings", {}).items()
        }
        actual_holdings = {item.name: float(item.weight_pct) for item in matched}
        holdings_match = {
            canonical_relation_entity(key) for key in expected_holdings
        } == {canonical_relation_entity(key) for key in actual_holdings} and all(
            abs(
                next(
                    value
                    for key, value in actual_holdings.items()
                    if canonical_relation_entity(key)
                    == canonical_relation_entity(expected_key)
                )
                - expected_value
            )
            <= 1e-8
            for expected_key, expected_value in expected_holdings.items()
        )
        if bool(row.get("matched")) != accepted or not holdings_match:
            failures.append(
                {
                    "reason": "official_relation_recalculation_mismatch",
                    "profile": profile_key,
                    "expected_matched": bool(row.get("matched")),
                    "actual_matched": accepted,
                    "expected_holdings": expected_holdings,
                    "actual_holdings": actual_holdings,
                }
            )
    return failures


def audit(
    *,
    questions_path: Path,
    gold_path: Path,
    database_dir: Path,
    corpus_dir: Path,
    case_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    question_payload = json.loads(questions_path.read_bytes())
    gold_payload = json.loads(gold_path.read_bytes())
    questions = {str(item["id"]): str(item["question"]) for item in question_payload["cases"]}
    database_paths = _database_paths(database_dir)
    compound = TypedCompoundQueryEngine(
        database_paths,
        record_cache=RecordSnapshotCache(max_entries=4),
    )
    documents = TypedDocumentQueryEngine(
        database_paths,
        compound_engine=compound,
        corpus_dir=corpus_dir,
    )
    counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    gold_relation_failures: list[dict[str, Any]] = []
    contract_warnings: list[dict[str, Any]] = []

    for case in gold_payload["cases"]:
        case_id = str(case["id"])
        if case_ids and case_id not in case_ids:
            continue
        question = questions[case_id]
        case["_question"] = question
        contract_warnings.extend(_gold_contract_warnings(case, question))
        case_type = str(case["question_type"])
        if case_type in COMPOUND_TYPES:
            counts["compound_total"] += 1
            plan = compound.compile(question, case_id)
            if plan is None:
                failures.append({"id": case_id, "layer": "compound", "reason": "not_compiled"})
                continue
            execution = compound.execute(plan)
            product_match = _execution_products(execution) == _expected_products(case)
            candidate_match = _actual_candidates(execution) == _expected_candidates(case)
            if product_match and candidate_match:
                counts["compound_pass"] += 1
            else:
                failures.append(
                    {
                        "id": case_id,
                        "layer": "compound",
                        "reason": "result_mismatch",
                        "expected_products": _expected_products(case),
                        "actual_products": _execution_products(execution),
                        "expected_candidates": _expected_candidates(case),
                        "actual_candidates": _actual_candidates(execution),
                    }
                )
            continue

        if case_type not in DOCUMENT_TYPES:
            continue
        counts["document_total"] += 1
        relation_failures = _audit_gold_relation_case(case, engine=documents)
        counts["gold_document_relation_total"] += 1
        if relation_failures:
            gold_relation_failures.extend(
                {"id": case_id, "layer": "gold_relation", **item}
                for item in relation_failures
            )
        else:
            counts["gold_document_relation_pass"] += 1
        execution = documents.execute(question, case_id)
        if execution is None:
            failures.append({"id": case_id, "layer": "document", "reason": "not_compiled"})
            continue
        product_match = _execution_products(execution.compound) == _expected_products(case)
        candidate_match = _actual_candidates(execution.compound) == _expected_candidates(case)
        expected_relations = _relation_map(case)
        actual_relations = _selection_map(execution)
        relation_match = set(expected_relations) == set(actual_relations)
        if relation_match:
            relation_match = all(
                str(actual_relations[key].product_id) == str(expected_relations[key]["product_id"])
                and actual_relations[key].product_family.value
                == str(expected_relations[key]["family"])
                and _same_holdings(
                    expected_relations[key].get("matched_holdings", {}),
                    actual_relations[key],
                )
                for key in expected_relations
            )
        if product_match and candidate_match and relation_match:
            counts["document_pass"] += 1
        else:
            failures.append(
                {
                    "id": case_id,
                    "layer": "document",
                    "reason": "result_or_relation_mismatch",
                    "expected_products": _expected_products(case),
                    "actual_products": _execution_products(execution.compound),
                    "expected_candidates": _expected_candidates(case),
                    "actual_candidates": _actual_candidates(execution.compound),
                    "expected_relation_profiles": sorted(expected_relations),
                    "actual_relation_profiles": sorted(actual_relations),
                    "expected_holdings": {
                        key: value.get("matched_holdings", {})
                        for key, value in expected_relations.items()
                    },
                    "actual_holdings": {
                        key: _holding_map(value) for key, value in actual_relations.items()
                    },
                }
            )

    counts["total"] = counts["compound_total"] + counts["document_total"]
    counts["passed"] = counts["compound_pass"] + counts["document_pass"]
    return {
        "schema_version": "1.0",
        "audit_scope": {
            "description": (
                "Gold의 계산 결과를 사용하지 않고 질문 원문, 공식 DB, 해시가 검증된 "
                "공식 문서 snapshot으로 복합 계산과 문서 관계를 독립 재실행"
            ),
            "gold_cases": len(gold_payload["cases"]),
            "compound_case_types": sorted(COMPOUND_TYPES),
            "document_case_types": sorted(DOCUMENT_TYPES),
        },
        "counts": dict(sorted(counts.items())),
        "gold_semantics_passed": not gold_relation_failures,
        "runtime_coverage_passed": not failures,
        "passed": not gold_relation_failures and not failures,
        "gold_relation_failure_count": len(gold_relation_failures),
        "gold_relation_failures": gold_relation_failures,
        "failure_count": len(failures),
        "failures": failures,
        "contract_warning_count": len(contract_warnings),
        "contract_warning_case_count": len(
            {item["id"] for item in contract_warnings}
        ),
        "contract_warning_counts": dict(
            sorted(Counter(item["kind"] for item in contract_warnings).items())
        ),
        "contract_warnings": contract_warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case-id", action="append", default=[])
    arguments = parser.parse_args()
    report = audit(
        questions_path=arguments.questions.resolve(),
        gold_path=arguments.gold.resolve(),
        database_dir=arguments.database_dir.resolve(),
        corpus_dir=arguments.corpus_dir.resolve(),
        case_ids=frozenset(arguments.case_id),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
