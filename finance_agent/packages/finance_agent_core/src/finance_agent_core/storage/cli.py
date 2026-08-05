from __future__ import annotations

import argparse
from pathlib import Path

from finance_agent_core.storage import (
    build_bond_database,
    build_domestic_etp_database,
    build_overseas_etp_database,
    build_public_fund_database,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a normalized financial-product SQLite database."
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument(
        "--dataset",
        choices=("overseas_etp", "domestic_etp", "bond", "fund"),
        default="overseas_etp",
    )
    parser.add_argument(
        "--output",
        type=Path,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    output = arguments.output or Path(f"artifacts/normalized/{arguments.dataset}.sqlite3")
    builders = {
        "overseas_etp": build_overseas_etp_database,
        "domestic_etp": build_domestic_etp_database,
        "bond": build_bond_database,
        "fund": build_public_fund_database,
    }
    manifest = builders[arguments.dataset](arguments.data_dir, output)
    print(manifest.model_dump_json(indent=2))
    return 0
