from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.evaluation.coverage_ablation import (
    CoverageAblationReport,
    compare_coverage_profiles,
)
from finance_agent_core.evaluation.coverage_plan import (
    CoverageModel,
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionCampaignReport,
    CoverageQuestionRunner,
    CoverageQuestionRunReport,
    load_coverage_question_batch,
    merge_coverage_question_run_reports,
)
from finance_agent_core.evaluation.coverage_questions import (
    CoverageQuestionBatch,
    CoverageQuestionProvider,
    coverage_question_batch_semantic_sha256,
    generate_coverage_question_batch,
    merge_coverage_question_batches,
)
from finance_agent_core.evaluation.coverage_run_cli import _PROFILES
from finance_agent_core.evaluation.coverage_runner import (
    CoverageAgentProfile,
    load_coverage_plan_suite,
    verify_coverage_databases,
)
from finance_agent_core.evaluation.red_team_cli import _build_services, _database_paths
from finance_agent_core.evaluation.red_team_e2e import ProviderCallSnapshot, ProviderTelemetry
from finance_agent_core.evaluation.semantic_roundtrip import LocalQwenSemanticQuestionProvider
from finance_agent_core.evaluation.semantics import canonical_json_sha256

type CampaignPhase = Literal["generate", "run", "compare", "all"]


