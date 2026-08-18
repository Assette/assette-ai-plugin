"""Offline test for the Phase 0 build planner (plan_build.py).

Builds a tiny SYNTHETIC /analyze-deck workspace (no tenant, no LLM) covering the cases the
planner must get right — cross-deck dedup, the Block->Object->Shell->Page DAG, impact ranking,
the grounding guard, the blocking-question gate — runs plan_build.main(), and asserts on the
emitted build_plan.json.

Runs under pytest, OR as a plain script (`python test_plan_build.py`) so it can be verified
without pytest installed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import plan_build  # noqa: E402

DECK_A = "aaaaaaaaaaaa"
DECK_B = "bbbbbbbbbbbb"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def build_workspace(ws: Path) -> None:
    """Write a synthetic finished workspace: 2 decks, a deduped holdings table, a chart, a
    footnote, a fixed logo, a parameter, plus one fabricated id to exercise the grounding guard."""
    ws.mkdir(parents=True, exist_ok=True)

    def el(deck: str, slide: int, idx: int, kind: str) -> dict:
        return {"element_id": f"{deck}:{slide}:{idx}", "element_index": idx, "kind": kind}

    inventory = {
        "schema_version": "0.2.0",
        "decks": [
            {
                "deck_id": DECK_A, "filename": "A.pptx",
                "slides": [{"slide_index": 0, "elements": [
                    el(DECK_A, 0, 0, "table"),   # holdings
                    el(DECK_A, 0, 1, "chart"),   # performance chart
                    el(DECK_A, 0, 2, "text"),    # footnote
                    el(DECK_A, 0, 3, "picture"), # logo (fixed-content)
                    el(DECK_A, 0, 4, "text"),    # parameter
                ]}],
            },
            {
                "deck_id": DECK_B, "filename": "B.pptx",
                "slides": [{"slide_index": 0, "elements": [
                    el(DECK_B, 0, 0, "table"),   # holdings (dup of A's -> groups)
                ]}],
            },
        ],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")

    _write_jsonl(ws / "classifications.jsonl", [
        {"element_id": f"{DECK_A}:0:0", "tag": "data-driven-quantitative", "data_category": "holdings", "authoring_component": "smart-shell:table", "confidence": 0.9, "rationale": "Holdings table.", "timestamp": "2026-06-30T00:00:00Z"},
        {"element_id": f"{DECK_B}:0:0", "tag": "data-driven-quantitative", "data_category": "holdings", "authoring_component": "smart-shell:table", "confidence": 0.9, "rationale": "Holdings table.", "timestamp": "2026-06-30T00:00:00Z"},
        {"element_id": f"{DECK_A}:0:1", "tag": "data-driven-quantitative", "data_category": "performance", "authoring_component": "smart-shell:chart", "confidence": 0.85, "rationale": "Performance chart.", "timestamp": "2026-06-30T00:00:00Z"},
        {"element_id": f"{DECK_A}:0:2", "tag": "static", "data_category": None, "authoring_component": "footnote", "confidence": 0.8, "rationale": "Footnote text.", "timestamp": "2026-06-30T00:00:00Z"},
        {"element_id": f"{DECK_A}:0:3", "tag": "static", "data_category": None, "authoring_component": "fixed-content", "confidence": 0.95, "rationale": "Firm logo.", "timestamp": "2026-06-30T00:00:00Z"},
        {"element_id": f"{DECK_A}:0:4", "tag": "parameterized", "data_category": None, "authoring_component": "parameter", "confidence": 0.8, "rationale": "As-of date.", "timestamp": "2026-06-30T00:00:00Z"},
        # Fabricated element_id -> must be dropped by the grounding guard, never planned.
        {"element_id": "ffffffffffff:9:9", "tag": "data-driven-quantitative", "data_category": "holdings", "authoring_component": "smart-shell:table", "confidence": 0.7, "rationale": "Hallucinated.", "timestamp": "2026-06-30T00:00:00Z"},
    ])

    groups = {
        "schema_version": "0.1.0", "groups": [
            {"group_id": "tcg_holdings", "shape": "table", "label": None, "data_category": "holdings",
             "authoring_component": "smart-shell:table", "sample_count": 2,
             "representative_element_id": f"{DECK_A}:0:0",
             "members": [
                 {"element_id": f"{DECK_A}:0:0", "deck_id": DECK_A, "filename": "A.pptx", "slide_index": 0},
                 {"element_id": f"{DECK_B}:0:0", "deck_id": DECK_B, "filename": "B.pptx", "slide_index": 0},
             ]},
            {"group_id": "tcg_chart", "shape": "chart", "label": None, "data_category": "performance",
             "authoring_component": "smart-shell:chart", "sample_count": 1,
             "representative_element_id": f"{DECK_A}:0:1",
             "members": [{"element_id": f"{DECK_A}:0:1", "deck_id": DECK_A, "filename": "A.pptx", "slide_index": 0}]},
        ],
    }
    (ws / "table_chart_groups.json").write_text(json.dumps(groups), encoding="utf-8")

    _write_jsonl(ws / "element_understanding.jsonl", [
        {"element_id": f"{DECK_A}:0:0", "member_element_ids": [f"{DECK_A}:0:0", f"{DECK_B}:0:0"],
         "shape": "table", "data_category": "holdings", "authoring_component": "smart-shell:table",
         "samples_analyzed": 2, "readable": True, "confidence": 0.8, "rationale": "Holdings.",
         "table": {"orientation": "rows-are-records",
                   "columns": [{"name": "Security", "role": "row-key", "value_format": "text", "growth": "fixed"},
                               {"name": "Weight", "role": "measure", "value_format": "percent", "growth": "grows-per-entity"}],
                   "row_model": {"kind": "repeating-group", "observed_row_count_max": 30, "special_rows": [{"role": "total"}]},
                   "conditional_formatting": [{"trigger": "sign-negative", "effect": "sign-paren", "evidence": "observed"}],
                   "volume": "bounded"},
         "system_block_candidates": ["Sectors", "Currency Codes"], "dynamic_field_candidates": ["As of Date"],
         "questions": [
             {"question_id": "q1", "facet": "top-n-vs-full", "question": "Top-N or full list?", "options": ["Top-N display", "Full list"], "default": "Top-N display", "why": "Shell type.", "blocking": True},
             {"question_id": "q2", "facet": "units", "question": "Percent or bps?", "blocking": False},
         ]},
        {"element_id": f"{DECK_A}:0:1", "shape": "chart", "data_category": "performance",
         "authoring_component": "smart-shell:chart", "samples_analyzed": 1, "readable": True,
         "confidence": 0.75, "rationale": "Chart.",
         "chart": {"chart_type": "LINE", "category_axis": "time", "series": [{"name": "Portfolio", "role": "portfolio"}]},
         "system_block_candidates": [], "dynamic_field_candidates": [], "questions": []},
    ])

    _write_jsonl(ws / "question_queue.jsonl", [
        {"element_id": f"{DECK_A}:0:0", "question_id": "q1", "facet": "top-n-vs-full", "question": "Top-N or full list?", "options": ["Top-N display", "Full list"], "default": "Top-N display", "why": "Shell type.", "blocking": True},
        {"element_id": f"{DECK_A}:0:0", "question_id": "q2", "facet": "units", "question": "Percent or bps?", "blocking": False},
        {"element_id": "ffffffffffff:9:9", "question_id": "q1", "facet": "source", "question": "fabricated", "blocking": True},
    ])


def _run(ws: Path) -> dict:
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0, f"plan_build returned {rc}"
    return json.loads(out.read_text(encoding="utf-8"))


def _nodes_by_id(plan: dict) -> dict:
    return {n["node_id"]: n for n in plan["build_order"]}


def test_build_plan(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    plan = _run(ws)
    nodes = _nodes_by_id(plan)

    # --- node inventory ---
    assert plan["summary"]["by_kind"] == {
        "data-block": 2, "data-object": 2, "smart-shell": 2, "footnote": 1, "smart-page": 2
    }, plan["summary"]["by_kind"]
    assert plan["summary"]["elements_not_planned"] == {"fixed-content": 1, "parameter": 1}
    assert plan["summary"]["deduped_from_groups"] == 1  # holdings group (sample_count 2)

    # --- dedup: holdings is ONE unit carrying both element_ids ---
    hblock = nodes["block:tcg_holdings"]
    assert set(hblock["element_ids"]) == {f"{DECK_A}:0:0", f"{DECK_B}:0:0"}
    assert hblock["impact"]["factors"]["frequency"] == 2
    assert hblock["impact"]["factors"]["deck_spread"] == 2

    # --- DAG / topo order: every dep appears earlier AND at a lower stage ---
    order_index = {n["node_id"]: i for i, n in enumerate(plan["build_order"])}
    for n in plan["build_order"]:
        for dep in n["depends_on"]:
            assert dep in nodes, f"{n['node_id']} depends on unknown {dep}"
            assert nodes[dep]["stage"] < n["stage"], f"{dep} not upstream of {n['node_id']}"
            assert order_index[dep] < order_index[n["node_id"]], f"{dep} ordered after {n['node_id']}"

    # --- one shell feeds many pages (downstream leverage) ---
    assert hblock["impact"]["factors"]["downstream_unblock"] == 4   # object, shell, 2 pages
    cblock = nodes["block:tcg_chart"]
    assert cblock["impact"]["factors"]["downstream_unblock"] == 3   # object, shell, 1 page

    # --- impact ranking: high-frequency high-leverage holdings outranks the chart ---
    assert hblock["impact"]["score"] > cblock["impact"]["score"]
    # score is computed from its own factors by the documented formula
    w = plan_build.DEFAULT_WEIGHTS
    f = hblock["impact"]["factors"]
    expected = (w["frequency"] * f["frequency"] + w["deck_spread"] * f["deck_spread"]
                + w["downstream_unblock"] * f["downstream_unblock"] + w["criticality"] * f["criticality"]
                - w["complexity"] * f["complexity"] + w["reuse_discount"] * f["reuse_discount"])
    assert hblock["impact"]["score"] == round(expected, 3)
    # complexity (5) + reuse (3) transcribed from the understanding record
    assert f["complexity"] == 5 and f["reuse_discount"] == 3

    # --- blocking-question gate ---
    for nid in ("block:tcg_holdings", "object:tcg_holdings", "shell:tcg_holdings"):
        assert nodes[nid]["status"] == "blocked-on-question", nid
        assert f"{DECK_A}:0:0:q1" in nodes[nid]["open_blocking_questions"]
    assert cblock["status"] == "needs-confirmation"  # source-family gate, no blocking q

    # --- up-front blocking-question batch: only the real, blocking question ---
    batch = plan["blocking_question_batch"]
    assert len(batch) == 1
    assert batch[0]["element_id"] == f"{DECK_A}:0:0" and batch[0]["question_id"] == "q1"
    assert batch[0]["blocks_node_impact"] > 0

    # --- grounding guard: the fabricated id is dropped and never planned ---
    assert "ffffffffffff:9:9" in plan["dropped_unknown_ids"]
    assert plan["summary"]["dropped_unknown_id_count"] >= 1
    blob = json.dumps(plan["build_order"])
    assert "ffffffffffff" not in blob


def test_bindings_attach_to_block_nodes(tmp_path: Path) -> None:
    """When /bind-sources has produced source_bindings.jsonl, the bound unit's DATA BLOCK node
    carries a proposed_source (the ASK -> CONFIRM flip); unbound units are untouched."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _write_jsonl(ws / "source_bindings.jsonl", [{
        "element_id": f"{DECK_A}:0:0",  # the holdings group's representative
        "data_category": "holdings",
        "source_family_proposal": "database",
        "total_columns": 3, "bound_columns": 2, "clarify_columns": 0,
        "bindings": [
            {"deck_column": "Security", "resolution": "confirm", "candidates": [
                {"column_id": "c1c1c1c1c1c1:0:0", "table": "HOLDINGS", "column": "SECURITY_NAME",
                 "source_id": "c1c1c1c1c1c1", "source_file": "dump.csv", "source_family": "database",
                 "match_signal": "synonym", "score": 0.85}]},
            {"deck_column": "Weight", "resolution": "confirm", "candidates": [
                {"column_id": "c1c1c1c1c1c1:0:1", "table": "HOLDINGS", "column": "WEIGHT_PCT",
                 "source_id": "c1c1c1c1c1c1", "source_file": "dump.csv", "source_family": "database",
                 "match_signal": "abbreviation+format", "score": 0.86}]},
            {"deck_column": "Excess Return", "resolution": "derived", "candidates": []},
        ],
    }])
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--bindings", str(ws / "source_bindings.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    nodes = _nodes_by_id(plan)

    ps = nodes["block:tcg_holdings"].get("proposed_source")
    assert ps is not None
    assert ps["source_family"] == "database"
    assert ps["columns_bound"] == 2 and ps["columns_total"] == 3
    assert ps["bindings"] == {"Security": "HOLDINGS.SECURITY_NAME", "Weight": "HOLDINGS.WEIGHT_PCT"}
    assert ps["source_files"] == ["dump.csv"]

    # the un-bound chart block gets no proposal; only block nodes carry it
    assert "proposed_source" not in nodes["block:tcg_chart"]
    assert "proposed_source" not in nodes["object:tcg_holdings"]
    assert plan["summary"]["blocks_with_source_proposal"] == 1


