from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.planning_policy import PlanningDecision
from finance_agent_core.agent.providers import first_vertical_slice_plan
from finance_agent_core.contracts.queryplan import (
    Ambiguity,
    ProductFamily,
    UnsupportedCondition,
)
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.execution import (
    DatasetAuthorityStatus,
    PlanAuthorityCode,
    PlanAuthorityError,
    PlanAuthorityGate,
    PlanCompilerKind,
    SQLiteAggregateOracle,
    SQLiteOracle,
    ValidatedPlan,
    authorize_internal_evaluation_plan,
    query_plan_authority_sha256,
)
from finance_agent_core.execution import authority as authority_module
from finance_agent_core.execution.authority import require_verifier_budget
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)
from finance_agent_core.storage.pinned_sqlite import PinnedSQLiteArtifact


def test_pinned_sqlite_open_close_is_stable_under_concurrency(sample_database) -> None:
    path, _, _ = sample_database
    guard = PinnedSQLiteArtifact(path)

    def read_schema(_: int) -> int:
        observed = 0
        for _ in range(20):
            with guard.connect_read_only() as connection:
                observed += int(connection.execute("PRAGMA schema_version").fetchone()[0])
        return observed

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            observations = list(executor.map(read_schema, range(8)))
    finally:
        guard.close()

    assert all(value > 0 for value in observations)


def _aggregate_plan(agent: RoutedFinanceAgent, request_id: str):
    decision = agent.router.route(
        "해외 ETF의 총보수율 평균을 집계해줘",
        request_id,
    )
    return agent.compiler.compile(decision)


def _mutate_database_file(
    path: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    if mutation == "replace":
        replacement = tmp_path / f"replacement-{path.name}"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return
    if mutation == "in_place":
        with path.open("ab") as stream:
            stream.write(b"\x00")
        return
    raise AssertionError(f"unknown database mutation: {mutation}")


def _plan_and_executor(
    path: Path,
    execution_path: str,
):
    if execution_path == "aggregate":
        plan = _aggregate_plan(
            RoutedFinanceAgent({"overseas_etp": path}),
            "authority-real-race-aggregate",
        )
        return plan, lambda validated: SQLiteAggregateOracle(path).execute(validated)
    plan = first_vertical_slice_plan(f"authority-real-race-{execution_path}")
    if execution_path == "search":
        return plan, lambda validated: SQLiteOracle(path).execute(validated)
    if execution_path == "projection":
        return plan, lambda validated: load_projected_verifier_records(path, validated)
    raise AssertionError(f"unknown execution path: {execution_path}")


def test_gate_issues_nominal_validated_plan_with_complete_receipt(
    sample_database,
) -> None:
    path, _, manifest = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "authority-receipt-001",
    )
    proposal = agent.compiler.compile(trace.route_decision)

    validated = agent.plan_authority_gate.validate_routed(
        proposal,
        trace.route_decision,
        planning_decision=trace.planning_decision,
    )
    receipt = validated.receipt

    assert type(validated) is ValidatedPlan
    assert validated.canonical_plan == proposal
    assert validated.canonical_plan is not proposal
    assert receipt.plan_sha256 == query_plan_authority_sha256(proposal)
    assert receipt.request_id == proposal.question_id
    assert receipt.route_decision_sha256 is not None
    assert receipt.planning_decision_sha256 is not None
    assert receipt.planning_policy_version == "adaptive-shadow-v1"
    assert receipt.capability_matrix_version == "2026-07-30.3"
    assert len(receipt.capability_matrix_sha256) == 64
    assert receipt.registry_schema_version == "1.3"
    assert len(receipt.registry_sha256) == 64
    assert len(receipt.ontology_bundle_sha256) == 64
    assert receipt.dataset is ProductFamily.OVERSEAS_ETP
    assert receipt.dataset_authority_status is DatasetAuthorityStatus.TEST_FIXTURE
    assert receipt.source_snapshot_date == manifest.source_snapshot_date
    assert receipt.database_manifest_sha256
    assert receipt.database_sha256
    assert receipt.compiler_version == "server-queryplan-compiler-v1"
    assert receipt.compiler_kind is PlanCompilerKind.SERVER_QUERY_PLAN
    assert receipt.proposal_provider_name is None
    assert receipt.proposal_model_name is None
    assert receipt.verifier_version == "result-verifier-v1"
    assert receipt.max_candidate_rows == manifest.searchable_rows
    assert receipt.max_verifier_rows == manifest.total_rows
    assert receipt.max_result_rows == proposal.limit
    assert "authority_seal" not in validated.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ValidatedPlan.model_validate(validated.model_dump(mode="json"))


