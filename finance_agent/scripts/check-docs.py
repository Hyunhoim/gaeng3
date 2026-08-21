from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
DOCS_ROOT = PROJECT_ROOT / "docs"
PROPOSAL_ROOT = REPOSITORY_ROOT / "docs" / "proposal"
BASELINE_ROOT = PROJECT_ROOT / "evaluation" / "baselines"
EVALUATION_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "evaluation"
AREA_READMES = (
    PROJECT_ROOT / "docs" / "README.md",
    PROJECT_ROOT / "scripts" / "README.md",
    PROJECT_ROOT / "packages" / "README.md",
    PROJECT_ROOT / "requirements" / "README.md",
    PROJECT_ROOT / "notebooks" / "README.md",
    PROJECT_ROOT / "reports" / "README.md",
)
READINESS_MANIFEST = (
    PROJECT_ROOT / "evaluation" / "protocols" / "pre-hcx-readiness-v1.manifest.json"
)
PRODUCT_COMPARE_COMMITMENT = (
    PROJECT_ROOT
    / "evaluation"
    / "protocols"
    / "product-compare-core-30.commitment.json"
)
PRODUCT_COMPARE_SUITE = (
    PROJECT_ROOT
    / "packages"
    / "finance_agent_core"
    / "src"
    / "finance_agent_core"
    / "evaluation"
    / "suites"
    / "product_compare_core_30.json"
)

