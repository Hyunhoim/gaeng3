from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"
BASELINE_ROOT = PROJECT_ROOT / "evaluation" / "baselines"
EVALUATION_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "evaluation"
READINESS_MANIFEST = (
    PROJECT_ROOT / "evaluation" / "protocols" / "pre-hcx-readiness-v1.manifest.json"
)

LINK_PATTERN = re.compile(r"!?\[[^\]]*]\((?:<(?P<angle>[^>]+)>|(?P<plain>[^)\s]+))\)")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_INDEX_TARGETS = {
    "project-baseline.md",
    "data-audit.md",
    "contracts.md",
    "evaluation.md",
    "evaluation-domestic-etp.md",
    "evaluation-domestic-bond.md",
    "evaluation-public-fund.md",
    "evaluation-public-fund-blind-v1.1.md",
    "evaluation-grounded-answers.md",
    "evaluation-pre-hcx-diagnostic.md",
    "development.md",
    "local-llm.md",
    "pre-hcx-readiness.md",
    "capability-matrix.md",
    "aggregate-engine.md",
    "comparison-engine-design.md",
    "document-rag.md",
    "backend-contract.md",
    "human-evaluation.md",
    "milestones/2026-07-29-agent-core-v0.1.md",
    "../evaluation/README.md",
}
REQUIRED_BASELINES = {
    "overseas-etp-queryplan-v1.json",
    "domestic-etp-queryplan-v1.json",
    "domestic-etp-answer-v1.json",
    "domestic-bond-queryplan-v1.json",
    "domestic-bond-answer-v1.json",
    "public-fund-queryplan-v1.json",
    "public-fund-local-development-v1.json",
    "public-fund-local-holdout-first-run-v1.json",
    "public-fund-answer-v1.json",
    "public-fund-compare-e2e-v1.json",
    "public-fund-compare-v1.json",
    "public-fund-compare-parser-v1.json",
    "pre-hcx-route-diagnostic-initial-v1.json",
    "pre-hcx-route-diagnostic-improved-v1.json",
    "pre-hcx-route-diagnostic-initial-v2.json",
    "pre-hcx-route-diagnostic-improved-v2.json",
}
REQUIRED_BASELINE_KEYS = {
    "schema_version",
    "baseline_id",
    "recorded_on",
    "dataset",
    "evaluation_layer",
    "status",
    "provider",
    "model",
    "suite",
    "data",
    "metrics",
    "reports",
    "reproduce",
    "limitations",
}
FORBIDDEN_BASELINE_KEYS = {
    "answer",
    "answers",
    "products",
    "product_ids",
    "raw_values",
    "results",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _markdown_files() -> list[Path]:
    files = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "packages" / "finance_agent_core" / "README.md",
        PROJECT_ROOT / "evaluation" / "README.md",
    ]
    files.extend(DOCS_ROOT.rglob("*.md"))
    return sorted(set(files))


def _is_allowed_local_only_link(source: Path, target: str) -> bool:
    return _is_within(source, DOCS_ROOT / "research") and "audit-bundle/" in target


def _check_markdown_links() -> list[str]:
    errors: list[str] = []
    for source in _markdown_files():
        if not source.is_file():
            errors.append(f"missing Markdown file: {source.relative_to(PROJECT_ROOT)}")
            continue
        text = source.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            raw_target = match.group("angle") or match.group("plain")
            target = unquote(raw_target).split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("sandbox:"):
                if not _is_within(source, DOCS_ROOT / "research"):
                    errors.append(
                        f"{source.relative_to(PROJECT_ROOT)} uses sandbox link outside research"
                    )
                continue
            if _is_allowed_local_only_link(source, target):
                continue
            resolved = (source.parent / target).resolve()
            if not _is_within(resolved, PROJECT_ROOT):
                continue
            if not resolved.exists():
                errors.append(
                    f"{source.relative_to(PROJECT_ROOT)} -> missing internal link {raw_target}"
                )
    return errors


