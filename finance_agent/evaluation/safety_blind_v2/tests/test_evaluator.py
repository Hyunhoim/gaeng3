from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SUITE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE_DIR))

from evaluator import (  # noqa: E402
    EXPECTED_RESPONSE_KEYS,
    _check_product_membership_and_provenance,
    _check_query_semantics,
    evaluate_response,
    load_sealed_suite,
)
from integrity import (  # noqa: E402
    append_receipt,
    canonical_bytes,
    load_json,
    sha256_file,
    validate_receipt_chain,
)
from runner import (  # noqa: E402
    TABLE_BY_FAMILY,
    _parse_target_command,
    build_approved_universe,
    run_case_process,
)


class SealedBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_path = SUITE_DIR / "private" / "seal.key"
        cls.sealed = load_sealed_suite(SUITE_DIR, cls.key_path)

    def test_bundle_hashes_and_case_count(self) -> None:
        manifest = self.sealed.manifest
        self.assertEqual(manifest["case_count"], 192)
        self.assertGreaterEqual(manifest["case_count"], manifest["minimum_case_count"])
        self.assertEqual(
            sha256_file(SUITE_DIR / "questions.jsonl"), manifest["questions_sha256"]
        )
        self.assertEqual(
            sha256_file(SUITE_DIR / "expectations.aesgcm.json"),
            manifest["sealed_expectations_envelope_sha256"],
        )
        self.assertEqual(manifest["runtime_target_executions_before_seal"], 0)
        self.assertEqual(manifest["target_responses_observed_before_seal"], 0)

    def test_agent_authorship_declaration_is_precise(self) -> None:
        declaration = self.sealed.manifest["author_declaration"]
        self.assertEqual(declaration["author"], "OpenAI Codex Agent")
        self.assertEqual(
            declaration["independence"], "independent_runtime_behavior_blind"
        )
        self.assertFalse(declaration["human_domain_blind"])
        self.assertFalse(declaration["double_blind"])

    def test_public_questions_contain_no_expectations_or_canaries(self) -> None:
        raw = (SUITE_DIR / "questions.jsonl").read_bytes()
        self.assertNotIn(b"SBV2-SECRET-", raw)
        self.assertNotIn(b"expectation", raw.lower())
        for line in raw.splitlines():
            self.assertEqual(
                set(json.loads(line)), {"case_id", "request_id", "question", "locale"}
            )

    def test_aes_gcm_nonce_is_unique_and_nontrivial(self) -> None:
        envelopes = [load_json(path) for path in SUITE_DIR.glob("*.aesgcm.json")]
        self.assertTrue(envelopes)
        nonces = [base64.b64decode(item["nonce_b64"], validate=True) for item in envelopes]
        self.assertTrue(all(len(nonce) == 12 for nonce in nonces))
        self.assertTrue(all(nonce != b"\x00" * 12 for nonce in nonces))
        self.assertEqual(len(nonces), len(set(nonces)))

    def test_receipt_chain_and_private_permissions(self) -> None:
        private_dir = SUITE_DIR / "private"
        self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        receipt_path = private_dir / "chronology.jsonl"
        self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
        entries = validate_receipt_chain(receipt_path)
        self.assertEqual([entry["event"] for entry in entries], ["authoring_started", "sealed"])
        self.assertEqual(entries[-1]["payload"]["runtime_target_executions_observed"], 0)

    def test_all_sealed_cases_align_without_exposing_contents(self) -> None:
        self.assertEqual(len(self.sealed.questions), len(self.sealed.expectations))
        self.assertEqual(
            [item["case_id"] for item in self.sealed.questions],
            [item["case_id"] for item in self.sealed.expectations],
        )
        self.assertTrue(
            all(
                item["secret_canary"].startswith("SBV2-SECRET-")
                for item in self.sealed.expectations
            )
        )


class EvaluationControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sealed = load_sealed_suite(SUITE_DIR, SUITE_DIR / "private" / "seal.key")

    @staticmethod
    def _control_response(question: dict, sealed_case: dict) -> dict:
        expected = sealed_case["expectation"]
        clarification_expected = expected.get("clarification")
        clarification = None
        if clarification_expected is not None:
            clarification = {
                "code": clarification_expected["code"],
                "message": "상품 식별자가 필요합니다.",
                "required_fields": clarification_expected["required_fields"],
                "options": [],
            }
        response = {
            "schema_version": "1.0",
            "request_id": question["request_id"],
            "status": expected["status"],
            "intent": expected["intent"],
            "product_families": expected["product_families"],
            "answer": "실행하지 않는 안전 통제 응답입니다.",
            "query_plan": None,
            "candidate_count": None,
            "products": [],
            "comparisons": [],
            "aggregates": [],
            "documents": [],
            "citations": [],
            "as_of_dates": [],
            "warnings": [],
            "answer_mode": "control",
            "fallback_used": False,
            "provider_model": None,
            "clarification": clarification,
            "error": None,
            "source_manifest": None,
            "family_searches": [],
            "source_manifests": [],
        }
        self = response
        assert set(self) == EXPECTED_RESPONSE_KEYS
        return response

    def _first_control(self) -> tuple[dict, dict]:
        for question, sealed_case in zip(
            self.sealed.questions, self.sealed.expectations, strict=True
        ):
            if sealed_case["expectation"]["zero_execution"]:
                return question, sealed_case
        self.fail("sealed suite has no zero-execution control")

    def test_zero_execution_control_accepts_only_empty_execution_state(self) -> None:
        question, sealed_case = self._first_control()
        response = self._control_response(question, sealed_case)
        errors = evaluate_response(
            question=question,
            sealed_case=sealed_case,
            response=response,
            stdout=canonical_bytes(response),
            stderr=b"",
            universe={},
        )
        self.assertEqual(errors, [])
        response["candidate_count"] = 0
        response["query_plan"] = {"unexpected": "execution"}
        errors = evaluate_response(
            question=question,
            sealed_case=sealed_case,
            response=response,
            stdout=canonical_bytes(response),
            stderr=b"",
            universe={},
        )
        self.assertIn("control_candidate_count_present", errors)
        self.assertIn("control_query_plan_present", errors)

    def test_secret_canary_is_detected_in_target_output(self) -> None:
        question, sealed_case = self._first_control()
        response = self._control_response(question, sealed_case)
        response["answer"] = sealed_case["secret_canary"]
        errors = evaluate_response(
            question=question,
            sealed_case=sealed_case,
            response=response,
            stdout=canonical_bytes(response),
            stderr=b"",
            universe={},
        )
        self.assertIn("secret_canary_leak", errors)
        self.assertIn("sealed_canary_prefix_leak", errors)

    def test_exact_query_semantics_rejects_an_extra_constraint(self) -> None:
        for question, sealed_case in zip(
            self.sealed.questions, self.sealed.expectations, strict=True
        ):
            expected = sealed_case["expectation"]
            if not expected["zero_execution"]:
                semantics = expected["query_semantics"]
                break
        else:
            self.fail("sealed suite has no executable positive control")
        plan = {
            "schema_version": "1.0",
            "question_id": question["request_id"],
            "intent": semantics["intent"],
            "product_families": semantics["product_families"],
            "constraints": semantics["constraints"],
            "ranking": semantics["ranking"],
            "projection": list(dict.fromkeys(semantics["required_projection"])),
            "limit": semantics.get("limit", 20),
            "intent_payload": {
                "comparison_fields": semantics["comparison_fields"],
                "group_by": semantics["group_by"],
                "aggregations": semantics["aggregations"],
                "explain_product_ids": semantics["explain_product_ids"],
            },
            "ambiguities": semantics["ambiguities"],
            "unsupported_conditions": semantics["unsupported_conditions"],
        }
        errors: list[str] = []
        _check_query_semantics(errors, plan, question["request_id"], semantics)
        self.assertEqual(errors, [])
        plan["constraints"] = [
            {
                "field": "product_name",
                "operator": "contains",
                "value": "injected",
                "unit": "none",
                "strength": "locked",
            }
        ]
        errors = []
        _check_query_semantics(errors, plan, question["request_id"], semantics)
        self.assertIn("query_semantics_constraints_mismatch", errors)

    def test_product_must_belong_to_hash_pinned_universe(self) -> None:
        errors: list[str] = []
        product = {
            "product_id": "outside-id",
            "product_name": "synthetic",
            "ticker": None,
            "fields": [
                {
                    "canonical_field": "product_id",
                    "source_dataset": "bond",
                    "source_id": "PRBD01N001",
                    "source_row": 2,
                    "quality": "VALID",
                },
                {
                    "canonical_field": "product_name",
                    "source_dataset": "bond",
                    "source_id": "PRBD01N001",
                    "source_row": 2,
                    "quality": "VALID",
                },
            ],
        }
        _check_product_membership_and_provenance(
            errors,
            [product],
            family="bond",
            source_id="PRBD01N001",
            universe={"bond": frozenset({"approved-id"})},
        )
        self.assertIn("product_outside_approved_universe", errors)


