"""Phase 1 — Multi-format ingestion dispatcher.

Detects each input file's format (.pptx / .docx / .pdf) and routes it to the
correct ingester, producing a single unified corpus_inventory.json. This is the
entry point /analyze-deck calls; it lets a corpus mix formats freely.

Per-format ingesters are imported LAZILY, so a PPTX-only corpus does not require
pdfplumber or python-docx to be installed (and vice versa).

CLI:
    py tools/ingest_content.py --input <dir-or-file> --output workspace/corpus_inventory.json

Accepts a single file of any supported format, or a directory which is scanned
recursively for all supported formats.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from _inventory_common import SUPPORTED_FORMATS, build_corpus


def _ingester_for(source_format: str) -> Callable[[Path, Path], dict[str, Any]]:
    """Lazily import and return the ingest_deck function for a source format."""
    if source_format == "pptx":
        import ingest_pptx

        return ingest_pptx.ingest_deck
    if source_format == "docx":
        import ingest_docx

        return ingest_docx.ingest_deck
    if source_format == "pdf":
        import ingest_pdf

        return ingest_pdf.ingest_deck
    raise ValueError(f"No ingester for source format: {source_format}")


def _discover(input_path: Path) -> list[Path]:
    """Return supported files under input_path (file or directory), sorted."""
    if input_path.is_file():
        if input_path.suffix.lower() in SUPPORTED_FORMATS:
            return [input_path]
        return []
    paths: list[Path] = []
    for ext in SUPPORTED_FORMATS:
        paths.extend(input_path.rglob(f"*{ext}"))
    # Sorting by relative path keeps deck order deterministic across runs.
    return sorted(paths)


def ingest_corpus(input_path: Path) -> dict[str, Any]:
    input_path = input_path.resolve()
    relative_to = input_path.parent if input_path.is_file() else input_path
    files = _discover(input_path)

    decks: list[dict[str, Any]] = []
    for path in files:
        source_format = SUPPORTED_FORMATS[path.suffix.lower()]
        ingest_deck = _ingester_for(source_format)
        decks.append(ingest_deck(path, relative_to))
    return build_corpus(decks, input_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a mixed corpus (.pptx/.docx/.pdf) into one normalized JSON inventory."
    )
    parser.add_argument("--input", required=True, help="Path to a supported file or a directory.")
    parser.add_argument("--output", required=True, help="Path to write corpus_inventory.json.")
    args = parser.parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        print(f"Input path does not exist: {input_path}", file=sys.stderr)
        return 2

    files = _discover(input_path)
    if not files:
        exts = ", ".join(sorted(SUPPORTED_FORMATS))
        print(f"No supported files ({exts}) found under {input_path}", file=sys.stderr)
        return 3

    inventory = ingest_corpus(input_path)
    output_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    fmt_counts = inventory["corpus"]["formats"]
    fmt_summary = ", ".join(f"{n} {fmt}" for fmt, n in sorted(fmt_counts.items()))
    print(
        f"Ingested {inventory['corpus']['deck_count']} file(s) [{fmt_summary}], "
        f"{inventory['corpus']['element_count']} element(s) -> {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