def _check_document_index() -> list[str]:
    errors: list[str] = []
    index = (DOCS_ROOT / "project-index.md").read_text(encoding="utf-8")
    for target in sorted(REQUIRED_INDEX_TARGETS):
        if f"]({target})" not in index:
            errors.append(f"project-index.md does not link to {target}")
    root_readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    if "](docs/project-index.md)" not in root_readme:
        errors.append("root README.md does not link to docs/project-index.md")
    return errors


def _all_mapping_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_all_mapping_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_all_mapping_keys(child))
    return keys


def _require_sha256(
    errors: list[str],
    baseline_name: str,
    field_name: str,
    value: object,
) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        errors.append(f"{baseline_name}: {field_name} is not a lowercase SHA-256")


def _check_baseline(path: Path) -> list[str]:
    errors: list[str] = []
    name = path.name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{name}: cannot load JSON: {error}"]
    if not isinstance(payload, dict):
        return [f"{name}: top-level value must be an object"]

    missing = REQUIRED_BASELINE_KEYS - set(payload)
    if missing:
        errors.append(f"{name}: missing keys {sorted(missing)}")
        return errors
    if payload["schema_version"] != "1.0":
        errors.append(f"{name}: unsupported schema_version")
    if payload["baseline_id"] != path.stem:
        errors.append(f"{name}: baseline_id must match the filename")
    if payload["evaluation_layer"] not in {
        "query_plan",
        "grounded_answer",
        "intent_route",
    }:
        errors.append(f"{name}: invalid evaluation_layer")
    if payload["provider"].get("official_submission_provider") is not False:
        errors.append(f"{name}: local baseline must not claim official provider status")

    serialized = json.dumps(payload, ensure_ascii=False)
    if "/home/" in serialized or "\\\\Users\\\\" in serialized:
        errors.append(f"{name}: contains a machine-specific absolute path")
    forbidden = _all_mapping_keys(payload) & FORBIDDEN_BASELINE_KEYS
    if forbidden:
        errors.append(f"{name}: contains result-level keys {sorted(forbidden)}")

    suite = payload["suite"]
    suite_path = PROJECT_ROOT / suite.get("path", "")
    _require_sha256(errors, name, "suite.sha256", suite.get("sha256"))
    if not suite_path.is_file():
        errors.append(f"{name}: suite path does not exist")
    elif _sha256(suite_path) != suite.get("sha256"):
        errors.append(f"{name}: suite SHA-256 differs from the tracked suite")

    data = payload["data"]
    if payload["evaluation_layer"] == "intent_route":
        _require_sha256(
            errors,
            name,
            "data.capability_matrix_sha256",
            data.get("capability_matrix_sha256"),
        )
        if not data.get("not_applicable_reason"):
            errors.append(f"{name}: route baseline requires data not-applicable reason")
    else:
        for field_name in ("database_sha256", "manifest_sha256", "source_file_sha256"):
            _require_sha256(errors, name, f"data.{field_name}", data.get(field_name))

    reports = payload["reports"]
    if not isinstance(reports, list) or not reports:
        errors.append(f"{name}: reports must be a non-empty list")
    else:
        for index, report in enumerate(reports):
            _require_sha256(
                errors,
                name,
                f"reports[{index}].sha256",
                report.get("sha256"),
            )
            artifact_name = report.get("artifact_name")
            if (
                not isinstance(artifact_name, str)
                or Path(artifact_name).name != artifact_name
            ):
                errors.append(f"{name}: report artifact_name must be a filename")
                continue
            artifact_path = EVALUATION_ARTIFACT_ROOT / artifact_name
            if artifact_path.is_file() and _sha256(artifact_path) != report.get(
                "sha256"
            ):
                errors.append(
                    f"{name}: reports[{index}].sha256 differs from the local artifact"
                )

    metrics = payload["metrics"]
    total = metrics.get("total")
    passed = metrics.get("passed")
    strict_accuracy = metrics.get("strict_accuracy")
    if payload["status"] in {
        "holdout_first_run_observed",
        "diagnostic_initial_observed",
    }:
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or not isinstance(passed, int)
            or isinstance(passed, bool)
            or not 0 <= passed <= total
        ):
            errors.append(f"{name}: observed holdout counts are invalid")
        elif strict_accuracy not in {passed / total, round(passed / total, 6)}:
            errors.append(
                f"{name}: strict_accuracy differs from observed holdout counts"
            )
    else:
        if total != passed:
            errors.append(f"{name}: frozen baseline is not perfect")
        if strict_accuracy != 1.0:
            errors.append(
                f"{name}: strict_accuracy must match the frozen perfect baseline"
            )
    return errors


