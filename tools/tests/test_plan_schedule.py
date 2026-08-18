"""Offline test for the Part 3 build scheduler (plan_schedule.py).

Hand-crafts a small build_plan.json (two chains — a complex 'holdings' chain and a light
'performance' chain — feeding a page, plus a footnote blocked on a question), schedules it, and
asserts the effort model, the critical path, both makespans, gate-wait handling, the CSV export,
and the resolve-question -> unblock behaviour. Runs under pytest OR as a plain script.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import plan_schedule  # noqa: E402


def _node(node_id, kind, stage, deps, gates, complexity, blocking=None):
    return {
        "node_id": node_id, "kind": kind, "stage": stage, "title": node_id,
        "depends_on": deps, "gates": gates, "status": "ready",
        "open_blocking_questions": blocking or [],
        "impact": {"score": 1.0, "factors": {"complexity": complexity}},
    }


def build_plan() -> dict:
    # build_order MUST be topological (deps before dependents); stages: block 1, footnote 1,
    # object 2, shell 3, page 4.
    return {
        "schema_version": "0.1.0",
        "build_order": [
            _node("block:h", "data-block", 1, [], ["source-family", "publish"], 4),
            _node("block:p", "data-block", 1, [], ["source-family", "publish"], 1),
            _node("footnote:f", "footnote", 1, [], ["reuse-first", "publish"], 0, blocking=["x:q1"]),
            _node("object:h", "data-object", 2, ["block:h"], ["publish"], 4),
            _node("object:p", "data-object", 2, ["block:p"], ["publish"], 1),
            _node("shell:h", "smart-shell", 3, ["object:h"], ["publish"], 4),
            _node("shell:p", "smart-shell", 3, ["object:p"], ["publish"], 1),
            _node("page:s", "smart-page", 4, ["shell:h", "shell:p"], ["composition", "publish"], 2),
        ],
    }


def _by_id(result: dict) -> dict:
    return {r["node_id"]: r for r in result["scheduled"]}


def test_schedule() -> None:
    plan = build_plan()
    profile = plan_schedule.load_profile(None)  # defaults
    result = plan_schedule.compute_schedule(plan, profile, concurrency=2, answered=set())

    # --- blocked footnote excluded; the seven buildable nodes scheduled ---
    assert result["scheduled_count"] == 7
    assert result["blocked_count"] == 1
    assert result["blocked"][0]["node_id"] == "footnote:f"

    # --- AI preset is the default: agent-hours build effort, gate SLAs dominate ---
    assert profile["unit"] == "hours"
    base = profile["base_effort"]
    cw = profile["complexity_weight"]
    by = _by_id(result)
    assert by["block:h"]["effort"] == round(base["data-block"] * (1 + cw * 4), 2)   # 2.0h
    assert by["block:p"]["effort"] == round(base["data-block"] * (1 + cw * 1), 2)   # 1.25h
    assert by["page:s"]["effort"] == round(base["smart-page"] * (1 + cw * 2), 2)    # 0.75h

    # --- gate waits (human SLAs): block = source-family(2)+publish(4)=6h ; page = composition+publish=6h ---
    assert by["block:h"]["gate_wait"] == 6.0
    assert by["object:h"]["gate_wait"] == 4.0
    assert by["page:s"]["gate_wait"] == 6.0
    # gate wait dominates agent build effort — the AI-implementation property
    assert by["block:h"]["gate_wait"] > by["block:h"]["effort"]

    # --- critical path is the complex holdings chain -> page (in topo order) ---
    assert result["critical_path"] == ["block:h", "object:h", "shell:h", "page:s"]
    assert by["shell:p"]["critical"] is False

    # --- makespans: min-possible == wall-clock here (2 lanes cover the parallel branch) ---
    assert result["makespan_infinite"] == 25.75
    assert result["makespan"] == 25.75

    # --- the manual preset still carries human-implementer numbers (days) ---
    manual = plan_schedule.load_profile(None, preset="manual")
    assert manual["unit"] == "days"
    m = plan_schedule.compute_schedule(plan, manual, concurrency=2, answered=set())
    assert m["makespan_infinite"] == 23.8

    # --- schedule respects dependency availability (a node starts >= each dep's available time) ---
    for row in result["scheduled"]:
        for dep in row["depends_on"]:
            if dep in by:
                assert row["start"] >= by[dep]["available"] - 1e-9, f"{row['node_id']} starts before {dep} available"


def test_resolve_question_unblocks() -> None:
    plan = build_plan()
    profile = plan_schedule.load_profile(None)
    result = plan_schedule.compute_schedule(plan, profile, concurrency=2, answered={"x:q1"})
    assert result["blocked_count"] == 0
    assert result["scheduled_count"] == 8
    assert any(r["node_id"] == "footnote:f" for r in result["scheduled"])


def test_render_and_cli(tmp_path: Path) -> None:
    plan = build_plan()
    plan_path = tmp_path / "build_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    md = tmp_path / "build_timeline.md"
    csv = tmp_path / "build_timeline.csv"
    rc = plan_schedule.main(["--plan", str(plan_path), "--output", str(md), "--csv", str(csv), "--concurrency", "2"])
    assert rc == 0

    text = md.read_text(encoding="utf-8")
    assert "```mermaid" in text and "gantt" in text
    assert "## Critical path" in text and "## Schedule" in text
    assert "## Blocked / unscheduled" in text and "footnote:f" in text
    assert "crit," in text  # at least one critical task tagged in the mermaid gantt

    csv_lines = [l for l in csv.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert csv_lines[0].startswith("Task,Kind,Module,Shared,Effort")
    assert len(csv_lines) == 1 + 7  # header + 7 scheduled nodes
    assert any(";" in l and "block:h" in l for l in csv_lines)  # predecessors joined with ';'


def test_module_rollup_and_columns(tmp_path: Path) -> None:
    """Nodes carrying modules render the rollup section, Module column, and shared marker."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = build_plan()
    for n in plan["build_order"]:
        n["modules"] = ["Factsheets", "Pitchbooks"] if n["node_id"].endswith(":h") else ["Factsheets"]
        n["shared_across_modules"] = n["node_id"].endswith(":h")
    plan["summary"] = {"scoped_to_module": None}
    plan_path = tmp_path / "build_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    md = tmp_path / "t.md"
    csvp = tmp_path / "t.csv"
    rc = plan_schedule.main(["--plan", str(plan_path), "--output", str(md), "--csv", str(csvp)])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    assert "## Module rollup" in text
    assert "- **Modules:** Factsheets, Pitchbooks" in text
    assert "Factsheets + Pitchbooks (shared)" in text  # the shared chain's Module cell
    csv_text = csvp.read_text(encoding="utf-8")
    assert "Factsheets;Pitchbooks,yes" in csv_text  # Module + Shared CSV columns


if __name__ == "__main__":
    test_schedule()
    test_resolve_question_unblocks()
    tmp = Path(tempfile.mkdtemp(prefix="plan_schedule_test_"))
    try:
        test_render_and_cli(tmp)
        test_module_rollup_and_columns(tmp / "mod")
        print("\nALL ASSERTIONS PASSED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
