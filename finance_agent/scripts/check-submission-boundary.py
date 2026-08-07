from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from finance_agent_core.config.submission_boundary import (
    BoundaryProfile,
    scan_submission_boundary,
)


def _tracked_paths(repository_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [path.decode("utf-8") for path in result.stdout.split(b"\0") if path]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit local-LLM and unsafe-artifact boundaries in tracked submission files."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--profile",
        type=BoundaryProfile,
        choices=list(BoundaryProfile),
        required=True,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--expect-blocked",
        action="store_true",
        help="Return success only when the selected profile is blocked.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    repository_root = arguments.repository_root.resolve()
    report = scan_submission_boundary(
        repository_root,
        _tracked_paths(repository_root),
        profile=arguments.profile,
    )
    rendered = f"{report.model_dump_json(indent=2)}\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "profile": report.profile.value,
                "tracked_file_count": report.tracked_file_count,
                "finding_count": report.finding_count,
                "blocker_count": report.blocker_count,
                "passed": report.passed,
                "output": str(arguments.output) if arguments.output else None,
            },
            ensure_ascii=False,
        )
    )
    if arguments.expect_blocked:
        return 0 if not report.passed else 1
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
