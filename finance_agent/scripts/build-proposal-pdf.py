#!/usr/bin/env python
"""Render the technical proposal to PDF without adding a dependency.

The organizer recommends submitting the technical proposal as PDF. Pandoc is not
installed and adding a toolchain the team has to reproduce on another machine is
its own risk, so this converts the Markdown subset the proposal actually uses to
styled HTML and hands that to LibreOffice, which is already present and ships
with the Noto Sans CJK KR font needed for Korean.

Usage:
    build-proposal-pdf.py --input docs/proposal/technical-proposal.md \
        --output-dir dist
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UNORDERED = re.compile(r"^([ ]*)[-*]\s+(.*)$")
_ORDERED = re.compile(r"^([ ]*)(\d+)\.\s+(.*)$")
_TABLE_DIVIDER = re.compile(r"^\|[\s:|-]+\|$")


def _inline(text: str) -> str:
    """Escape first, then re-introduce only the inline markup we support."""

    escaped = html.escape(text, quote=False)
    escaped = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = _LINK.sub(lambda m: f"<a>{m.group(1)}</a>", escaped)
    return escaped


def _table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def markdown_to_html_body(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_stack: list[str] = []
    index = 0

    def close_lists(depth: int = 0) -> None:
        while len(list_stack) > depth:
            out.append(f"</{list_stack.pop()}>")

    while index < len(lines):
        line = lines[index]

        if line.startswith("```"):
            close_lists()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue

        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and _TABLE_DIVIDER.match(lines[index + 1].strip())
        ):
            close_lists()
            header = _table_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(_table_row(lines[index]))
                index += 1
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            out.append(
                f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            )
            continue

        if heading := _HEADING.match(line):
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if not line.strip():
            close_lists()
            index += 1
            continue

        if line.startswith("> "):
            close_lists()
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
            index += 1
            continue

        if match := _UNORDERED.match(line):
            depth = len(match.group(1)) // 2 + 1
            while len(list_stack) > depth:
                out.append(f"</{list_stack.pop()}>")
            while len(list_stack) < depth:
                list_stack.append("ul")
                out.append("<ul>")
            text, index = _consume_item(lines, index, match.group(2))
            out.append(f"<li>{_inline(text)}</li>")
            continue

        if match := _ORDERED.match(line):
            depth = len(match.group(1)) // 3 + 1
            while len(list_stack) > depth:
                out.append(f"</{list_stack.pop()}>")
            while len(list_stack) < depth:
                list_stack.append("ol")
                out.append("<ol>")
            text, index = _consume_item(lines, index, match.group(3))
            out.append(f"<li>{_inline(text)}</li>")
            continue

        close_lists()
        paragraph = [line]
        index += 1
        while (
            index < len(lines)
            and lines[index].strip()
            and not _is_block_start(lines[index])
        ):
            paragraph.append(lines[index])
            index += 1
        out.append(
            "<p>" + _inline(" ".join(part.strip() for part in paragraph)) + "</p>"
        )

    close_lists()
    return "\n".join(out)


def _consume_item(lines: list[str], index: int, first: str) -> tuple[str, int]:
    """Fold a wrapped list item back into one entry.

    The proposal wraps long bullets across lines. Without this the continuation
    line falls through to the paragraph branch and is rendered outside the list,
    which reads as a stray sentence in the PDF.
    """

    parts = [first.strip()]
    index += 1
    while (
        index < len(lines)
        and lines[index].strip()
        and not _is_block_start(lines[index])
    ):
        parts.append(lines[index].strip())
        index += 1
    return " ".join(parts), index


def _is_block_start(line: str) -> bool:
    return bool(
        line.startswith(("#", "```", "|", "> "))
        or _UNORDERED.match(line)
        or _ORDERED.match(line)
    )


_STYLE = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Noto Sans CJK KR", "Noto Sans KR", sans-serif;
       font-size: 10.5pt; line-height: 1.65; color: #1a1a1a; }
h1 { font-size: 20pt; margin: 0 0 4mm; padding-bottom: 3mm;
     border-bottom: 2px solid #333; }
h2 { font-size: 14pt; margin: 9mm 0 3mm; padding-top: 2mm; border-top: 1px solid #ccc; }
h3 { font-size: 12pt; margin: 6mm 0 2mm; }
h4 { font-size: 11pt; margin: 4mm 0 2mm; color: #333; }
p, li { orphans: 3; widows: 3; }
ul, ol { margin: 2mm 0 2mm 6mm; padding-left: 3mm; }
li { margin-bottom: 1mm; }
code { font-family: "Noto Sans Mono CJK KR", monospace; font-size: 9.5pt;
       background: #f2f2f2; padding: 0 1mm; }
pre { background: #f7f7f7; border-left: 3px solid #bbb; padding: 3mm 4mm;
      margin: 3mm 0; page-break-inside: avoid; }
pre code { background: none; font-size: 9pt; line-height: 1.45; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0;
        page-break-inside: avoid; font-size: 9.5pt; }
th, td { border: 1px solid #bbb; padding: 1.6mm 2.4mm; text-align: left;
         vertical-align: top; }
th { background: #eee; font-weight: 600; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #999;
             color: #444; background: #fafafa; }
a { color: #1a1a1a; text-decoration: none; }
"""


def build_html(markdown: str, title: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"{markdown_to_html_body(markdown)}\n</body>\n</html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"input not found: {args.input}", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)

    markdown = args.input.read_text(encoding="utf-8")
    title = next(
        (line[2:].strip() for line in markdown.split("\n") if line.startswith("# ")),
        args.input.stem,
    )
    html_path = args.output_dir / f"{args.input.stem}.html"
    html_path.write_text(build_html(markdown, title), encoding="utf-8")
    print(f"html: {html_path}")
    if args.html_only:
        return 0

    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if soffice is None:
        print("libreoffice not found; wrote HTML only", file=sys.stderr)
        return 1
    subprocess.run(
        [
            soffice,
            "--headless",
            "--convert-to",
            "pdf:writer_web_pdf_Export",
            "--outdir",
            str(args.output_dir),
            str(html_path),
        ],
        check=True,
        capture_output=True,
    )
    pdf_path = args.output_dir / f"{args.input.stem}.pdf"
    if not pdf_path.is_file():
        print("libreoffice did not produce a PDF", file=sys.stderr)
        return 1
    print(f"pdf : {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