def test_file_family_staging_and_chain(tmp_path: Path) -> None:
    """FILE-family bindings: the block node gains the staging gate + the chain shape, and
    two deck columns bound to the SAME source column triggers the pivot reshape hint."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _write_jsonl(ws / "source_bindings.jsonl", [{
        "element_id": f"{DECK_A}:0:0",  # the holdings group's representative
        "data_category": "holdings",
        "source_family_proposal": "file",
        "total_columns": 3, "bound_columns": 2, "clarify_columns": 0,
        "bindings": [
            # Two deck columns whose TOP candidate is the SAME source column -> pivot suspected.
            {"deck_column": "2024", "resolution": "confirm", "candidates": [
                {"column_id": "e0e0e0e0e0e0:0:3", "table": "annual_performance", "column": "composite_gross_pct",
                 "source_id": "e0e0e0e0e0e0", "source_file": "annual_performance.csv", "source_family": "file",
                 "match_signal": "category+format", "score": 0.75}]},
            {"deck_column": "2025", "resolution": "confirm", "candidates": [
                {"column_id": "e0e0e0e0e0e0:0:3", "table": "annual_performance", "column": "composite_gross_pct",
                 "source_id": "e0e0e0e0e0e0", "source_file": "annual_performance.csv", "source_family": "file",
                 "match_signal": "category+format", "score": 0.75}]},
            {"deck_column": "Strategy", "resolution": "confirm", "candidates": [
                {"column_id": "e0e0e0e0e0e0:0:0", "table": "annual_performance", "column": "strategy",
                 "source_id": "e0e0e0e0e0e0", "source_file": "annual_performance.csv", "source_family": "file",
                 "match_signal": "synonym", "score": 0.8}]},
        ],
    }])
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--bindings", str(ws / "source_bindings.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    nodes = _nodes_by_id(plan)

    blk = nodes["block:tcg_holdings"]
    ps = blk["proposed_source"]
    assert ps["source_family"] == "file"
    assert ps["staging"].startswith("content-service")
    assert ps["chain"] == ["content-service-read", "reader", "transform"]
    assert ps["reshape_hint"] == "pivot-suspected"
    assert blk["gates"] == ["source-family", "staging", "publish"]
    assert blk["title"].endswith("(file chain)")
    assert plan["summary"]["file_blocks_staging_unconfirmed"] == 1

    # non-file nodes untouched: the chart block keeps the standard gates, no staging field
    assert nodes["block:tcg_chart"]["gates"] == ["source-family", "publish"]
    assert "proposed_source" not in nodes["block:tcg_chart"]
    assert plan["schema_version"] == "0.2.0"


def test_database_family_gains_no_staging(tmp_path: Path) -> None:
    """Regression: a database-family binding must NOT get the staging gate / chain."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _write_jsonl(ws / "source_bindings.jsonl", [{
        "element_id": f"{DECK_A}:0:0", "data_category": "holdings",
        "source_family_proposal": "database",
        "total_columns": 3, "bound_columns": 1, "clarify_columns": 0,
        "bindings": [
            {"deck_column": "Security", "resolution": "confirm", "candidates": [
                {"column_id": "c1c1c1c1c1c1:0:0", "table": "HOLDINGS", "column": "SECURITY_NAME",
                 "source_id": "c1c1c1c1c1c1", "source_file": "dump.csv", "source_family": "database",
                 "match_signal": "synonym", "score": 0.85}]},
        ],
    }])
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--bindings", str(ws / "source_bindings.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    nodes = _nodes_by_id(plan)
    ps = nodes["block:tcg_holdings"]["proposed_source"]
    assert "staging" not in ps and "chain" not in ps and "reshape_hint" not in ps
    assert nodes["block:tcg_holdings"]["gates"] == ["source-family", "publish"]
    assert plan["summary"]["file_blocks_staging_unconfirmed"] == 0


