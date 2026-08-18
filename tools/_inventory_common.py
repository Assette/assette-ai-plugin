"""Shared contract for all ingestion tools (PPTX, Word, PDF).

Every ingester emits the SAME `corpus_inventory.json` schema so that downstream
phases (classification, estimation, future canonicalization) are format-agnostic.
This module is the single source of truth for the schema version and the corpus
envelope. Bumping SCHEMA_VERSION is a breaking change — update workspace/README.md
and any consumer that asserts a specific version.

================================================================================
UNIFIED SCHEMA — workspace/corpus_inventory.json   (schema_version 0.3.0)
================================================================================
{
  "schema_version": "0.3.0",
  "ingested_at": "2026-05-19T12:34:56Z",
  "corpus": {
    "input_path": "<absolute path>",
    "deck_count": <int>,
    "element_count": <int>,
    "formats": {"pptx": <int>, "docx": <int>, "pdf": <int>}   # per-format deck counts
  },
  "decks": [
    {
      "deck_id": "<sha256(file_bytes)[:12]>",
      "filename": "<basename>",
      "relative_path": "<path relative to corpus root>",
      "source_format": "pptx" | "docx" | "pdf",               # NEW in 0.2.0
      "metadata": { "slide_width_emu", "slide_height_emu", "orientation",
                    "slide_count", "theme_colors", "fonts" },
      "slides": [                                              # container key kept for back-compat
        {
          "slide_index": <int, 0-based>,
          "slide_id": "<deck_id>:<slide_index>",
          "page_kind": "slide" | "page" | "section",          # NEW in 0.2.0
          "layout_name": <str>,
          "elements": [
            {
              "element_id": "<deck_id>:<slide_index>:<element_index>",
              "element_index": <int>,
              "kind": "text"|"table"|"chart"|"picture"|"placeholder"|"group"|"shape",
                      # ("vision-region" appears only in workspace/vision_regions.json,
                      #  the companion registry of code-minted synthetic elements the
                      #  segment merge materializes for vision components — see
                      #  merge_validate_analysis.py. Never emitted by an ingester.)
              "position": {"left_emu","top_emu","width_emu","height_emu"},
              "name": <str|null>,
              "content": { ... kind-specific ... },
              "raw": { ... }
            }
          ]
        }
      ]
    }
  ]
}

NON-PPTX FIDELITY NOTES (encoded in content-ingestion skill):
- Word/PDF have no native chart objects — charts are flattened images and surface
  as kind:"picture" with content.likely_chart_image=true. Their series cannot be
  read at ingest; the Phase 1.5 vision pass (render -> segment -> vision-capable
  analyzer) recovers the STRUCTURE (never the values) with
  structure_provenance:"vision"|"mixed" + verification:"needs-confirmation".
- content.needs_vision=true (0.3.0) marks a picture whose page has no text layer
  (or a full-page placeholder on raster/vector-only pages) — the vision pass is
  the ONLY structural signal for it. OCR of VALUES remains out of scope.
- Word has no pages; the whole document is one container with page_kind:"section".
  Word decks also have no render path — flattened Word charts stay unreadable;
  ask for the PPTX/PDF original.
- PDF table extraction (pdfplumber) runs the lines strategy first and falls back
  to the text strategy on text-bearing pages where lines found nothing
  (content.table_strategy = "lines" | "text"); borderless tables are still
  best-effort — vision components corroborate downstream.
================================================================================
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.3.0"

# Extension -> source_format label. Used by the dispatcher to route files.
SUPPORTED_FORMATS = {
    ".pptx": "pptx",
    ".docx": "docx",
    ".pdf": "pdf",
}

# Points -> EMU (English Metric Units). 1 pt = 12700 EMU. Used by the PDF ingester
# to express bounding boxes in the same units PPTX uses.
#
# CANONICAL COORDINATE TRANSFORMS (the pipeline uses three spaces):
#   - inventory element positions: EMU over the slide/page box
#     (normalize: left_emu / metadata.slide_width_emu, top_emu / slide_height_emu)
#   - PDF page geometry (pdfplumber / PyMuPDF): points over the page box
#     (normalize: x / page.width, y / page.height; points -> EMU via EMU_PER_POINT)
#   - vision component bbox (slide_components.jsonl): ALREADY normalized 0..1
#     [left, top, right, bottom] over the rendered PNG, which shares the page box.
# All cross-space comparisons (bbox validation, crops) happen in normalized space.
EMU_PER_POINT = 12700

# The slide-vision-segmenter component_types that are DATA components — the ones
# Phase 2c deep-analyzes and the segment merge materializes synthetic vision-region
# elements for. Shared by group_table_chart_samples.py and merge_validate_analysis.py.
DATA_COMPONENT_TYPES = frozenset({"table", "chart", "kpi-grid", "performance-history"})

# Deterministic component_type -> authoring primitive fallback, applied when the
# segmenter abstains (proposed_authoring_component "none"/missing) on a data
# component. Code enforces promotion — prose obedience alone already failed once.
VISION_COMPONENT_DEFAULT_PRIMITIVE = {
    "table": "smart-shell:table",
    "chart": "smart-shell:chart",
    "kpi-grid": "smart-shell:table",
    "performance-history": "smart-shell:performance-history",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deck_id_for(path: Path) -> str:
    """Deterministic deck id: first 12 hex chars of sha256(file_bytes)."""
    return sha256_file(path)[:12]


def now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def empty_position() -> dict[str, int]:
    """Layout coordinates are unavailable for Word (and optional for PDF)."""
    return {"left_emu": 0, "top_emu": 0, "width_emu": 0, "height_emu": 0}


def count_elements(decks: list[dict[str, Any]]) -> int:
    return sum(len(c["elements"]) for d in decks for c in d["slides"])


def load_vision_regions(path: Path) -> list[dict[str, Any]]:
    """Read workspace/vision_regions.json — the registry of code-minted synthetic
    vision-region elements the segment merge materializes. [] when absent/unreadable.
    Shared by merge_validate_analysis.py, group_table_chart_samples.py and
    slice_payloads.py."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    regions = data.get("regions") if isinstance(data, dict) else data
    return [r for r in regions or [] if isinstance(r, dict)]


