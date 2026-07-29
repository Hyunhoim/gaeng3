from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_agent_core.evaluation import blind_cli
from finance_agent_core.evaluation.blind import (
    BLIND_CATEGORY_DISPOSITION_QUOTAS,
    BLIND_CATEGORY_QUOTAS,
    BLIND_DISPOSITION_QUOTAS,
    BLIND_LANGUAGE_DISPOSITION_QUOTAS,
    BLIND_LANGUAGE_PROFILE_QUOTAS,
    BlindAnswer,
    BlindAnswerKey,
    BlindCommitment,
    BlindQuestionSet,
    create_blind_commitment,
    reject_near_duplicates,
    validate_blind_bundle,
    verify_blind_commitment,
)
from finance_agent_core.evaluation.blind_cli import main as blind_cli_main


def _blind_bundle() -> tuple[BlindQuestionSet, BlindAnswerKey]:
    category_dispositions = [
        (category, disposition)
        for category, dispositions in BLIND_CATEGORY_DISPOSITION_QUOTAS.items()
        for disposition, count in dispositions.items()
        for _ in range(count)
    ]
    profiles_by_disposition = {
        disposition: [
            profile
            for profile, dispositions in BLIND_LANGUAGE_DISPOSITION_QUOTAS.items()
            for _ in range(dispositions[disposition])
        ]
        for disposition in BLIND_DISPOSITION_QUOTAS
    }
    profile_offsets = {disposition: 0 for disposition in BLIND_DISPOSITION_QUOTAS}
    questions: list[dict[str, object]] = []
    answers: list[dict[str, object]] = []
    for index, (category, disposition) in enumerate(category_dispositions):
        case_id = f"fund-blind-v1.1-{index + 1:03d}"
        profile_index = profile_offsets[disposition]
        profile = profiles_by_disposition[disposition][profile_index]
        profile_offsets[disposition] += 1
        if profile == "explicit":
            product_phrase = "공모펀드"
        elif profile == "implicit_public_scope":
            product_phrase = "이 펀드 상품"
        else:
            product_phrase = "공모 펀드 상품"
        questions.append(
            {
                "id": case_id,
                "question": (
                    f"{product_phrase} 중 독립 작성 문항 {index + 1:03d}을 "
                    f"상품명 순으로 확인해 주세요"
                ),
                "category": category,
                "language_profile": profile,
            }
        )
        answer: dict[str, object] = {
            "id": case_id,
            "constraints": [
                {
                    "field": "public_offering",
                    "operator": "eq",
                    "value": True,
                    "strength": "locked",
                }
            ],
            "ranking": [],
            "limit": 5,
            "rationale": f"독립 작성자가 기록한 문항 {index + 1:03d}의 기대 근거",
        }
        if disposition == "execute":
            answer.update(
                {
                    "ranking": [
                        {
                            "field": "product_name",
                            "direction": "asc",
                            "nulls": "last",
                        }
                    ],
                    "disposition": "execute",
                    "blocker": None,
                    "oracle": {"candidate_count": 0, "top_product_ids": []},
                }
            )
        elif disposition == "ambiguity":
            answer.update(
                {
                    "disposition": "block",
                    "blocker": "ambiguity",
                    "oracle": None,
                }
            )
        else:
            answer.update(
                {
                    "disposition": "block",
                    "blocker": "unsupported",
                    "oracle": None,
                }
            )
        answers.append(answer)
    return (
        BlindQuestionSet.model_validate(
            {
                "schema_version": "1.0",
                "suite_id": "fund-blind-v1.1-100",
                "dataset": "fund",
                "author_role": "financial_domain",
                "cases": questions,
            }
        ),
        BlindAnswerKey.model_validate(
            {
                "schema_version": "1.0",
                "suite_id": "fund-blind-v1.1-100",
                "dataset": "fund",
                "database_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "cases": answers,
            }
        ),
    )


