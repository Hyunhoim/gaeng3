from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from finance_agent_core.retrieval.official_connector import (
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.pending_corpus import collect_pending_corpus


class PdftotextExtractor:
    def extract(self, content: bytes) -> str:
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", "-", "-"],
                input=content,
                capture_output=True,
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("pdftotext failed for an official snapshot") from error
        return completed.stdout.decode("utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect official sources into a non-authoritative review queue"
    )
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    manifest = collect_pending_corpus(
        arguments.output_root,
        connector=OfficialSourceConnector(UrllibOfficialHttpTransport()),
        pdf_text_extractor=PdftotextExtractor(),
    )
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
