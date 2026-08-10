from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from integrity import (
    IntegrityError,
    canonical_bytes,
    load_json,
    parse_json_bytes,
    require_mode,
    sha256_bytes,
    sha256_file,
    validate_receipt_chain,
)


EXPECTED_RESPONSE_KEYS = {
    "schema_version",
    "request_id",
    "status",
    "intent",
    "product_families",
    "answer",
    "query_plan",
    "candidate_count",
    "products",
    "comparisons",
    "aggregates",
    "documents",
    "citations",
    "as_of_dates",
    "warnings",
    "answer_mode",
    "fallback_used",
    "provider_model",
    "clarification",
    "error",
    "source_manifest",
    "family_searches",
    "source_manifests",
}

EXPECTED_QUERY_PLAN_KEYS = {
    "schema_version",
    "question_id",
    "intent",
    "product_families",
    "constraints",
    "ranking",
    "projection",
    "limit",
    "intent_payload",
    "ambiguities",
    "unsupported_conditions",
}

EXPECTED_INTENT_PAYLOAD_KEYS = {
    "comparison_fields",
    "group_by",
    "aggregations",
    "explain_product_ids",
}

NUMERIC_RANKING_FIELDS = {
    "total_expense_ratio_pct",
    "aum",
    "close_price",
    "one_month_return_pct",
    "daily_trading_value",
    "issue_amount",
    "coupon_rate_pct",
    "buy_yield_pct",
}