@pytest.mark.parametrize(
    "mutation",
    ["limit", "field", "operator", "value_type", "unit", "direction", "family"],
)
def test_gate_revalidates_model_copy_or_nested_mutation(
    sample_database,
    mutation: str,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan(f"authority-invalid-{mutation}")
    if mutation == "limit":
        invalid = plan.model_copy(update={"limit": 101})
    elif mutation == "field":
        invalid = plan.model_copy(deep=True)
        invalid.constraints[0] = invalid.constraints[0].model_copy(
            update={"field": "not_a_registry_field"}
        )
    elif mutation == "operator":
        invalid = plan.model_copy(deep=True)
        invalid.constraints[0] = invalid.constraints[0].model_copy(
            update={"operator": type(invalid.constraints[0].operator).CONTAINS}
        )
    elif mutation == "value_type":
        invalid = plan.model_copy(deep=True)
        invalid.constraints[0] = invalid.constraints[0].model_copy(update={"value": 123})
    elif mutation == "unit":
        invalid = plan.model_copy(deep=True)
        invalid.constraints[0] = invalid.constraints[0].model_copy(
            update={"unit": type(invalid.constraints[0].unit).PCT_POINT}
        )
    elif mutation == "direction":
        invalid = plan.model_copy(deep=True)
        invalid.ranking[0] = invalid.ranking[0].model_copy(update={"direction": "sideways"})
    else:
        invalid = plan.model_copy(
            update={
                "product_families": [
                    ProductFamily.OVERSEAS_ETP,
                    ProductFamily.DOMESTIC_ETP,
                ]
            }
        )

    if mutation == "direction":
        with pytest.warns(UserWarning, match="serializer warnings"):
            with pytest.raises(PlanAuthorityError) as raised:
                authorize_internal_evaluation_plan(invalid, path)
    else:
        with pytest.raises(PlanAuthorityError) as raised:
            authorize_internal_evaluation_plan(invalid, path)

    assert raised.value.code is PlanAuthorityCode.INVALID_PROPOSAL


@pytest.mark.parametrize("blocker", ["ambiguity", "unsupported"])
def test_gate_never_issues_a_plan_with_unresolved_conditions(
    sample_database,
    blocker: str,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan(f"authority-blocked-{blocker}")
    if blocker == "ambiguity":
        plan = plan.model_copy(
            update={
                "ambiguities": [
                    Ambiguity(
                        span="수익률",
                        reason="기간을 확정할 수 없음",
                        options=["1개월", "3개월"],
                    )
                ]
            }
        )
    else:
        plan = plan.model_copy(
            update={
                "unsupported_conditions": [
                    UnsupportedCondition(
                        span="전망",
                        reason="제공 데이터로 미래 전망을 검증할 수 없음",
                    )
                ]
            }
        )

    with pytest.raises(PlanAuthorityError) as raised:
        authorize_internal_evaluation_plan(plan, path)

    assert raised.value.code is PlanAuthorityCode.EXECUTION_POLICY_BLOCKED


def test_raw_queryplan_cannot_reach_oracle_or_projection_loader(
    sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, _ = sample_database
    search_plan = first_vertical_slice_plan("authority-raw-search")
    aggregate_plan = _aggregate_plan(
        RoutedFinanceAgent({"overseas_etp": path}),
        "authority-raw-aggregate",
    )

    def fail_connect(*args, **kwargs):
        raise AssertionError("unauthorized plan must fail before a DB connection")

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", fail_connect)

    with pytest.raises(PlanAuthorityError) as search_error:
        SQLiteOracle(path).execute(search_plan)  # type: ignore[arg-type]
    with pytest.raises(PlanAuthorityError) as aggregate_error:
        SQLiteAggregateOracle(path).execute(aggregate_plan)  # type: ignore[arg-type]
    with pytest.raises(PlanAuthorityError) as projection_error:
        load_projected_verifier_records(path, search_plan)  # type: ignore[arg-type]

    assert search_error.value.code is PlanAuthorityCode.UNAUTHORIZED_PLAN_TYPE
    assert aggregate_error.value.code is PlanAuthorityCode.UNAUTHORIZED_PLAN_TYPE
    assert projection_error.value.code is PlanAuthorityCode.UNAUTHORIZED_PLAN_TYPE


def test_forged_or_serialized_receipt_never_restores_execution_authority(
    sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("authority-forged-001")
    issued = authorize_internal_evaluation_plan(plan, path)
    forged = ValidatedPlan(
        canonical_plan_json=issued.canonical_plan_json,
        receipt=issued.receipt,
        authority_seal="0" * 64,
        database_guard=issued.database_guard,
    )

    def fail_connect(*args, **kwargs):
        raise AssertionError("forged authority must fail before a DB connection")

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", fail_connect)
    with pytest.raises(PlanAuthorityError) as raised:
        SQLiteOracle(path).execute(forged)

    assert raised.value.code in {
        PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
        PlanAuthorityCode.DATASET_MISMATCH,
    }
    with pytest.raises(ValidationError):
        ValidatedPlan.model_validate(issued.model_dump(mode="json"))


@pytest.mark.parametrize("tamper", ["plan", "receipt", "deep_view"])
def test_model_copy_and_deep_mutation_cannot_change_issued_authority(
    sample_database,
    tamper: str,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan(f"authority-tamper-{tamper}")
    issued = authorize_internal_evaluation_plan(plan, path)

    if tamper == "plan":
        changed = plan.model_copy(update={"limit": 1})
        candidate = issued.model_copy(update={"canonical_plan_json": changed.model_dump_json()})
    elif tamper == "receipt":
        changed_receipt = issued.receipt.model_copy(update={"max_result_rows": 100})
        candidate = issued.model_copy(update={"receipt": changed_receipt})
    else:
        mutable_view = issued.canonical_plan
        mutable_view.projection.append("isin")
        assert "isin" not in issued.canonical_plan.projection
        candidate = issued.model_copy()

    if tamper == "deep_view":
        executed = SQLiteOracle(path).execute(candidate)
        assert executed.question_id == plan.question_id
    else:
        with pytest.raises(PlanAuthorityError) as raised:
            SQLiteOracle(path).execute(candidate)
        assert raised.value.code is PlanAuthorityCode.INVALID_AUTHORITY_SEAL


def test_validated_plan_is_bound_to_database_path_and_oracle_kind(
    sample_database,
    tmp_path: Path,
) -> None:
    path, _, _ = sample_database
    search_plan = first_vertical_slice_plan("authority-binding-search")
    search_validated = authorize_internal_evaluation_plan(search_plan, path)
    other_path = tmp_path / "same-bytes.sqlite3"
    other_path.write_bytes(path.read_bytes())

    with pytest.raises(PlanAuthorityError) as path_error:
        SQLiteOracle(other_path).execute(search_validated)
    with pytest.raises(PlanAuthorityError) as mode_error:
        SQLiteAggregateOracle(path).execute(search_validated)

    assert path_error.value.code is PlanAuthorityCode.INVALID_AUTHORITY_SEAL
    assert mode_error.value.code is PlanAuthorityCode.ORACLE_MODE_MISMATCH

    agent = RoutedFinanceAgent({"overseas_etp": path})
    aggregate_plan = _aggregate_plan(agent, "authority-binding-aggregate")
    aggregate_validated = authorize_internal_evaluation_plan(aggregate_plan, path)
    with pytest.raises(PlanAuthorityError) as reverse_mode_error:
        SQLiteOracle(path).execute(aggregate_validated)
    assert reverse_mode_error.value.code is PlanAuthorityCode.ORACLE_MODE_MISMATCH


def test_gate_rejects_a_database_changed_during_authorization(
    sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, _ = sample_database
    actual_stat = authority_module._database_stat(path)
    changed_stat = (*actual_stat[:-1], actual_stat[-1] + 1)
    observed = iter((actual_stat, changed_stat))
    monkeypatch.setattr(
        authority_module,
        "_database_stat",
        lambda _path: next(observed),
    )

    with pytest.raises(PlanAuthorityError) as raised:
        authorize_internal_evaluation_plan(
            first_vertical_slice_plan("authority-database-race-001"),
            path,
        )

    assert raised.value.code is PlanAuthorityCode.DATASET_MISMATCH


@pytest.mark.parametrize("mutation", ["replace", "in_place"])
@pytest.mark.parametrize("execution_path", ["search", "aggregate", "projection"])
def test_real_database_mutation_after_issuance_fails_before_connection(
    sample_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    execution_path: str,
) -> None:
    path, _, _ = sample_database
    plan, execute = _plan_and_executor(path, execution_path)
    validated = authorize_internal_evaluation_plan(plan, path)
    _mutate_database_file(path, tmp_path, mutation)

    def fail_connect(*args, **kwargs):
        raise AssertionError("stale database authority must fail before a DB connection")

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", fail_connect)

    with pytest.raises(PlanAuthorityError) as raised:
        execute(validated)

    assert raised.value.code in {
        PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
        PlanAuthorityCode.DATASET_MISMATCH,
    }


@pytest.mark.parametrize("mutation", ["replace", "in_place"])
@pytest.mark.parametrize("execution_path", ["search", "aggregate", "projection"])
def test_real_database_mutation_during_execution_discards_the_result(
    sample_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    execution_path: str,
) -> None:
    path, _, _ = sample_database
    plan, execute = _plan_and_executor(path, execution_path)
    validated = authorize_internal_evaluation_plan(plan, path)
    original_connect = PinnedSQLiteArtifact.connect_read_only

    @contextmanager
    def mutate_after_query(guard):
        with original_connect(guard) as connection:
            try:
                yield connection
            finally:
                # Change the real path while the read-only connection is still
                # open, after the Oracle/projection has fetched its rows but
                # before its post-execution authority check can return them.
                _mutate_database_file(path, tmp_path, mutation)

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", mutate_after_query)

    with pytest.raises(PlanAuthorityError) as raised:
        execute(validated)

    assert raised.value.code in {
        PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
        PlanAuthorityCode.DATASET_MISMATCH,
    }


@pytest.mark.parametrize("execution_path", ["search", "aggregate", "projection"])
def test_replace_and_restore_before_sqlite_open_fails_closed(
    sample_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_path: str,
) -> None:
    path, _, _ = sample_database
    plan, execute = _plan_and_executor(path, execution_path)
    validated = authorize_internal_evaluation_plan(plan, path)
    approved_hold = tmp_path / "approved-held.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(path.read_bytes())
    original_connect = PinnedSQLiteArtifact.connect_read_only

    @contextmanager
    def replace_while_opening(guard):
        path.replace(approved_hold)
        replacement.replace(path)
        try:
            with original_connect(guard) as connection:
                yield connection
        finally:
            path.unlink(missing_ok=True)
            approved_hold.replace(path)

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", replace_while_opening)

    with pytest.raises(PlanAuthorityError) as raised:
        execute(validated)

    assert raised.value.code in {
        PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
        PlanAuthorityCode.DATASET_MISMATCH,
    }


@pytest.mark.parametrize("execution_path", ["search", "aggregate", "projection"])
def test_replace_and_restore_after_connection_never_returns_a_result(
    sample_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_path: str,
) -> None:
    path, _, _ = sample_database
    plan, execute = _plan_and_executor(path, execution_path)
    validated = authorize_internal_evaluation_plan(plan, path)
    approved_hold = tmp_path / "approved-held.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(path.read_bytes())
    original_connect = PinnedSQLiteArtifact.connect_read_only

    @contextmanager
    def replace_after_connection(guard):
        with original_connect(guard) as connection:
            path.replace(approved_hold)
            replacement.replace(path)
            try:
                yield connection
            finally:
                path.unlink(missing_ok=True)
                approved_hold.replace(path)

    monkeypatch.setattr(PinnedSQLiteArtifact, "connect_read_only", replace_after_connection)

    with pytest.raises(PlanAuthorityError) as raised:
        execute(validated)

    assert raised.value.code in {
        PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
        PlanAuthorityCode.DATASET_MISMATCH,
    }


def test_routed_gate_rejects_request_and_capability_mismatch(sample_database) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "authority-route-001",
    )
    plan = agent.compiler.compile(trace.route_decision)
    wrong_draft = trace.route_decision.draft.model_copy(
        update={"request_id": "authority-route-other"}
    )
    wrong_request = trace.route_decision.model_copy(update={"draft": wrong_draft})
    stale_matrix = trace.route_decision.model_copy(
        update={"capability_matrix_version": "stale-matrix"}
    )

    for route in (wrong_request, stale_matrix):
        with pytest.raises(PlanAuthorityError) as raised:
            agent.plan_authority_gate.validate_routed(
                plan,
                route,
                planning_decision=trace.planning_decision,
            )
        assert raised.value.code in {
            PlanAuthorityCode.ROUTE_MISMATCH,
            PlanAuthorityCode.CAPABILITY_MISMATCH,
        }


def test_routed_gate_rejects_missing_failed_or_misaligned_planning_record(
    sample_database,
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "authority-planning-001",
    )
    plan = agent.compiler.compile(trace.route_decision)
    policy_error = PlanningDecision.fail_closed(trace.route_decision)
    wrong_reason = trace.planning_decision.model_copy(
        update={"route_reason_code": "different_route"}
    )

    for planning in (None, policy_error, wrong_reason, trace.route_decision):
        with pytest.raises(PlanAuthorityError) as raised:
            agent.plan_authority_gate.validate_routed(
                plan,
                trace.route_decision,
                planning_decision=planning,  # type: ignore[arg-type]
            )
        assert raised.value.code in {
            PlanAuthorityCode.ROUTE_MISMATCH,
            PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
        }


def test_grounded_compiler_receipt_requires_explicit_hclx_planning_authority(
    sample_database,
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "authority-grounded-permission",
    )
    plan = agent.compiler.compile(trace.route_decision)

    with pytest.raises(PlanAuthorityError) as raised:
        agent.plan_authority_gate.validate_routed(
            plan,
            trace.route_decision,
            planning_decision=trace.planning_decision,
            compiler_kind=PlanCompilerKind.GROUNDED_PLAN_GATE,
            proposal_provider_name="hyperclova",
            proposal_model_name="HCX-007",
        )

    assert raised.value.code is PlanAuthorityCode.STALE_AUTHORITY_CONTEXT


def test_internal_evaluation_authority_requires_an_explicit_issuer_gate(
    sample_database,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("authority-internal-gate-001")
    ordinary_gate = PlanAuthorityGate(
        {"overseas_etp": path},
        require_approved_databases=False,
        allow_internal_disabled_dataset=True,
    )

    with pytest.raises(PlanAuthorityError) as raised:
        ordinary_gate.validate_internal_evaluation(plan)

    assert raised.value.code is PlanAuthorityCode.EXECUTION_POLICY_BLOCKED


def test_public_gate_can_require_an_active_request_deadline(sample_database) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    trace = agent.router.route_with_planning(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "authority-required-deadline-001",
    )
    plan = agent.compiler.compile(trace.route_decision)
    gate = PlanAuthorityGate(
        {"overseas_etp": path},
        require_approved_databases=False,
        require_request_deadline=True,
    )

    with pytest.raises(PlanAuthorityError) as absent:
        gate.validate_routed(
            plan,
            trace.route_decision,
            planning_decision=trace.planning_decision,
        )
    assert absent.value.code is PlanAuthorityCode.DEADLINE_EXCEEDED

    with bind_request_deadline(RequestDeadline.after(5)):
        issued = gate.validate_routed(
            plan,
            trace.route_decision,
            planning_decision=trace.planning_decision,
        )
        executed = SQLiteOracle(path).execute(issued)

    assert executed.question_id == plan.question_id


def test_deadline_scope_is_required_and_cancellation_revokes_authority(
    sample_database,
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("authority-deadline-001")
    deadline = RequestDeadline.after(5)
    with bind_request_deadline(deadline):
        issued = authorize_internal_evaluation_plan(plan, path)
        assert issued.receipt.deadline_budget_ms is not None

    with pytest.raises(PlanAuthorityError) as absent:
        SQLiteOracle(path).execute(issued)
    assert absent.value.code is PlanAuthorityCode.DEADLINE_EXCEEDED

    deadline = RequestDeadline.after(5)
    with bind_request_deadline(deadline):
        cancelled = authorize_internal_evaluation_plan(plan, path)
        deadline.cancel()
        with pytest.raises(PlanAuthorityError) as stopped:
            SQLiteOracle(path).execute(cancelled)
    assert stopped.value.code is PlanAuthorityCode.DEADLINE_EXCEEDED


def test_receipt_row_budget_cannot_be_raised_with_model_copy(sample_database) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("authority-budget-001")
    issued = authorize_internal_evaluation_plan(plan, path)
    forged_receipt = issued.receipt.model_copy(
        update={"max_candidate_rows": issued.receipt.max_candidate_rows + 1}
    )
    forged = issued.model_copy(update={"receipt": forged_receipt})

    with pytest.raises(PlanAuthorityError) as raised:
        SQLiteOracle(path).execute(forged)

    assert raised.value.code is PlanAuthorityCode.INVALID_AUTHORITY_SEAL


def test_verifier_universe_requires_the_exact_manifest_row_count(sample_database) -> None:
    path, _, _ = sample_database
    issued = authorize_internal_evaluation_plan(
        first_vertical_slice_plan("authority-verifier-size-001"),
        path,
    )
    expected = issued.receipt.max_verifier_rows

    require_verifier_budget(issued, expected)
    for observed in (expected - 1, expected + 1):
        with pytest.raises(PlanAuthorityError) as raised:
            require_verifier_budget(issued, observed)
        assert raised.value.code is PlanAuthorityCode.DATASET_MISMATCH
