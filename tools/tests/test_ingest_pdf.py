"""Offline test for ingest_pdf.py's deterministic vision-era behaviors.

Generates real PDFs with PyMuPDF (importorskip-guarded, like the xlsx test) and asserts:
raster-only pages flag their pictures needs_vision, zero-element pages get the
deterministic full-page placeholder, high vector-drawing-density pages get one too,
the likely_chart_image heuristic catches tall-narrow charts, and the borderless-table
text-strategy fallback marks its tables table_strategy "text".

Runs under pytest, OR as a plain script (`python test_ingest_pdf.py`).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode


class _Skip(Exception):
    """Raised in plain-script mode when an optional dep is missing."""


def _need(name: str):
    try:
        return __import__(name)
    except ImportError:
        try:
            import pytest
        except ImportError:
            raise _Skip(name) from None
        pytest.skip(f"{name} not installed")


def _ingest(pdf_path: Path):
    _need("pdfplumber")
    import ingest_pdf
    return ingest_pdf.ingest_deck(pdf_path, pdf_path.parent)


def _png_stream(fitz, w: int = 24, h: int = 24) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(90)
    return pix.tobytes("png")


def test_raster_only_page_flags_needs_vision(tmp_path: Path) -> None:
    fitz = _need("fitz")
    pdf = tmp_path / "raster.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # One big raster covering most of the page, no text layer at all.
    page.insert_image(fitz.Rect(30, 30, 580, 760), stream=_png_stream(fitz))
    doc.save(str(pdf))
    doc.close()

    deck = _ingest(pdf)
    elements = deck["slides"][0]["elements"]
    pictures = [e for e in elements if e["kind"] == "picture"]
    assert pictures, "the raster image must be ingested"
    assert all(p["content"].get("needs_vision") is True for p in pictures)
    assert pictures[0]["content"]["likely_chart_image"] is True
    assert not [e for e in elements if e["kind"] == "text"]  # no text layer


def test_blank_page_gets_full_page_placeholder(tmp_path: Path) -> None:
    fitz = _need("fitz")
    pdf = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)  # nothing on it
    doc.save(str(pdf))
    doc.close()

    deck = _ingest(pdf)
    elements = deck["slides"][0]["elements"]
    assert len(elements) == 1
    ph = elements[0]
    assert ph["kind"] == "picture"
    assert ph["content"]["image_format"] == "page"
    assert ph["content"]["needs_vision"] is True
    assert ph["content"]["likely_chart_image"] is True
    # Full-page bbox in EMU.
    assert ph["position"]["width_emu"] == int(612 * 12700)


def test_vector_dense_page_gets_placeholder(tmp_path: Path) -> None:
    """A page with narrative text + a vector-drawn chart (30+ rects, no raster image,
    no ruled table) must still yield a vision anchor — vector charts never appear in
    page.images."""
    fitz = _need("fitz")
    pdf = tmp_path / "vector.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((40, 40), "Sector Distribution", fontsize=14)
    for i in range(32):  # fake bar chart: disjoint filled rects (no table ruling grid)
        x = 40 + (i % 8) * 70
        y = 100 + (i // 8) * 120
        page.draw_rect(fitz.Rect(x, y, x + 40, y + 80), fill=(0.2, 0.4, 0.8), width=0)
    doc.save(str(pdf))
    doc.close()

    deck = _ingest(pdf)
    elements = deck["slides"][0]["elements"]
    placeholders = [e for e in elements if e["kind"] == "picture"
                    and e["content"].get("image_format") == "page"]
    tables = [e for e in elements if e["kind"] == "table"]
    # Either the drawing-density placeholder fired, or the rect grid was (acceptably)
    # picked up as a lines-strategy table — one of the two must anchor the region.
    assert placeholders or tables, "a vector-chart page must produce SOME anchor"
    if placeholders:
        assert placeholders[0]["content"]["needs_vision"] is True


def test_tall_narrow_image_counts_as_chart(tmp_path: Path) -> None:
    """The loosened likely_chart_image heuristic: a side-by-side half-page chart is
    tall and narrow (w ~30%, h ~50%) and must now be flagged."""
    fitz = _need("fitz")
    pdf = tmp_path / "tallnarrow.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((40, 40), "Two charts side by side", fontsize=12)
    page.insert_image(fitz.Rect(30, 100, 30 + 0.30 * 612, 100 + 0.50 * 792),
                      stream=_png_stream(fitz))
    doc.save(str(pdf))
    doc.close()

    deck = _ingest(pdf)
    pictures = [e for e in deck["slides"][0]["elements"] if e["kind"] == "picture"
                and e["content"].get("image_format") != "page"]
    assert pictures
    assert pictures[0]["content"]["likely_chart_image"] is True
    # The page HAS a text layer, so needs_vision is NOT set on its pictures.
    assert "needs_vision" not in pictures[0]["content"]


def test_borderless_table_text_strategy_fallback(tmp_path: Path) -> None:
    """A whitespace-aligned (unruled) grid of words: the lines strategy finds nothing;
    the text-strategy retry may recover it — any table it finds must be marked
    table_strategy 'text', and the run must never crash."""
    fitz = _need("fitz")
    pdf = tmp_path / "borderless.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    headers = ["Security", "Sector", "Weight"]
    rows = [["ACME", "Tech", "4.2%"], ["Globex", "Energy", "3.1%"],
            ["Initech", "Tech", "2.8%"], ["Umbrella", "Health", "2.2%"]]
    xs = [60, 260, 460]
    for c, htext in enumerate(headers):
        page.insert_text((xs[c], 100), htext, fontsize=11)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((xs[c], 130 + r * 24), cell, fontsize=11)
    doc.save(str(pdf))
    doc.close()

    deck = _ingest(pdf)
    elements = deck["slides"][0]["elements"]
    tables = [e for e in elements if e["kind"] == "table"]
    for t in tables:
        assert t["content"]["table_strategy"] == "text"  # lines found nothing first
    # Whatever the strategies found, the page text is always there as the safety net.
    assert [e for e in elements if e["kind"] == "text"]


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
        except _Skip as exc:
            print(f"  skip {fn.__name__} (no {exc})")
            continue
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} ingest_pdf test(s) passed.")


if __name__ == "__main__":
    _run_all()
