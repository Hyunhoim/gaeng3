from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

_RUNTIME_SOURCE_PREFIX = "src/finance_agent_core/"


def _load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("runtime core manifest is invalid")
    return payload


def _string_set(manifest: dict[str, object], key: str) -> frozenset[str]:
    values = manifest.get(key)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"runtime core manifest {key} must be a string list")
    return frozenset(values)


def build_runtime_core(
    *,
    source: Path,
    output: Path,
    manifest_path: Path,
    pyproject_path: Path,
) -> tuple[int, int]:
    source = source.resolve(strict=True)
    manifest = _load_manifest(manifest_path.resolve(strict=True))
    excluded_prefixes = _string_set(manifest, "excluded_prefixes")
    excluded_paths = _string_set(manifest, "excluded_paths")
    excluded_suffixes = _string_set(manifest, "excluded_suffixes")
    allowed_excluded = _string_set(manifest, "allowed_excluded_prefix_paths")
    empty_initializers = _string_set(manifest, "empty_package_initializers")

    if output.exists():
        raise FileExistsError("runtime core output must not exist")
    output.mkdir(parents=True, mode=0o755)
    copied_files = 0
    excluded_files = 0
    for candidate in sorted(source.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        relative = candidate.relative_to(source).as_posix()
        if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
            excluded_files += 1
            continue
        excluded = (
            not relative.startswith(_RUNTIME_SOURCE_PREFIX)
            or relative in excluded_paths
            or any(relative.startswith(prefix) for prefix in excluded_prefixes)
            or any(relative.endswith(suffix) for suffix in excluded_suffixes)
        )
        if excluded and relative not in allowed_excluded:
            excluded_files += 1
            continue
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if relative in empty_initializers:
            destination.write_text(
                '"""Evaluation Runtime package boundary; development exports are excluded."""\n',
                encoding="utf-8",
            )
        else:
            shutil.copyfile(candidate, destination)
        destination.chmod(0o644)
        copied_files += 1

    shutil.copyfile(pyproject_path.resolve(strict=True), output / "pyproject.toml")
    (output / "pyproject.toml").chmod(0o644)
    copied_files += 1
    if copied_files == 0 or excluded_files == 0:
        raise RuntimeError("runtime core boundary did not copy and exclude files")
    return copied_files, excluded_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the allowlisted evaluation Runtime core")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, required=True)
    arguments = parser.parse_args()
    copied, excluded = build_runtime_core(
        source=arguments.source,
        output=arguments.output,
        manifest_path=arguments.manifest,
        pyproject_path=arguments.pyproject,
    )
    print(json.dumps({"copied_files": copied, "excluded_files": excluded}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
