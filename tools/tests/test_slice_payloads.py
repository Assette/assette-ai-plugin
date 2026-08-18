"""Offline test for slice_payloads.py — the understand-mode vision plumbing.

Covers: render-image-path + bbox propagation into understand payloads, the G_IMAGE_CAP
(representative first, distinct decks preferred), stat-checked missing renders, the
needs_vision passthrough on picture content (sha256 still dropped), the PDF page-text
cap raise, and — guarded by importorskip — the deterministic pre-crop + pdfplumber
region_text extraction from a real generated PDF.

Runs under pytest, OR as a plain script (`python test_slice_payloads.py`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import slice_payloads as sp  # noqa: E402

DECK_A = "aaaaaaaaaaaa"
DECK_B = "bbbbbbbbbbbb"


class _Skip(Exception):
    """Raised in plain-script mode when an optional dep is missing."""


def _need(name: str):
    """importorskip that works under pytest AND in plain-script mode."""
    try:
        return __import__(name)
    except ImportError:
        try:
            import pytest
        except ImportError:
            raise _Skip(name) from None
        pytest.skip(f"{name} not installed")


def _inventory(input_path: str = "C:/nowhere") -> dict:
    def deck(deck_id: str, filename: str) -> dict:
        return {
            "deck_id": deck_id, "filename": filename, "relative_path": filename,
            "source_format": "pdf",
            "metadata": {"slide_width_emu": 7772400, "slide_height_emu": 10058400},
            "slides": [{"slide_index": 0, "layout_name": "page-1", "elements": [
                {"element_id": f"{deck_id}:0:0", "element_index": 0, "kind": "picture",
                 "content": {"image_format": "png", "alt_text": None, "sha256": "deadbeef",
                             "likely_chart_image": True, "needs_vision": True},
                 "position": {"left_emu": 0, "top_emu": 0, "width_emu": 7772400, "height_emu": 10058400}},
            ]}],
        }
    return {"schema_version": "0.3.0",
            "corpus": {"input_path": input_path},
            "decks": [deck(DECK_A, "A.pdf"), deck(DECK_B, "B.pdf")]}


def _groups(members: list[dict], rep: str, label: str = "Gross Returns") -> dict:
    return {"schema_version": "0.2.0", "groups": [{
        "group_id": "tcg_test000", "shape": "table", "label": label,
        "data_category": "performance", "authoring_component": "smart-shell:table",
        "sample_count": len(members), "representative_element_id": rep,
        "members": members,
    }]}


def _member(deck_id: str, eid: str, *, vision: bool = True, bbox=None, suspect: bool = False) -> dict:
    m = {"element_id": eid, "deck_id": deck_id, "filename": f"{deck_id[:1]}.pdf",
         "slide_index": 0, "layout_name": "page-1"}
    if vision:
        m["vision"] = {"component_id": f"{deck_id}:s0:c0", "component_type": "table",
                       "label": "Gross Returns", "bbox": bbox or [0.5, 0.3, 0.9, 0.5],
                       "bbox_suspect": suspect}
    return m


def _regions(*deck_ids: str) -> dict:
    return {"regions": [
        {"element_id": f"{d}:0:v0", "kind": "vision-region", "deck_id": d, "slide_index": 0,
         "position": {"left_emu": 0, "top_emu": 0, "width_emu": 100, "height_emu": 100},
         "content": {"label": "Gross Returns", "component_type": "table", "needs_vision": True}}
        for d in deck_ids
    ]}


def _run_understand(ws: Path, inventory: dict, groups: dict, regions: dict) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    (ws / "table_chart_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (ws / "vision_regions.json").write_text(json.dumps(regions), encoding="utf-8")
    rc = sp.main([
        "--mode", "understand",
        "--inventory", str(ws / "corpus_inventory.json"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--renders-dir", str(ws / "renders"),
        "--vision-regions", str(ws / "vision_regions.json"),
        "--output-dir", str(ws / "payloads"),
    ])
    assert rc == 0
    return json.loads((ws / "payloads" / "understand_tcg_test000.json").read_text(encoding="utf-8"))


def _touch_render(ws: Path, deck_id: str, slide: int = 0) -> None:
    p = ws / "renders" / deck_id / f"slide_{slide}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\nstub")


def test_understand_payload_carries_render_path_and_bbox(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _touch_render(ws, DECK_A)
    members = [_member(DECK_A, f"{DECK_A}:0:v0")]
    payload = _run_understand(ws, _inventory(), _groups(members, f"{DECK_A}:0:v0"), _regions(DECK_A))
    sample = payload["samples"][0]
    # The synthetic element resolves from the registry...
    assert sample["element"]["kind"] == "vision-region"
    # ...and the vision block carries the bbox + the FULL page render (no crop possible —
    # there is no deck.pdf and the source PDF does not exist).
    vis = sample["vision"]
    assert vis["bbox"] == [0.5, 0.3, 0.9, 0.5]
    assert vis["render_image_path"].endswith("slide_0.png")
    assert "crop_image_path" not in vis


def test_understand_payload_image_cap_prefers_distinct_decks(tmp_path: Path) -> None:
    """5 members (4 in deck A, 1 in deck B, all rendered) -> exactly G_IMAGE_CAP carry a
    path; the rep goes first and deck B's only member wins a slot over deck A's third."""
    ws = tmp_path / "ws"
    _touch_render(ws, DECK_A)
    _touch_render(ws, DECK_B)
    ids = [f"{DECK_A}:0:v0", f"{DECK_A}:0:0", f"{DECK_B}:0:v0"]
    inventory = _inventory()
    # Add two more picture elements to deck A so 4 A-members exist.
    extra = []
    for i in (1, 2):
        el = {"element_id": f"{DECK_A}:0:{i}", "element_index": i, "kind": "picture",
              "content": {"image_format": "png", "alt_text": None, "likely_chart_image": True},
              "position": {"left_emu": 0, "top_emu": 0, "width_emu": 100, "height_emu": 100}}
        inventory["decks"][0]["slides"][0]["elements"].append(el)
        extra.append(f"{DECK_A}:0:{i}")
    ordered_ids = [ids[0], ids[1], extra[0], extra[1], ids[2]]  # rep, A, A, A, B
    members = [_member(eid.split(":")[0], eid) for eid in ordered_ids]
    payload = _run_understand(ws, inventory, _groups(members, ordered_ids[0]), _regions(DECK_A, DECK_B))
    with_img = [s for s in payload["samples"] if "render_image_path" in (s.get("vision") or {})]
    assert len(with_img) == sp.G_IMAGE_CAP
    # Representative first; deck B's member holds a slot (distinct decks preferred).
    assert payload["samples"][0]["vision"].get("render_image_path")
    b_sample = next(s for s in payload["samples"] if s["element"]["element_id"] == f"{DECK_B}:0:v0")
    assert b_sample["vision"].get("render_image_path")


def test_understand_payload_missing_render_omits_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"  # no renders written at all
    members = [_member(DECK_A, f"{DECK_A}:0:v0")]
    payload = _run_understand(ws, _inventory(), _groups(members, f"{DECK_A}:0:v0"), _regions(DECK_A))
    vis = payload["samples"][0]["vision"]
    assert "render_image_path" not in vis
    assert "crop_image_path" not in vis
    assert vis["bbox"] == [0.5, 0.3, 0.9, 0.5]   # the block itself still rides along


def test_picture_content_keeps_needs_vision_drops_sha() -> None:
    out = sp.lean_content("picture", {"image_format": "png", "alt_text": None,
                                      "sha256": "deadbeef", "likely_chart_image": True,
                                      "needs_vision": True},
                          cell_cap=80, row_cap=50)
    assert out["needs_vision"] is True
    assert out["likely_chart_image"] is True
    assert "sha256" not in out


def test_pdf_page_text_gets_looser_cap() -> None:
    long_text = "x" * 5000
    # PDF page text (has includes_table_text) -> capped at PDF_PAGE_TEXT_CAP.
    out = sp.lean_content("text", {"text": long_text, "includes_table_text": False},
                          cell_cap=80, row_cap=50)
    assert len(out["text"]) == sp.PDF_PAGE_TEXT_CAP
    # Ordinary text content keeps the aggressive cap.
    out2 = sp.lean_content("text", {"text": long_text}, cell_cap=80, row_cap=50)
    assert len(out2["text"]) == sp.TEXT_CAP


def test_crop_and_region_text_from_real_pdf(tmp_path: Path) -> None:
    """End-to-end vision plumbing against a REAL generated PDF: the pre-crop PNG is
    written and the region_text carries the actual characters inside the bbox."""
    fitz = _need("fitz")
    _need("pdfplumber")

    ws = tmp_path / "ws"
    src_dir = ws / "corpus"
    src_dir.mkdir(parents=True)
    pdf_path = src_dir / "A.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # Text inside the future bbox [0.1..0.9, 0.2..0.6]: headers + a data row.
    page.insert_text((80, 220), "Security Weight Return", fontsize=14)
    page.insert_text((80, 260), "ACME Corp 4.2% 1.3%", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    inventory = _inventory(input_path=str(src_dir))
    inventory["decks"] = [inventory["decks"][0]]
    inventory["decks"][0]["filename"] = "A.pdf"
    inventory["decks"][0]["relative_path"] = "A.pdf"
    _touch_render(ws, DECK_A)  # render exists, but the crop should win

    members = [_member(DECK_A, f"{DECK_A}:0:v0", bbox=[0.1, 0.2, 0.9, 0.6])]
    payload = _run_understand(ws, inventory, _groups(members, f"{DECK_A}:0:v0"), _regions(DECK_A))
    vis = payload["samples"][0]["vision"]
    assert "crop_image_path" in vis
    assert Path(vis["crop_image_path"]).exists()
    assert Path(vis["crop_image_path"]).stat().st_size > 0
    assert "render_image_path" not in vis            # crop replaces the full page
    assert "Security" in vis["region_text"]          # REAL extracted characters
    assert "Weight" in vis["region_text"]


def test_suspect_bbox_never_crops(tmp_path: Path) -> None:
    """bbox_suspect components fall back to the full-page render — never a crop."""
    ws = tmp_path / "ws"
    _touch_render(ws, DECK_A)
    members = [_member(DECK_A, f"{DECK_A}:0:v0", bbox=None, suspect=True)]
    payload = _run_understand(ws, _inventory(), _groups(members, f"{DECK_A}:0:v0"), _regions(DECK_A))
    vis = payload["samples"][0]["vision"]
    assert "crop_image_path" not in vis
    assert vis["render_image_path"].endswith("slide_0.png")


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    import inspect
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                if "tmp_path" in inspect.signature(fn).parameters:
                    fn(Path(td))
                else:
                    fn()
        except _Skip as exc:
            print(f"  skip {fn.__name__} (no {exc})")
            continue
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} slice_payloads test(s) passed.")


if __name__ == "__main__":
    _run_all()
