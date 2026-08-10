from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.evaluation.briefing_examples import (
    BriefingAnswerability,
    BriefingDifficulty,
    BriefingExampleSuite,
    evaluate_briefing_example,
    load_briefing_example_suite,
)


def test_briefing_example_suite_preserves_official_distribution() -> None:
    loaded = load_briefing_example_suite()

    assert loaded.suite.status == "official_examples_public_not_blind"
    assert len(loaded.suite.cases) == 8
    assert Counter(case.answerability for case in loaded.suite.cases) == Counter(
        {
            BriefingAnswerability.ANSWERABLE: 5,
            BriefingAnswerability.UNANSWERABLE: 3,
        }
    )
    assert Counter(
        case.difficulty
        for case in loaded.suite.cases
        if case.answerability is BriefingAnswerability.ANSWERABLE
    ) == Counter(
        {
            BriefingDifficulty.LOW: 1,
            BriefingDifficulty.MEDIUM: 2,
            BriefingDifficulty.HIGH: 2,
        }
    )


def test_briefing_example_suite_rejects_duplicate_question() -> None:
    payload = load_briefing_example_suite().suite.model_dump(mode="json")
    payload["cases"][1]["question"] = payload["cases"][0]["question"]

    with pytest.raises(ValidationError, match="questions must be unique"):
        BriefingExampleSuite.model_validate(payload)


def test_unknown_entity_is_safely_controlled_without_evidence() -> None:
    case = load_briefing_example_suite().suite.cases[6]
    service = RoutedFinanceAgent({})
    request = BackendAgentRequest(request_id=case.id, question=case.question)

    result = evaluate_briefing_example(
        case,
        execute_answer_request(service, request),
        latency_ms=1.0,
    )

    assert result.passed
    assert result.actual_backend_status.value == "clarification"
    assert result.evidence_count == 0
