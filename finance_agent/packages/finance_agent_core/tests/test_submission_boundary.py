from pathlib import Path

from finance_agent_core.config.submission_boundary import (
    BoundaryProfile,
    scan_submission_boundary,
)


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_development_allows_isolated_local_llm_but_not_production_leak(tmp_path: Path) -> None:
    paths = [
        "finance_agent/scripts/local-llm/serve.sh",
        "finance_agent/docs/local-llm.md",
        "docker-compose.yml",
    ]
    _write(tmp_path, paths[0], "vllm serve Qwen/model\n")
    _write(tmp_path, paths[1], "local_test development guide\n")
    _write(tmp_path, paths[2], "services: {}\n")

    report = scan_submission_boundary(
        tmp_path,
        paths,
        profile=BoundaryProfile.DEVELOPMENT,
    )

    assert report.passed
    assert report.finding_count > 0
    assert report.blocker_count == 0

    _write(tmp_path, "docker-compose.yml", "LLM_PROVIDER: local_test\n")
    leaked = scan_submission_boundary(
        tmp_path,
        paths,
        profile=BoundaryProfile.DEVELOPMENT,
    )
    assert not leaked.passed
    assert any(finding.path == "docker-compose.yml" for finding in leaked.findings)


def test_submission_blocks_every_local_llm_footprint(tmp_path: Path) -> None:
    paths = [
        "finance_agent/docs/local-llm.md",
        "fastapi_backend/docker-compose.local-llm.yml",
    ]
    _write(tmp_path, paths[0], "Qwen local_test\n")
    _write(tmp_path, paths[1], "ENABLE_NON_HCX_TEST_LLM: 1\n")

    report = scan_submission_boundary(
        tmp_path,
        paths,
        profile=BoundaryProfile.SUBMISSION,
    )

    assert not report.passed
    assert report.blocker_count == report.finding_count
    assert {finding.kind for finding in report.findings} == {
        "local_llm_marker",
        "local_llm_path",
    }


def test_both_profiles_block_tracked_secret_model_and_database_artifacts(tmp_path: Path) -> None:
    paths = ["fastapi_backend/.env", "weights/model.safetensors", "data/fund.sqlite3"]
    for path in paths:
        _write(tmp_path, path, "placeholder")

    for profile in BoundaryProfile:
        report = scan_submission_boundary(tmp_path, paths, profile=profile)
        assert not report.passed
        assert report.blocker_count == 3
        assert all(finding.kind == "unsafe_artifact" for finding in report.findings)


def test_clean_submission_profile_passes(tmp_path: Path) -> None:
    paths = ["docker-compose.yml", "fastapi_backend/requirements.txt"]
    _write(tmp_path, paths[0], "services: {}\n")
    _write(tmp_path, paths[1], "fastapi==0.116.1\n")

    report = scan_submission_boundary(
        tmp_path,
        paths,
        profile=BoundaryProfile.SUBMISSION,
    )

    assert report.passed
    assert report.findings == []
