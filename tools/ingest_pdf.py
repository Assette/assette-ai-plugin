"""Phase 1 — PDF ingestion.

Parse one or more .pdf files into the unified corpus_inventory schema defined in
_inventory_common.py. Pure deterministic I/O — no LLM calls.

Scanned / image-only pages ARE ingested: their pictures (or a full-page raster
placeholder when nothing else is extractable) carry content.needs_vision=true and
the Phase 1.5 vision pass (render -> segment -> vision-capable analyzer) recovers
their STRUCTURE. OCR of VALUES remains out of scope — a number behind pixels is
never data.

Each PDF page is one container with page_kind="page" and slide_index = page number
(0-based). Per page we emit, in this order:
- table   : one per detected table, with real bbox position. Lines-strategy
            find_tables() first; when it finds nothing on a text-bearing page we
            retry with the text strategy (borderless/whitespace-aligned tables —
            the dominant factsheet style). content.table_strategy = "lines"|"text".
- picture : one per embedded raster image (charts in PDFs are images ->
            likely_chart_image; pages with no text layer add needs_vision=true)
- text    : ONE element holding the full page text (page.extract_text())
- picture (placeholder): when a page emitted NO elements at all, or has high
            drawing density (vector chart) but no table/picture was captured, one
            deterministic full-page picture (image_format="page", needs_vision=true)
            so the vision segmenter has a real element id to ground components on.

NOTE (encoded in content-ingestion skill): the page-level text element overlaps
with table text — pdfplumber's extract_text() includes characters that are also
inside tables. Downstream treats the `table` element as authoritative for the
data; the page text gives surrounding narrative/context.

CLI:
    py tools/ingest_pdf.py --input <dir-or-file.pdf> --output workspace/corpus_inventory.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _inventory_common import (
    EMU_PER_POINT,
    SCHEMA_VERSION,  # noqa: F401  (re-exported for parity with other ingesters)
    build_corpus,
    deck_id_for,
    sha256_bytes,
)

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover
    print(
        "ingest_pdf.py requires pdfplumber. Install with: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


SOURCE_FORMAT = "pdf"

# Heuristic: an image occupying a large fraction of the page is probably a chart.
# Two orientations: wide-and-modest-height OR tall-and-modest-width (side-by-side
# half-page charts are tall and narrow).
_CHART_WIDTH_FRACTION = 0.40
_CHART_HEIGHT_FRACTION = 0.25

# A page with at least this many vector drawing objects (lines + rects + curves)
# but no captured table/picture almost certainly draws a chart with vector
# primitives — pdfplumber's page.images lists only rasters, so vector charts
# would otherwise produce NO element for the vision pass to ground on.
_VECTOR_DENSITY_MIN = 30


def _bbox_to_position(x0: float, top: float, x1: float, bottom: float) -> dict[str, int]:
    return {
        "left_emu": int(x0 * EMU_PER_POINT),
        "top_emu": int(top * EMU_PER_POINT),
        "width_emu": int(max(0.0, x1 - x0) * EMU_PER_POINT),
        "height_emu": int(max(0.0, bottom - top) * EMU_PER_POINT),
    }


def _normalize_table(rows: list[list[Any]]) -> dict[str, Any]:
    cells: list[list[dict[str, Any]]] = []
    max_cols = 0
    for row in rows:
        row_out: list[dict[str, Any]] = []
        for cell in row:
            text = "" if cell is None else str(cell).strip()
            row_out.append({"text": text, "number_format": None})
        max_cols = max(max_cols, len(row_out))
        cells.append(row_out)
    # Pad ragged rows so every row has max_cols entries.
    for row_out in cells:
        while len(row_out) < max_cols:
            row_out.append({"text": "", "number_format": None})
    has_header = bool(cells) and all(c["text"] for c in cells[0])
    return {"rows": len(cells), "cols": max_cols, "cells": cells, "has_header": has_header}


def _deck_metadata(first_page, page_count: int) -> dict[str, Any]:
    width = float(getattr(first_page, "width", 0) or 0)
    height = float(getattr(first_page, "height", 0) or 0)
    orientation = "landscape" if width >= height else "portrait"
    return {
        "slide_width_emu": int(width * EMU_PER_POINT),
        "slide_height_emu": int(height * EMU_PER_POINT),
        "orientation": orientation,
        "slide_count": page_count,
        "theme_colors": [],
        "fonts": {"major": None, "minor": None},
    }


def ingest_deck(path: Path, relative_to: Path) -> dict[str, Any]:
    deck_id = deck_id_for(path)
    slides_out: list[dict[str, Any]] = []

    with pdfplumber.open(str(path)) as pdf:
        first_page = pdf.pages[0] if pdf.pages else None
        metadata = _deck_metadata(first_page, len(pdf.pages))

        for page_index, page in enumerate(pdf.pages):
            elements: list[dict[str, Any]] = []
            element_index = 0

            def _emit(kind: str, content: dict[str, Any], position: dict[str, int]) -> None:
                nonlocal element_index
                elements.append(
                    {
                        "element_id": f"{deck_id}:{page_index}:{element_index}",
                        "element_index": element_index,
                        "kind": kind,
                        "position": position,
                        "name": None,
                        "content": content,
                        "raw": {"page_number": page_index + 1},
                    }
                )
                element_index += 1

            # Page text is computed up-front: it drives the borderless-table
            # fallback (text-bearing pages only) and the needs_vision flag
            # (textless pages), but is still EMITTED last to keep element order.
            page_w = float(page.width or 0) or 1.0
            page_h = float(page.height or 0) or 1.0
            try:
                page_text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - text extraction is best-effort
                page_text = ""
            has_text_layer = bool(page_text.strip())

            # 1) Tables (with bbox). find_tables() exposes both geometry and extract().
            # Lines strategy first; on a text-bearing page where it finds nothing,
            # retry with the text strategy — borderless / whitespace-aligned tables
            # (the dominant factsheet style) have no ruling lines to find.
            table_strategy = "lines"
            try:
                found_tables = page.find_tables()
            except Exception:  # noqa: BLE001 - pdfplumber table finding can be fragile
                found_tables = []
            if not found_tables and has_text_layer:
                try:
                    found_tables = page.find_tables(
                        {"vertical_strategy": "text", "horizontal_strategy": "text"}
                    )
                    table_strategy = "text"
                except Exception:  # noqa: BLE001
                    found_tables = []
            for tbl in found_tables:
                try:
                    rows = tbl.extract()
                except Exception:  # noqa: BLE001
                    continue
                x0, top, x1, bottom = tbl.bbox
                content = _normalize_table(rows)
                content["table_strategy"] = table_strategy
                _emit("table", content, _bbox_to_position(x0, top, x1, bottom))
            tables_emitted = element_index > 0

            # 2) Images (charts in PDFs are images).
            pictures_emitted = False
            for img in page.images:
                x0 = float(img.get("x0", 0))
                top = float(img.get("top", 0))
                x1 = float(img.get("x1", 0))
                bottom = float(img.get("bottom", 0))
                frac_w = (x1 - x0) / page_w
                frac_h = (bottom - top) / page_h
                likely_chart = (
                    frac_w >= _CHART_WIDTH_FRACTION and frac_h >= _CHART_HEIGHT_FRACTION
                ) or (
                    frac_w >= _CHART_HEIGHT_FRACTION and frac_h >= _CHART_WIDTH_FRACTION
                )
                sha = None
                try:
                    stream = img.get("stream")
                    if stream is not None:
                        sha = sha256_bytes(stream.get_data())
                except Exception:  # noqa: BLE001 - image stream decoding is best-effort
                    sha = None
                content = {
                    "image_format": img.get("name") or "unknown",
                    "alt_text": None,
                    "sha256": sha,
                    "likely_chart_image": bool(likely_chart),
                }
                if not has_text_layer:
                    # No text layer -> the vision pass is the only structural signal.
                    content["needs_vision"] = True
                _emit("picture", content, _bbox_to_position(x0, top, x1, bottom))
                pictures_emitted = True

            # 3) Full page text (one element). Overlaps table text by design.
            if has_text_layer:
                _emit(
                    "text",
                    {"text": page_text.strip(), "runs": [], "includes_table_text": bool(found_tables)},
                    _bbox_to_position(0, 0, page_w, page_h),
                )

            # 4) Full-page placeholder. Two cases need a deterministic anchor for
            # the vision segmenter (grounded ids only — a page with no elements
            # would make it drop everything it sees):
            #   a) the page emitted NOTHING (pure raster/vector, no text layer);
            #   b) high vector-drawing density but no table/picture captured —
            #      vector charts appear in page.lines/rects/curves, never in
            #      page.images.
            if not elements or (not tables_emitted and not pictures_emitted):
                try:
                    drawing_density = (
                        len(page.lines or []) + len(page.rects or []) + len(page.curves or [])
                    )
                except Exception:  # noqa: BLE001
                    drawing_density = 0
                if not elements or drawing_density >= _VECTOR_DENSITY_MIN:
                    _emit(
                        "picture",
                        {
                            "image_format": "page",
                            "alt_text": None,
                            "sha256": None,
                            "likely_chart_image": True,
                            "needs_vision": True,
                        },
                        _bbox_to_position(0, 0, page_w, page_h),
                    )

            slides_out.append(
                {
                    "slide_index": page_index,
                    "slide_id": f"{deck_id}:{page_index}",
                    "page_kind": "page",
                    "layout_name": f"page-{page_index + 1}",
                    "elements": elements,
                }
            )

    return {
        "deck_id": deck_id,
        "filename": path.name,
        "relative_path": str(path.relative_to(relative_to)).replace("\\", "/"),
        "source_format": SOURCE_FORMAT,
        "metadata": metadata,
        "slides": slides_out,
    }


def ingest_corpus(input_path: Path) -> dict[str, Any]:
    input_path = input_path.resolve()
    if input_path.is_file():
        deck_paths = [input_path]
        relative_to = input_path.parent
    else:
        deck_paths = sorted(input_path.rglob("*.pdf"))
        relative_to = input_path
    decks = [ingest_deck(p, relative_to) for p in deck_paths]
    return build_corpus(decks, input_path)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest digital PDF corpus into normalized JSON.")
    parser.add_argument("--input", required=True, help="Path to a .pdf file or a directory.")
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
        f"Ingested {inventory['corpus']['deck_count']} PDF(s), "
        f"{inventory['corpus']['element_count']} element(s) -> {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
