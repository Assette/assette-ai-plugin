#!/usr/bin/env python3
"""Dump every Assette smart tag in a .pptx — UNTRUNCATED.

The fabricator skill ships ``assette_smartpage.scripts.extract_tags``, but it
truncates long values (e.g. ADVANCESETTINGS, ROWSMAPPING, SMARTLABELS) to keep
its console output readable. When you need to *clone* an existing shell — copy
its exact ADVANCESETTINGS / DISPLAYSETTINGS / COLUMNSMAPPING etc. — you need the
full values. This script reads the .pptx zip directly and emits the complete tag
set for the presentation, every slide (slide-level ``custDataLst`` hangs off
``<p:cSld>``, not ``<p:sld>`` — a place the naive reader misses), and every
tagged shape.

``--json`` emits a structured document you can pipe into other tooling;
``--shell NAME`` narrows shape output to shells whose SHELLNAME contains NAME.

Usage:
    python -m assette_mcp_shim.scripts.pptx_extract_tags --pptx "SmartPages/X/X.pptx"
    python -m assette_mcp_shim.scripts.pptx_extract_tags --pptx <f> --json
    python -m assette_mcp_shim.scripts.pptx_extract_tags --pptx <f> --shell "Region"

Read-only. Exit 0 on success, 2 on usage error.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _tagfile_values(zf, part):
    root = etree.fromstring(zf.read(part))
    return {t.get("name"): t.get("val") for t in root.findall(f"{{{P}}}tag")}


def _rels_for(zf, part):
    """Map rId -> tag part path for a slide/presentation part's .rels."""
    d = Path(part)
    rels_part = f"{d.parent.as_posix()}/_rels/{d.name}.rels"
    out = {}
    if rels_part not in zf.namelist():
        return out
    base = d.parent.as_posix()  # directory the part lives in, targets are relative to it
    for rel in etree.fromstring(zf.read(rels_part)):
        if (rel.get("Type") or "").endswith("/tags"):
            tgt = posixpath.normpath(posixpath.join(base, rel.get("Target")))
            out[rel.get("Id")] = tgt
    return out


def _collect(zf, part, owner, rels):
    """Collect tags from every <p:tags> under an owner element's custDataLst."""
    cdl = owner.find(f"{{{P}}}custDataLst")
    tags = {}
    if cdl is not None:
        for t in cdl.findall(f"{{{P}}}tags"):
            rid = t.get(f"{{{R}}}id")
            if rid in rels:
                tags.update(_tagfile_values(zf, rels[rid]))
    return tags


def extract(pptx: Path):
    zf = zipfile.ZipFile(str(pptx))
    names = zf.namelist()
    doc = {"presentation": {}, "slides": []}

    # Presentation-level tags.
    if "ppt/presentation.xml" in names:
        rels = _rels_for(zf, "ppt/presentation.xml")
        root = etree.fromstring(zf.read("ppt/presentation.xml"))
        doc["presentation"] = _collect(zf, "ppt/presentation.xml", root, rels)

    slide_parts = sorted(
        [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)],
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )
    for part in slide_parts:
        rels = _rels_for(zf, part)
        root = etree.fromstring(zf.read(part))
        csld = root.find(f"{{{P}}}cSld")
        slide_tags = _collect(zf, part, csld if csld is not None else root, rels)
        shapes = []
        for owner_tag in (f"{{{P}}}sp", f"{{{P}}}pic", f"{{{P}}}graphicFrame"):
            for shp in root.iter(owner_tag):
                nv = shp.find(f".//{{{P}}}nvPr")
                if nv is None:
                    continue
                tags = _collect(zf, part, nv, rels)
                if not tags:
                    continue
                cnvpr = shp.find(f".//{{{P}}}cNvPr")
                shapes.append({
                    "shapeName": cnvpr.get("name") if cnvpr is not None else "",
                    "kind": owner_tag.split("}")[-1],
                    "tags": tags,
                })
        doc["slides"].append({"part": part, "slideTags": slide_tags, "shapes": shapes})
    zf.close()
    return doc


def _print_text(doc, shell_filter):
    print("[presentation]")
    for k, v in doc["presentation"].items():
        print(f"  {k} = {v}")
    for i, s in enumerate(doc["slides"], 1):
        print(f"\n[slide {i}] ({s['part']})")
        for k, v in s["slideTags"].items():
            print(f"  {k} = {v}")
        for shp in s["shapes"]:
            name = shp["tags"].get("SHELLNAME", shp["shapeName"])
            if shell_filter and shell_filter.lower() not in str(name).lower():
                continue
            print(f"  - {shp['kind']} '{shp['shapeName']}'  (SHELLNAME={shp['tags'].get('SHELLNAME')})")
            for k, v in shp["tags"].items():
                print(f"      {k} = {v}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dump all Assette smart tags in a .pptx, untruncated.")
    p.add_argument("--pptx", required=True, help="Path to the .pptx.")
    p.add_argument("--json", action="store_true", help="Emit a structured JSON document.")
    p.add_argument("--shell", help="Only show shapes whose SHELLNAME contains this substring (text mode).")
    args = p.parse_args(argv)

    pptx = Path(args.pptx)
    if not pptx.is_file():
        print(f"error: file not found: {pptx}", file=sys.stderr)
        return 2

    doc = extract(pptx)
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        _print_text(doc, args.shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
