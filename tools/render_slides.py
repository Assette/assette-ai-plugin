"""Phase 1.5 (render) — rasterize each deck's slides to PNG for the vision pass.

The python-pptx object model is blind to how a slide is visually composed (dozens of
loose text boxes + freeforms that LOOK like a chart/table are just disconnected shapes).
This tool renders the *pixels* so a vision agent can see the real layout.

Pipeline: PPTX --(PowerPoint COM)--> PDF --(PyMuPDF)--> one PNG per slide.
PowerPoint's format has no faithful pure-Python renderer, so the pptx->pdf leg uses the
installed PowerPoint via COM (Windows + Office only). PDF source decks are rendered
directly with PyMuPDF (no PowerPoint needed). Word decks are skipped (no render path).

Output: <output_dir>/<deck_id>/slide_<slide_index>.png  (+ deck.pdf)
The PNG's <slide_index> matches the element-id container index (<deck_id>:<slide_index>:*),
so the vision segmenter can map what it sees back to real element ids.

Vision is an AUGMENTATION: if the render engine is unavailable (non-Windows, no Office,
missing pywin32) the tool exits non-zero with a clear message and the rest of /analyze-deck
runs structure-only.

Exit codes: 0 rendered >=1 deck | 2 inventory missing/bad | 3 no renderable decks |
            4 render engine unavailable (pptx decks present but COM unusable).

CLI:
    py tools/render_slides.py --inventory workspace/corpus_inventory.json \
        --output-dir workspace/renders [--deck-id <id>] [--dpi 150]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _inventory_common import resolve_source  # noqa: F401  (shared with slice_payloads)

PP_SAVE_AS_PDF = 32          # PpSaveAsFileType.ppSaveAsPDF
PP_FIXED_FORMAT_PDF = 2      # PpFixedFormatType.ppFixedFormatTypePDF


def pptx_to_pdf_com(pptx_path: Path, pdf_path: Path) -> None:
    """pptx -> pdf via the installed PowerPoint (COM). Windows + Office only."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    pres = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            app.Visible = 1  # PowerPoint COM is unreliable when not visible
        except Exception:
            pass
        pres = app.Presentations.Open(
            str(pptx_path), ReadOnly=1, Untitled=0, WithWindow=0
        )
        try:
            pres.SaveAs(str(pdf_path), PP_SAVE_AS_PDF)
        except Exception:
            # Fallback to the dedicated fixed-format exporter.
            pres.ExportAsFixedFormat(str(pdf_path), PP_FIXED_FORMAT_PDF)
    finally:
        if pres is not None:
            try:
                pres.Close()
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def pdf_to_pngs(pdf_path: Path, out_dir: Path, dpi: int) -> int:
    """Render every page of a PDF to slide_<index>.png. Returns page count."""
    import fitz  # PyMuPDF

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            pix.save(str(out_dir / f"slide_{i}.png"))
        return doc.page_count
    finally:
        doc.close()


def _com_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except Exception:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render each deck's slides to PNG (PowerPoint COM -> PDF -> PyMuPDF) for the vision pass."
    )
    parser.add_argument("--inventory", default="workspace/corpus_inventory.json")
    parser.add_argument("--output-dir", default="workspace/renders")
    parser.add_argument("--deck-id", default=None, help="Render only this deck_id.")
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args(argv)

    inv_path = Path(args.inventory)
    if not inv_path.exists():
        print(f"Inventory not found: {inv_path}", file=sys.stderr)
        return 2
    try:
        inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Inventory is not valid JSON: {exc}", file=sys.stderr)
        return 2

    decks = inventory.get("decks", [])
    if args.deck_id:
        decks = [d for d in decks if d.get("deck_id") == args.deck_id]

    renderable = [d for d in decks if d.get("source_format") in ("pptx", "pdf")]
    if not renderable:
        print("No renderable (pptx/pdf) decks in inventory.", file=sys.stderr)
        return 3

    needs_com = any(d.get("source_format") == "pptx" for d in renderable)
    com_ok = _com_available()
    if needs_com and not com_ok:
        print(
            "PowerPoint COM render is unavailable (needs Windows + PowerPoint + pywin32). "
            "Vision render skipped; structure-only analysis still runs.",
            file=sys.stderr,
        )
        # If there are also PDF decks we can still render those; otherwise bail.
        if not any(d.get("source_format") == "pdf" for d in renderable):
            return 4

    out_root = Path(args.output_dir)
    rendered = 0
    skipped: list[str] = []
    for deck in renderable:
        deck_id = deck.get("deck_id")
        fmt = deck.get("source_format")
        src = resolve_source(inventory, deck)
        if src is None:
            skipped.append(f"{deck_id} (source file not found)")
            continue
        deck_dir = out_root / deck_id
        deck_dir.mkdir(parents=True, exist_ok=True)
        try:
            if fmt == "pptx":
                if not com_ok:
                    skipped.append(f"{deck_id} (pptx, no PowerPoint COM)")
                    continue
                pdf_path = deck_dir / "deck.pdf"
                pptx_to_pdf_com(src.resolve(), pdf_path.resolve())
                n = pdf_to_pngs(pdf_path, deck_dir, args.dpi)
            else:  # pdf
                n = pdf_to_pngs(src, deck_dir, args.dpi)
            rendered += 1
            print(f"rendered deck {deck_id} ({fmt}): {n} slide(s) -> {deck_dir}")
        except Exception as exc:  # one deck failing must not abort the rest
            skipped.append(f"{deck_id} ({fmt}: {type(exc).__name__}: {exc})")

    if skipped:
        print(f"skipped {len(skipped)} deck(s): " + "; ".join(skipped[:8]), file=sys.stderr)
    print(f"render summary: {rendered} deck(s) rendered, {len(skipped)} skipped -> {out_root}")
    return 0 if rendered else 4


if __name__ == "__main__":
    raise SystemExit(main())
