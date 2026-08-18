"""Phase 1 — Word (.docx) ingestion.

Parse one or more .docx files into the unified corpus_inventory schema defined in
_inventory_common.py. Pure deterministic I/O — no LLM calls.

Word has no slides or pages (python-docx cannot resolve rendered page breaks
reliably), so the whole document is modelled as a SINGLE container with
page_kind="section" and slide_index=0. Element order follows document body order:
paragraphs and tables are interleaved, and inline images are emitted in place.

Element kinds emitted:
- text       : a non-empty paragraph (heading paragraphs carry content.style + heading_level)
- table      : a Word table, cells normalized to {text, number_format:null}
- picture    : an inline image (sha256 best-effort from the embedded blip)

Charts in Word are usually flattened images and surface as kind:"picture" with
content.likely_chart_image=true — their series cannot be read. See the
content-ingestion skill for the fidelity caveat.

CLI:
    py tools/ingest_docx.py --input <dir-or-file.docx> --output workspace/corpus_inventory.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _inventory_common import (
    SCHEMA_VERSION,  # noqa: F401  (re-exported for parity with other ingesters)
    build_corpus,
    deck_id_for,
    empty_position,
    sha256_bytes,
)

try:
    import docx
    from docx.document import Document as _DocumentClass
    from docx.oxml.ns import qn
    from docx.table import Table as _Table
    from docx.text.paragraph import Paragraph as _Paragraph
except ImportError as exc:  # pragma: no cover
    print(
        "ingest_docx.py requires python-docx. Install with: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


SOURCE_FORMAT = "docx"

# A paragraph whose style name starts with one of these is treated as a heading.
_HEADING_PREFIXES = ("Heading", "Title", "Subtitle")


# --------------------------------------------------------------------------- helpers


def _iter_block_items(document: "_DocumentClass"):
    """Yield Paragraph and Table objects in document body order.

    python-docx exposes doc.paragraphs and doc.tables separately, losing their
    interleaving. Walking the body XML children preserves order.
    """
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield _Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield _Table(child, document)


def _heading_level(style_name: str) -> int | None:
    if not style_name:
        return None
    if style_name.startswith("Heading"):
        # "Heading 1" -> 1
        tail = style_name.replace("Heading", "").strip()
        return int(tail) if tail.isdigit() else 1
    if style_name in ("Title",):
        return 0
    return None


def _paragraph_runs(paragraph: "_Paragraph") -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run in paragraph.runs:
        color_hex = None
        try:
            rgb = run.font.color.rgb if run.font.color is not None else None
            if rgb is not None:
                color_hex = f"#{rgb}"
        except (AttributeError, ValueError):
            color_hex = None
        runs.append(
            {
                "text": run.text,
                "bold": bool(run.font.bold),
                "italic": bool(run.font.italic),
                "size_pt": float(run.font.size.pt) if run.font.size else None,
                "font_name": run.font.name,
                "color_hex": color_hex,
            }
        )
    return runs


def _drawing_image_sha(paragraph: "_Paragraph", document: "_DocumentClass") -> list[str]:
    """Return sha256 hex digests for any inline images embedded in this paragraph.

    Resolves the blip r:embed relationship to the image part blob. Best-effort:
    failures yield no digest rather than raising.
    """
    digests: list[str] = []
    blips = paragraph._p.findall(".//" + qn("a:blip"))  # noqa: SLF001
    for blip in blips:
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        try:
            part = document.part.related_parts[rid]
            digests.append(sha256_bytes(part.blob))
        except (KeyError, AttributeError):
            continue
    return digests


def _extract_table(table: "_Table") -> dict[str, Any]:
    rows = len(table.rows)
    cols = len(table.columns)
    cells: list[list[dict[str, Any]]] = []
    for row in table.rows:
        row_out: list[dict[str, Any]] = []
        for cell in row.cells:
            row_out.append({"text": cell.text.strip(), "number_format": None})
        cells.append(row_out)
    # Word tables have no reliable header flag; assume the first row is a header
    # only when every cell in it is non-empty and short. Kept conservative.
    has_header = bool(cells) and all(c["text"] for c in cells[0])
    return {"rows": rows, "cols": cols, "cells": cells, "has_header": has_header}


# --------------------------------------------------------------------------- deck


def _deck_metadata(container_count: int) -> dict[str, Any]:
    # Word has no slide geometry; coordinates are unavailable.
    return {
        "slide_width_emu": 0,
        "slide_height_emu": 0,
        "orientation": "portrait",
        "slide_count": container_count,
        "theme_colors": [],
        "fonts": {"major": None, "minor": None},
    }


def ingest_deck(path: Path, relative_to: Path) -> dict[str, Any]:
    deck_id = deck_id_for(path)
    document = docx.Document(str(path))

    elements: list[dict[str, Any]] = []
    element_index = 0

    def _emit(kind: str, content: dict[str, Any], name: str | None) -> None:
        nonlocal element_index
        elements.append(
            {
                "element_id": f"{deck_id}:0:{element_index}",
                "element_index": element_index,
                "kind": kind,
                "position": empty_position(),
                "name": name,
                "content": content,
                "raw": {},
            }
        )
        element_index += 1

    for block in _iter_block_items(document):
        if isinstance(block, _Paragraph):
            # Inline images first (so they precede the paragraph's text in order).
            for sha in _drawing_image_sha(block, document):
                _emit(
                    "picture",
                    {
                        "image_format": "unknown",
                        "alt_text": None,
                        "sha256": sha,
                        "likely_chart_image": True,
                    },
                    name=None,
                )
            text = block.text.strip()
            if text:
                style_name = block.style.name if block.style is not None else ""
                content: dict[str, Any] = {"text": text, "runs": _paragraph_runs(block)}
                level = _heading_level(style_name)
                if level is not None:
                    content["style"] = style_name
                    content["heading_level"] = level
                _emit("text", content, name=style_name or None)
        elif isinstance(block, _Table):
            _emit("table", _extract_table(block), name=None)

    return {
        "deck_id": deck_id,
        "filename": path.name,
        "relative_path": str(path.relative_to(relative_to)).replace("\\", "/"),
        "source_format": SOURCE_FORMAT,
        "metadata": _deck_metadata(container_count=1),
        "slides": [
            {
                "slide_index": 0,
                "slide_id": f"{deck_id}:0",
                "page_kind": "section",
                "layout_name": "document",
                "elements": elements,
            }
        ],
    }


def ingest_corpus(input_path: Path) -> dict[str, Any]:
    input_path = input_path.resolve()
    if input_path.is_file():
        deck_paths = [input_path]
        relative_to = input_path.parent
    else:
        deck_paths = sorted(input_path.rglob("*.docx"))
        relative_to = input_path
    decks = [ingest_deck(p, relative_to) for p in deck_paths]
    return build_corpus(decks, input_path)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Word (.docx) corpus into normalized JSON.")
    parser.add_argument("--input", required=True, help="Path to a .docx file or a directory.")
    parser.add_argument("--output", required=True, help="Path to write corpus_inventory.json.")
    args = parser.parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        print(f"Input path does not exist: {input_path}", file=sys.stderr)
        return 2
    inventory = ingest_corpus(input_path)
    output_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    print(
        f"Ingested {inventory['corpus']['deck_count']} document(s), "
        f"{inventory['corpus']['element_count']} element(s) -> {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
