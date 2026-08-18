"""Offline test for merge_validate_analysis.py's understand-mode queue derivation.

Verifies the triage enrichment: every question_queue.jsonl line carries the owning
understanding record's data_category + authoring_component (so presenters can group
and rank questions without joining back to element_understanding.jsonl), and that
answered questions still drop out.

Runs under pytest, OR as a plain script (`python test_merge_validate_analysis.py`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import merge_validate_analysis as mva  # noqa: E402

DECK = "aaaaaaaaaaaa"
EID = f"{DECK}:0:0"


def _build_ws(ws: Path, with_answer: bool = False) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "batches").mkdir()
    inventory = {
        "schema_version": "0.2.0",
        "decks": [{"deck_id": DECK, "filename": "A.pptx",
                   "slides": [{"slide_index": 0, "elements": [
                       {"element_id": EID, "element_index": 0, "kind": "table"}]}]}],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    understanding = {
        "element_id": EID,
        "data_category": "performance",
        "authoring_component": "smart-shell:table",
        "questions": [
            {"question_id": "q1", "facet": "gross-net",
             "question": "Gross or net of fees?", "options": ["gross", "net"],
             "default": None, "why": "disclosure pairing", "blocking": True},
            {"question_id": "q2", "facet": "top-n-vs-full",
             "question": "Top-10 or full?", "options": ["top-10", "full"],
             "default": "top-10", "why": "row count", "blocking": False},
        ],
    }
    (ws / "batches" / "understand_g1.json").write_text(json.dumps(understanding), encoding="utf-8")
    if with_answer:
        (ws / "question_answers.jsonl").write_text(
            json.dumps({"element_id": EID, "question_id": "q1", "answer": "net",
                        "source": "human-answer"}) + "\n", encoding="utf-8")


def _run_understand(ws: Path) -> list[dict]:
    rc = mva.main([
        "--mode", "understand",
        "--inventory", str(ws / "corpus_inventory.json"),
        "--batches-dir", str(ws / "batches"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--answers", str(ws / "question_answers.jsonl"),
    ])
    assert rc == 0
    lines = (ws / "question_queue.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def test_queue_lines_carry_triage_metadata(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_ws(ws)
    queue = _run_understand(ws)
    assert len(queue) == 2
    for q in queue:
        assert q["data_category"] == "performance"
        assert q["authoring_component"] == "smart-shell:table"
        assert q["element_id"] == EID
    assert sum(1 for q in queue if q["blocking"]) == 1


def test_answered_questions_still_drop_out(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_ws(ws, with_answer=True)
    queue = _run_understand(ws)
    assert [q["question_id"] for q in queue] == ["q2"]
    assert queue[0]["data_category"] == "performance"


# ------------------------------------------------- vision provenance (understand)


def _build_vision_ws(ws: Path, *, agent_confirm_q: bool = False, answered: bool = False,
                     provenance: str = "vision", confidence: float = 0.9) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "batches").mkdir()
    inventory = {
        "schema_version": "0.3.0",
        "decks": [{"deck_id": DECK, "filename": "A.pdf",
                   "slides": [{"slide_index": 0, "elements": [
                       {"element_id": EID, "element_index": 0, "kind": "picture"}]}]}],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    # The analyzer's record is keyed by a SYNTHETIC vision-region id — valid only
    # because the registry admits it (grounding guard extension).
    rid = f"{DECK}:0:v0"
    (ws / "vision_regions.json").write_text(json.dumps({"regions": [
        {"element_id": rid, "kind": "vision-region", "deck_id": DECK, "slide_index": 0}
    ]}), encoding="utf-8")
    questions = []
    if agent_confirm_q:
        questions.append({"question_id": "vision-confirm", "facet": "structure-confirm",
                          "question": "agent-emitted confirm", "blocking": False})
    rec = {
        "element_id": rid, "shape": "table", "data_category": "performance",
        "authoring_component": "smart-shell:table", "samples_analyzed": 1,
        "readable": True, "structure_provenance": provenance,
        "verification": "deterministic",  # deliberately wrong: the merge must force it
        "confidence": confidence, "rationale": "Read from render.",
        "vision_source_images": ["crops/g1_0.png"],
        "questions": questions,
    }
    (ws / "batches" / "understand_g1.json").write_text(json.dumps(rec), encoding="utf-8")
    if answered:
        (ws / "question_answers.jsonl").write_text(
            json.dumps({"element_id": rid, "question_id": "vision-confirm",
                        "answer": "Structure is correct as read"}) + "\n", encoding="utf-8")


def _run_understand_vision(ws: Path) -> tuple[list[dict], list[dict]]:
    rc = mva.main([
        "--mode", "understand",
        "--inventory", str(ws / "corpus_inventory.json"),
        "--batches-dir", str(ws / "batches"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--answers", str(ws / "question_answers.jsonl"),
        "--vision-regions", str(ws / "vision_regions.json"),
    ])
    assert rc == 0
    recs = [json.loads(ln) for ln in (ws / "element_understanding.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    queue = [json.loads(ln) for ln in (ws / "question_queue.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    return recs, queue


def test_vision_record_clamped_and_confirm_question_auto_generated(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_vision_ws(ws)
    recs, queue = _run_understand_vision(ws)
    assert len(recs) == 1  # the synthetic id passed the guard via the registry
    rec = recs[0]
    assert rec["verification"] == "needs-confirmation"          # forced, never trusted
    assert rec["confidence"] == mva.VISION_CONFIDENCE_CAP       # clamped below review thr
    confirms = [q for q in queue if q["question_id"] == "vision-confirm"]
    assert len(confirms) == 1
    assert confirms[0]["blocking"] is False
    assert confirms[0]["facet"] == "structure-confirm"
    assert "crops/g1_0.png" in confirms[0]["question"]          # points at the crop


def test_object_model_record_untouched(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_ws(ws)
    _run_understand(ws)
    recs = [json.loads(ln) for ln in (ws / "element_understanding.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    rec = recs[0]
    assert rec["structure_provenance"] == "object-model"        # legacy default stamped
    assert rec["verification"] == "deterministic"
    assert not any(q.get("question_id") == "vision-confirm" for q in rec.get("questions", []))


def test_vision_confirm_dedups_when_answered(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_vision_ws(ws, answered=True)
    _, queue = _run_understand_vision(ws)
    assert not any(q["question_id"] == "vision-confirm" for q in queue)


def test_vision_confirm_not_duplicated_when_agent_emitted(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_vision_ws(ws, agent_confirm_q=True)
    recs, queue = _run_understand_vision(ws)
    assert sum(1 for q in recs[0]["questions"] if q["question_id"] == "vision-confirm") == 1
    assert sum(1 for q in queue if q["question_id"] == "vision-confirm") == 1


# --------------------------------------------------- classify promotion / merge


def _classify_args(ws: Path) -> list[str]:
    return [
        "--mode", "classify",
        "--inventory", str(ws / "corpus_inventory.json"),
        "--batches-dir", str(ws / "batches"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--review-queue", str(ws / "review_queue.jsonl"),
        "--components", str(ws / "slide_components.jsonl"),
        "--vision-regions", str(ws / "vision_regions.json"),
    ]


def _build_classify_ws(ws: Path) -> None:
    """One raster picture + one page text (a flattened component's members) + one
    parsed table (a component whose object-model record must survive the fold)."""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "batches").mkdir()
    pic, txt, tbl = f"{DECK}:0:0", f"{DECK}:0:1", f"{DECK}:0:2"
    inventory = {
        "schema_version": "0.3.0",
        "decks": [{"deck_id": DECK, "filename": "A.pdf",
                   "slides": [{"slide_index": 0, "elements": [
                       {"element_id": pic, "element_index": 0, "kind": "picture",
                        "content": {"likely_chart_image": True, "needs_vision": True}},
                       {"element_id": txt, "element_index": 1, "kind": "text",
                        "content": {"text": "page narrative"}},
                       {"element_id": tbl, "element_index": 2, "kind": "table",
                        "content": {"rows": 2, "cols": 2, "cells": [[{"text": "a"}, {"text": "b"}],
                                                                    [{"text": "1"}, {"text": "2"}]]}},
                   ]}]}],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    classify = [
        {"element_id": pic, "tag": "ambiguous", "data_category": None,
         "authoring_component": "none", "confidence": 0.3, "rationale": "unreadable image"},
        {"element_id": txt, "tag": "static", "data_category": None,
         "authoring_component": "none", "confidence": 0.8, "rationale": "narrative"},
        {"element_id": tbl, "tag": "data-driven-quantitative", "data_category": "holdings",
         "authoring_component": "smart-shell:table", "confidence": 0.8, "rationale": "parsed table"},
    ]
    (ws / "batches" / "classify_a.jsonl").write_text(
        "\n".join(json.dumps(r) for r in classify) + "\n", encoding="utf-8")
    components = [
        # Flattened data component: segmenter ABSTAINED (none) — promotion must default
        # the primitive from component_type and land on the SYNTHETIC id.
        {"component_id": f"{DECK}:s0:c0", "deck_id": DECK, "slide_index": 0,
         "component_type": "table", "label": "Gross Returns",
         "representative_element_id": pic, "member_element_ids": [pic, txt],
         "proposed_authoring_component": "none", "proposed_data_category": "performance",
         "confidence": 0.8, "rationale": "flattened returns table",
         "vision_region_element_id": f"{DECK}:0:v0"},
        # Component anchored on a PARSED table with a NULL vision category — must merge,
        # never overwrite, never null the classifier's category.
        {"component_id": f"{DECK}:s0:c1", "deck_id": DECK, "slide_index": 0,
         "component_type": "table", "label": "Characteristics",
         "representative_element_id": tbl, "member_element_ids": [tbl],
         "proposed_authoring_component": "smart-shell:table", "proposed_data_category": None,
         "confidence": 0.6, "rationale": "parsed table region"},
    ]
    (ws / "slide_components.jsonl").write_text(
        "\n".join(json.dumps(c) for c in components) + "\n", encoding="utf-8")
    (ws / "vision_regions.json").write_text(json.dumps({"regions": [
        {"element_id": f"{DECK}:0:v0", "kind": "vision-region", "deck_id": DECK,
         "slide_index": 0, "content": {"label": "Gross Returns", "component_type": "table"}}
    ]}), encoding="utf-8")


def test_apply_components_promotes_onto_synthetic_with_primitive_default(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_classify_ws(ws)
    assert mva.main(_classify_args(ws)) == 0
    recs = {r["element_id"]: r for r in (
        json.loads(ln) for ln in (ws / "classifications.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    )}
    synth = recs[f"{DECK}:0:v0"]
    assert synth["tag"] == "data-driven-quantitative"
    assert synth["authoring_component"] == "smart-shell:table"   # defaulted from component_type
    assert synth["data_category"] == "performance"
    assert synth["source"] == "slide-vision-component"
    # Members (incl. the old representative) demoted to part-of-component.
    assert recs[f"{DECK}:0:0"]["authoring_component"] == "none"
    assert recs[f"{DECK}:0:0"]["routing_hint"].startswith("part-of-component:")
    assert recs[f"{DECK}:0:1"]["routing_hint"].startswith("part-of-component:")


def test_apply_components_merges_parsed_member_never_overwrites(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _build_classify_ws(ws)
    assert mva.main(_classify_args(ws)) == 0
    recs = {r["element_id"]: r for r in (
        json.loads(ln) for ln in (ws / "classifications.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    )}
    tbl = recs[f"{DECK}:0:2"]
    assert tbl["data_category"] == "holdings"                    # NOT nulled by the vision null
    assert tbl["authoring_component"] == "smart-shell:table"
    assert tbl["confidence"] == 0.8                              # max(existing, component)
    assert tbl["routing_hint"] == f"component:{DECK}:s0:c1"      # hint attached
    assert tbl["source"] == "element-classifier"                 # record survived, not replaced


# ------------------------------------------------------------- segment mode


def test_segment_materializes_regions_and_validates_bboxes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "batches").mkdir()
    pic, tbl = f"{DECK}:0:0", f"{DECK}:0:1"
    w_emu, h_emu = 7772400, 10058400  # US-letter-ish portrait, EMU
    inventory = {
        "schema_version": "0.3.0",
        "decks": [{"deck_id": DECK, "filename": "A.pdf",
                   "metadata": {"slide_width_emu": w_emu, "slide_height_emu": h_emu},
                   "slides": [{"slide_index": 0, "elements": [
                       {"element_id": pic, "element_index": 0, "kind": "picture",
                        "content": {"likely_chart_image": True},
                        "position": {"left_emu": 0, "top_emu": 0, "width_emu": w_emu, "height_emu": h_emu}},
                       {"element_id": tbl, "element_index": 1, "kind": "table",
                        "content": {"cells": [[{"text": "x"}]]},
                        "position": {"left_emu": int(0.1 * w_emu), "top_emu": int(0.1 * h_emu),
                                     "width_emu": int(0.3 * w_emu), "height_emu": int(0.2 * h_emu)}},
                   ]}]}],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    comps = [
        # Two DATA components sharing the page's single picture anchor: each must get
        # its OWN synthetic region (the one-anchor-per-page collision fix).
        {"component_id": f"{DECK}:s0:c0", "deck_id": DECK, "slide_index": 0,
         "component_type": "table", "label": "Gross Returns",
         "representative_element_id": pic, "member_element_ids": [pic],
         "proposed_authoring_component": "smart-shell:table", "proposed_data_category": "performance",
         "bbox": [0.5, 0.3, 0.9, 0.5], "confidence": 0.8, "rationale": "returns table"},
        {"component_id": f"{DECK}:s0:c1", "deck_id": DECK, "slide_index": 0,
         "component_type": "chart", "label": "Sector Distribution",
         "representative_element_id": pic, "member_element_ids": [pic],
         "proposed_authoring_component": "smart-shell:chart", "proposed_data_category": "allocation-exposure",
         "bbox": [0.1, 0.55, 0.45, 0.85], "confidence": 0.7, "rationale": "sector chart"},
        # Component on the PARSED table: no synthetic region (object-model outranks vision),
        # and its far-away bbox must be flagged suspect + nulled.
        {"component_id": f"{DECK}:s0:c2", "deck_id": DECK, "slide_index": 0,
         "component_type": "table", "label": "Characteristics",
         "representative_element_id": tbl, "member_element_ids": [tbl],
         "proposed_authoring_component": "smart-shell:table", "proposed_data_category": "characteristics-risk",
         "bbox": [0.85, 0.85, 1.0, 1.0], "confidence": 0.6, "rationale": "characteristics"},
    ]
    (ws / "batches" / "segment_a.jsonl").write_text(
        "\n".join(json.dumps(c) for c in comps) + "\n", encoding="utf-8")
    rc = mva.main([
        "--mode", "segment",
        "--inventory", str(ws / "corpus_inventory.json"),
        "--batches-dir", str(ws / "batches"),
        "--components", str(ws / "slide_components.jsonl"),
        "--vision-regions", str(ws / "vision_regions.json"),
    ])
    assert rc == 0
    kept = {c["component_id"]: c for c in (
        json.loads(ln) for ln in (ws / "slide_components.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    )}
    regions = json.loads((ws / "vision_regions.json").read_text(encoding="utf-8"))["regions"]

    # Deterministic synthetic ids, one per flattened data component, ordered by component_id.
    assert [r["element_id"] for r in regions] == [f"{DECK}:0:v0", f"{DECK}:0:v1"]
    assert kept[f"{DECK}:s0:c0"]["vision_region_element_id"] == f"{DECK}:0:v0"
    assert kept[f"{DECK}:s0:c1"]["vision_region_element_id"] == f"{DECK}:0:v1"
    assert regions[0]["kind"] == "vision-region"
    # Region position derives from the bbox (EMU over the page box).
    assert regions[0]["position"]["left_emu"] == int(0.5 * w_emu)
    # The parsed-table component got NO region...
    assert "vision_region_element_id" not in kept[f"{DECK}:s0:c2"]
    # ...and its mismatched bbox was nulled + flagged (containment < threshold).
    assert kept[f"{DECK}:s0:c2"]["bbox_suspect"] is True
    assert kept[f"{DECK}:s0:c2"]["bbox"] is None
    # Full-page anchors trivially contain their bboxes -> not suspect, bbox kept.
    assert kept[f"{DECK}:s0:c0"]["bbox_suspect"] is False
    assert kept[f"{DECK}:s0:c0"]["bbox"] == [0.5, 0.3, 0.9, 0.5]


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} merge_validate_analysis test(s) passed.")


if __name__ == "__main__":
    _run_all()