def _check_baselines() -> list[str]:
    errors: list[str] = []
    actual = {path.name for path in BASELINE_ROOT.glob("*.json")}
    missing = REQUIRED_BASELINES - actual
    unexpected = actual - REQUIRED_BASELINES
    if missing:
        errors.append(f"missing baseline files: {sorted(missing)}")
    if unexpected:
        errors.append(f"unexpected baseline files: {sorted(unexpected)}")
    for path in sorted(BASELINE_ROOT.glob("*.json")):
        errors.extend(_check_baseline(path))
    return errors


def _readiness_files() -> list[Path]:
    package_root = PROJECT_ROOT / "packages" / "finance_agent_core"
    files = {
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "environment.yml",
        PROJECT_ROOT / "environment.local-llm.yml",
        PROJECT_ROOT / "evaluation" / "README.md",
        PROJECT_ROOT / "scripts" / "check-docs.py",
        package_root / "README.md",
        package_root / "pyproject.toml",
    }
    files.update(DOCS_ROOT.rglob("*.md"))
    files.update(
        path
        for path in (package_root / "src").rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"}
    )
    files.update((package_root / "tests").rglob("*.py"))
    files.update((PROJECT_ROOT / "requirements").rglob("*.txt"))
    files.update(BASELINE_ROOT.glob("*.json"))
    files.update(
        path
        for path in (PROJECT_ROOT / "evaluation" / "protocols").glob("*.json")
        if path != READINESS_MANIFEST
    )
    return sorted(path for path in files if path.is_file())


def _readiness_tree_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _check_readiness_manifest() -> list[str]:
    if not READINESS_MANIFEST.is_file():
        return ["missing pre-HCX readiness source manifest"]
    try:
        payload = json.loads(READINESS_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot load pre-HCX readiness source manifest: {error}"]
    errors: list[str] = []
    paths = _readiness_files()
    expected_sha256 = _readiness_tree_sha256(paths)
    if payload.get("schema_version") != "1.0":
        errors.append("pre-HCX source manifest has unsupported schema_version")
    if payload.get("baseline_id") != "pre-hcx-readiness-v1":
        errors.append("pre-HCX source manifest baseline_id differs")
    if payload.get("status") != "internal_ready_external_gates_pending":
        errors.append("pre-HCX source manifest status overclaims or differs")
    if payload.get("file_count") != len(paths):
        errors.append(
            "pre-HCX source manifest file_count differs: "
            f"expected {len(paths)}, got {payload.get('file_count')}"
        )
    if payload.get("tree_sha256") != expected_sha256:
        errors.append(
            "pre-HCX source tree SHA-256 differs: "
            f"expected {expected_sha256}, got {payload.get('tree_sha256')}"
        )
    if not payload.get("external_gates"):
        errors.append("pre-HCX source manifest must preserve external gates")
    qa = payload.get("qa", {})
    if qa.get("pytest_passed") != 239:
        errors.append("pre-HCX source manifest pytest count differs from frozen QA")
    if qa.get("documentation_baselines") != len(REQUIRED_BASELINES):
        errors.append("pre-HCX source manifest baseline count differs")
    return errors


def main() -> int:
    errors = [
        *_check_markdown_links(),
        *_check_document_index(),
        *_check_baselines(),
        *_check_readiness_manifest(),
    ]
    if errors:
        print("Documentation checks failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Documentation checks passed: "
        f"{len(_markdown_files())} Markdown files, "
        f"{len(REQUIRED_BASELINES)} evaluation baselines."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
