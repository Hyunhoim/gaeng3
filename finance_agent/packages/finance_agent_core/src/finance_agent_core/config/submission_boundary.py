from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BoundaryProfile(StrEnum):
    DEVELOPMENT = "development"
    SUBMISSION = "submission"


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoundaryFinding(BoundaryModel):
    path: str
    kind: Literal["local_llm_path", "local_llm_marker", "unsafe_artifact"]
    marker: str
    line: int | None = None
    blocking: bool
    reason: str


class SubmissionBoundaryReport(BoundaryModel):
    schema_version: Literal["1.0"] = "1.0"
    profile: BoundaryProfile
    tracked_file_count: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    blocker_count: int = Field(ge=0)
    passed: bool
    findings: list[BoundaryFinding]
    interpretation_limits: list[str]


_LOCAL_PATH_PARTS = (
    "environment.local-llm.yml",
    "requirements/local-llm.txt",
    "scripts/local-llm/",
    "providers/local_test.py",
    "docker-compose.local-llm.yml",
)
_LOCAL_MARKERS = (
    "enable_non_hcx_test_llm",
    "local_test",
    "local_test_llm_",
    "qwen",
    "vllm",
    "local-llm",
    "docker-compose.local-llm",
)
_PRODUCTION_RUNTIME_PATHS = {
    "docker-compose.yml",
    "fastapi_backend/dockerfile",
    "fastapi_backend/requirements.txt",
    "finance_agent/requirements/base.txt",
    "finance_agent/requirements/constraints.txt",
}
_UNSAFE_SUFFIXES = {
    ".bin",
    ".gguf",
    ".key",
    ".onnx",
    ".pem",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
}
_TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".dockerignore",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _normalize_path(path: Path | str) -> str:
    normalized = PurePosixPath(str(path).replace("\\", "/")).as_posix()
    return normalized.removeprefix("./")


def _is_local_path(path: str) -> str | None:
    lowered = path.casefold()
    return next((part for part in _LOCAL_PATH_PARTS if part in lowered), None)


def _unsafe_artifact(path: str) -> str | None:
    lowered = path.casefold()
    if lowered.endswith("/.env") or lowered == ".env":
        return "tracked .env"
    suffix = PurePosixPath(lowered).suffix
    return suffix if suffix in _UNSAFE_SUFFIXES else None


def _read_text(path: Path) -> str | None:
    if path.suffix.casefold() not in _TEXT_SUFFIXES and path.name.casefold() not in {
        "dockerfile",
        "makefile",
    }:
        return None
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def scan_submission_boundary(
    repository_root: Path,
    tracked_paths: Iterable[Path | str],
    *,
    profile: BoundaryProfile,
) -> SubmissionBoundaryReport:
    normalized_paths = sorted({_normalize_path(path) for path in tracked_paths})
    findings: list[BoundaryFinding] = []
    for relative_path in normalized_paths:
        unsafe_marker = _unsafe_artifact(relative_path)
        if unsafe_marker is not None:
            findings.append(
                BoundaryFinding(
                    path=relative_path,
                    kind="unsafe_artifact",
                    marker=unsafe_marker,
                    blocking=True,
                    reason="제출 소스에 비밀·모델·DB 아티팩트를 포함할 수 없음",
                )
            )

        local_path_marker = _is_local_path(relative_path)
        if local_path_marker is not None:
            findings.append(
                BoundaryFinding(
                    path=relative_path,
                    kind="local_llm_path",
                    marker=local_path_marker,
                    blocking=profile is BoundaryProfile.SUBMISSION,
                    reason=(
                        "제출 프로필에서 로컬 LLM 전용 파일은 제거 대상"
                        if profile is BoundaryProfile.SUBMISSION
                        else "개발 프로필에서만 허용된 로컬 LLM 파일"
                    ),
                )
            )

        content = _read_text(repository_root / relative_path)
        if content is None:
            continue
        production_runtime = relative_path.casefold() in _PRODUCTION_RUNTIME_PATHS
        for line_number, line in enumerate(content.splitlines(), start=1):
            lowered = line.casefold()
            matched_markers = [marker for marker in _LOCAL_MARKERS if marker in lowered]
            # More specific markers carry the same evidence as their shorter substring.
            markers = [
                marker
                for marker in matched_markers
                if not any(
                    marker != candidate and marker in candidate for candidate in matched_markers
                )
            ]
            for marker in markers:
                blocking = profile is BoundaryProfile.SUBMISSION or production_runtime
                findings.append(
                    BoundaryFinding(
                        path=relative_path,
                        kind="local_llm_marker",
                        marker=marker,
                        line=line_number,
                        blocking=blocking,
                        reason=(
                            "실행용 배포 파일에 로컬 LLM 표식이 섞임"
                            if production_runtime
                            else "제출 프로필에서 제거할 로컬 LLM 표식"
                        ),
                    )
                )

    blockers = [finding for finding in findings if finding.blocking]
    return SubmissionBoundaryReport(
        profile=profile,
        tracked_file_count=len(normalized_paths),
        finding_count=len(findings),
        blocker_count=len(blockers),
        passed=not blockers,
        findings=findings,
        interpretation_limits=[
            "Git이 추적하는 현재 파일만 검사하며 Git 이력은 재작성하지 않는다.",
            "development 통과는 로컬 LLM 흔적이 없다는 뜻이 아니라 실행용 배포 파일에 "
            "섞이지 않았다는 뜻이다.",
            "submission 통과는 공식 범위 확인 후의 clean checkout·build·runtime 검증과 함께 "
            "사용해야 한다.",
        ],
    )