class CoverageCampaignFile(CoverageModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CoverageCampaignProtocol(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    campaign_id: str
    recorded_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    source_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_worktree_clean: Literal[True] = True
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_offset: int = Field(ge=0)
    selected_source_count: int = Field(ge=1)
    shard_size: int = Field(ge=1)
    generator: Literal["local_test"] = "local_test"
    generator_model: str = Field(min_length=1)
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


class CoverageCampaignShard(CoverageModel):
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    source_case_ids: list[str] = Field(min_length=1)
    questions: CoverageCampaignFile
    runs: dict[str, CoverageCampaignFile]


class CoverageCampaignProfile(CoverageModel):
    profile: CoverageAgentProfile
    model: str | None
    campaign: CoverageCampaignFile
    accepted: int = Field(ge=0)
    executed: int = Field(ge=0)
    passed: int = Field(ge=0)
    strict_accuracy: float | None = Field(default=None, ge=0, le=1)
    end_to_end_yield: float = Field(ge=0, le=1)
    fallback_count: int = Field(ge=0)
    provider_calls: ProviderCallSnapshot


class CoverageExperimentManifest(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    campaign_id: str
    recorded_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    completed_stages: list[Literal["questions", "runs", "ablation"]]
    protocol: CoverageCampaignFile
    source_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_offset: int = Field(ge=0)
    selected_source_count: int = Field(ge=1)
    shard_size: int = Field(ge=1)
    generator: Literal["local_test"]
    generator_model: str
    question_campaign: CoverageCampaignFile
    shards: list[CoverageCampaignShard] = Field(min_length=1)
    profiles: list[CoverageCampaignProfile]
    ablation: CoverageCampaignFile | None
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _semantic_model_sha256(model: CoverageModel) -> str:
    payload = model.model_dump(mode="json")
    payload.pop("generated_at_utc", None)
    return canonical_json_sha256(payload)


def _protocol_semantic_sha256(protocol: CoverageCampaignProtocol) -> str:
    payload = protocol.model_dump(mode="json")
    payload.pop("recorded_at_utc", None)
    return canonical_json_sha256(payload)


def _git_source_state(path: Path) -> tuple[str, bool]:
    try:
        root = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tracked_changes = subprocess.run(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("coverage campaign requires a readable Git worktree") from error
    if len(commit) != 40:
        raise ValueError("coverage campaign Git commit is invalid")
    return commit, not tracked_changes


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_new_model(path: Path, model: CoverageModel) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite first-observation artifact: {path}")
    _atomic_write_text(path, f"{model.model_dump_json(indent=2)}\n")


def _load_or_create_protocol(
    path: Path,
    *,
    campaign_id: str,
    suite: CoveragePlanSuite,
    ranges: list[tuple[int, int]],
    shard_size: int,
    generator_model: str,
    source_git_commit: str,
) -> CoverageCampaignProtocol:
    expected = CoverageCampaignProtocol(
        campaign_id=campaign_id,
        recorded_at_utc=datetime.now(UTC).replace(microsecond=0).isoformat(),
        source_git_commit=source_git_commit,
        source_worktree_clean=True,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=coverage_plan_suite_semantic_sha256(suite),
        selection_offset=ranges[0][0],
        selected_source_count=sum(limit for _, limit in ranges),
        shard_size=shard_size,
        generator_model=generator_model,
        interpretation_limits=[
            "질문 생성과 Agent 실행은 이 Git commit과 suite 선택에서만 재개한다.",
            "tracked source가 바뀌면 같은 디렉터리에 결과를 섞지 않고 새 캠페인을 만든다.",
            "로컬 Qwen은 개발 전용이며 공식 제출 또는 HyperCLOVA X 성능이 아니다.",
        ],
    )
    if not path.exists():
        _write_new_model(path, expected)
        return expected
    observed = CoverageCampaignProtocol.model_validate_json(path.read_text(encoding="utf-8"))
    if _protocol_semantic_sha256(observed) != _protocol_semantic_sha256(expected):
        raise ValueError("coverage campaign protocol differs; use a new output directory")
    return observed


def _campaign_ranges(
    total: int,
    *,
    offset: int,
    limit: int | None,
    shard_size: int,
) -> list[tuple[int, int]]:
    if total < 1:
        raise ValueError("coverage campaign suite is empty")
    if offset < 0 or offset >= total:
        raise ValueError("coverage campaign offset is outside the suite")
    if limit is not None and limit < 1:
        raise ValueError("coverage campaign source limit must be positive")
    if shard_size < 1:
        raise ValueError("coverage campaign shard size must be positive")
    stop = total if limit is None else min(total, offset + limit)
    return [(start, min(shard_size, stop - start)) for start in range(offset, stop, shard_size)]


def _batch_source_ids(batch: CoverageQuestionBatch) -> set[str]:
    return {
        *(candidate.source_case_id for candidate in batch.candidates),
        *(failure.source_case_id for failure in batch.generation_failures),
    }


def _validate_batch_selection(
    batch: CoverageQuestionBatch,
    suite: CoveragePlanSuite,
    *,
    offset: int,
    limit: int,
    expected_model: str | None,
) -> None:
    expected_ids = {case.id for case in suite.cases[offset : offset + limit]}
    if batch.plan_suite_id != suite.suite_id:
        raise ValueError("coverage campaign question suite ID differs")
    if batch.plan_suite_semantic_sha256 != coverage_plan_suite_semantic_sha256(suite):
        raise ValueError("coverage campaign question suite SHA-256 differs")
    if batch.generator != "local_test":
        raise ValueError("coverage campaign requires local Qwen-generated questions")
    if not batch.model:
        raise ValueError("coverage campaign question generator model is missing")
    if expected_model is not None and batch.model != expected_model:
        raise ValueError("coverage campaign question generator model differs")
    if batch.selected_source_count != len(expected_ids):
        raise ValueError("coverage campaign question source count differs")
    if _batch_source_ids(batch) != expected_ids:
        raise ValueError("coverage campaign question source IDs differ")


def _question_path(output_dir: Path, offset: int, limit: int) -> Path:
    return output_dir / "questions" / f"questions-{offset:04d}-{offset + limit:04d}.json"


def _run_path(
    output_dir: Path,
    profile: CoverageAgentProfile,
    offset: int,
    limit: int,
) -> Path:
    return output_dir / "runs" / profile / f"run-{offset:04d}-{offset + limit:04d}.json"


def _file_reference(
    path: Path,
    output_dir: Path,
    *,
    semantic_sha256: str | None = None,
) -> CoverageCampaignFile:
    return CoverageCampaignFile(
        path=str(path.relative_to(output_dir)),
        sha256=_sha256_file(path),
        semantic_sha256=semantic_sha256,
    )


def _load_or_generate_batch(
    *,
    path: Path,
    provider: CoverageQuestionProvider | None,
    suite: CoveragePlanSuite,
    offset: int,
    limit: int,
    workers: int,
    expected_model: str | None,
) -> CoverageQuestionBatch:
    if path.exists():
        batch = load_coverage_question_batch(path)
    else:
        if provider is None:
            raise ValueError(f"coverage campaign question shard is missing: {path}")
        batch = generate_coverage_question_batch(
            provider,
            suite,
            offset=offset,
            limit=limit,
            workers=workers,
        )
        _write_new_model(path, batch)
    _validate_batch_selection(
        batch,
        suite,
        offset=offset,
        limit=limit,
        expected_model=expected_model,
    )
    return batch


def _load_or_write_merged_questions(
    path: Path,
    batches: list[CoverageQuestionBatch],
) -> CoverageQuestionBatch:
    expected = merge_coverage_question_batches(batches)
    if not path.exists():
        _write_new_model(path, expected)
        return expected
    observed = load_coverage_question_batch(path)
    if coverage_question_batch_semantic_sha256(observed) != (
        coverage_question_batch_semantic_sha256(expected)
    ):
        raise ValueError("coverage campaign merged question artifact differs")
    return observed


def _load_run_report(
    path: Path,
    *,
    suite: CoveragePlanSuite,
    batch: CoverageQuestionBatch,
    profile: CoverageAgentProfile,
) -> CoverageQuestionRunReport:
    report = CoverageQuestionRunReport.model_validate_json(path.read_text(encoding="utf-8"))
    if report.agent_profile != profile:
        raise ValueError("coverage campaign run profile differs")
    merge_coverage_question_run_reports(
        suite=suite,
        batches=[batch],
        reports=[report],
    )
    return report


def _load_or_run_shard(
    *,
    path: Path,
    suite: CoveragePlanSuite,
    batch: CoverageQuestionBatch,
    profile: CoverageAgentProfile,
    database_paths: dict[Any, Path],
) -> CoverageQuestionRunReport:
    if path.exists():
        return _load_run_report(
            path,
            suite=suite,
            batch=batch,
            profile=profile,
        )
    telemetry = ProviderTelemetry()
    services, model = _build_services(
        provider_name=profile,
        database_paths=database_paths,
        telemetry=telemetry,
    )
    report = CoverageQuestionRunner(
        suite=suite,
        batch=batch,
        services=services,
        agent_profile=profile,
        agent_model=model,
        telemetry=telemetry,
    ).run()
    _write_new_model(path, report)
    return report


def _load_or_write_profile_campaign(
    path: Path,
    *,
    suite: CoveragePlanSuite,
    batches: list[CoverageQuestionBatch],
    reports: list[CoverageQuestionRunReport],
) -> CoverageQuestionCampaignReport:
    expected = merge_coverage_question_run_reports(
        suite=suite,
        batches=batches,
        reports=reports,
    )
    if not path.exists():
        _write_new_model(path, expected)
        return expected
    observed = CoverageQuestionCampaignReport.model_validate_json(path.read_text(encoding="utf-8"))
    if _semantic_model_sha256(observed) != _semantic_model_sha256(expected):
        raise ValueError("coverage campaign merged run artifact differs")
    return observed


def _load_or_write_ablation(
    path: Path,
    reports: dict[str, CoverageQuestionCampaignReport],
) -> CoverageAblationReport:
    expected = compare_coverage_profiles(reports)
    if not path.exists():
        _write_new_model(path, expected)
        return expected
    observed = CoverageAblationReport.model_validate_json(path.read_text(encoding="utf-8"))
    if _semantic_model_sha256(observed) != _semantic_model_sha256(expected):
        raise ValueError("coverage campaign ablation artifact differs")
    return observed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a write-once, resumable local-Qwen coverage campaign: generate "
            "semantic questions in shards, evaluate identical questions across Agent "
            "profiles, and compute paired ablations."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, default=Path("artifacts/normalized"))
    parser.add_argument("--phase", choices=("generate", "run", "compare", "all"), default="all")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--source-limit", type=int)
    parser.add_argument("--shard-size", type=int, default=25)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--profile", action="append", choices=_PROFILES)
    return parser


def _manifest(
    *,
    output_dir: Path,
    protocol_path: Path,
    protocol: CoverageCampaignProtocol,
    suite: CoveragePlanSuite,
    ranges: list[tuple[int, int]],
    batches: list[CoverageQuestionBatch],
    merged_questions_path: Path,
    merged_questions: CoverageQuestionBatch,
    profile_reports_by_shard: dict[str, list[CoverageQuestionRunReport]],
    profile_campaigns: dict[str, CoverageQuestionCampaignReport],
    ablation_path: Path | None,
    ablation: CoverageAblationReport | None,
    shard_size: int,
) -> CoverageExperimentManifest:
    shards: list[CoverageCampaignShard] = []
    for index, ((offset, limit), batch) in enumerate(zip(ranges, batches, strict=True)):
        question_path = _question_path(output_dir, offset, limit)
        runs = {
            profile: _file_reference(
                _run_path(output_dir, profile, offset, limit),
                output_dir,
                semantic_sha256=_semantic_model_sha256(reports[index]),
            )
            for profile, reports in profile_reports_by_shard.items()
        }
        shards.append(
            CoverageCampaignShard(
                offset=offset,
                limit=limit,
                source_case_ids=[case.id for case in suite.cases[offset : offset + limit]],
                questions=_file_reference(
                    question_path,
                    output_dir,
                    semantic_sha256=coverage_question_batch_semantic_sha256(batch),
                ),
                runs=runs,
            )
        )
    profiles = [
        CoverageCampaignProfile(
            profile=campaign.agent_profile,
            model=campaign.agent_model,
            campaign=_file_reference(
                output_dir / "runs" / profile / "campaign.json",
                output_dir,
                semantic_sha256=_semantic_model_sha256(campaign),
            ),
            accepted=campaign.summary.accepted,
            executed=campaign.summary.executed,
            passed=campaign.summary.passed,
            strict_accuracy=campaign.summary.agent_strict_accuracy,
            end_to_end_yield=campaign.summary.end_to_end_yield,
            fallback_count=campaign.summary.fallback_count,
            provider_calls=campaign.provider_calls,
        )
        for profile, campaign in profile_campaigns.items()
    ]
    completed: list[Literal["questions", "runs", "ablation"]] = ["questions"]
    if profiles:
        completed.append("runs")
    if ablation is not None:
        completed.append("ablation")
    return CoverageExperimentManifest(
        campaign_id=output_dir.name,
        recorded_at_utc=datetime.now(UTC).replace(microsecond=0).isoformat(),
        completed_stages=completed,
        protocol=_file_reference(
            protocol_path,
            output_dir,
            semantic_sha256=_protocol_semantic_sha256(protocol),
        ),
        source_git_commit=protocol.source_git_commit,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=coverage_plan_suite_semantic_sha256(suite),
        selection_offset=ranges[0][0],
        selected_source_count=sum(limit for _, limit in ranges),
        shard_size=shard_size,
        generator="local_test",
        generator_model=merged_questions.model or "unknown",
        question_campaign=_file_reference(
            merged_questions_path,
            output_dir,
            semantic_sha256=coverage_question_batch_semantic_sha256(merged_questions),
        ),
        shards=shards,
        profiles=profiles,
        ablation=(
            None
            if ablation_path is None or ablation is None
            else _file_reference(
                ablation_path,
                output_dir,
                semantic_sha256=_semantic_model_sha256(ablation),
            )
        ),
        interpretation_limits=[
            "모든 shard 산출물은 최초 생성 후 덮어쓰지 않고 유효하면 재사용한다.",
            "Qwen은 canonical 질문 원문이 아니라 정답 QueryPlan 의미 명세만 받는다.",
            "기계 선별에서 거절된 질문과 생성 오류도 전체 생성 분모에 남긴다.",
            "동일 질문·동일 데이터 지문에서만 Agent profile의 구제와 퇴행을 비교한다.",
            "공개 데이터 기반 내부 synthetic 실험이며 독립 blind나 HCX 성능이 아니다.",
        ],
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    phase: CampaignPhase = arguments.phase
    suite = load_coverage_plan_suite(arguments.suite_input)
    ranges = _campaign_ranges(
        len(suite.cases),
        offset=arguments.offset,
        limit=arguments.source_limit,
        shard_size=arguments.shard_size,
    )
    profiles: list[CoverageAgentProfile] = arguments.profile or [
        "expected",
        "local_test_grounded_plan_only",
    ]
    if len(profiles) != len(set(profiles)):
        raise ValueError("coverage campaign profiles must be unique")
    if phase in {"compare", "all"} and len(profiles) < 2:
        raise ValueError("coverage campaign comparison requires at least two profiles")

    source_git_commit, source_worktree_clean = _git_source_state(Path.cwd())
    if not source_worktree_clean:
        raise ValueError(
            "coverage campaign refuses tracked source changes; "
            "commit them or use a new clean worktree"
        )

    provider: LocalQwenSemanticQuestionProvider | None = None
    configured_model: str | None = None
    if phase in {"generate", "all"}:
        settings = LocalTestSettings.from_environment()
        provider = LocalQwenSemanticQuestionProvider(settings)
        provider.healthcheck()
        configured_model = settings.model

    protocol_path = arguments.output_dir / "protocol.json"
    if configured_model is None:
        if not protocol_path.exists():
            raise ValueError("coverage campaign protocol is missing; run --phase generate first")
        existing_protocol = CoverageCampaignProtocol.model_validate_json(
            protocol_path.read_text(encoding="utf-8")
        )
        configured_model = existing_protocol.generator_model
    protocol = _load_or_create_protocol(
        protocol_path,
        campaign_id=arguments.output_dir.name,
        suite=suite,
        ranges=ranges,
        shard_size=arguments.shard_size,
        generator_model=configured_model,
        source_git_commit=source_git_commit,
    )

    batches: list[CoverageQuestionBatch] = []
    for offset, limit in ranges:
        batches.append(
            _load_or_generate_batch(
                path=_question_path(arguments.output_dir, offset, limit),
                provider=provider,
                suite=suite,
                offset=offset,
                limit=limit,
                workers=arguments.workers,
                expected_model=protocol.generator_model,
            )
        )
    merged_questions_path = arguments.output_dir / "questions" / "campaign.json"
    merged_questions = _load_or_write_merged_questions(merged_questions_path, batches)

    profile_reports_by_shard: dict[str, list[CoverageQuestionRunReport]] = {}
    profile_campaigns: dict[str, CoverageQuestionCampaignReport] = {}
    if phase in {"run", "all"}:
        database_paths = _database_paths(arguments.database_dir)
        verify_coverage_databases(suite, database_paths)
        for profile in profiles:
            reports = [
                _load_or_run_shard(
                    path=_run_path(arguments.output_dir, profile, offset, limit),
                    suite=suite,
                    batch=batch,
                    profile=profile,
                    database_paths=database_paths,
                )
                for (offset, limit), batch in zip(ranges, batches, strict=True)
            ]
            profile_reports_by_shard[profile] = reports
            profile_campaigns[profile] = _load_or_write_profile_campaign(
                arguments.output_dir / "runs" / profile / "campaign.json",
                suite=suite,
                batches=batches,
                reports=reports,
            )
    elif phase == "compare":
        for profile in profiles:
            reports = [
                _load_run_report(
                    _run_path(arguments.output_dir, profile, offset, limit),
                    suite=suite,
                    batch=batch,
                    profile=profile,
                )
                for (offset, limit), batch in zip(ranges, batches, strict=True)
            ]
            profile_reports_by_shard[profile] = reports
            profile_campaigns[profile] = _load_or_write_profile_campaign(
                arguments.output_dir / "runs" / profile / "campaign.json",
                suite=suite,
                batches=batches,
                reports=reports,
            )

    ablation_path: Path | None = None
    ablation: CoverageAblationReport | None = None
    if phase in {"compare", "all"}:
        ablation_path = arguments.output_dir / "ablation.json"
        ablation = _load_or_write_ablation(ablation_path, profile_campaigns)

    manifest = _manifest(
        output_dir=arguments.output_dir,
        protocol_path=protocol_path,
        protocol=protocol,
        suite=suite,
        ranges=ranges,
        batches=batches,
        merged_questions_path=merged_questions_path,
        merged_questions=merged_questions,
        profile_reports_by_shard=profile_reports_by_shard,
        profile_campaigns=profile_campaigns,
        ablation_path=ablation_path,
        ablation=ablation,
        shard_size=arguments.shard_size,
    )
    _atomic_write_text(
        arguments.output_dir / "manifest.json",
        f"{manifest.model_dump_json(indent=2)}\n",
    )
    print(
        json.dumps(
            {
                "manifest": str(arguments.output_dir / "manifest.json"),
                "source_git_commit": manifest.source_git_commit,
                "completed_stages": manifest.completed_stages,
                "sources": manifest.selected_source_count,
                "questions_requested": merged_questions.requested_count,
                "questions_generated": merged_questions.generated_count,
                "questions_accepted": merged_questions.accepted_count,
                "generation_failures": merged_questions.generation_failure_count,
                "profiles": [
                    {
                        "profile": item.profile,
                        "passed": item.passed,
                        "executed": item.executed,
                        "strict_accuracy": item.strict_accuracy,
                        "end_to_end_yield": item.end_to_end_yield,
                        "fallback_count": item.fallback_count,
                    }
                    for item in manifest.profiles
                ],
                "ablation": (
                    None
                    if ablation is None
                    else [
                        {
                            "candidate": item.candidate_label,
                            "delta": item.strict_accuracy_delta,
                            "delta_ci95": item.strict_accuracy_delta_ci95,
                            "rescued": item.rescued,
                            "regressed": item.regressed,
                            "holm_p": item.holm_adjusted_p_value,
                        }
                        for item in ablation.pairwise_deltas
                    ]
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
