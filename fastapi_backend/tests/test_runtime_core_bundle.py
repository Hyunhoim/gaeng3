from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from finance_agent_core.release import (
    _ANSWER_PROMPT_FILES,
    _GENERATION_CONTRACT_FILES,
    _INTENT_AND_RESOLUTION_PROMPT_FILES,
)


def test_runtime_bundle_ignores_stale_build_tree(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    package = source / "src" / "finance_agent_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")

    stale_module = source / "build" / "lib" / "finance_agent_core" / "agent" / "cli.py"
    stale_module.parent.mkdir(parents=True)
    stale_module.write_text("RAISE_IF_PACKAGED = True\n", encoding="utf-8")

    output = tmp_path / "runtime-core"
    subprocess.run(
        [
            sys.executable,
            str(backend_root / "scripts" / "build_runtime_core.py"),
            "--source",
            str(source),
            "--output",
            str(output),
            "--manifest",
            str(backend_root / "runtime-core-manifest.json"),
            "--pyproject",
            str(backend_root / "runtime-core-pyproject.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (output / "src" / "finance_agent_core" / "runtime.py").is_file()
    assert not (output / "build").exists()
    assert not (output / "src" / "finance_agent_core" / "agent" / "cli.py").exists()


def test_runtime_bundle_contains_every_release_prompt_contract(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    backend_root = repository_root / "fastapi_backend"
    source = repository_root / "finance_agent" / "packages" / "finance_agent_core"
    output = tmp_path / "runtime-core"
    subprocess.run(
        [
            sys.executable,
            str(backend_root / "scripts" / "build_runtime_core.py"),
            "--source",
            str(source),
            "--output",
            str(output),
            "--manifest",
            str(backend_root / "runtime-core-manifest.json"),
            "--pyproject",
            str(backend_root / "runtime-core-pyproject.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    runtime_core = output / "src" / "finance_agent_core"
    required_contracts = {
        *_INTENT_AND_RESOLUTION_PROMPT_FILES,
        *_ANSWER_PROMPT_FILES,
        *_GENERATION_CONTRACT_FILES,
    }
    assert all((runtime_core / path).is_file() for path in required_contracts)
    retired = {
        "agent/grounded_planning.py",
        "agent/providers/local_test.py",
        "answering/context.py",
        "answering/providers.py",
        "answering/verifier.py",
    }
    assert all(not (runtime_core / path).exists() for path in retired)
