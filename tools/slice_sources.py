"""Slice source_catalog.json into lean per-source payloads (+ the bind elements payload).

Mirror of slice_payloads.py for the source side: a wide multi-schema catalog can balloon
past the Read token cap, so the source-binding agent must read ONE lean per-source
payload, never the monolith. Deterministic, stdlib-only.

Each payload keeps the binding-relevant facts (table/column names, types, keys, value-format
hints) and trims sample_values to a few — both for size and because real sample rows may carry
client PII (treat the catalog as session-scoped working state).

When --understanding points at an existing element_understanding.jsonl, this also writes
bind_elements.json — the DECK side of the binding join: every readable data-driven element
with its resolved columns (and chart series flattened in as pseudo-columns). The
source-binding-analyzer agent reads that file plus one per-source payload.

CLI:
    py tools/slice_sources.py --catalog workspace/source_catalog.json \
        --understanding workspace/element_understanding.jsonl \
        --output-dir workspace/source_payloads
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SAMPLE_CAP = 3            # sample values kept per column in a lean payload
SIZE_WARN_BYTES = 80_000  # ~20K tokens — same guard slice_payloads.py uses


def lean_column(col: dict[str, Any]) -> dict[str, Any]:
    return {
        "column_id": col.get("column_id"),
        "name": col.get("name"),
        "data_type": col.get("data_type"),
        "data_type_bucket": col.get("data_type_bucket"),
        "nullable": col.get("nullable"),
        "is_pk": col.get("is_pk"),
        "fk_ref": col.get("fk_ref"),
        "inferred_value_format": col.get("inferred_value_format"),
        "sample_values": (col.get("sample_values") or [])[:SAMPLE_CAP],
    }


def lean_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source.get("source_id"),
        "filename": source.get("filename"),
        "source_format": source.get("source_format"),
        "parsed_as": source.get("parsed_as"),
        "source_family_hint": source.get("source_family_hint"),
        "tables": [
            {
                "table_id": t.get("table_id"),
                "name": t.get("name"),
                "schema": t.get("schema"),
                "columns": [lean_column(c) for c in t.get("columns") or []],
            }
            for t in source.get("tables") or []
        ],
    }


def binding_columns_for(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The deck-side columns of one understanding record, chart series flattened in as
    pseudo-columns. Shared with merge_validate_analysis --mode bind (total-column counts)."""
    cols: list[dict[str, Any]] = []
    table = rec.get("table") or {}
    for c in table.get("columns") or []:
        cols.append({"name": c.get("name"), "role": c.get("role"), "value_format": c.get("value_format")})
    chart = rec.get("chart") or {}
    for s in chart.get("series") or []:
        cols.append({
            "name": s.get("name") or "(unnamed series)",
            "role": f"series:{s.get('role') or 'unknown'}",
            "value_format": chart.get("y_units") or "unknown",
        })
    if chart:
        cols.append({"name": "(categories)", "role": "dimension", "value_format": chart.get("category_axis") or "unknown"})
    return cols


def lean_binding_elements(understanding: list[dict[str, Any]]) -> dict[str, Any]:
    """The deck side of the binding join: every readable data-driven element + its
    columns. Vision-derived structures (readable:true, provenance vision|mixed) flow
    through identically — the provenance rides along so the binding report can label
    their columns "vision-derived — confirm"; records without the field are legacy
    object-model reads."""
    elements: list[dict[str, Any]] = []
    for rec in understanding:
        if not rec.get("readable"):
            continue
        cols = binding_columns_for(rec)
        if not cols:
            continue
        elements.append({
            "element_id": rec.get("element_id"),
            "data_category": rec.get("data_category"),
            "shape": rec.get("shape"),
            "structure_provenance": rec.get("structure_provenance") or "object-model",
            "columns": cols,
        })
    return {"element_count": len(elements), "elements": elements}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slice source_catalog.json into lean per-source payloads.")
    parser.add_argument("--catalog", default="workspace/source_catalog.json")
    parser.add_argument("--understanding", default="workspace/element_understanding.jsonl")
    parser.add_argument("--output-dir", default="workspace/source_payloads")
    args = parser.parse_args(argv)

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print(f"Catalog not found: {catalog_path} -- run ingest_sources.py first.", file=sys.stderr)
        return 2

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    big: list[str] = []
    for source in catalog.get("sources", []):
        sid = source.get("source_id")
        if not sid:
            continue
        payload = lean_source(source)
        path = out_dir / f"source_{sid}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        size = path.stat().st_size
        count += 1
        flag = "  large" if size > SIZE_WARN_BYTES else ""
        print(f"  source_{sid}.json: {len(payload['tables'])} table(s), ~{size // 1024} KB{flag}")
        if size > SIZE_WARN_BYTES:
            big.append(path.name)
    print(f"source payloads: {count} written -> {out_dir}")
    if big:
        print(f"  {len(big)} payload(s) exceed ~20K tokens — a very wide source may need per-table splitting: {big}", file=sys.stderr)

    # Deck-side elements payload for the binding agents (only when Phase 2c has run).
    upath = Path(args.understanding)
    if upath.exists():
        payload = lean_binding_elements(read_jsonl(upath))
        epath = out_dir / "bind_elements.json"
        epath.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"bind elements payload: {payload['element_count']} element(s) -> {epath}")
    else:
        print(f"  no element understanding at {upath} — bind_elements.json not written (run /analyze-deck Phase 2c first).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