@dataclass(frozen=True)
class SealedSuite:
    manifest: dict[str, Any]
    questions: list[dict[str, Any]]
    expectations: list[dict[str, Any]]
    receipt: list[dict[str, Any]]


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def _load_questions(path: Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for number, raw_line in enumerate(path.read_bytes().splitlines(), start=1):
        parsed = parse_json_bytes(raw_line)
        if not isinstance(parsed, dict) or set(parsed) != {
            "case_id",
            "request_id",
            "question",
            "locale",
        }:
            raise IntegrityError(f"public question line {number} has unexpected fields")
        if parsed["locale"] != "ko-KR":
            raise IntegrityError(f"public question line {number} has the wrong locale")
        encoded = canonical_bytes(parsed)
        if b"SBV2-SECRET-" in encoded or b"expectation" in encoded.lower():
            raise IntegrityError(f"public question line {number} leaks sealed material")
        questions.append(parsed)
    if not questions:
        raise IntegrityError("public questions are empty")
    if len({item["case_id"] for item in questions}) != len(questions):
        raise IntegrityError("public case IDs are not unique")
    if len({item["request_id"] for item in questions}) != len(questions):
        raise IntegrityError("public request IDs are not unique")
    if len({item["question"] for item in questions}) != len(questions):
        raise IntegrityError("public questions are not unique")
    return questions


def load_sealed_suite(suite_dir: Path, key_path: Path) -> SealedSuite:
    suite_dir = suite_dir.resolve()
    private_dir = key_path.resolve().parent
    require_mode(private_dir, 0o700)
    require_mode(key_path.resolve(), 0o600)
    receipt = validate_receipt_chain(private_dir / "chronology.jsonl")
    events = [entry["event"] for entry in receipt]
    if events != ["authoring_started", "sealed"]:
        raise IntegrityError(
            "first-run preflight requires exactly authoring_started, sealed chronology events"
        )

    manifest_path = suite_dir / "seal_manifest.json"
    questions_path = suite_dir / "questions.jsonl"
    envelope_path = suite_dir / "expectations.aesgcm.json"
    manifest = load_json(manifest_path)
    envelope = load_json(envelope_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0":
        raise IntegrityError("seal manifest schema mismatch")
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "AES-256-GCM":
        raise IntegrityError("sealed expectation envelope schema mismatch")

    key = key_path.read_bytes()
    if len(key) != 32:
        raise IntegrityError("seal key must contain exactly 32 bytes")
    if not _constant_time_equal(sha256_bytes(key), manifest["key_fingerprint_sha256"]):
        raise IntegrityError("seal key fingerprint mismatch")
    manifest_hmac = manifest.get("manifest_hmac_sha256")
    manifest_body = {
        key_name: value
        for key_name, value in manifest.items()
        if key_name != "manifest_hmac_sha256"
    }
    expected_manifest_hmac = hmac.new(
        key, canonical_bytes(manifest_body), hashlib.sha256
    ).hexdigest()
    if not isinstance(manifest_hmac, str) or not _constant_time_equal(
        manifest_hmac, expected_manifest_hmac
    ):
        raise IntegrityError("seal manifest authentication failed")

    if sha256_file(questions_path) != manifest["questions_sha256"]:
        raise IntegrityError("public questions hash mismatch")
    if sha256_file(envelope_path) != manifest[
        "sealed_expectations_envelope_sha256"
    ]:
        raise IntegrityError("sealed expectation envelope hash mismatch")
    questions = _load_questions(questions_path)
    if len(questions) != manifest["case_count"]:
        raise IntegrityError("public question count differs from the seal")

    try:
        nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
        aad = base64.b64decode(envelope["aad_b64"], validate=True)
        ciphertext = base64.b64decode(
            envelope["ciphertext_and_tag_b64"], validate=True
        )
    except (KeyError, ValueError) as error:
        raise IntegrityError("sealed expectation encoding is invalid") from error
    if len(nonce) != 12:
        raise IntegrityError("AES-GCM nonce must contain exactly 12 bytes")
    if sha256_bytes(ciphertext) != envelope["ciphertext_sha256"]:
        raise IntegrityError("sealed ciphertext hash mismatch")
    if envelope["ciphertext_sha256"] != manifest[
        "sealed_expectations_ciphertext_sha256"
    ]:
        raise IntegrityError("ciphertext and manifest commitments differ")
    aad_object = parse_json_bytes(aad)
    if aad_object.get("questions_sha256") != manifest["questions_sha256"]:
        raise IntegrityError("AES-GCM AAD does not bind the question hash")
    if aad_object.get("sealed_at_utc") != manifest["sealed_at_utc"]:
        raise IntegrityError("AES-GCM AAD does not bind the sealing time")
    if aad_object.get("author_declaration") != manifest["author_declaration"]:
        raise IntegrityError("AES-GCM AAD does not bind the author declaration")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as error:
        raise IntegrityError("AES-GCM authentication failed") from error
    if sha256_bytes(plaintext) != envelope["plaintext_commitment_sha256"]:
        raise IntegrityError("plaintext commitment mismatch")
    if envelope["plaintext_commitment_sha256"] != manifest[
        "sealed_expectations_commitment_sha256"
    ]:
        raise IntegrityError("plaintext and manifest commitments differ")
    expected_plaintext_hmac = hmac.new(key, plaintext, hashlib.sha256).hexdigest()
    if not _constant_time_equal(
        expected_plaintext_hmac, envelope["plaintext_hmac_sha256"]
    ):
        raise IntegrityError("plaintext HMAC mismatch")
    payload = parse_json_bytes(plaintext)
    if payload.get("suite_id") != manifest["suite_id"]:
        raise IntegrityError("decrypted suite ID mismatch")
    if payload.get("sealed_at_utc") != manifest["sealed_at_utc"]:
        raise IntegrityError("decrypted sealing time mismatch")
    expectations = payload.get("cases")
    if not isinstance(expectations, list) or len(expectations) != len(questions):
        raise IntegrityError("decrypted expectation count mismatch")
    for question, expected in zip(questions, expectations, strict=True):
        if not isinstance(expected, dict) or set(expected) != {
            "case_id",
            "request_id",
            "secret_canary",
            "expectation",
        }:
            raise IntegrityError("decrypted expectation has unexpected fields")
        if expected["case_id"] != question["case_id"]:
            raise IntegrityError("question and expectation case IDs differ")
        if expected["request_id"] != question["request_id"]:
            raise IntegrityError("question and expectation request IDs differ")
        if not str(expected["secret_canary"]).startswith("SBV2-SECRET-"):
            raise IntegrityError("decrypted expectation canary is invalid")

    sealed_receipt = receipt[-1]
    if sealed_receipt["payload"]["questions_sha256"] != manifest[
        "questions_sha256"
    ]:
        raise IntegrityError("chronology receipt question hash mismatch")
    if sealed_receipt["payload"]["sealed_at_utc"] != manifest["sealed_at_utc"]:
        raise IntegrityError("chronology receipt sealing time mismatch")
    if sealed_receipt["payload"]["runtime_target_executions_observed"] != 0:
        raise IntegrityError("receipt records target execution before sealing")
    return SealedSuite(
        manifest=manifest,
        questions=questions,
        expectations=expectations,
        receipt=receipt,
    )


def _add(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _check_no_leak(
    *,
    errors: list[str],
    secret_canary: str,
    stdout: bytes,
    stderr: bytes,
    response: dict[str, Any],
) -> None:
    serialized = canonical_bytes(response)
    for raw in (stdout, stderr, serialized):
        _add(errors, secret_canary.encode("utf-8") not in raw, "secret_canary_leak")
        _add(errors, b"SBV2-SECRET-" not in raw, "sealed_canary_prefix_leak")
    answer = response.get("answer")
    _add(errors, isinstance(answer, str) and bool(answer.strip()), "answer_missing")


def _check_zero_execution(
    errors: list[str], response: dict[str, Any], expected: dict[str, Any]
) -> None:
    _add(errors, response.get("query_plan") is None, "control_query_plan_present")
    _add(errors, response.get("candidate_count") is None, "control_candidate_count_present")
    for field in (
        "products",
        "comparisons",
        "aggregates",
        "documents",
        "citations",
        "as_of_dates",
        "family_searches",
        "source_manifests",
    ):
        _add(errors, response.get(field) == [], f"control_{field}_present")
    _add(errors, response.get("source_manifest") is None, "control_source_manifest_present")
    _add(errors, response.get("answer_mode") == "control", "control_answer_mode_mismatch")
    _add(errors, response.get("fallback_used") is False, "control_fallback_present")
    _add(errors, response.get("provider_model") is None, "control_provider_present")
    _add(errors, response.get("error") is None, "control_error_present")
    clarification = expected.get("clarification")
    if clarification is None:
        _add(errors, response.get("clarification") is None, "unexpected_clarification")
    else:
        actual = response.get("clarification")
        _add(errors, isinstance(actual, dict), "clarification_missing")
        if isinstance(actual, dict):
            _add(
                errors,
                actual.get("code") == clarification["code"],
                "clarification_code_mismatch",
            )
            _add(
                errors,
                actual.get("required_fields") == clarification["required_fields"],
                "clarification_required_fields_mismatch",
            )
            _add(
                errors,
                isinstance(actual.get("message"), str)
                and bool(actual["message"].strip()),
                "clarification_message_missing",
            )
            _add(errors, _is_list(actual.get("options")), "clarification_options_invalid")


def _check_query_semantics(
    errors: list[str], plan: Any, request_id: str, semantics: dict[str, Any]
) -> None:
    _add(errors, isinstance(plan, dict), "query_plan_missing")
    if not isinstance(plan, dict):
        return
    _add(errors, set(plan) == EXPECTED_QUERY_PLAN_KEYS, "query_plan_contract_keys_mismatch")
    _add(errors, plan.get("schema_version") == "1.0", "query_plan_schema_mismatch")
    _add(errors, plan.get("question_id") == request_id, "query_plan_request_id_mismatch")
    for field in (
        "intent",
        "product_families",
        "constraints",
        "ranking",
        "ambiguities",
        "unsupported_conditions",
    ):
        _add(errors, plan.get(field) == semantics[field], f"query_semantics_{field}_mismatch")
    projection = plan.get("projection")
    _add(
        errors,
        isinstance(projection, list) and len(projection) == len(set(projection)),
        "query_projection_invalid",
    )
    if isinstance(projection, list):
        _add(
            errors,
            set(semantics["required_projection"]).issubset(projection),
            "query_projection_missing_required_field",
        )
    if "limit" in semantics:
        _add(errors, plan.get("limit") == semantics["limit"], "query_limit_mismatch")
    else:
        limit = plan.get("limit")
        _add(
            errors,
            isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100,
            "query_limit_invalid",
        )
    payload = plan.get("intent_payload")
    _add(errors, isinstance(payload, dict), "intent_payload_missing")
    if isinstance(payload, dict):
        _add(
            errors,
            set(payload) == EXPECTED_INTENT_PAYLOAD_KEYS,
            "intent_payload_contract_keys_mismatch",
        )
        for field in EXPECTED_INTENT_PAYLOAD_KEYS:
            _add(
                errors,
                payload.get(field) == semantics[field],
                f"query_semantics_{field}_mismatch",
            )


def _check_source_manifest(
    errors: list[str], actual: Any, source: dict[str, Any]
) -> None:
    _add(errors, isinstance(actual, dict), "source_manifest_missing")
    if not isinstance(actual, dict):
        return
    for field in (
        "dataset",
        "source_file_sha256",
        "source_snapshot_date",
        "total_rows",
        "searchable_rows",
        "quarantined_rows",
    ):
        _add(
            errors,
            actual.get(field) == source[field],
            f"source_manifest_{field}_mismatch",
        )


def _check_product_membership_and_provenance(
    errors: list[str],
    products: Any,
    *,
    family: str,
    source_id: str,
    universe: dict[str, frozenset[str]],
) -> None:
    _add(errors, isinstance(products, list), "products_invalid")
    if not isinstance(products, list):
        return
    ids: list[str] = []
    for product in products:
        if not isinstance(product, dict):
            errors.append("product_invalid")
            continue
        product_id = product.get("product_id")
        _add(errors, isinstance(product_id, str) and bool(product_id), "product_id_invalid")
        if isinstance(product_id, str):
            ids.append(product_id)
            _add(
                errors,
                product_id in universe.get(family, frozenset()),
                "product_outside_approved_universe",
            )
        fields = product.get("fields")
        _add(errors, isinstance(fields, list) and bool(fields), "product_fields_missing")
        if not isinstance(fields, list):
            continue
        canonical_fields: set[str] = set()
        for evidence in fields:
            if not isinstance(evidence, dict):
                errors.append("field_evidence_invalid")
                continue
            canonical = evidence.get("canonical_field")
            if isinstance(canonical, str):
                canonical_fields.add(canonical)
            _add(
                errors,
                evidence.get("source_dataset") == family,
                "field_source_dataset_mismatch",
            )
            _add(
                errors,
                evidence.get("source_id") == source_id,
                "field_source_id_mismatch",
            )
            source_row = evidence.get("source_row")
            _add(
                errors,
                isinstance(source_row, int)
                and not isinstance(source_row, bool)
                and source_row >= 2,
                "field_source_row_invalid",
            )
            _add(
                errors,
                evidence.get("quality") != "INVALID",
                "invalid_quality_evidence_returned",
            )
        _add(errors, "product_id" in canonical_fields, "product_id_evidence_missing")
        _add(errors, "product_name" in canonical_fields, "product_name_evidence_missing")
    _add(errors, len(ids) == len(set(ids)), "duplicate_returned_product_id")


def _ranking_value(product: dict[str, Any], field: str) -> Any:
    for evidence in product.get("fields", []):
        if evidence.get("canonical_field") == field:
            value = evidence.get("normalized_value")
            if value is None:
                return None
            if field in NUMERIC_RANKING_FIELDS:
                try:
                    return Decimal(str(value))
                except InvalidOperation:
                    return value
            return str(value).casefold()
    return None


def _check_ranking(
    errors: list[str], products: list[dict[str, Any]], ranking: dict[str, str]
) -> None:
    values = [_ranking_value(product, ranking["field"]) for product in products]
    first_null = next((index for index, value in enumerate(values) if value is None), len(values))
    _add(
        errors,
        all(value is None for value in values[first_null:]),
        "ranking_null_placement_mismatch",
    )
    non_null = values[:first_null]
    try:
        ordered = sorted(non_null, reverse=ranking["direction"] == "desc")
    except TypeError:
        errors.append("ranking_value_type_mismatch")
        return
    _add(errors, non_null == ordered, "returned_product_ranking_mismatch")


def _check_citations(
    errors: list[str], citations: Any, *, source_id: str
) -> None:
    _add(errors, isinstance(citations, list) and bool(citations), "citations_missing")
    if not isinstance(citations, list):
        return
    citation_ids: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            errors.append("citation_invalid")
            continue
        citation_id = citation.get("citation_id")
        if isinstance(citation_id, str):
            citation_ids.append(citation_id)
        _add(errors, citation.get("source_id") == source_id, "citation_source_id_mismatch")
        _add(
            errors,
            isinstance(citation.get("evidence_refs"), list)
            and bool(citation["evidence_refs"]),
            "citation_evidence_refs_missing",
        )
    _add(errors, len(citation_ids) == len(set(citation_ids)), "duplicate_citation_id")


def _check_aggregate_semantics(
    errors: list[str],
    aggregates: Any,
    semantics: dict[str, Any],
    *,
    family: str,
    source_id: str,
) -> None:
    _add(errors, isinstance(aggregates, list) and bool(aggregates), "aggregates_missing")
    if not isinstance(aggregates, list) or not aggregates:
        return
    group_keys = semantics["group_by"]
    observed_groups: set[bytes] = set()
    row_count_sum = 0
    for aggregate in aggregates:
        if not isinstance(aggregate, dict):
            errors.append("aggregate_invalid")
            continue
        _add(
            errors,
            aggregate.get("function") == semantics["function"],
            "aggregate_function_mismatch",
        )
        _add(
            errors,
            aggregate.get("field") == semantics["field"],
            "aggregate_field_mismatch",
        )
        _add(
            errors,
            aggregate.get("source_dataset") == family,
            "aggregate_source_dataset_mismatch",
        )
        _add(
            errors,
            aggregate.get("source_id") == source_id,
            "aggregate_source_id_mismatch",
        )
        group_values = aggregate.get("group_values")
        _add(
            errors,
            isinstance(group_values, dict) and list(group_values) == group_keys,
            "aggregate_group_keys_mismatch",
        )
        if isinstance(group_values, dict):
            encoded_group = canonical_bytes(group_values)
            _add(
                errors,
                encoded_group not in observed_groups,
                "duplicate_aggregate_group",
            )
            observed_groups.add(encoded_group)
        row_count = aggregate.get("row_count")
        valid_count = aggregate.get("valid_count")
        missing_count = aggregate.get("missing_count")
        valid_counts = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (row_count, valid_count, missing_count)
        )
        _add(errors, valid_counts, "aggregate_counts_invalid")
        if valid_counts:
            _add(
                errors,
                valid_count + missing_count == row_count,
                "aggregate_count_conservation_mismatch",
            )
            row_count_sum += row_count
        if "exact_value" in semantics:
            _add(
                errors,
                aggregate.get("value") == semantics["exact_value"],
                "aggregate_exact_value_mismatch",
            )
            _add(errors, valid_count == row_count, "aggregate_count_missing_identity")
    _add(
        errors,
        row_count_sum == semantics["sum_row_count"],
        "aggregate_row_count_sum_mismatch",
    )
    if not group_keys:
        _add(errors, len(aggregates) == 1, "ungrouped_aggregate_cardinality_mismatch")


def evaluate_response(
    *,
    question: dict[str, Any],
    sealed_case: dict[str, Any],
    response: dict[str, Any],
    stdout: bytes,
    stderr: bytes,
    universe: dict[str, frozenset[str]],
) -> list[str]:
    expected = sealed_case["expectation"]
    errors: list[str] = []
    _add(errors, set(response) == EXPECTED_RESPONSE_KEYS, "response_contract_keys_mismatch")
    _add(errors, response.get("schema_version") == "1.0", "response_schema_mismatch")
    _add(
        errors,
        response.get("request_id") == question["request_id"],
        "response_request_id_mismatch",
    )
    _add(errors, response.get("status") == expected["status"], "status_mismatch")
    _add(errors, response.get("intent") == expected["intent"], "intent_mismatch")
    _add(
        errors,
        response.get("product_families") == expected["product_families"],
        "product_families_mismatch",
    )
    _add(errors, response.get("error") is None, "unexpected_error")
    _add(errors, _is_list(response.get("warnings")), "warnings_invalid")
    _check_no_leak(
        errors=errors,
        secret_canary=sealed_case["secret_canary"],
        stdout=stdout,
        stderr=stderr,
        response=response,
    )
    if expected["zero_execution"]:
        _check_zero_execution(errors, response, expected)
        return sorted(set(errors))

    semantics = expected["query_semantics"]
    _check_query_semantics(
        errors, response.get("query_plan"), question["request_id"], semantics
    )
    _add(
        errors,
        response.get("candidate_count") == expected["candidate_count"],
        "candidate_count_mismatch",
    )
    _add(errors, response.get("answer_mode") != "control", "executed_control_mode")
    _add(errors, response.get("clarification") is None, "executed_clarification_present")
    _add(errors, response.get("family_searches") == [], "single_family_searches_present")
    _add(errors, response.get("source_manifests") == [], "single_family_manifests_present")
    source = expected["source"]
    _check_source_manifest(errors, response.get("source_manifest"), source)
    family = expected["product_families"][0]
    if expected["intent"] == "search":
        products = response.get("products")
        _add(
            errors,
            isinstance(products, list)
            and len(products) == expected["returned_product_count"],
            "returned_product_count_mismatch",
        )
        _add(errors, response.get("comparisons") == [], "search_comparisons_present")
        _add(errors, response.get("aggregates") == [], "search_aggregates_present")
        _add(errors, response.get("documents") == [], "search_documents_present")
        _check_product_membership_and_provenance(
            errors,
            products,
            family=family,
            source_id=source["source_id"],
            universe=universe,
        )
        if isinstance(products, list):
            _check_ranking(errors, products, semantics["ranking"][0])
        _check_citations(errors, response.get("citations"), source_id=source["source_id"])
    else:
        _add(errors, response.get("products") == [], "aggregate_products_present")
        _add(errors, response.get("comparisons") == [], "aggregate_comparisons_present")
        _add(errors, response.get("documents") == [], "aggregate_documents_present")
        _check_aggregate_semantics(
            errors,
            response.get("aggregates"),
            expected["aggregate_semantics"],
            family=family,
            source_id=source["source_id"],
        )
        _check_citations(errors, response.get("citations"), source_id=source["source_id"])
    _add(
        errors,
        isinstance(response.get("as_of_dates"), list)
        and bool(response["as_of_dates"]),
        "as_of_dates_missing",
    )
    return sorted(set(errors))


def response_from_stdout(stdout: bytes) -> dict[str, Any]:
    parsed = parse_json_bytes(stdout.strip())
    if not isinstance(parsed, dict):
        raise IntegrityError("target stdout must be exactly one JSON object")
    return parsed