def has_parsed_structure(element: dict[str, Any] | None) -> bool:
    """True when the element carries machine-readable table cells or chart series —
    the object-model signal that ALWAYS outranks a vision read of the same region.
    Shared by merge_validate_analysis.py (promotion/materialization) and
    group_table_chart_samples.py (structural-signature preference)."""
    if not element:
        return False
    kind = element.get("kind")
    content = element.get("content") or {}
    if kind == "table":
        return bool(content.get("cells"))
    if kind == "chart":
        return bool(content.get("series"))
    return False


def resolve_source(inventory: dict[str, Any], deck: dict[str, Any]) -> Path | None:
    """Reconstruct a deck's original source file from the inventory.

    `corpus.input_path` is the resolved --input ingest was given; `relative_path` is
    relative to it (its parent when input was a single file). Try the obvious candidates
    and return the first that exists. Shared by render_slides.py (pptx->pdf leg) and
    slice_payloads.py (PDF crop render + pdfplumber region-text extraction).
    """
    rel = deck.get("relative_path")
    ip = inventory.get("corpus", {}).get("input_path")
    cands: list[Path] = []
    if ip:
        base = Path(ip)
        root = base if base.is_dir() else base.parent
        if rel:
            cands.append(root / rel)
        cands.append(base)  # single-file ingest: input_path IS the file
        if deck.get("filename"):
            cands.append(root / deck["filename"])
    if rel:
        cands.append(Path(rel))  # already absolute?
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return c
        except OSError:
            continue
    return None


def build_corpus(decks: list[dict[str, Any]], input_path: Path) -> dict[str, Any]:
    """Wrap a list of deck records in the corpus envelope."""
    formats: dict[str, int] = {}
    for d in decks:
        fmt = d.get("source_format", "unknown")
        formats[fmt] = formats.get(fmt, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "ingested_at": now_iso(),
        "corpus": {
            "input_path": str(input_path),
            "deck_count": len(decks),
            "element_count": count_elements(decks),
            "formats": formats,
        },
        "decks": decks,
    }
