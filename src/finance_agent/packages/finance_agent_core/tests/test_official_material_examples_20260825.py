"""Historical 2026-08-25 pre-corpus contract; excluded from the current freeze gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.config.knowledge_coverage import (
    KnowledgeCapability,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.queryplan import ProductFamily

_SUITE_PATH = (
    Path(__file__).resolve().parents[3]
    / "evaluation"
    / "suites"
    / "official-material-examples-8-20260825.json"
)
_FROZEN_SUITE_SHA256 = "aaa58625595fa7266d9304312dcaa2e0e2cc5a53f900367b04e93047d1dfe759"

pytestmark = pytest.mark.historical_contract


def _suite() -> dict[str, object]:
    raw = _SUITE_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FROZEN_SUITE_SHA256
    return json.loads(raw)


def test_public_official_examples_are_frozen_against_the_20260824_databases() -> None:
    suite = _suite()

    assert suite["suite_id"] == "official-material-examples-8-20260825"
    assert suite["dataset_release_id"] == "miraeasset-ai-festival-2026-20260824-v2"
    assert len(suite["cases"]) == 8
    assert [case["id"] for case in suite["cases"]] == [
        "EXAMPLE-01",
        "EXAMPLE-02",
        "EXAMPLE-03",
        "EXAMPLE-04",
        "EXAMPLE-05",
        "UNANSWERABLE-01",
        "UNANSWERABLE-02",
        "UNANSWERABLE-03",
    ]


def test_unapproved_relation_and_document_examples_never_reach_product_sql() -> None:
    cases = _suite()["cases"]
    relation_cases = [*cases[1:5], cases[6]]
    for case in relation_cases:
        decision = IntentRouter().route(case["question"], case["id"])

        assert decision.disposition.value == "unsupported", case["id"]
        assert decision.draft.intent.value == "unsupported", case["id"]
        assert decision.query_plan_intent is None, case["id"]


def test_named_fund_identity_is_preserved_before_unsupported_document_gate() -> None:
    decision = IntentRouter().route(
        "국민성장펀드 정보를 알려줘.",
        "official-named-fund-identity",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "detail"
    assert decision.draft.product_families == [ProductFamily.FUND]
    assert decision.draft.product_mentions == ["국민성장펀드"]


def test_absent_branded_product_name_executes_locked_lookup_and_returns_not_found(
    domestic_sample_database,
) -> None:
    path, _, _ = domestic_sample_database
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})
    decision = agent.router.route(
        "KODEX AI로봇 ETF 정보 알려줘.",
        "UNANSWERABLE-03-plan",
    )
    plan = agent.compiler.compile(decision)

    assert [
        (item.field, item.operator.value, item.value, item.strength.value)
        for item in plan.constraints
        if item.field == "product_name"
    ] == [("product_name", "eq", "KODEX AI로봇", "locked")]
    assert plan.ambiguities == []
    assert plan.unsupported_conditions == []

    result = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="UNANSWERABLE-03",
            question="KODEX AI로봇 ETF 정보 알려줘.",
        ),
    )
    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.NOT_FOUND
    assert result.response.intent.value == "detail"
    assert result.response.candidate_count == 0
    assert result.response.products == []
    assert result.response.query_plan is not None
    assert result.response.query_plan == plan.model_copy(update={"question_id": "UNANSWERABLE-03"})


def test_exact_product_name_alias_resolves_its_family_and_canonical_id(
    domestic_sample_database,
) -> None:
    path, records, _ = domestic_sample_database
    expected = records[0]
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})

    result = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="official-exact-name-alias",
            question=f'"{expected.product_name}" 상품 정보를 알려줘.',
        ),
    )
    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.SUCCESS
    assert result.response.product_families == [ProductFamily.DOMESTIC_ETP]
    assert [product.product_id for product in result.response.products] == [expected.product_id]
    assert result.response.query_plan is not None
    assert [
        item.value for item in result.response.query_plan.constraints if item.field == "product_id"
    ] == [expected.product_id]


def test_external_knowledge_capabilities_use_only_approved_cache_authority() -> None:
    manifest = load_approved_knowledge_coverage_manifest()

    assert manifest.dataset_release_id == "miraeasset-ai-festival-2026-20260824-v5"
    assert {entry.capability for entry in manifest.capabilities} == set(KnowledgeCapability)
    enabled = tuple(entry for entry in manifest.capabilities if entry.runtime_execution_enabled)
    disabled = tuple(
        entry for entry in manifest.capabilities if not entry.runtime_execution_enabled
    )

    assert {entry.capability for entry in enabled} == {
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    }
    assert all(entry.status == "approved" for entry in enabled)
    assert len({entry.corpus_sha256 for entry in enabled}) == 1
    assert len({entry.index_sha256 for entry in enabled}) == 1
    assert all(entry.corpus_sha256 is not None for entry in enabled)
    assert all(entry.index_sha256 is not None for entry in enabled)

    assert all(entry.status == "not_configured" for entry in disabled)
    assert all(entry.corpus_sha256 is None for entry in disabled)
    assert all(entry.index_sha256 is None for entry in disabled)
    assert all(entry.status != "approved_live_official" for entry in manifest.capabilities)