def _set_module_folders(ws: Path) -> None:
    """Give deck A/B relative_paths under per-module corpus folders."""
    inv = json.loads((ws / "corpus_inventory.json").read_text(encoding="utf-8"))
    inv["decks"][0]["relative_path"] = "Factsheets/A.pptx"
    inv["decks"][1]["relative_path"] = "Pitchbooks\\B.pptx"  # windows separator on purpose
    (ws / "corpus_inventory.json").write_text(json.dumps(inv), encoding="utf-8")


def test_modules_from_folders_and_shared_flag(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _set_module_folders(ws)
    plan = _run(ws)
    s = plan["summary"]
    assert s["modules"] == ["Factsheets", "Pitchbooks"]
    assert s["scoped_to_module"] is None
    # The cross-deck holdings unit serves BOTH modules -> shared chain.
    holdings_block = next(n for n in plan["build_order"]
                          if n["kind"] == "data-block" and n["data_category"] == "holdings")
    assert holdings_block["modules"] == ["Factsheets", "Pitchbooks"]
    assert holdings_block["shared_across_modules"] is True
    assert s["shared_node_count"] >= 3  # block + object + shell of the shared unit
    # Page nodes belong to their own deck's module only.
    pages = {n["node_id"]: n for n in plan["build_order"] if n["kind"] == "smart-page"}
    assert pages[f"page:{DECK_A}:0"]["modules"] == ["Factsheets"]
    assert pages[f"page:{DECK_B}:0"]["modules"] == ["Pitchbooks"]
    # Batch entries carry the question element's home module; rollups count it there.
    assert plan["blocking_question_batch"][0]["module"] == "Factsheets"
    assert s["by_module"]["Factsheets"]["blocking_question_count"] == 1
    assert s["by_module"]["Pitchbooks"]["blocking_question_count"] == 0


def test_module_scoping_keeps_shared_chain(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _set_module_folders(ws)
    out = ws / "scoped_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--module", "pitchbooks",  # case-insensitive
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    s = plan["summary"]
    assert s["scoped_to_module"] == "Pitchbooks"
    kinds = {n["node_id"] for n in plan["build_order"]}
    # Kept: the shared holdings chain + Pitchbooks' own page. Dropped: everything
    # Factsheets-only (chart chain, footnote, page:A).
    assert f"page:{DECK_B}:0" in kinds
    assert f"page:{DECK_A}:0" not in kinds
    assert all("performance" != n.get("data_category") for n in plan["build_order"])
    assert {n["kind"] for n in plan["build_order"]} == {"data-block", "data-object", "smart-shell", "smart-page"}
    # The shared chain's blocking question stays - it gates a node Pitchbooks needs.
    assert len(plan["blocking_question_batch"]) == 1


def test_modules_json_override(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _set_module_folders(ws)
    (ws / "modules.json").write_text(json.dumps({"B.pptx": "Factsheets"}), encoding="utf-8")
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--modules-file", str(ws / "modules.json"),
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["summary"]["modules"] == ["Factsheets"]
    assert plan["summary"]["shared_node_count"] == 0


def test_unknown_module_exits_2(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    build_workspace(ws)
    _set_module_folders(ws)
    try:
        plan_build.main([
            "--inventory", str(ws / "corpus_inventory.json"),
            "--classifications", str(ws / "classifications.jsonl"),
            "--understanding", str(ws / "element_understanding.jsonl"),
            "--groups", str(ws / "table_chart_groups.json"),
            "--question-queue", str(ws / "question_queue.jsonl"),
            "--module", "nope",
            "--output", str(ws / "x.json"),
        ])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unknown --module must exit 2")


def test_missing_classifications_returns_2(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "corpus_inventory.json").write_text('{"decks": []}', encoding="utf-8")
    rc = plan_build.main(["--inventory", str(ws / "corpus_inventory.json"),
                          "--classifications", str(ws / "nope.jsonl"),
                          "--output", str(ws / "build_plan.json")])
    assert rc == 2


def test_default_parameters_stamped_on_block_nodes(tmp_path: Path) -> None:
    """Every Data Block node carries the platform parameter convention (exact
    AsofDate spelling — lowercase 'o'), per data_category."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    plan = _run(ws)
    blocks = [n for n in plan["build_order"] if n["kind"] == "data-block"]
    assert blocks
    for n in blocks:  # fixture categories: holdings + performance -> account-scoped
        assert n["default_parameters"] == ["AccountCode", "AsofDate"]
    # Category branches (pure function).
    assert plan_build._default_parameters("transactions") == ["AccountCode", "FromDate", "ToDate"]
    assert plan_build._default_parameters("cash-flows") == ["AccountCode", "FromDate", "ToDate"]
    assert plan_build._default_parameters("reference-other") == ["AsofDate"]
    assert plan_build._default_parameters("personnel") == ["AsofDate"]
    assert plan_build._default_parameters(None) == ["AccountCode", "AsofDate"]
    # Non-block nodes never carry it.
    assert all("default_parameters" not in n for n in plan["build_order"] if n["kind"] != "data-block")


def test_structure_verification_gate(tmp_path: Path) -> None:
    """A vision-derived understanding (verification: needs-confirmation) gives its whole
    chain the structure-verification gate -> status needs-confirmation, never blocking;
    other chains and the blocking-question gate are unaffected."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    # Make the CHART's understanding vision-derived; the holdings table keeps its
    # blocking question (which must still win the status precedence on ITS chain).
    recs = [json.loads(ln) for ln in (ws / "element_understanding.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    for rec in recs:
        if rec["element_id"] == f"{DECK_A}:0:1":
            rec["structure_provenance"] = "vision"
            rec["verification"] = "needs-confirmation"
    _write_jsonl(ws / "element_understanding.jsonl", recs)
    plan = _run(ws)
    nodes = _nodes_by_id(plan)

    # Whole chart chain: structure-verification gate + needs-confirmation (not blocked).
    for nid, n in nodes.items():
        if n.get("data_category") == "performance" and n["kind"] in ("data-block", "data-object", "smart-shell"):
            assert "structure-verification" in n["gates"], nid
            assert n["status"] == "needs-confirmation", nid
    # The holdings chain is untouched by the gate and still blocked on its question.
    for nid, n in nodes.items():
        if n.get("data_category") == "holdings" and n["kind"] in ("data-block", "data-object", "smart-shell"):
            assert "structure-verification" not in n["gates"], nid
            assert n["status"] == "blocked-on-question", nid
    assert plan["summary"]["vision_structures_needing_confirmation"] == 1


def test_vision_region_questions_survive_grounding(tmp_path: Path) -> None:
    """Questions on synthetic vision-region ids pass the planner's grounding guard when
    the registry is supplied — without it they'd be dropped as fabricated."""
    ws = tmp_path / "workspace"
    build_workspace(ws)
    rid = f"{DECK_A}:0:v0"
    (ws / "vision_regions.json").write_text(json.dumps({"regions": [
        {"element_id": rid, "kind": "vision-region", "deck_id": DECK_A, "slide_index": 0}
    ]}), encoding="utf-8")
    # A blocking question keyed on the synthetic id.
    queue = [json.loads(ln) for ln in (ws / "question_queue.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    queue.append({"element_id": rid, "question_id": "q9", "facet": "dynamic-columns",
                  "question": "Do periods grow?", "blocking": True})
    _write_jsonl(ws / "question_queue.jsonl", queue)
    out = ws / "build_plan.json"
    rc = plan_build.main([
        "--inventory", str(ws / "corpus_inventory.json"),
        "--classifications", str(ws / "classifications.jsonl"),
        "--understanding", str(ws / "element_understanding.jsonl"),
        "--vision-regions", str(ws / "vision_regions.json"),
        "--groups", str(ws / "table_chart_groups.json"),
        "--question-queue", str(ws / "question_queue.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    batch_ids = {(b["element_id"], b["question_id"]) for b in plan["blocking_question_batch"]}
    assert (rid, "q9") in batch_ids          # survived: the registry made it a real id
    assert not any(e.startswith("ffffffffffff") for e, _ in batch_ids)  # guard still drops fabrications


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="plan_build_test_"))
    try:
        test_build_plan(tmp)
        test_bindings_attach_to_block_nodes(tmp / "bind")
        test_file_family_staging_and_chain(tmp / "file")
        test_database_family_gains_no_staging(tmp / "db")
        test_modules_from_folders_and_shared_flag(tmp / "mod")
        test_module_scoping_keeps_shared_chain(tmp / "scope")
        test_modules_json_override(tmp / "ovr")
        test_unknown_module_exits_2(tmp / "unk")
        test_missing_classifications_returns_2(tmp / "b")
        test_default_parameters_stamped_on_block_nodes(tmp / "dp")
        test_structure_verification_gate(tmp / "sv")
        test_vision_region_questions_survive_grounding(tmp / "vr")
        print("\nALL ASSERTIONS PASSED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
