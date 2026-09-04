from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from pypdf import PdfReader

from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.pending_corpus import PendingCorpusCatalog, collect_pending_corpus


class PypdfTextExtractor:
    def extract(self, content: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(content), strict=False)
            if len(reader.pages) > 250:
                raise RuntimeError("official PDF exceeds the page budget")
            parts: list[str] = []
            extracted_chars = 0
            for page in reader.pages:
                value = page.extract_text() or ""
                extracted_chars += len(value)
                if extracted_chars > 2_000_000:
                    raise RuntimeError("official PDF text exceeds the budget")
                parts.append(value)
            return "\n".join(parts)
        except RuntimeError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed on malformed PDFs
            raise RuntimeError("official PDF extraction failed") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect official sources into a non-authoritative review queue"
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Optional generated pending catalog; never grants approval authority.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print hashes and counts instead of the complete pending manifest.",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    catalog = None
    if arguments.catalog is not None:
        raw_catalog = arguments.catalog.read_bytes()
        if len(raw_catalog) > 4 * 1024 * 1024:
            raise RuntimeError("pending catalog exceeds the input budget")
        catalog = PendingCorpusCatalog.model_validate_json(raw_catalog)
    manifest = collect_pending_corpus(
        arguments.output_root,
        connector=OfficialSourceConnector(
            UrllibOfficialHttpTransport(),
            load_pending_intake_source_registry(),
        ),
        pdf_text_extractor=PypdfTextExtractor(),
        catalog=catalog,
    )
    if arguments.summary_only:
        print(
            json.dumps(
                {
                    "status": manifest.status,
                    "catalog_id": manifest.catalog_id,
                    "document_count": manifest.document_count,
                    "failure_count": len(manifest.failures),
                    "requested_document_count": manifest.requested_document_count,
                    "raw_file_manifest_sha256": manifest.raw_file_manifest_sha256,
                    "text_file_manifest_sha256": manifest.text_file_manifest_sha256,
                    "manifest_sha256": manifest.canonical_sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
