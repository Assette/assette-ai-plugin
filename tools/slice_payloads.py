"""Phase 2 prep — slice the monolithic inventory into LEAN, bounded per-agent payloads.

The analysis agents cannot Read the whole `corpus_inventory.json` (a real corpus is
multi-MB / hundreds of thousands of lines). The Read tool caps at ~25K tokens, so a big
file comes back paginated and a small model gets lost — it hallucinates "deck not found",
drops slides, under-counts, or fabricates. The fix: this deterministic slicer copies the
REAL elements (so output stays grounded — no LLM re-serialization) into small payloads,
stripping the bulk an agent never needs (per-run text styling, 200K-char disclosure blobs,
chart value arrays, raw round-trip props). Each payload then reads in a single shot.

The full inventory is still the deterministic id-validator in merge_validate_analysis.py
(Python has no Read cap) — so the grounding guard is unchanged. No model ever reads the
monolithic file.

--------------------------------------------------------------------------------
MODE: deck        (default) — one lean payload per deck, for the element-classifier and
                   the slide-vision-segmenter.
  Output: workspace/payloads/deck_<deck_id>.json
  { deck_id, filename, source_format, element_count,
    elements: [ {element_id, kind, name, slide_index, layout_name, position, content} ] }

MODE: understand  (needs --groups table_chart_groups.json) — one lean payload per group,
                   for the table-chart-analyzer; pre-resolves each group's member records
                   so the analyzer never touches the inventory.
  Output: workspace/payloads/understand_<group_id>.json
  { group_id, shape, label, classification:{element_id,data_category,authoring_component},
    samples: [ {element:{...lightly-leaned...}, deck_context:{filename,slide_index,layout_name},
                vision?: {component_id, component_type, label, bbox,
                          crop_image_path?, render_image_path?, region_text?}} ] }

  THE VISION PLUMBING (understand mode): a group member covered by a slide-vision
  component (its `vision` block, written by group_table_chart_samples.py) gets, for at
  most G_IMAGE_CAP samples per group (representative first, distinct decks preferred):
    - crop_image_path   : a deterministic pre-crop of exactly that component — rendered
                          via PyMuPDF from renders/<deck>/deck.pdf (pptx) or the source
                          PDF (pdf decks) using the validated bbox. Pre-cropping is what
                          keeps the analyzer from mis-mapping normalized bbox coords onto
                          pixels, and cuts image tokens 3-5x vs a full slide.
    - render_image_path : the full rendered page PNG (stat-checked), only when no crop
                          could be made — the analyzer locates the region by bbox/label.
    - region_text       : REAL text extracted by pdfplumber from inside the bbox (source
                          PDF or the pptx's exported deck.pdf) — actual characters, which
                          the analyzer combines with the image as provenance "mixed".
  All of it degrades gracefully: no renders / no fitz / no pdfplumber / suspect bbox →
  the block simply omits those fields and the analyzer falls back (ultimately to
  readable:false exactly as before the vision path existed). Samples for synthetic
  vision-region elements (workspace/vision_regions.json) resolve from the registry.
--------------------------------------------------------------------------------

CLI:
  py tools/slice_payloads.py --inventory workspace/corpus_inventory.json --output-dir workspace/payloads
  py tools/slice_payloads.py --mode understand --inventory workspace/corpus_inventory.json \
      --groups workspace/table_chart_groups.json --output-dir workspace/payloads \
      [--renders-dir workspace/renders] [--vision-regions workspace/vision_regions.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _inventory_common import load_vision_regions, now_iso, resolve_source

# Caps for the classifier/segmenter (deck mode) — aggressive; they only need to recognise.
TEXT_CAP = 600
CELL_CAP = 80
ROW_CAP = 50
CAT_CAP = 40
# PDF page-text elements carry a WHOLE page and are the segmenter's member-mapping
# signal — 600 chars starves it on a dense factsheet page. (PDF page text is the only
# text content carrying `includes_table_text`.)
PDF_PAGE_TEXT_CAP = 3000
# Caps for the analyzer (understand mode) — looser; it reads cells for structure.
G_CELL_CAP = 120
G_ROW_CAP = 80
# At most this many samples per understand payload carry an image path — each image the
# analyzer Reads costs real tokens; the remaining samples still inform text variance.
G_IMAGE_CAP = 3
# region_text caps (real pdfplumber text from inside a component bbox).
REGION_TEXT_LINES = 60
REGION_TEXT_CHARS = 2500
CROP_DPI = 200
# Warn above this payload size (~bytes/4 tokens; 80 KB ≈ 20K tokens — the single-Read budget).
SIZE_WARN_BYTES = 80_000


def _cap(text: Any, n: int) -> tuple[str, bool]:
    s = "" if text is None else str(text)
    return (s[:n], True) if len(s) > n else (s, False)


def lean_content(kind: str, content: dict[str, Any] | None, *, cell_cap: int, row_cap: int) -> dict[str, Any]:
    """Strip an element's content to what's needed to recognise/structure it."""
    c = content or {}
    if kind in ("text", "placeholder"):
        # A PDF page-text element (the only text content with `includes_table_text`)
        # gets the looser cap — the segmenter maps regions to ids by this text.
        text_cap = PDF_PAGE_TEXT_CAP if "includes_table_text" in c else TEXT_CAP
        text, trunc = _cap(c.get("text"), text_cap)
        out: dict[str, Any] = {"text": text}
        if trunc:
            out["text_truncated"] = True
        return out  # drop `runs` (per-run styling is the bulk)
    if kind == "shape":
        text, trunc = _cap(c.get("text"), TEXT_CAP)
        out = {"shape_type": c.get("shape_type")}
        if text:
            out["text"] = text
            if trunc:
                out["text_truncated"] = True
        return out
    if kind == "table":
        cells = c.get("cells") or []
        kept_rows = cells[:row_cap]
        lean_cells = [[{"text": _cap(cell.get("text"), cell_cap)[0]} for cell in row] for row in kept_rows]
        out = {
            "rows": c.get("rows"),
            "cols": c.get("cols"),
            "has_header": c.get("has_header"),
            "cells": lean_cells,  # drop the always-null number_format
        }
        if len(cells) > row_cap:
            out["rows_truncated"] = len(cells)
        return out
    if kind == "chart":
        series = c.get("series") or []
        cats = c.get("categories") or []
        return {
            "chart_type": c.get("chart_type"),
            "series": [{"name": s.get("name"), **({"role": s["role"]} if "role" in s else {})} for s in series],
            "categories": cats[:CAT_CAP],  # drop per-series value arrays
            "category_count": len(cats),
            "embedded_xlsx_present": c.get("embedded_xlsx_present"),
        }
    if kind == "picture":
        out = {"image_format": c.get("image_format"), "alt_text": c.get("alt_text")}
        if "likely_chart_image" in c:
            out["likely_chart_image"] = c["likely_chart_image"]
        if "needs_vision" in c:
            out["needs_vision"] = c["needs_vision"]
        return out  # drop sha256
    if kind == "vision-region":
        # Synthetic region (registry element): its content is already lean.
        return dict(c)
    if kind == "group":
        return {"member_count": c.get("member_count"), "member_ids": c.get("member_ids")}
    # Unknown kind: keep small scalar/text fields only.
    return {k: v for k, v in c.items() if k != "runs" and not isinstance(v, (list, dict))}


def lean_element(el: dict[str, Any], *, slide_index: Any, layout_name: Any, cell_cap: int, row_cap: int) -> dict[str, Any]:
    return {
        "element_id": el.get("element_id"),
        "kind": el.get("kind"),
        "name": el.get("name"),
        "slide_index": slide_index,
        "layout_name": layout_name,
        "position": el.get("position"),
        "content": lean_content(el.get("kind"), el.get("content"), cell_cap=cell_cap, row_cap=row_cap),
    }  # drop `raw`


def _write(path: Path, obj: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def run_deck(inventory: dict[str, Any], out_dir: Path) -> int:
    big: list[str] = []
    count = 0
    for deck in inventory.get("decks", []):
        did = deck.get("deck_id")
        elements: list[dict[str, Any]] = []
        for container in deck.get("slides", []):
            si = container.get("slide_index")
            ln = container.get("layout_name")
            for el in container.get("elements", []):
                elements.append(lean_element(el, slide_index=si, layout_name=ln, cell_cap=CELL_CAP, row_cap=ROW_CAP))
        payload = {
            "generated_at": now_iso(),
            "deck_id": did,
            "filename": deck.get("filename"),
            "source_format": deck.get("source_format"),
            "element_count": len(elements),
            "elements": elements,
        }
        size = _write(out_dir / f"deck_{did}.json", payload)
        count += 1
        flag = "  ⚠ large" if size > SIZE_WARN_BYTES else ""
        print(f"  deck_{did}.json: {len(elements)} elements, ~{size // 1024} KB (~{size // 4 // 1000}K tok){flag}")
        if size > SIZE_WARN_BYTES:
            big.append(did)
    print(f"deck payloads: {count} written -> {out_dir}")
    if big:
        print(f"  ⚠ {len(big)} payload(s) exceed ~20K tokens — a vast deck may need per-slide splitting: {big}", file=sys.stderr)
    return 0


def index_elements(
    inventory: dict[str, Any],
    vision_regions: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    filenames: dict[str, Any] = {}
    for deck in inventory.get("decks", []):
        filenames[deck.get("deck_id")] = deck.get("filename")
        for container in deck.get("slides", []):
            for el in container.get("elements", []):
                eid = el.get("element_id")
                if eid:
                    idx[eid] = {
                        "element": el,
                        "deck_context": {
                            "filename": deck.get("filename"),
                            "slide_index": container.get("slide_index"),
                            "layout_name": container.get("layout_name"),
                        },
                    }
    for region in vision_regions or []:
        rid = region.get("element_id")
        if not rid:
            continue
        slide_index = region.get("slide_index")
        idx[rid] = {
            "element": {
                "element_id": rid,
                "kind": region.get("kind", "vision-region"),
                "position": region.get("position"),
                "name": (region.get("content") or {}).get("label"),
                "content": region.get("content") or {},
            },
            "deck_context": {
                "filename": filenames.get(region.get("deck_id")),
                "slide_index": slide_index,
                "layout_name": f"page-{(slide_index or 0) + 1}",
            },
        }
    return idx


# ------------------------------------------------- understand-mode vision plumbing


def _bbox_ok(bbox: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        left, top, right, bottom = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not (left < right and top < bottom):
        return None
    return (left, top, right, bottom)


def _crop_pdf_for_deck(inventory: dict[str, Any], deck: dict[str, Any] | None, renders_dir: Path) -> Path | None:
    """The PDF to crop/extract from: the pptx render's deck.pdf when it exists, else the
    original source file for PDF decks. None -> no crop/region_text for this deck."""
    if deck is None:
        return None
    deck_pdf = renders_dir / str(deck.get("deck_id")) / "deck.pdf"
    if deck_pdf.exists():
        return deck_pdf
    if deck.get("source_format") == "pdf":
        return resolve_source(inventory, deck)
    return None


def _render_crop(pdf_path: Path, page_index: int, bbox: tuple[float, float, float, float], out_path: Path) -> bool:
    """Deterministic pre-crop via PyMuPDF — the analyzer reads exactly the component,
    never mis-mapping normalized coords onto pixels. False on any failure."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return False
    try:
        doc = fitz.open(str(pdf_path))
        try:
            page = doc[page_index]
            r = page.rect
            clip = fitz.Rect(
                r.x0 + bbox[0] * r.width,
                r.y0 + bbox[1] * r.height,
                r.x0 + bbox[2] * r.width,
                r.y0 + bbox[3] * r.height,
            )
            pix = page.get_pixmap(clip=clip, dpi=CROP_DPI)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(out_path))
            return True
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - cropping is best-effort, never fatal
        return False


