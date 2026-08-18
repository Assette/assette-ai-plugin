"""Offline test for the Phase 2d binding layer (merge_validate --mode bind + slice_sources
bind-elements extraction).

Hand-crafts an element_understanding.jsonl, a source_catalog.json (two sources), and two
agent batch files (bind_*.jsonl) containing good candidates PLUS a fabricated column_id, a
fabricated element_id, and a wrong table name — then asserts the grounding guard drops the
fabrications, names are canonicalized from the catalog, candidates merge + rank across
sources, and the resolution rules (confirm >= 0.7 / clarify / derived) hold.

Runs under pytest OR as a plain script.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import merge_validate_analysis  # noqa: E402
import slice_sources  # noqa: E402

EID = "aaaaaaaaaaaa:0:0"
MEMBER = "bbbbbbbbbbbb:0:0"
SRC_DB = "c1c1c1c1c1c1"   # INFORMATION_SCHEMA-style source (database family)
SRC_FILE = "d2d2d2d2d2d2"  # CSV data-file source (file family)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def build_workspace(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    _write_jsonl(ws / "element_understanding.jsonl", [{
        "element_id": EID, "member_element_ids": [EID, MEMBER],
        "shape": "table", "data_category": "holdings", "authoring_component": "smart-shell:table",
        "samples_analyzed": 2, "readable": True, "confidence": 0.85, "rationale": "Holdings.",
        "table": {
            "orientation": "rows-are-records",
            "columns": [
                {"name": "Security", "role": "row-key", "value_format": "text", "growth": "fixed"},
                {"name": "Weight", "role": "measure", "value_format": "percent", "growth": "fixed"},
                {"name": "Excess Return", "role": "derived", "value_format": "percent", "growth": "fixed"},
            ],
            "row_model": {"kind": "repeating-group"}, "volume": "bounded",
        },
        "questions": [],
    }])

    def col(sid, t, c, name, dtype, bucket, fmt):
        return {"column_id": f"{sid}:{t}:{c}", "name": name, "ordinal": c, "data_type": dtype,
                "data_type_bucket": bucket, "nullable": None, "is_pk": False, "fk_ref": None,
                "sample_values": [], "inferred_value_format": fmt}

    catalog = {
        "schema_version": "0.1.0",
        "catalog": {"source_count": 2},
        "sources": [
            {"source_id": SRC_DB, "filename": "warehouse_dump.csv", "source_format": "csv",
             "parsed_as": "information-schema", "source_family_hint": "database", "table_count": 1,
             "tables": [{"table_id": f"{SRC_DB}:0", "name": "HOLDINGS", "schema": "PUBLIC", "row_sample_count": 0,
                         "columns": [col(SRC_DB, 0, 0, "SECURITY_NAME", "VARCHAR", "text", "text"),
                                     col(SRC_DB, 0, 1, "WEIGHT_PCT", "NUMBER", "number", "percent")]}]},
            {"source_id": SRC_FILE, "filename": "positions.csv", "source_format": "csv",
             "parsed_as": "data-file", "source_family_hint": "file", "table_count": 1,
             "tables": [{"table_id": f"{SRC_FILE}:0", "name": "positions", "schema": None, "row_sample_count": 2,
                         "columns": [col(SRC_FILE, 0, 0, "SEC", None, "text", "text"),
                                     col(SRC_FILE, 0, 1, "WGT", None, "number", "percent")]}]},
        ],
    }
    (ws / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    # Agent for the DB source: strong matches; one candidate carries a WRONG table name
    # (must be canonicalized from the catalog) and one candidate is fabricated (dropped).
    _write_jsonl(ws / "batches" / f"bind_{SRC_DB}.jsonl", [{
        "element_id": EID, "source_id": SRC_DB,
        "bindings": [
            {"deck_column": "Security", "candidates": [
                {"column_id": f"{SRC_DB}:0:0", "table": "HOLDINGZ", "column": "SECURITY_NAME",
                 "match_signal": "synonym+category", "score": 0.85}], "resolution": "confirm"},
            {"deck_column": "Weight", "candidates": [
                {"column_id": f"{SRC_DB}:0:1", "table": "HOLDINGS", "column": "WEIGHT_PCT",
                 "match_signal": "abbreviation+format", "score": 0.86},
                {"column_id": "ffffffffffff:9:9", "table": "GHOST", "column": "FAKE",
                 "match_signal": "fabricated", "score": 0.99}], "resolution": "confirm"},
            {"deck_column": "Excess Return", "candidates": [], "resolution": "derived",
             "note": "Computed column."},
        ],
        "confidence": 0.85, "rationale": "Maps onto HOLDINGS cleanly.",
    }])

    # Agent for the FILE source: weaker matches, plus a record for a fabricated element (dropped).
    _write_jsonl(ws / "batches" / f"bind_{SRC_FILE}.jsonl", [
        {"element_id": EID, "source_id": SRC_FILE,
         "bindings": [
             {"deck_column": "Weight", "candidates": [
                 {"column_id": f"{SRC_FILE}:0:1", "table": "positions", "column": "WGT",
                  "match_signal": "abbreviation", "score": 0.4}], "resolution": "clarify"},
         ],
         "confidence": 0.4, "rationale": "Weak abbreviation match only."},
        {"element_id": "eeeeeeeeeeee:1:1", "source_id": SRC_FILE,
         "bindings": [{"deck_column": "X", "candidates": [], "resolution": "no-match"}],
         "confidence": 0.2, "rationale": "Fabricated element record."},
    ])


def test_bind_merge(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    out = ws / "source_bindings.jsonl"
    rc = merge_validate_analysis.main([
        "--mode", "bind",
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--catalog", str(ws / "source_catalog.json"),
        "--batches-dir", str(ws / "batches"),
        "--bindings", str(out),
    ])
    assert rc == 0
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]

    # --- grounding guard: the fabricated element never reaches the canonical file ---
    assert len(records) == 1
    rec = records[0]
    assert rec["element_id"] == EID
    assert "eeeeeeeeeeee" not in json.dumps(records)

    by_col = {b["deck_column"]: b for b in rec["bindings"]}

    # --- fabricated candidate dropped; real candidates merged ACROSS sources + ranked ---
    weight = by_col["Weight"]
    ids = [c["column_id"] for c in weight["candidates"]]
    assert "ffffffffffff:9:9" not in ids
    assert ids == [f"{SRC_DB}:0:1", f"{SRC_FILE}:0:1"]          # sorted by score desc (0.86, 0.4)
    assert weight["resolution"] == "confirm"                     # top score >= 0.7

    # --- canonicalization: the agent's wrong table name is repaired from the catalog ---
    security = by_col["Security"]
    assert security["candidates"][0]["table"] == "HOLDINGS"      # agent wrote HOLDINGZ
    assert security["candidates"][0]["source_file"] == "warehouse_dump.csv"
    assert security["resolution"] == "confirm"

    # --- derived stays derived, no candidates forced ---
    assert by_col["Excess Return"]["resolution"] == "derived"
    assert by_col["Excess Return"]["candidates"] == []

    # --- rollups + family proposal (both confirms come from the database source) ---
    assert rec["bound_columns"] == 2
    assert rec["total_columns"] == 3
    assert rec["source_family_proposal"] == "database"
    assert rec["data_category"] == "holdings"


def test_bind_elements_extraction() -> None:
    understanding = [
        {"element_id": EID, "readable": True, "data_category": "holdings", "shape": "table",
         "table": {"columns": [{"name": "Security", "role": "row-key", "value_format": "text"}]}},
        {"element_id": "cccccccccccc:1:0", "readable": True, "data_category": "performance", "shape": "chart",
         "chart": {"y_units": "percent", "category_axis": "time",
                   "series": [{"name": "Strategy", "role": "portfolio"}]}},
        {"element_id": "dddddddddddd:2:0", "readable": False},  # unreadable -> excluded
    ]
    payload = slice_sources.lean_binding_elements(understanding)
    assert payload["element_count"] == 2
    chart_el = payload["elements"][1]
    names = [c["name"] for c in chart_el["columns"]]
    assert names == ["Strategy", "(categories)"]                # series + category axis as pseudo-columns
    assert chart_el["columns"][0]["role"] == "series:portfolio"
    assert chart_el["columns"][0]["value_format"] == "percent"


def test_bind_elements_carry_provenance() -> None:
    """Vision-derived structures flow to binding with their provenance riding along;
    legacy records without the field default to object-model; unreadable still excluded."""
    understanding = [
        {"element_id": EID, "readable": True, "data_category": "performance", "shape": "table",
         "structure_provenance": "vision",
         "table": {"columns": [{"name": "MTD", "role": "measure", "value_format": "percent"}]}},
        {"element_id": "cccccccccccc:1:0", "readable": True, "data_category": "holdings", "shape": "table",
         "table": {"columns": [{"name": "Security", "role": "row-key", "value_format": "text"}]}},
        {"element_id": "dddddddddddd:2:0", "readable": False, "structure_provenance": "vision"},
    ]
    payload = slice_sources.lean_binding_elements(understanding)
    assert payload["element_count"] == 2
    by_id = {e["element_id"]: e for e in payload["elements"]}
    assert by_id[EID]["structure_provenance"] == "vision"
    assert by_id["cccccccccccc:1:0"]["structure_provenance"] == "object-model"  # legacy default
    # Vision columns are shape-identical to object-model ones — binding treats them alike.
    assert by_id[EID]["columns"] == [{"name": "MTD", "role": "measure", "value_format": "percent"}]


def test_bind_merge_carries_provenance(tmp_path: Path) -> None:
    """source_bindings.jsonl records carry the understanding's structure_provenance
    (defaulting to object-model for legacy records)."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    out = ws / "source_bindings.jsonl"
    args = [
        "--mode", "bind",
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--catalog", str(ws / "source_catalog.json"),
        "--batches-dir", str(ws / "batches"),
        "--bindings", str(out),
    ]
    # Legacy record (no provenance field) -> object-model.
    assert merge_validate_analysis.main(args) == 0
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["structure_provenance"] == "object-model"
    # Vision record -> vision.
    u_path = ws / "element_understanding.jsonl"
    u_rec = json.loads(u_path.read_text(encoding="utf-8").splitlines()[0])
    u_rec["structure_provenance"] = "vision"
    u_rec["verification"] = "needs-confirmation"
    u_path.write_text(json.dumps(u_rec) + "\n", encoding="utf-8")
    assert merge_validate_analysis.main(args) == 0
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["structure_provenance"] == "vision"


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="bind_sources_test_"))
    try:
        test_bind_merge(tmp / "t1")
        test_bind_elements_extraction()
        test_bind_elements_carry_provenance()
        test_bind_merge_carries_provenance(tmp / "t2")
        print("\nALL ASSERTIONS PASSED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