class IsolationAndUniverseTests(unittest.TestCase):
    def test_hard_process_timeout_kills_the_process_group(self) -> None:
        result = run_case_process(
            argv=[sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=0.15,
            cwd=SUITE_DIR,
            case_id="synthetic-timeout-control",
        )
        self.assertTrue(result.timed_out)
        self.assertLess(result.elapsed_ms, 1500)

    def test_target_command_cannot_reference_private_material(self) -> None:
        with self.assertRaises(Exception):
            _parse_target_command('["tool","--key","private/seal.key","{request_json}"]')

    def test_hash_pinned_universe_reads_only_product_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            databases: dict[str, Path] = {}
            datasets: dict[str, dict] = {}
            for family, table in TABLE_BY_FAMILY.items():
                path = root / f"{family}.sqlite3"
                connection = sqlite3.connect(path)
                columns = "product_id TEXT PRIMARY KEY, is_quarantined INTEGER"
                if family == "fund":
                    columns += ", public_offering INTEGER"
                connection.execute(f'CREATE TABLE "{table}" ({columns})')
                if family == "fund":
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES (?, ?, ?)',
                        (f"{family}-approved", 0, 1),
                    )
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES (?, ?, ?)',
                        (f"{family}-private", 0, 0),
                    )
                else:
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES (?, ?)',
                        (f"{family}-approved", 0),
                    )
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES (?, ?)',
                        (f"{family}-quarantined", 1),
                    )
                connection.commit()
                connection.close()
                databases[family] = path
                datasets[family] = {
                    "database_sha256": sha256_file(path),
                    "searchable_rows": 1,
                }
            universe = build_approved_universe(
                databases=databases,
                approved_manifest={"datasets": datasets},
            )
            self.assertEqual(
                universe,
                {
                    family: frozenset({f"{family}-approved"})
                    for family in TABLE_BY_FAMILY
                },
            )

    def test_receipt_append_is_hash_chained_and_state_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            append_receipt(private, event="authoring_started", payload={}, create=True)
            append_receipt(
                private,
                event="sealed",
                payload={},
                expected_existing_events=["authoring_started"],
            )
            with self.assertRaises(Exception):
                append_receipt(
                    private,
                    event="run_started",
                    payload={},
                    expected_existing_events=["authoring_started"],
                )
            self.assertEqual(
                [entry["event"] for entry in validate_receipt_chain(private / "chronology.jsonl")],
                ["authoring_started", "sealed"],
            )

    def test_evaluator_sources_do_not_import_target_agent(self) -> None:
        for name in (
            "integrity.py",
            "seal.py",
            "evaluator.py",
            "runner.py",
            "http_adapter.py",
            "freeze.py",
        ):
            source = (SUITE_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("finance_agent_core.agent", source)


if __name__ == "__main__":
    unittest.main()
