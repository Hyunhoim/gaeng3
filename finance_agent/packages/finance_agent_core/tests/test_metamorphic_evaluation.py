from __future__ import annotations

import json

import pytest

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.providers import (
    LocalProviderError,
    LocalTestSettings,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.metamorphic import (
    QWEN_EVAL_LAB_AXES,
    ExpectedMutationProvider,
    GeneratedMutation,
    LocalQwenMutationProvider,
    MutationAxis,
    MutationBatch,
    generate_mutation_batch,
    load_metamorphic_protocol,
    mutation_batch_semantic_sha256,
    validate_mutation,
)
from finance_agent_core.evaluation.metamorphic_cli import main as metamorphic_main
from finance_agent_core.evaluation.metamorphic_run_cli import build_parser as build_run_parser
from finance_agent_core.evaluation.metamorphic_runner import MetamorphicRunner
from finance_agent_core.evaluation.official_mock import load_official_mock_suite
from finance_agent_core.evaluation.official_mock_gold_audit import (
    apply_official_mock_gold_audit,
    load_official_mock_gold_audit,
)
from finance_agent_core.evaluation.red_team_e2e import ProviderTelemetry


def test_protocol_pins_every_official_mock_case_and_source_hash() -> None:
    protocol = load_metamorphic_protocol().protocol
    source = load_official_mock_suite()

    assert protocol.source_suite_sha256 == source.sha256
    assert protocol.source_case_ids == [case.id for case in source.suite.cases]
    assert protocol.axes == list(QWEN_EVAL_LAB_AXES)
    assert protocol.status == "internal_development_not_blind"


def test_gold_audit_corrects_two_sort_directions_without_mutating_source() -> None:
    source = load_official_mock_suite()
    loaded_audit = load_official_mock_gold_audit()
    source_by_id = {case.id: case for case in source.suite.cases}

    audited_cases, observed_audit = apply_official_mock_gold_audit(
        source.suite.cases,
        source_suite_sha256=source.sha256,
        database_sha256_by_family={
            "overseas_etp": "eee9009ca741713a9a61e498cd5ed8366836d754c7d0c2dbd74ed7e456a2ebbe",
            "bond": "40265aa326d63244727294ac29c1cd38c898f05b6a80dfd51fd8ac38e08764bc",
        },
    )
    audited_by_id = {case.id: case for case in audited_cases}

    assert observed_audit == loaded_audit
    assert [item.case_id for item in loaded_audit.audit.corrections] == [
        "official-mock-v1-009",
        "official-mock-v1-016",
    ]
    assert source_by_id["official-mock-v1-009"].expectation.product_ids == [
        "AMX:SURI.K",
        "AMX:XBNB.K",
        "AMX:XXRP.K",
    ]
    assert audited_by_id["official-mock-v1-009"].expectation.product_ids == [
        "AMX:AAAD.K",
        "AMX:BBUS.K",
        "AMX:BIV",
    ]
    assert audited_by_id["official-mock-v1-016"].expectation.product_ids == [
        "KR6169374E75",
        "KR6066571474",
        "KR6029889674",
    ]


def test_gold_audit_fails_closed_on_database_hash_drift() -> None:
    source = load_official_mock_suite()

    with pytest.raises(ValueError, match="database SHA-256 differs"):
        apply_official_mock_gold_audit(
            source.suite.cases,
            source_suite_sha256=source.sha256,
            database_sha256_by_family={
                "overseas_etp": "0" * 64,
                "bond": "0" * 64,
            },
        )


def test_hard_literal_validation_protects_labeled_overseas_ticker() -> None:
    source = "해외 ETF 종목코드 IVEG.O의 상세 정보 조회"
    changed = GeneratedMutation(
        axis=MutationAxis.PARAPHRASE,
        question="해외 ETF 종목코드 IWTR.O의 상세 정보를 조회해 주세요",
    )

    result = validate_mutation(source, changed)

    assert not result.passed
    assert "hard_literals_exact" in result.violations


def test_validation_preserves_hard_literals_and_rejects_numeric_change() -> None:
    source = "총보수 0.20% 이하인 해외 ETF를 5개 보여줘"
    valid = GeneratedMutation(
        axis=MutationAxis.PARAPHRASE,
        question="해외 ETF 중 총보수가 0.20% 이하인 상품 5개를 보여 주세요",
    )
    changed = valid.model_copy(
        update={"question": "해외 ETF 중 총보수가 0.25% 이하인 상품 3개를 보여 주세요"}
    )

    assert validate_mutation(source, valid).passed
    rejected = validate_mutation(source, changed)
    assert not rejected.passed
    assert "hard_literals_exact" in rejected.violations


@pytest.mark.parametrize(
    ("source", "question", "violation"),
    [
        (
            "1개월 수익률 순으로 보여줘",
            "1개월 수익률 순으로 보여줘. 결과는 1개만 보여줘",
            "hard_literals_exact",
        ),
        (
            "총보수 0.20% 이하인 상품",
            "총보수 0.20% 미만인 상품",
            "comparison_operators_exact",
        ),
        (
            "거래가 중지된 해외 ETF 4개",
            "상장폐지된 해외 ETF 4개",
            "critical_concepts_exact",
        ),
    ],
)
def test_validator_rejects_condition_semantics_changes(
    source: str,
    question: str,
    violation: str,
) -> None:
    validation = validate_mutation(
        source,
        GeneratedMutation(axis=MutationAxis.PARAPHRASE, question=question),
    )

    assert violation in validation.violations


def test_validator_requires_every_expected_product_family() -> None:
    source = "공모펀드 KR5010101714와 KR5010101702를 비교해줘"
    validation = validate_mutation(
        source,
        GeneratedMutation(
            axis=MutationAxis.CLAUSE_REORDERING,
            question="KR5010101714와 KR5010101702를 비교해줘",
        ),
        expected_families=[ProductFamily.FUND],
    )

    assert "product_families_preserved" in validation.violations


def test_validation_rejects_source_copy_sibling_duplicate_and_json_wrapper() -> None:
    source = "현재 매수 가능한 국내채권은 총 몇 개인지 계산해줘"
    copied = GeneratedMutation(axis=MutationAxis.PARAPHRASE, question=source)
    duplicate = GeneratedMutation(
        axis=MutationAxis.CLAUSE_REORDERING,
        question="현재 매수 가능한 국내채권 수를 계산해 주세요",
    )
    wrapped = GeneratedMutation(
        axis=MutationAxis.DISTRACTOR_RESISTANCE,
        question='{"question":"현재 매수 가능한 국내채권 수를 계산해 주세요"}',
    )

    assert "not_source_copy" in validate_mutation(source, copied).violations
    assert (
        "not_duplicate"
        in validate_mutation(
            source,
            duplicate,
            sibling_questions=[duplicate.question],
        ).violations
    )
    assert "no_code_or_json_wrapper" in validate_mutation(source, wrapped).violations


def test_expected_batch_is_complete_accepted_and_timestamp_independent() -> None:
    provider = ExpectedMutationProvider()

    first = generate_mutation_batch(
        provider,
        generated_at_utc="2026-08-08T00:00:00+00:00",
    )
    second = generate_mutation_batch(
        provider,
        generated_at_utc="2026-08-08T01:00:00+00:00",
    )

    assert first.requested_count == 90
    assert first.generated_count == 90
    assert first.accepted_count == 90
    assert first.rejected_count == 0
    assert mutation_batch_semantic_sha256(first) == mutation_batch_semantic_sha256(second)


def test_expected_cli_writes_reproducible_batch(tmp_path, capsys) -> None:
    output = tmp_path / "mutations.json"

    exit_code = metamorphic_main(
        [
            "--generator",
            "expected",
            "--output",
            str(output),
            "--require-all-accepted",
        ]
    )

    assert exit_code == 0
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["accepted_count"] == 90
    summary = json.loads(capsys.readouterr().out)
    assert summary["all_accepted"] is True


def test_runner_checks_control_mutations_without_databases() -> None:
    full_batch = generate_mutation_batch(
        ExpectedMutationProvider(),
        generated_at_utc="2026-08-08T00:00:00+00:00",
    )
    selected_source_ids = {
        "official-mock-v1-010",
        "official-mock-v1-020",
        "official-mock-v1-029",
        "official-mock-v1-030",
    }
    selected = [
        candidate
        for candidate in full_batch.candidates
        if candidate.source_case_id in selected_source_ids
    ]
    payload = full_batch.model_dump(mode="json")
    payload.update(
        requested_count=len(selected),
        generated_count=len(selected),
        accepted_count=len(selected),
        rejected_count=0,
        candidates=[candidate.model_dump(mode="json") for candidate in selected],
    )
    batch = MutationBatch.model_validate(payload)
    service = RoutedFinanceAgent({})
    services = {family: service for family in ProductFamily}

    report = MetamorphicRunner(
        batch=batch,
        services=services,
        agent_profile="expected",
        database_sha256_by_family={family.value: "0" * 64 for family in ProductFamily},
        telemetry=ProviderTelemetry(),
        agent_model=None,
    ).run(generated_at_utc="2026-08-08T00:00:00+00:00")

    assert report.summary.source_passed == report.summary.source_total == 4
    assert report.summary.candidate_passed == report.summary.candidate_executed == 12
    assert report.summary.semantic_consistency_rate == 1.0
    assert report.summary.safety_pass_rate == 1.0
    assert report.summary.perfect


def test_runner_cli_accepts_frozen_batch_replay_without_generator() -> None:
    arguments = build_run_parser().parse_args(
        [
            "--batch-input",
            "mutations.json",
            "--agent-provider",
            "expected",
        ]
    )

    assert arguments.batch_input.name == "mutations.json"
    assert arguments.agent_provider == "expected"


@pytest.mark.parametrize(
    "profile",
    ["local_test_plan_only", "local_test_answer_only", "local_test"],
)
def test_runner_cli_accepts_qwen_ablation_profiles(profile: str) -> None:
    arguments = build_run_parser().parse_args(
        [
            "--batch-input",
            "mutations.json",
            "--agent-provider",
            profile,
        ]
    )

    assert arguments.agent_provider == profile


def test_local_qwen_provider_enforces_structured_axis_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalQwenMutationProvider(
        LocalTestSettings(
            base_url="http://127.0.0.1:18000/v1",
            model="qwen-test",
        )
    )
    case = load_official_mock_suite().suite.cases[0]
    variants = [
        {
            "axis": axis.value,
            "question": f"{case.question} - {axis.value} 표현",
        }
        for axis in QWEN_EVAL_LAB_AXES
    ]
    captured: dict[str, object] = {}

    def fake_request(path: str, payload: dict[str, object]):
        captured.update(path=path, payload=payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"variants": variants},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(provider._client, "_request_json", fake_request)

    generated = provider.generate_mutations(case, list(QWEN_EVAL_LAB_AXES))

    assert [item.axis for item in generated] == list(QWEN_EVAL_LAB_AXES)
    assert captured["path"] == "chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0.6
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_local_qwen_provider_rejects_missing_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalQwenMutationProvider(
        LocalTestSettings(
            base_url="http://127.0.0.1:18000/v1",
            model="qwen-test",
        )
    )
    case = load_official_mock_suite().suite.cases[0]
    monkeypatch.setattr(
        provider._client,
        "_request_json",
        lambda _path, _payload: {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "variants": [
                                    {
                                        "axis": "paraphrase",
                                        "question": "해외 ETF 상세 정보를 다른 표현으로 조회해줘",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        },
    )

    with pytest.raises(LocalProviderError, match="axes differ"):
        provider.generate_mutations(case, list(QWEN_EVAL_LAB_AXES))
