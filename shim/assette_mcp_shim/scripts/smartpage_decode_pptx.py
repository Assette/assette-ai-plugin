#!/usr/bin/env python3
"""Decode the base64 .pptx (and metadata JSON) out of a get_smart_page response.

Why this exists: ``mcp__assette__get_smart_page`` with ``outputDir`` set is
*supposed* to write the .pptx to disk and replace the binary with a
``{Path, Bytes, Sha256}`` summary. In practice the shim only externalizes the
top-level ``metadataJson`` field — the .pptx stays inline as base64 at
``GetQuries.content.Item.Contents.Data`` (the batch response nests it one level
deeper than the shim's externalizer looks). When that happens the raw response
overflows the model context and the harness spills it to
``tool-results/<id>.txt``. This script recovers the .pptx from that spilled file
(or any saved get_smart_page JSON) WITHOUT routing the megabytes back through
the model.

It walks the response for the first ``Contents.Data`` base64 blob, decodes it,
and writes ``<out-dir>/<stem>.pptx``. With ``--metadata`` it also writes the
companion metadata JSON if the response carries it inline.

Usage:
    python -m assette_mcp_shim.scripts.smartpage_decode_pptx \
        --file tool-results/mcp-assette-get_smart_page-XXXX.txt \
        --out-dir "SmartPages/My Page" --stem "My Page"

Exit code 0 on success, 2 on usage / decode error.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path


def _find_first(obj, key):
    """Depth-first search for the first value under ``key`` anywhere in obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur and isinstance(cur[key], str) and cur[key]:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _find_contents_data(obj):
    """Find Item.Contents.Data base64 — the .pptx payload — anywhere in obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            contents = cur.get("Contents")
            if isinstance(contents, dict) and isinstance(contents.get("Data"), str):
                return contents["Data"]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def load_response(path: Path):
    """Return the parsed response. Handles the {body: "<json string>"} wrapper
    the shim/harness emit, and the doubly-wrapped tool-result spill files."""
    text = path.read_text(encoding="utf-8")
    obj = json.loads(text)
    # Unwrap a string-encoded body one or more times.
    for _ in range(3):
        if isinstance(obj, dict) and isinstance(obj.get("body"), str):
            try:
                obj = json.loads(obj["body"])
                continue
            except json.JSONDecodeError:
                break
        break
    return obj


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Decode the .pptx out of a get_smart_page response file.")
    p.add_argument("--file", required=True, help="Saved get_smart_page response (response_cache or tool-results spill).")
    p.add_argument("--out-dir", required=True, help="Directory to write the .pptx (and metadata) into.")
    p.add_argument("--stem", required=True, help="Filename stem, e.g. the Smart Page name.")
    p.add_argument("--metadata", action="store_true", help="Also write <stem>.metadata.json if inline in the response.")
    args = p.parse_args(argv)

    src = Path(args.file)
    if not src.is_file():
        print(f"error: file not found: {src}", file=sys.stderr)
        return 2
    try:
        obj = load_response(src)
    except json.JSONDecodeError as exc:
        print(f"error: {src} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    data_b64 = _find_contents_data(obj)
    if not data_b64:
        print("error: no Contents.Data base64 found — was the response fetched with includePptx=true?", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = out_dir / f"{args.stem}.pptx"
    raw = base64.b64decode(data_b64)
    pptx_path.write_bytes(raw)
    print(f"wrote {pptx_path} ({len(raw)} bytes)")

    if args.metadata:
        meta = _find_first(obj, "metadataJson")
        if isinstance(meta, str) and meta.strip():
            meta_path = out_dir / f"{args.stem}.metadata.json"
            # Pretty-print if it parses; otherwise write verbatim.
            try:
                meta_path.write_text(json.dumps(json.loads(meta), indent=1, ensure_ascii=False), encoding="utf-8")
            except json.JSONDecodeError:
                meta_path.write_text(meta, encoding="utf-8")
            print(f"wrote {meta_path}")
        else:
            print("note: no inline metadataJson found (it may already be on disk via outputDir).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