LINK_PATTERN = re.compile(r"!?\[[^\]]*]\((?:<(?P<angle>[^>]+)>|(?P<plain>[^)\s]+))\)")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FROZEN_PYTEST_PASSED = 507
FROZEN_READINESS_MARKDOWN_FILES = 59
FROZEN_READINESS_BASELINES = 41

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
    "evaluation-product-comparison.md",
    "evaluation-search-aggregate-performance.md",
    "cross-family-search.md",
    "evaluation-internal-red-team.md",
    "evaluation-official-mock.md",
    "evaluation-qwen-metamorphic.md",
    "evaluation-coverage-guided.md",
    "evaluation-domain-qa.md",
    "evaluation-schema-embedding-cpu.md",
    "schema-dense-stage4-stage5-readiness-2026-08-13.md",
    "stage4-audit-blind-image-api-report-2026-08-13.md",
    "p0-4-official-acceptance-handover-2026-08-19.md",
    "p0-8-retry-contract-handover-2026-08-19.md",
    "p0-5-external-corpus-intake-2026-08-19.md",
    "p0-6-provided-relation-retrieval-handover-2026-08-19.md",
    "p0-7-knowledge-claim-verifier-handover-2026-08-20.md",
    "p0-10-public-relation-release-integration-handover-2026-08-20.md",
    "final-hcx-answer-release-freeze-2026-08-21.md",
    "submission-model-boundary.md",
    "hyperclova-provider.md",
    "evaluation-pre-hcx-diagnostic.md",
    "development.md",
    "local-llm.md",
    "pre-hcx-readiness.md",
    "capability-matrix.md",
    "aggregate-engine.md",
    "comparison-engine-design.md",
    "document-rag.md",
    "backend-contract.md",
    "ontology.md",
    "human-evaluation.md",
    "milestones/2026-07-29-agent-core-v0.1.md",
    "../evaluation/README.md",
}
REQUIRED_BASELINES = {
    "official-acceptance-p0-4-v1.json",
    "retry-contract-p0-8-v1.json",
    "external-corpus-intake-contract-v1.json",
    "p0-6-provided-relation-retrieval-v1.json",
    "p0-7-knowledge-claim-contract-v1.json",
    "p0-10-public-relation-release-integration-v1.json",
    "briefing-examples-v1-bond-improved.json",
    "briefing-examples-v1-initial.json",
    "briefing-examples-v1-safety-improved.json",
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
    "product-compare-v1.json",
    "search-aggregate-performance-v1.json",
    "cross-family-search-v1.json",
    "cross-family-answer-v1.json",
    "hcx-contract-e2e-v1.json",
    "answer-adapter-contract-v1.json",
    "internal-red-team-v1.json",
    "official-mock-v1-30.json",
    "qwen-eval-lab-v1.json",
    "semantic-roundtrip-v1.json",
    "coverage-guided-v1.json",
    "official-mock-http-v1-30.json",
    "official-mock-http-fund-approved-v1-30.json",
    "official-mock-http-concurrency-v1.json",
    "official-mock-http-qwen-approved-c2-v1.json",
    "docker-http-smoke-v2.json",
    "docker-http-smoke-qwen-v2.json",
    "pre-hcx-route-diagnostic-initial-v1.json",
    "pre-hcx-route-diagnostic-improved-v1.json",
    "pre-hcx-route-diagnostic-initial-v2.json",
    "pre-hcx-route-diagnostic-improved-v2.json",
    "pre-hcx-route-diagnostic-initial-v3.json",
    "pre-hcx-route-diagnostic-improved-v3.json",
    "domain-qa-e2e-v1.json",
    "domain-qa-e2e-v1.1-gold.json",
    "domain-qa-e2e-v1.2-router.json",
    "stage2-approved-db-revalidation-2026-08-12.json",
    "stage3-release-contract-2026-08-12.json",
    "stage3-local-oci-rollback-2026-08-12.json",
    "schema-embedding-cpu-public-v1.json",
    "schema-embedding-docker-runtime-2026-08-13.json",
    "deterministic-api-stage4-final-2026-08-13.json",
    "final-hcx-answer-release-freeze-v1.json",
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
# Frozen evidence files are immutable.  These exact successor hashes pin components
# intentionally superseded by later Stage 4 and P0-10 release contracts; keeping the
# migration here avoids rewriting historical evidence whose own SHA-256 is cited.
HISTORICAL_BASELINE_SUITE_SUCCESSOR_SHA256 = {
    "docker-http-smoke-v2.json": (
        "4060178049bf575e02b0d63853df4f7dfe1a456520a36498285474ab310773ee"
    ),
    "docker-http-smoke-qwen-v2.json": (
        "4060178049bf575e02b0d63853df4f7dfe1a456520a36498285474ab310773ee"
    ),
}
HISTORICAL_BASELINE_COMPONENT_ALLOWLIST = {
    "stage3-local-oci-rollback-2026-08-12.json": {
        "compose-release.sh": (
            "2b2435fd261b476630cb423a8dd3e0ef9c2427e7c9a55acf3970e7487112eec0"
        ),
        "fastapi_backend/Dockerfile": (
            "84c52ee0aac7e565d48242fef2a13c3bcde0b997b1d54ebd8e7f13998c07684d"
        ),
        "fastapi_backend/docker-compose.release.yml": (
            "3c308f367b1fa6ff84d9c4f847831f40685d797b79b0b8bee0a4fb0a73012949"
        ),
        "fastapi_backend/scripts/rollback_drill.py": (
            "91c23b2e93e6a342cfae03dc4e07b06b901e432017996dc64dae7e5407f5e4e5"
        ),
        "fastapi_backend/scripts/release_ci.py": (
            "1086929554fa26ed9883e52c51608d9af22e2595bdb548aaef8c361b3bf57bd6"
        ),
        "fastapi_backend/scripts/release_trust.py": (
            "0326cf1462a61d82abe539b7d84555a77e21f73f4a8444cad5e8d152f10552b1"
        ),
    },
    "stage3-release-contract-2026-08-12.json": {
        "fastapi_backend/tests/test_release_activation.py": (
            "7aa29731e8820665ec15fa51c51ed770584d485b386465b066b2ab602d9d97df"
        ),
        "fastapi_backend/tests/test_release_deployment_contract.py": (
            "1f0630497123985300f5f2e81dfe4802bdb0fadd262e390f73c1eea5e6df0a6f"
        ),
        "fastapi_backend/tests/test_release_rollback_drill.py": (
            "488fec38c6120726a0a4b29061506c4d6307d2810db781d45cfb3c2279681a76"
        ),
        "fastapi_backend/tests/test_release_startup.py": (
            "89c1282710d698a634878571846c230081bc0d26cefc6cfa6db9d15e7468196c"
        ),
        "finance_agent/packages/finance_agent_core/tests/test_agent_release.py": (
            "1858ae74ea07cc11002495dd07747669cfbdaf4261a8142fd863c07e0dbffdb8"
        ),
        "finance_agent/packages/finance_agent_core/tests/test_plan_authority.py": (
            "f33e0d47813d73e91a7e6778490d34ca2efd92e6cbbf97b58c0614c332ccd54f"
        ),
        "finance_agent/packages/finance_agent_core/tests/test_release_cli.py": (
            "0116d25054ac2a4baf2fa2dcf7101b236517914457d70a164e3e19c0127f2c52"
        ),
        "fastapi_backend/tests/test_release_ci_contract.py": (
            "5f4303ef41286577040dad15c2e5581f940cb08310d35fa1809d177f344b559e"
        ),
        "fastapi_backend/tests/test_release_trust.py": (
            "b9126c6d2d0e3e8f2418470a1d0d12cd91c9fe05e1046fd4746a2756f65a4a9a"
        ),
    },
    "p0-10-public-relation-release-integration-v1.json": {
        "finance_agent/packages/finance_agent_core/tests/test_knowledge_router.py": (
            "6fdefadd174165a58018972654818617add618e0b1c811a12441610382965a5b"
        ),
        "finance_agent/packages/finance_agent_core/tests/test_knowledge_agent.py": (
            "e031e5c5bbd476116345d0223120c9e88f99663d34807806e00ec4ac8505903c"
        ),
        "finance_agent/packages/finance_agent_core/tests/test_agent_release.py": (
            "1858ae74ea07cc11002495dd07747669cfbdaf4261a8142fd863c07e0dbffdb8"
        ),
        "fastapi_backend/tests/test_relation_runtime_wiring.py": (
            "4fac71fa83941887a58f52a08de29ca05cf561647a6917d0d365e98c1c3abc8e"
        ),
        "fastapi_backend/tests/test_smoke.py": (
            "c37c4a2d263a8077598baa4af7bfbeacb8d5abf229493aafb7da56c03b851b66"
        ),
        "fastapi_backend/tests/test_dependencies.py": (
            "49a49dbc45518d590582629cc20b7bf08b9a1a717a7464165417f69def123437"
        ),
        "fastapi_backend/tests/test_answer.py": (
            "602163de21dfe45259fecbb83a3ed451fc6e624d9f5553a432a3a82c5e15481a"
        ),
        "fastapi_backend/tests/test_release_ci_contract.py": (
            "5f4303ef41286577040dad15c2e5581f940cb08310d35fa1809d177f344b559e"
        ),
        "fastapi_backend/tests/test_release_deployment_contract.py": (
            "1f0630497123985300f5f2e81dfe4802bdb0fadd262e390f73c1eea5e6df0a6f"
        ),
        "fastapi_backend/tests/test_release_rollback_drill.py": (
            "488fec38c6120726a0a4b29061506c4d6307d2810db781d45cfb3c2279681a76"
        ),
    },
}
FORBIDDEN_BASELINE_KEYS = {
    "answer",
    "answers",
    "products",
    "product_ids",
    "raw_values",
    "results",
}
REQUIRED_PROPOSAL_TARGETS = {
    "briefing-2026-08-06.md",
    "technical-proposal.md",
    "evidence-map.md",
    "research-basis.md",
    "user-scenarios.md",
    "submission-checklist.md",
    "diagrams/system-architecture.md",
    "diagrams/answer-flow.md",
}
REQUIRED_PROPOSAL_SECTIONS = {
    "## 1. 제안 요약",
    "## 2. 문제 정의",
    "## 3. 제안 방법",
    "## 4. 시스템 구성도",
    "## 5. 주요 기능 흐름도",
    "## 6. 사용자 시나리오",
    "## 7. 기대효과·확장성",
}
REQUIRED_PROPOSAL_EVALUATION_AXES = {
    "문제정의",
    "기술완성도·성능",
    "창의성·확장성",
    "답변 정확성·완결성",
    "현업 활용성·리스크 관리",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_local_research_artifact(path: Path) -> bool:
    return _is_within(path, DOCS_ROOT / "research") and "audit-bundle" in path.parts


def _markdown_files() -> list[Path]:
    files = [
        REPOSITORY_ROOT / "README.md",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "packages" / "finance_agent_core" / "README.md",
        PROJECT_ROOT / "evaluation" / "README.md",
        *AREA_READMES,
    ]
    files.extend(
        path
        for path in DOCS_ROOT.rglob("*.md")
        if not _is_local_research_artifact(path)
    )
    files.extend(PROPOSAL_ROOT.rglob("*.md"))
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
            if not _is_within(resolved, REPOSITORY_ROOT):
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
    finance_readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    if "](docs/project-index.md)" not in finance_readme:
        errors.append("finance_agent/README.md does not link to docs/project-index.md")
    repository_readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    if "](docs/proposal/README.md)" not in repository_readme:
        errors.append("repository README.md does not link to docs/proposal/README.md")
    if f"pytest {FROZEN_PYTEST_PASSED}개" not in repository_readme:
        errors.append("repository README.md pytest count differs from frozen QA")
    if "](../../docs/proposal/README.md)" not in index:
        errors.append("project-index.md does not link to the proposal documentation")
    proposal_index_path = PROPOSAL_ROOT / "README.md"
    if not proposal_index_path.is_file():
        errors.append("missing docs/proposal/README.md")
        return errors
    proposal_index = proposal_index_path.read_text(encoding="utf-8")
    for target in sorted(REQUIRED_PROPOSAL_TARGETS):
        if f"]({target})" not in proposal_index:
            errors.append(f"docs/proposal/README.md does not link to {target}")
    return errors


def _check_proposal_content() -> list[str]:
    errors: list[str] = []
    technical_proposal_path = PROPOSAL_ROOT / "technical-proposal.md"
    proposal_index_path = PROPOSAL_ROOT / "README.md"
    if not technical_proposal_path.is_file() or not proposal_index_path.is_file():
        return ["proposal content files are missing"]
    technical_proposal = technical_proposal_path.read_text(encoding="utf-8")
    for section in sorted(REQUIRED_PROPOSAL_SECTIONS):
        if section not in technical_proposal:
            errors.append(f"technical-proposal.md is missing section: {section}")
    proposal_index = proposal_index_path.read_text(encoding="utf-8")
    for axis in sorted(REQUIRED_PROPOSAL_EVALUATION_AXES):
        if axis not in proposal_index:
            errors.append(f"docs/proposal/README.md is missing evaluation axis: {axis}")
    readiness = (DOCS_ROOT / "pre-hcx-readiness.md").read_text(encoding="utf-8")
    # pre-hcx-readiness.md is historical evidence for the frozen pre-HCX gate.
    # New release documents and baselines must not rewrite what that run counted.
    expected_documentation_count = (
        f"`{FROZEN_READINESS_MARKDOWN_FILES} Markdown files`, "
        f"`{FROZEN_READINESS_BASELINES} evaluation baselines`"
    )
    if expected_documentation_count not in readiness:
        errors.append("pre-hcx-readiness.md documentation counts differ")
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
        "system_performance",
        "system_contract",
        "system_regression",
        "schema_retrieval",
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
    historical_fields = {
        "historical_component_hashes",
        "historical_component_paths",
        "historical_component_reason",
    } & set(suite)
    if historical_fields:
        errors.append(
            f"{name}: frozen baseline embeds historical bypass fields "
            f"{sorted(historical_fields)}"
        )
    historical_component_hashes = HISTORICAL_BASELINE_COMPONENT_ALLOWLIST.get(name, {})
    historical_component_paths = set(historical_component_hashes)
    observed_historical_component_paths: set[str] = set()
    tracked_contract_sha256 = suite.get("tracked_contract_sha256", suite.get("sha256"))
    if "tracked_contract_sha256" in suite:
        _require_sha256(
            errors,
            name,
            "suite.tracked_contract_sha256",
            tracked_contract_sha256,
        )
        if tracked_contract_sha256 != suite.get("sha256") and not suite.get(
            "historical_contract_reason"
        ):
            errors.append(
                f"{name}: historical suite requires historical_contract_reason"
            )
    if not suite_path.is_file():
        errors.append(f"{name}: suite path does not exist")
    else:
        actual_suite_sha256 = _sha256(suite_path)
        approved_successor_sha256 = HISTORICAL_BASELINE_SUITE_SUCCESSOR_SHA256.get(name)
        if (
            actual_suite_sha256 != tracked_contract_sha256
            and actual_suite_sha256 != approved_successor_sha256
        ):
            errors.append(f"{name}: tracked suite SHA-256 differs")
        try:
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            suite_payload = None
        if isinstance(suite_payload, dict):
            for hash_map_name in (
                "input_file_sha256",
                "test_file_sha256",
                "implementation_sha256",
            ):
                component_hashes = suite_payload.get(hash_map_name)
                if component_hashes is None:
                    continue
                if not isinstance(component_hashes, dict) or not component_hashes:
                    errors.append(
                        f"{name}: suite {hash_map_name} must be a non-empty object"
                    )
                    continue
                for component_name, expected_hash in component_hashes.items():
                    _require_sha256(
                        errors,
                        name,
                        f"suite.{hash_map_name}.{component_name}",
                        expected_hash,
                    )
                    if not isinstance(component_name, str):
                        continue
                    component_path = (REPOSITORY_ROOT / component_name).resolve()
                    if (
                        not _is_within(component_path, REPOSITORY_ROOT)
                        or not component_path.is_file()
                    ):
                        errors.append(
                            f"{name}: suite component does not exist: {component_name}"
                        )
                    else:
                        actual_hash = _sha256(component_path)
                        if expected_hash != actual_hash:
                            observed_historical_component_paths.add(component_name)
                            successor_hash = historical_component_hashes.get(
                                component_name
                            )
                            if (
                                successor_hash is not None
                                and actual_hash != successor_hash
                            ):
                                errors.append(
                                    f"{name}: approved successor SHA-256 differs: "
                                    f"{component_name}"
                                )

    if historical_component_paths != observed_historical_component_paths:
        unexpected = sorted(
            observed_historical_component_paths - historical_component_paths
        )
        stale = sorted(historical_component_paths - observed_historical_component_paths)
        if unexpected:
            errors.append(
                f"{name}: unapproved suite component SHA-256 differences: {unexpected}"
            )
        if stale:
            errors.append(
                f"{name}: historical_component_paths no longer differ: {stale}"
            )

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
    elif payload["evaluation_layer"] == "system_contract":
        _require_sha256(
            errors,
            name,
            "data.fixture_definition_sha256",
            data.get("fixture_definition_sha256"),
        )
        if not data.get("not_applicable_reason"):
            errors.append(
                f"{name}: system contract baseline requires data not-applicable reason"
            )
    elif payload["evaluation_layer"] == "schema_retrieval":
        for field_name in ("field_registry_sha256", "model_registry_sha256"):
            _require_sha256(errors, name, f"data.{field_name}", data.get(field_name))
        if not data.get("not_applicable_reason"):
            errors.append(
                f"{name}: schema retrieval baseline requires data not-applicable reason"
            )
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
    if name == "schema-embedding-docker-runtime-2026-08-13.json":
        pass_rate = metrics.get("prerequisite_gate_pass_rate")
        pass_rate_scope = metrics.get("prerequisite_gate_pass_rate_scope")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or not isinstance(passed, int)
            or isinstance(passed, bool)
            or not 0 <= passed <= total
        ):
            errors.append(f"{name}: runtime prerequisite counts are invalid")
            return errors
        if pass_rate not in {passed / total, round(passed / total, 6)}:
            errors.append(
                f"{name}: prerequisite_gate_pass_rate differs from observed counts"
            )
        if (
            not isinstance(pass_rate_scope, str)
            or "정확도가 아님" not in pass_rate_scope
        ):
            errors.append(
                f"{name}: prerequisite gate scope must reject accuracy claims"
            )
        if strict_accuracy is not None:
            errors.append(
                f"{name}: runtime prerequisite must not claim strict_accuracy"
            )
        return errors
    if name == "deterministic-api-stage4-final-2026-08-13.json":
        pass_rate = metrics.get("contract_semantic_pass_rate")
        pass_rate_scope = metrics.get("contract_semantic_pass_rate_scope")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or not isinstance(passed, int)
            or isinstance(passed, bool)
            or not 0 <= passed <= total
        ):
            errors.append(f"{name}: deterministic API contract counts are invalid")
            return errors
        if total != passed:
            errors.append(f"{name}: deterministic API contract baseline is not perfect")
        if pass_rate not in {passed / total, round(passed / total, 6)}:
            errors.append(
                f"{name}: contract_semantic_pass_rate differs from observed counts"
            )
        if (
            not isinstance(pass_rate_scope, str)
            or "독립 상품 정확도가 아님" not in pass_rate_scope
        ):
            errors.append(
                f"{name}: contract_semantic_pass_rate_scope must reject accuracy claims"
            )
        if strict_accuracy is not None:
            errors.append(
                f"{name}: deterministic API baseline must not claim strict_accuracy"
            )
        return errors
    if payload["status"] in {
        "holdout_first_run_observed",
        "diagnostic_initial_observed",
        "domain_qa_initial_observed",
        "domain_qa_gold_observed",
        "domain_qa_router_improved",
        "briefing_examples_initial_observed",
        "briefing_examples_safety_improved",
        "briefing_examples_bond_improved",
        "official_http_first_observed",
        "coverage_diagnostic_first_observed",
        "public_development_not_blind",
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


def _check_product_comparison_commitment() -> list[str]:
    errors: list[str] = []
    if not PRODUCT_COMPARE_COMMITMENT.is_file():
        return ["missing product comparison commitment"]
    if not PRODUCT_COMPARE_SUITE.is_file():
        return ["missing product comparison suite"]
    try:
        commitment = json.loads(PRODUCT_COMPARE_COMMITMENT.read_text(encoding="utf-8"))
        suite = json.loads(PRODUCT_COMPARE_SUITE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot load product comparison commitment: {error}"]
    if commitment.get("suite_id") != suite.get("suite_id"):
        errors.append("product comparison commitment suite_id differs")
    if commitment.get("suite_version") != suite.get("suite_version"):
        errors.append("product comparison commitment suite_version differs")
    if commitment.get("suite_sha256") != _sha256(PRODUCT_COMPARE_SUITE):
        errors.append("product comparison commitment SHA-256 differs")
    if commitment.get("case_count") != len(suite.get("cases", [])):
        errors.append("product comparison commitment case_count differs")
    return errors


def _readiness_files() -> list[Path]:
    package_root = PROJECT_ROOT / "packages" / "finance_agent_core"
    backend_root = REPOSITORY_ROOT / "fastapi_backend"
    files = {
        REPOSITORY_ROOT / "compose.sh",
        REPOSITORY_ROOT / "rehearse.sh",
        REPOSITORY_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "environment.yml",
        PROJECT_ROOT / "environment.local-llm.yml",
        PROJECT_ROOT / "evaluation" / "README.md",
        PROJECT_ROOT / "scripts" / "check-docs.py",
        PROJECT_ROOT / "scripts" / "run-hcx-contract-e2e.py",
        PROJECT_ROOT / "scripts" / "run-answer-adapter-contract.py",
        PROJECT_ROOT / "scripts" / "sync-ontology.py",
        PROJECT_ROOT / "scripts" / "generate-official-mock-suite.py",
        package_root / "README.md",
        package_root / "pyproject.toml",
        backend_root / ".env.example",
        backend_root / "Dockerfile",
        backend_root / "Dockerfile.dockerignore",
        backend_root / "README.md",
        backend_root / "docker-compose.local-llm.yml",
        backend_root / "pyproject.toml",
        backend_root / "requirements.txt",
        *AREA_READMES,
    }
    files.update(
        path
        for path in DOCS_ROOT.rglob("*.md")
        if not _is_local_research_artifact(path)
    )
    files.update(
        path
        for path in (package_root / "src").rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"}
    )
    files.update((package_root / "tests").rglob("*.py"))
    files.update(
        path
        for path in (PROJECT_ROOT / "scripts").rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh"}
    )
    files.update(
        path
        for path in backend_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh"}
    )
    files.update((REPOSITORY_ROOT / "ontology").glob("*.ttl"))
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
        try:
            relative = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            relative = f"../{path.relative_to(REPOSITORY_ROOT).as_posix()}"
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
    if payload.get("schema_version") != "1.0":
        errors.append("pre-HCX source manifest has unsupported schema_version")
    if payload.get("baseline_id") != "pre-hcx-readiness-v1":
        errors.append("pre-HCX source manifest baseline_id differs")
    if payload.get("status") != "internal_ready_external_gates_pending":
        errors.append("pre-HCX source manifest status overclaims or differs")
    if not isinstance(payload.get("file_count"), int) or payload["file_count"] <= 0:
        errors.append("pre-HCX historical source manifest file_count is invalid")
    _require_sha256(
        errors,
        "pre-hcx-readiness-v1.manifest.json",
        "tree_sha256",
        payload.get("tree_sha256"),
    )
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("git_commit", ""))):
        errors.append("pre-HCX historical source manifest git_commit is invalid")
    if payload.get("generated_from_dirty_worktree") is not False:
        errors.append(
            "pre-HCX historical source manifest must remain clean-commit evidence"
        )
    if not payload.get("external_gates"):
        errors.append("pre-HCX source manifest must preserve external gates")
    qa = payload.get("qa", {})
    if qa.get("pytest_passed") != FROZEN_PYTEST_PASSED:
        errors.append("pre-HCX source manifest pytest count differs from frozen QA")
    if qa.get("documentation_baselines") != FROZEN_READINESS_BASELINES:
        errors.append("pre-HCX historical baseline count differs from its frozen QA")
    return errors


def main() -> int:
    errors = [
        *_check_markdown_links(),
        *_check_document_index(),
        *_check_proposal_content(),
        *_check_baselines(),
        *_check_product_comparison_commitment(),
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