def _extract_region_text(pdf_path: Path, page_index: int, bbox: tuple[float, float, float, float]) -> str | None:
    """REAL characters from inside the component's geometry (vision-locate +
    pdfplumber-read) — the analyzer marks structure built on this 'mixed'."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            page = pdf.pages[page_index]
            box = (
                bbox[0] * page.width,
                bbox[1] * page.height,
                bbox[2] * page.width,
                bbox[3] * page.height,
            )
            text = page.crop(box).extract_text() or ""
    except Exception:  # noqa: BLE001 - extraction is best-effort, never fatal
        return None
    text = text.strip()
    if not text:
        return None
    return "\n".join(text.splitlines()[:REGION_TEXT_LINES])[:REGION_TEXT_CHARS]


def _select_image_slots(members: list[dict[str, Any]], renders_dir: Path) -> set[int]:
    """Which samples (by index in the ordered member list) get an image path — at most
    G_IMAGE_CAP, representative first, preferring distinct decks. Deterministic."""
    eligible = [
        i
        for i, m in enumerate(members)
        if m.get("vision") is not None
        and (renders_dir / str(m.get("deck_id")) / f"slide_{m.get('slide_index')}.png").exists()
    ]
    chosen: list[int] = []
    seen_decks: set[str] = set()
    for i in eligible:
        deck = str(members[i].get("deck_id"))
        if deck not in seen_decks:
            chosen.append(i)
            seen_decks.add(deck)
        if len(chosen) >= G_IMAGE_CAP:
            return set(chosen)
    for i in eligible:
        if i not in chosen:
            chosen.append(i)
        if len(chosen) >= G_IMAGE_CAP:
            break
    return set(chosen)


def run_understand(
    inventory: dict[str, Any],
    groups_path: Path,
    out_dir: Path,
    renders_dir: Path,
    vision_regions: list[dict[str, Any]] | None = None,
) -> int:
    groups_doc = json.loads(groups_path.read_text(encoding="utf-8"))
    idx = index_elements(inventory, vision_regions)
    decks_by_id = {d.get("deck_id"): d for d in inventory.get("decks", [])}
    count = 0
    for grp in groups_doc.get("groups", []):
        gid = grp.get("group_id")
        rep = grp.get("representative_element_id")
        # representative first, then the rest — the agent picks the richest as anchor.
        members_by_id = {m["element_id"]: m for m in grp.get("members", []) if m.get("element_id")}
        member_ids = list(members_by_id)
        ordered = ([rep] if rep in member_ids else []) + [m for m in member_ids if m != rep]
        ordered_members = [members_by_id[eid] for eid in ordered]
        image_slots = _select_image_slots(ordered_members, renders_dir)

        samples = []
        images_attached = 0
        for i, eid in enumerate(ordered):
            info = idx.get(eid)
            if not info:
                continue
            el = info["element"]
            dc = info["deck_context"]
            sample: dict[str, Any] = {
                "element": lean_element(
                    el, slide_index=dc["slide_index"], layout_name=dc["layout_name"],
                    cell_cap=G_CELL_CAP, row_cap=G_ROW_CAP,
                ),
                "deck_context": dc,
            }
            member = members_by_id.get(eid) or {}
            vis = member.get("vision")
            if vis is not None:
                vision_block: dict[str, Any] = {
                    "component_id": vis.get("component_id"),
                    "component_type": vis.get("component_type"),
                    "label": vis.get("label"),
                    "bbox": vis.get("bbox"),
                }
                if i in image_slots:
                    deck_id = str(member.get("deck_id"))
                    slide_index = member.get("slide_index")
                    render_png = renders_dir / deck_id / f"slide_{slide_index}.png"
                    bbox = None if vis.get("bbox_suspect") else _bbox_ok(vis.get("bbox"))
                    cropped = False
                    if bbox is not None:
                        pdf_path = _crop_pdf_for_deck(inventory, decks_by_id.get(member.get("deck_id")), renders_dir)
                        if pdf_path is not None:
                            crop_path = out_dir / "crops" / f"{gid}_{i}.png"
                            if _render_crop(pdf_path, int(slide_index or 0), bbox, crop_path):
                                vision_block["crop_image_path"] = str(crop_path.resolve())
                                cropped = True
                            region_text = _extract_region_text(pdf_path, int(slide_index or 0), bbox)
                            if region_text:
                                vision_block["region_text"] = region_text
                    if not cropped and render_png.exists():
                        vision_block["render_image_path"] = str(render_png.resolve())
                    if cropped or "render_image_path" in vision_block:
                        images_attached += 1
                sample["vision"] = vision_block
            samples.append(sample)

        payload = {
            "generated_at": now_iso(),
            "group_id": gid,
            "shape": grp.get("shape"),
            "label": grp.get("label"),
            "classification": {
                "element_id": rep,
                "data_category": grp.get("data_category"),
                "authoring_component": grp.get("authoring_component"),
            },
            "sample_count": len(samples),
            "samples": samples,
        }
        size = _write(out_dir / f"understand_{gid}.json", payload)
        count += 1
        flag = "  ⚠ large" if size > SIZE_WARN_BYTES else ""
        img = f", {images_attached} image(s)" if images_attached else ""
        print(f"  understand_{gid}.json: {len(samples)} sample(s){img}, ~{size // 1024} KB{flag}")
    print(f"understand payloads: {count} written -> {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Slice corpus_inventory.json into lean, single-Read agent payloads."
    )
    parser.add_argument("--mode", default="deck", choices=["deck", "understand"])
    parser.add_argument("--inventory", default="workspace/corpus_inventory.json")
    parser.add_argument("--groups", default="workspace/table_chart_groups.json")
    parser.add_argument("--renders-dir", default="workspace/renders")
    parser.add_argument("--vision-regions", default="workspace/vision_regions.json")
    parser.add_argument("--output-dir", default="workspace/payloads")
    args = parser.parse_args(argv)

    inv_path = Path(args.inventory)
    if not inv_path.exists():
        print(f"Inventory not found: {inv_path}", file=sys.stderr)
        return 2
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    out_dir = Path(args.output_dir)

    if args.mode == "understand":
        gp = Path(args.groups)
        if not gp.exists():
            print(f"Groups file not found: {gp}", file=sys.stderr)
            return 2
        return run_understand(
            inventory,
            gp,
            out_dir,
            Path(args.renders_dir),
            load_vision_regions(Path(args.vision_regions)),
        )
    return run_deck(inventory, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