def _write_bundle(
    tmp_path: Path,
    questions: BlindQuestionSet,
    answers: BlindAnswerKey,
) -> tuple[Path, Path]:
    question_path = tmp_path / "questions.json"
    answer_path = tmp_path / "answers.json"
    question_path.write_text(
        f"{questions.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    answer_path.write_text(
        f"{answers.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    return question_path, answer_path


def test_blind_bundle_enforces_independent_authoring_quotas_and_scope() -> None:
    questions, answers = _blind_bundle()

    summary = validate_blind_bundle(questions, answers)

    assert summary["question_count"] == 100
    assert summary["categories"] == BLIND_CATEGORY_QUOTAS
    assert summary["language_profiles"] == BLIND_LANGUAGE_PROFILE_QUOTAS
    assert summary["dispositions"] == BLIND_DISPOSITION_QUOTAS
    assert summary["category_dispositions"] == BLIND_CATEGORY_DISPOSITION_QUOTAS
    assert summary["language_dispositions"] == BLIND_LANGUAGE_DISPOSITION_QUOTAS


def test_blind_bundle_rejects_skewed_cross_distribution() -> None:
    questions, answers = _blind_bundle()
    payload = answers.model_dump(mode="json")
    scope_execute = next(
        index
        for index, (question, answer) in enumerate(zip(questions.cases, answers.cases, strict=True))
        if question.category.value == "scope_status" and answer.blocker is None
    )
    classification_ambiguity = next(
        index
        for index, (question, answer) in enumerate(zip(questions.cases, answers.cases, strict=True))
        if question.category.value == "classification"
        and answer.blocker is not None
        and answer.blocker.value == "ambiguity"
    )
    transferable = ("ranking", "disposition", "blocker", "oracle")
    for field in transferable:
        (
            payload["cases"][scope_execute][field],
            payload["cases"][classification_ambiguity][field],
        ) = (
            payload["cases"][classification_ambiguity][field],
            payload["cases"][scope_execute][field],
        )
    skewed_answers = BlindAnswerKey.model_validate(payload)

    with pytest.raises(ValueError, match="category/disposition matrix differs"):
        validate_blind_bundle(questions, skewed_answers)


def test_blind_bundle_rejects_reference_question_reuse() -> None:
    questions, _ = _blind_bundle()

    with pytest.raises(ValueError, match="too similar"):
        reject_near_duplicates(
            questions,
            [questions.cases[0].question],
        )


def test_blocked_blind_answer_cannot_keep_executable_ranking() -> None:
    with pytest.raises(ValueError, match="must not retain executable ranking"):
        BlindAnswer.model_validate(
            {
                "id": "fund-blind-v1.1-001",
                "constraints": [
                    {
                        "field": "public_offering",
                        "operator": "eq",
                        "value": True,
                    }
                ],
                "ranking": [
                    {
                        "field": "aum",
                        "direction": "desc",
                        "nulls": "last",
                    }
                ],
                "limit": 5,
                "disposition": "block",
                "blocker": "unsupported",
                "oracle": None,
                "rationale": "지원할 수 없는 집계 조건이 포함된 문항",
            }
        )


def test_blind_commitment_detects_mutation_and_parser_commit_change(
    tmp_path: Path,
) -> None:
    questions, answers = _blind_bundle()
    question_path, answer_path = _write_bundle(tmp_path, questions, answers)
    parser_commit = "c" * 40
    commitment = create_blind_commitment(
        question_path,
        answer_path,
        parser_commit=parser_commit,
        created_at_utc="2026-07-29T20:00:00Z",
        reference_questions=[],
    )

    verify_blind_commitment(
        BlindCommitment.model_validate(commitment.model_dump()),
        question_path,
        answer_path,
        parser_commit=parser_commit,
    )
    with pytest.raises(ValueError, match="parser commit differs"):
        verify_blind_commitment(
            commitment,
            question_path,
            answer_path,
            parser_commit="d" * 40,
        )

    question_path.write_text(
        f"{question_path.read_text(encoding='utf-8')} ",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="question hash differs"):
        verify_blind_commitment(
            commitment,
            question_path,
            answer_path,
            parser_commit=parser_commit,
        )


def test_blind_cli_validates_commits_and_verifies_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    questions, answers = _blind_bundle()
    question_path, answer_path = _write_bundle(tmp_path, questions, answers)
    commitment_path = tmp_path / "commitment.json"
    parser_commit = "e" * 40

    assert (
        blind_cli_main(
            [
                "validate",
                "--questions",
                str(question_path),
                "--answers",
                str(answer_path),
            ]
        )
        == 0
    )
    assert (
        blind_cli_main(
            [
                "commit",
                "--questions",
                str(question_path),
                "--answers",
                str(answer_path),
                "--parser-commit",
                parser_commit,
                "--output",
                str(commitment_path),
            ]
        )
        == 0
    )
    assert (
        blind_cli_main(
            [
                "verify",
                "--questions",
                str(question_path),
                "--answers",
                str(answer_path),
                "--commitment",
                str(commitment_path),
                "--parser-commit",
                parser_commit,
            ]
        )
        == 0
    )
    assert '"status": "verified"' in capsys.readouterr().out


def test_blind_run_requires_matching_clean_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_commit = "f" * 40
    dirty = False

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if command[1:3] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{parser_commit}\n")
        return SimpleNamespace(stdout=" M linker.py\n" if dirty else "")

    monkeypatch.setattr(blind_cli.subprocess, "run", fake_run)

    blind_cli._require_frozen_checkout(parser_commit)
    dirty = True
    with pytest.raises(RuntimeError, match="clean worktree"):
        blind_cli._require_frozen_checkout(parser_commit)
    with pytest.raises(RuntimeError, match="checkout differs"):
        blind_cli._require_frozen_checkout("0" * 40)


def test_blind_first_run_state_can_only_be_claimed_once(tmp_path: Path) -> None:
    questions, answers = _blind_bundle()
    question_path, answer_path = _write_bundle(tmp_path, questions, answers)
    commitment = create_blind_commitment(
        question_path,
        answer_path,
        parser_commit="f" * 40,
        created_at_utc="2026-07-29T20:00:00Z",
        reference_questions=[],
    )
    state_path = tmp_path / "first-run.json"

    blind_cli._claim_first_run(state_path, commitment, "local-test-model")

    with pytest.raises(FileExistsError):
        blind_cli._claim_first_run(state_path, commitment, "local-test-model")

    state = state_path.read_text(encoding="utf-8")
    assert '"status": "started"' in state
    assert '"parser_commit": "ffffffffffffffffffffffffffffffffffffffff"' in state
