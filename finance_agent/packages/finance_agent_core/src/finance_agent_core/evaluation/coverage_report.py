from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from finance_agent_core.evaluation.coverage_ablation import (
    CoverageAblationReport,
    CoverageBucketDelta,
)
from finance_agent_core.evaluation.coverage_questions import CoverageQuestionBatch
from finance_agent_core.evaluation.semantics import canonical_json_sha256


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _signed_percent(value: float) -> str:
    return f"{value * 100:+.1f}%p"


def _milliseconds(value: float) -> str:
    return f"{value:+,.1f}ms"


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _shorten(value: object, *, limit: int = 180) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _table(headers: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    rendered = [f"| {' | '.join(headers)} |", f"| {' | '.join('---' for _ in headers)} |"]
    rendered.extend(f"| {' | '.join(_escape_cell(value) for value in row)} |" for row in rows)
    return rendered


def _accepted_source_sha256(batch: CoverageQuestionBatch) -> str:
    questions = [
        {"id": candidate.id, "question": candidate.question}
        for candidate in batch.candidates
        if candidate.validation.passed
    ]
    if not questions:
        raise ValueError("coverage report question batch has no accepted questions")
    return canonical_json_sha256({"kind": "naturalized", "questions": questions})


def _validate_question_batch(
    report: CoverageAblationReport,
    batch: CoverageQuestionBatch,
) -> None:
    if report.source_kind != "naturalized":
        raise ValueError("coverage generation metrics require a naturalized ablation")
    accepted = sum(candidate.validation.passed for candidate in batch.candidates)
    if accepted != batch.accepted_count:
        raise ValueError("coverage question batch accepted count differs")
    if _accepted_source_sha256(batch) != report.source_semantic_sha256:
        raise ValueError("coverage question batch and ablation questions differ")
    if any(profile.total != accepted for profile in report.profiles):
        raise ValueError("coverage profile denominator differs from accepted questions")


def _rejection_reasons(batch: CoverageQuestionBatch) -> Counter[str]:
    return Counter(
        violation
        for candidate in batch.candidates
        if not candidate.validation.passed
        for violation in candidate.validation.violations
    )


def _top_changes(
    breakdowns: dict[str, list[CoverageBucketDelta]],
    *,
    limit: int,
) -> list[tuple[str, CoverageBucketDelta]]:
    changed = [
        (dimension, bucket)
        for dimension, buckets in breakdowns.items()
        for bucket in buckets
        if bucket.rescued or bucket.regressed
    ]
    return sorted(
        changed,
        key=lambda item: (
            -abs(item[1].net_rescued),
            -item[1].regressed,
            -item[1].total,
            item[0],
            item[1].value,
        ),
    )[:limit]


def _candidate_metadata(candidate: object) -> tuple[str, str, str]:
    cell = getattr(candidate, "cell", None)
    if cell is None:
        return "—", "—", "—"
    family = getattr(cell, "product_family", None)
    intent = getattr(cell, "intent", None)
    axis = getattr(candidate, "axis", None)
    return (
        getattr(family, "value", family) or "—",
        getattr(intent, "value", intent) or "—",
        getattr(axis, "value", axis) or "—",
    )


def _generation_review_sections(
    batch: CoverageQuestionBatch,
    *,
    limit: int,
) -> list[str]:
    lines: list[str] = []
    rejected = sorted(
        (candidate for candidate in batch.candidates if not candidate.validation.passed),
        key=lambda candidate: (candidate.validation.violations, candidate.id),
    )
    if rejected:
        lines.extend(
            [
                "### 의미 보존 검사 탈락 표본",
                "",
                *_table(
                    ["질문 ID", "상품군", "의도", "문체", "탈락 사유", "생성 질문"],
                    [
                        [
                            candidate.id,
                            *_candidate_metadata(candidate),
                            ", ".join(candidate.validation.violations),
                            _shorten(candidate.question),
                        ]
                        for candidate in rejected[:limit]
                    ],
                ),
                "",
                f"전체 {len(rejected)}건 중 최대 {limit}건을 고정 순서로 표시",
                "",
            ]
        )
    failures = sorted(
        batch.generation_failures,
        key=lambda failure: (failure.error_type, failure.source_case_id),
    )
    if failures:
        lines.extend(
            [
                "### 질문 생성 오류 표본",
                "",
                *_table(
                    ["원본 계획 ID", "상품군", "의도", "오류", "메시지"],
                    [
                        [
                            failure.source_case_id,
                            getattr(
                                failure.cell.product_family,
                                "value",
                                failure.cell.product_family,
                            ),
                            getattr(failure.cell.intent, "value", failure.cell.intent),
                            failure.error_type,
                            _shorten(failure.error_message),
                        ]
                        for failure in failures[:limit]
                    ],
                ),
                "",
                f"전체 {len(failures)}건 중 최대 {limit}건을 고정 순서로 표시",
                "",
            ]
        )
    return lines


def _change_review_sections(
    report: CoverageAblationReport,
    batch: CoverageQuestionBatch,
    *,
    limit: int,
) -> list[str]:
    candidates = {candidate.id: candidate for candidate in batch.candidates}
    lines: list[str] = []
    for delta in report.pairwise_deltas:
        rows: list[list[object]] = []
        for change, identifiers in (
            ("구제", delta.rescued_case_ids),
            ("퇴행", delta.regressed_case_ids),
        ):
            for identifier in identifiers[:limit]:
                candidate = candidates.get(identifier)
                if candidate is None:
                    raise ValueError(
                        f"coverage change question is missing from batch: {identifier}"
                    )
                rows.append(
                    [
                        change,
                        identifier,
                        *_candidate_metadata(candidate),
                        _shorten(candidate.question),
                    ]
                )
        if not rows:
            continue
        lines.extend(
            [
                f"### {report.baseline_label} → {delta.candidate_label}",
                "",
                *_table(
                    ["변화", "질문 ID", "상품군", "의도", "문체", "질문"],
                    rows,
                ),
                "",
                f"구제·퇴행 각각 최대 {limit}건을 표시",
                "",
            ]
        )
    return lines


def render_coverage_experiment_markdown(
    report: CoverageAblationReport,
    *,
    question_batch: CoverageQuestionBatch | None = None,
    top_changes: int = 15,
    review_examples: int = 10,
) -> str:
    if top_changes < 1:
        raise ValueError("coverage report top_changes must be positive")
    if review_examples < 1:
        raise ValueError("coverage report review_examples must be positive")
    if question_batch is not None:
        _validate_question_batch(report, question_batch)

    lines = [
        "# 자연어 커버리지 실험 보고서",
        "",
        f"상태: `{report.status}` · 독립 blind 아님",
        "",
        f"생성 시각(UTC): `{report.generated_at_utc}`  ",
        f"질문 종류: `{report.source_kind}`  ",
        (f"통계 단위: `{report.statistical_unit}` ({report.statistical_unit_count}개)  "),
        f"질문 지문: `{report.source_semantic_sha256}`",
        "",
        "## 0. 해석 원칙",
        "",
        "이 보고서는 같은 질문에서 Agent 구성을 바꿨을 때 생긴 차이를 보여주는 내부 개발 실험임",
        "공모전 점수나 HyperCLOVA X 성능으로 해석하지 않으며, 개선 문항과 함께 "
        "기존 정답을 망가뜨린 퇴행 문항도 같은 분모에 포함",
        "",
    ]

    if question_batch is not None:
        acceptance = (
            0.0
            if question_batch.generated_count == 0
            else question_batch.accepted_count / question_batch.generated_count
        )
        overall_acceptance = question_batch.accepted_count / question_batch.requested_count
        lines.extend(
            [
                "## 1. Qwen 질문 생성",
                "",
                *_table(
                    ["요청", "생성", "기계 선별 통과", "거절", "생성 오류"],
                    [
                        [
                            question_batch.requested_count,
                            question_batch.generated_count,
                            question_batch.accepted_count,
                            question_batch.rejected_count,
                            question_batch.generation_failure_count,
                        ]
                    ],
                ),
                "",
                f"생성된 질문 중 의미 보존 통과율: **{_percent(acceptance)}**  ",
                f"전체 요청 기준 실행 가능 질문 비율: **{_percent(overall_acceptance)}**",
                "",
            ]
        )
        reasons = _rejection_reasons(question_batch)
        if reasons:
            lines.extend(
                [
                    "### 주요 기계 거절 사유",
                    "",
                    *_table(
                        ["거절 사유", "건수"],
                        reasons.most_common(10),
                    ),
                    "",
                ]
            )
        lines.extend(_generation_review_sections(question_batch, limit=review_examples))

    section_number = 2 if question_batch is not None else 1
    lines.extend(
        [
            f"## {section_number}. Agent 구성별 결과",
            "",
            *_table(
                [
                    "구성",
                    "strict",
                    "95% 구간",
                    "계획 일치",
                    "근거 일치",
                    "fallback",
                    "p95",
                ],
                [
                    [
                        profile.label,
                        f"{profile.passed}/{profile.total} ({_percent(profile.strict_accuracy)})",
                        (
                            f"{_percent(profile.strict_accuracy_ci95[0])}~"
                            f"{_percent(profile.strict_accuracy_ci95[1])}"
                        ),
                        _percent(profile.plan_semantic_rate),
                        _percent(profile.evidence_semantic_rate),
                        profile.fallback_count,
                        f"{profile.latency_ms['p95']:,.1f}ms",
                    ]
                    for profile in report.profiles
                ],
            ),
            "",
            "`strict`는 정답 계획과 최종 field evidence가 모두 같은 문항을 뜻함",
            "",
        ]
    )

    section_number += 1
    lines.extend(
        [
            f"## {section_number}. 기준선 대비 변화",
            "",
            *_table(
                [
                    "후보",
                    "정확도 변화",
                    "95% 구간",
                    "구제",
                    "퇴행",
                    "퇴행 0",
                    "검정 단위 구제/퇴행",
                    "Holm p",
                    "p95 변화",
                ],
                [
                    [
                        delta.candidate_label,
                        _signed_percent(delta.strict_accuracy_delta),
                        (
                            f"{_signed_percent(delta.strict_accuracy_delta_ci95[0])}~"
                            f"{_signed_percent(delta.strict_accuracy_delta_ci95[1])}"
                        ),
                        delta.rescued,
                        delta.regressed,
                        "예" if delta.zero_strict_regression else "아니오",
                        f"{delta.mcnemar_unit_rescued}/{delta.mcnemar_unit_regressed}",
                        f"{delta.holm_adjusted_p_value:.6g}",
                        _milliseconds(delta.latency_delta_ms["p95"]),
                    ]
                    for delta in report.pairwise_deltas
                ],
            ),
            "",
            "구제는 기준선이 틀리고 후보가 맞힌 문항, 퇴행은 기준선이 맞고 후보가 틀린 문항을 뜻함",
            (
                "검정 단위는 canonical이면 개별 문항, naturalized이면 같은 정답 계획에서 "
                "생성한 문체 변형 묶음이며 exact McNemar와 신뢰구간은 이 상관관계를 반영"
            ),
            "",
        ]
    )

    section_number += 1
    lines.extend([f"## {section_number}. 처음 실패한 단계", ""])
    failure_stages = sorted(
        {stage for profile in report.profiles for stage in profile.first_failure_stages}
    )
    if failure_stages:
        lines.extend(
            [
                *_table(
                    ["구성", *failure_stages],
                    [
                        [
                            profile.label,
                            *(
                                profile.first_failure_stages.get(stage, 0)
                                for stage in failure_stages
                            ),
                        ]
                        for profile in report.profiles
                    ],
                ),
                "",
            ]
        )
    else:
        lines.extend(["실패 문항 없음", ""])

    section_number += 1
    lines.extend([f"## {section_number}. 영향이 큰 기능 구간", ""])
    any_changes = False
    for delta in report.pairwise_deltas:
        changes = _top_changes(delta.breakdowns, limit=top_changes)
        if not changes:
            continue
        any_changes = True
        lines.extend(
            [
                f"### {report.baseline_label} → {delta.candidate_label}",
                "",
                *_table(
                    ["구분", "값", "문항", "기준선", "후보", "구제", "퇴행", "순개선"],
                    [
                        [
                            dimension,
                            bucket.value,
                            bucket.total,
                            _percent(bucket.baseline_accuracy),
                            _percent(bucket.candidate_accuracy),
                            bucket.rescued,
                            bucket.regressed,
                            bucket.net_rescued,
                        ]
                        for dimension, bucket in changes
                    ],
                ),
                "",
            ]
        )
    if not any_changes:
        lines.extend(["구제 또는 퇴행이 발생한 기능 구간 없음", ""])

    section_number += 1
    lines.extend([f"## {section_number}. 사람이 확인할 변화 문항", ""])
    if question_batch is None:
        lines.extend(["질문 batch를 함께 전달하면 구제·퇴행 질문 표본을 표시", ""])
    else:
        review_sections = _change_review_sections(
            report,
            question_batch,
            limit=review_examples,
        )
        if review_sections:
            lines.extend(review_sections)
        else:
            lines.extend(["구제 또는 퇴행 문항 없음", ""])

    section_number += 1
    lines.extend(
        [
            f"## {section_number}. 다음 판단",
            "",
            "- 정확도 개선뿐 아니라 퇴행 문항과 p95 지연을 함께 검토",
            "- 가장 많은 문항을 구제한 공통 실패 원인부터 수정",
            "- 수정에 사용한 같은 질문의 재실행은 회귀 결과로만 표기",
            "- 외부 blind와 HyperCLOVA X에서 같은 방향의 개선이 재현된 뒤 공식 후보로 채택",
            "",
            "## 해석 제한",
            "",
            *(f"- {limit}" for limit in report.interpretation_limits),
            "",
        ]
    )
    return "\n".join(lines)
