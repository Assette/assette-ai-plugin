"""Offline test for group_table_chart_samples.py — the Phase 2c admission gate + grouping.

Covers the baseline classifier gate (previously untested), the WIDENED vision admission
(a data component admits its anchor even when the classifier said none/ambiguous), the
anchor-only rule (no duplicate groups per faux-chart fragment), the object-model-outranks-
vision rule, the date-stripped label signature (monthly factsheet variants group), the
vision block on member records, and group_id stability.

Runs under pytest, OR as a plain script (`python test_group_table_chart_samples.py`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import group_table_chart_samples as gtcs  # noqa: E402

DECK_A = "aaaaaaaaaaaa"
DECK_B = "bbbbbbbbbbbb"


def _inventory() -> dict:
    def deck(deck_id: str, filename: str) -> dict:
        return {
            "deck_id": deck_id, "filename": filename,
            "metadata": {"slide_width_emu": 7772400, "slide_height_emu": 10058400},
            "slides": [{"slide_index": 0, "layout_name": "page-1", "elements": [
                {"element_id": f"{deck_id}:0:0", "element_index": 0, "kind": "picture",
                 "content": {"likely_chart_image": True, "needs_vision": True},
                 "position": {"left_emu": 0, "top_emu": 0, "width_emu": 7772400, "height_emu": 10058400}},
                {"element_id": f"{deck_id}:0:1", "element_index": 1, "kind": "table",
                 "content": {"rows": 1, "cols": 2, "has_header": True,
                             "cells": [[{"text": "Security"}, {"text": "Weight"}]]},
                 "position": {"left_emu": 100, "top_emu": 100, "width_emu": 1000, "height_emu": 1000}},
            ]}],
        }
    return {"schema_version": "0.3.0", "decks": [deck(DECK_A, "A.pdf"), deck(DECK_B, "B.pdf")]}


def _component(deck_id: str, n: int, *, ctype: str = "table", label: str = "",
               rep: str | None = None, members: list[str] | None = None,
               proposed_ac: str = "none", proposed_dc: str | None = None,
               bbox: list[float] | None = None, synthetic: str | None = None) -> dict:
    comp = {
        "component_id": f"{deck_id}:s0:c{n}", "deck_id": deck_id, "slide_index": 0,
        "component_type": ctype, "label": label,
        "representative_element_id": rep or f"{deck_id}:0:0",
        "member_element_ids": members or [f"{deck_id}:0:0"],
        "proposed_authoring_component": proposed_ac,
        "proposed_data_category": proposed_dc,
        "confidence": 0.8, "rationale": "a region the eye sees on the page",
        "bbox": bbox,
    }
    if synthetic:
        comp["vision_region_element_id"] = synthetic
    return comp


def _region(deck_id: str, n: int, label: str = "") -> dict:
    return {"element_id": f"{deck_id}:0:v{n}", "kind": "vision-region", "deck_id": deck_id,
            "slide_index": 0, "position": {"left_emu": 0, "top_emu": 0, "width_emu": 100, "height_emu": 100},
            "content": {"label": label, "component_type": "table", "needs_vision": True}}


def _run(ws: Path, inventory: dict, classifications: list[dict], components: list[dict],
         regions: list[dict]) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    (ws / "classifications.jsonl").write_text(
        "\n".join(json.dumps(r) for r in classifications) + "\n", encoding="utf-8")
    (ws / "slide_components.jsonl").write_text(
        "\n".join(json.dumps(c) for c in components) + ("\n" if components else ""), encoding="utf-8")
    (ws / "vision_regions.json").write_text(json.dumps({"regions": regions}), encoding="utf-8")
    out = ws / "table_chart_groups.json"
    rc = gtcs.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--components", str(ws / "slide_components.jsonl"),
        "--vision-regions", str(ws / "vision_regions.json"),
        "--output", str(out),
    ])
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _cls(eid: str, tag: str, dc, ac: str, conf: float = 0.8) -> dict:
    return {"element_id": eid, "tag": tag, "data_category": dc, "authoring_component": ac,
            "confidence": conf, "rationale": "t", "timestamp": "2026-07-01T00:00:00Z"}


def test_classifier_tagged_elements_grouped(tmp_path: Path) -> None:
    """Baseline: the classic classifier gate admits parsed tables and groups twins."""
    out = _run(tmp_path / "ws", _inventory(), [
        _cls(f"{DECK_A}:0:1", "data-driven-quantitative", "holdings", "smart-shell:table"),
        _cls(f"{DECK_B}:0:1", "data-driven-quantitative", "holdings", "smart-shell:table"),
    ], [], [])
    assert out["schema_version"] == "0.2.0"
    assert out["group_count"] == 1
    grp = out["groups"][0]
    assert grp["sample_count"] == 2                      # identical headers -> one group
    assert grp["authoring_component"] == "smart-shell:table"


def test_vision_component_admits_unclassified_anchor_with_primitive_default(tmp_path: Path) -> None:
    """The widened gate: a data component whose classifier verdict was none/ambiguous is
    admitted via its synthetic anchor, primitive defaulted from component_type."""
    synthetic = f"{DECK_A}:0:v0"
    out = _run(tmp_path / "ws", _inventory(), [
        _cls(f"{DECK_A}:0:0", "ambiguous", None, "none", 0.3),   # classifier abstained
    ], [
        _component(DECK_A, 0, label="Gross Returns", proposed_ac="none",
                   proposed_dc="performance", bbox=[0.5, 0.3, 0.9, 0.5], synthetic=synthetic),
    ], [_region(DECK_A, 0, "Gross Returns")])
    assert out["group_count"] == 1
    grp = out["groups"][0]
    assert grp["representative_element_id"] == synthetic
    assert grp["authoring_component"] == "smart-shell:table"     # defaulted from "table"
    assert grp["data_category"] == "performance"
    assert grp["label"] == "Gross Returns"
    # The member carries its vision block for the slicer.
    assert grp["members"][0]["vision"]["component_id"] == f"{DECK_A}:s0:c0"
    assert grp["members"][0]["vision"]["bbox"] == [0.5, 0.3, 0.9, 0.5]


def test_non_data_component_type_not_admitted(tmp_path: Path) -> None:
    out = _run(tmp_path / "ws", _inventory(), [
        _cls(f"{DECK_A}:0:0", "ambiguous", None, "none", 0.3),
    ], [
        _component(DECK_A, 0, ctype="text-block", label="Narrative"),
    ], [])
    assert out["group_count"] == 0


def test_non_anchor_members_not_admitted(tmp_path: Path) -> None:
    """Only the component's PREFERRED anchor is admitted — when a synthetic region
    exists, the old representative (a covered member) stays out, so a component never
    mints two groups."""
    synthetic = f"{DECK_A}:0:v0"
    out = _run(tmp_path / "ws", _inventory(), [], [
        _component(DECK_A, 0, label="Gross Returns", proposed_dc="performance",
                   members=[f"{DECK_A}:0:0"],
                   bbox=[0.5, 0.3, 0.9, 0.5], synthetic=synthetic),
    ], [_region(DECK_A, 0, "Gross Returns")])
    assert out["group_count"] == 1
    grp = out["groups"][0]
    assert grp["representative_element_id"] == synthetic
    member_ids = [m["element_id"] for m in grp["members"]]
    assert member_ids == [synthetic]          # the rep picture was NOT admitted too


def test_object_model_outranks_vision_no_duplicate(tmp_path: Path) -> None:
    """A component with a parsed member never mints a vision-keyed duplicate: the parsed
    element (classifier-admitted) is the analysis unit; the synthetic anchor is skipped."""
    synthetic = f"{DECK_A}:0:v0"
    out = _run(tmp_path / "ws", _inventory(), [
        _cls(f"{DECK_A}:0:1", "data-driven-quantitative", "holdings", "smart-shell:table"),
    ], [
        _component(DECK_A, 0, label="Holdings", rep=f"{DECK_A}:0:1",
                   members=[f"{DECK_A}:0:1"], proposed_ac="smart-shell:table",
                   proposed_dc="holdings", bbox=[0.0, 0.0, 0.5, 0.5], synthetic=synthetic),
    ], [_region(DECK_A, 0, "Holdings")])
    assert out["group_count"] == 1
    grp = out["groups"][0]
    assert grp["representative_element_id"] == f"{DECK_A}:0:1"   # the parsed element
    assert grp["sample_count"] == 1                              # no synthetic duplicate
    # Parsed elements keep the STRUCTURAL signature but still carry the vision block.
    assert grp["members"][0]["vision"]["label"] == "Holdings"


def test_date_stripped_labels_group_monthly_variants(tmp_path: Path) -> None:
    """'... as of June 30, 2025' and '... as of March 31, 2025' must land in ONE group —
    date-bearing headings would otherwise fragment monthly factsheets."""
    out = _run(tmp_path / "ws", _inventory(), [], [
        _component(DECK_A, 0, label="Sector Weightings (%) as of June 30, 2025",
                   proposed_dc="allocation-exposure", bbox=[0.1, 0.1, 0.9, 0.5],
                   synthetic=f"{DECK_A}:0:v0"),
        _component(DECK_B, 0, label="Sector Weightings (%) as of March 31, 2025",
                   proposed_dc="allocation-exposure", bbox=[0.1, 0.1, 0.9, 0.5],
                   synthetic=f"{DECK_B}:0:v0"),
    ], [_region(DECK_A, 0), _region(DECK_B, 0)])
    assert out["group_count"] == 1
    assert out["groups"][0]["sample_count"] == 2


def test_group_ids_stable_across_runs(tmp_path: Path) -> None:
    inventory = _inventory()
    cls = [_cls(f"{DECK_A}:0:1", "data-driven-quantitative", "holdings", "smart-shell:table")]
    out1 = _run(tmp_path / "ws1", inventory, cls, [], [])
    out2 = _run(tmp_path / "ws2", inventory, cls, [], [])
    assert [g["group_id"] for g in out1["groups"]] == [g["group_id"] for g in out2["groups"]]


def test_unlabeled_vision_component_keys_on_position(tmp_path: Path) -> None:
    """No label -> the fallback key (type, category, slide, quantized bbox) still admits
    and groups deterministically instead of colliding on an empty string."""
    out = _run(tmp_path / "ws", _inventory(), [], [
        _component(DECK_A, 0, label="", proposed_dc="performance",
                   bbox=[0.52, 0.31, 0.88, 0.52], synthetic=f"{DECK_A}:0:v0"),
        _component(DECK_B, 0, label="", proposed_dc="performance",
                   bbox=[0.50, 0.30, 0.90, 0.50], synthetic=f"{DECK_B}:0:v0"),
    ], [_region(DECK_A, 0), _region(DECK_B, 0)])
    # Same 0.25-grid cell -> one group across the two same-template decks.
    assert out["group_count"] == 1
    assert out["groups"][0]["sample_count"] == 2


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} group_table_chart_samples test(s) passed.")


if __name__ == "__main__":
    _run_all()
