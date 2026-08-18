"""Part 3 — BUILD SCHEDULER / timeline generator for the build orchestrator.

Deterministic, no-LLM, fully-offline pass that turns the `build_plan.json` a `plan_build.py`
run emits into a reviewable IMPLEMENTATION TIMELINE — a Gantt chart, a critical path, a
schedule table, and PM-adjacent registers — so a human can sign off on the plan before any
building starts (see docs/build-orchestrator-design.md, Part 3). It BUILDS NOTHING, calls no
MCP tool, and needs no tenant.

The plan is already a DAG (nodes + depends_on + stage + gates + a complexity signal); the one
thing it lacks is DURATION. This tool supplies it from a per-node effort model
(base_effort[kind] * (1 + complexity_weight * complexity)). THE DEFAULT PRESET ASSUMES
AI-BASED IMPLEMENTATION — the /build-deck conductor + authoring skills do the building
(minutes-to-hours per artifact) and humans appear only at the gates, so the wall-clock is
dominated by human gate-turnaround SLAs (publish approval, source-family / composition
confirmation), not build effort. Use --preset manual (or a firm effort_profile.json, which
deep-merges over the preset) for human-implementer estimates. Then it runs two deterministic
schedules:

  * a DEPENDENCY-ONLY (infinite-resource) pass -> the CRITICAL PATH and the minimum possible
    duration, and
  * a RESOURCE-CONSTRAINED pass under a stated concurrency (N implementers) -> the wall-clock
    calendar a PM actually reviews.

Gates are WAITS, not work: a publish/approval gate (and a source-family / composition confirm)
inserts human-turnaround latency after a node's build effort, delaying its dependents — modeled
by a settable gate SLA. Nodes still blocked on an unanswered question are NOT scheduled; they are
listed as gated. Answer the questions (build_answers.jsonl) and re-run to schedule them.

CLI:
  py tools/plan_schedule.py \
      --plan workspace/build_plan.json \
      --answers workspace/build_answers.jsonl \
      --profile workspace/effort_profile.json \
      --concurrency 2 \
      --output workspace/build_timeline.md \
      --csv workspace/build_timeline.csv
"""

from __future__ import annotations

import argparse
import csv as csvmod
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

from _inventory_common import now_iso

# Effort presets. The DEFAULT is the AI-BASED implementation the orchestrator is designed
# around: the agent does the building (draft -> preview -> verify, minutes-to-hours per
# artifact), and humans appear only at gates — so the calendar is dominated by human
# gate-turnaround SLAs, NOT build effort. The `manual` preset keeps human-implementer
# numbers (days) for firms building by hand. A firm's effort_profile.json is deep-merged
# over the chosen preset. Estimates are PROVISIONAL — calibrate against one real build.
PRESET_PROFILES: dict[str, dict[str, Any]] = {
    "ai": {
        "unit": "hours",
        "concurrency": 1,  # one conductor session builds node-by-node; gates overlap
        "base_effort": {
            # Agent build effort per artifact (draft + preview + verify iterations).
            "data-block": 1.0,
            "data-object": 0.75,
            "smart-shell": 0.75,
            "smart-page": 0.5,
            "footnote": 0.25,
            "disclosure": 0.25,
        },
        # Each complexity point buys extra preview/iteration cycles for the agent.
        "complexity_weight": 0.25,
        # Gate SLAs (hours): HUMAN turnaround — publish approval ~half a business day,
        # source-family / composition confirmations ~2h, file staging (content type +
        # naming pattern + upload) ~2h. These dominate the wall-clock.
        "gate_sla": {"publish": 4.0, "source-family": 2.0, "composition": 2.0, "staging": 2.0, "reuse-first": 0.0},
    },
    "manual": {
        "unit": "days",
        "concurrency": 2,
        "base_effort": {
            "data-block": 3.0,
            "data-object": 2.0,
            "smart-shell": 2.0,
            "smart-page": 1.5,
            "footnote": 1.0,
            "disclosure": 0.5,
        },
        "complexity_weight": 0.3,
        "gate_sla": {"publish": 1.0, "source-family": 1.0, "composition": 1.0, "staging": 1.0, "reuse-first": 0.0},
    },
}
DEFAULT_PROFILE: dict[str, Any] = PRESET_PROFILES["ai"]

# Mermaid gantt needs a concrete calendar; day offsets are added to this fixed base (a Monday).
_GANTT_BASE = dt.date(2026, 1, 5)
# Fixed section order for the gantt / WBS.
_KIND_ORDER = ["data-block", "data-object", "smart-shell", "smart-page", "footnote", "disclosure"]
_KIND_SECTION = {
    "data-block": "Data Blocks",
    "data-object": "Data Objects",
    "smart-shell": "Smart Shells",
    "smart-page": "Smart Pages",
    "footnote": "Footnotes",
    "disclosure": "Disclosures",
}


# --------------------------------------------------------------------------- io


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def load_profile(path: Path | None, preset: str = "ai") -> dict[str, Any]:
    profile = json.loads(json.dumps(PRESET_PROFILES.get(preset, DEFAULT_PROFILE)))  # deep copy
    if path and path.exists():
        override = read_json(path)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(profile.get(k), dict):
                profile[k].update(v)
            else:
                profile[k] = v
    return profile


def answered_set(path: Path | None) -> set[str]:
    """Set of answered blocking-question keys 'element_id:question_id' (build_answers.jsonl)."""
    out: set[str] = set()
    if not path:
        return out
    for rec in read_jsonl(path):
        eid, qid = rec.get("element_id"), rec.get("question_id")
        if eid and qid:
            out.add(f"{eid}:{qid}")
    return out


# ------------------------------------------------------------------ schedule


def _unit_key(node_id: str) -> str:
    """The build unit a node belongs to: the part after the first ':' (block:tcg_x -> tcg_x)."""
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def compute_schedule(plan: dict[str, Any], profile: dict[str, Any], concurrency: int, answered: set[str]) -> dict[str, Any]:
    nodes = plan.get("build_order", [])
    by_id = {n["node_id"]: n for n in nodes}
    base = profile["base_effort"]
    cw = float(profile["complexity_weight"])
    gate_sla = profile["gate_sla"]

    # 1) Blocked propagation (topo order — build_order has deps before dependents).
    blocked: dict[str, bool] = {}
    block_reason: dict[str, list[str]] = {}
    for n in nodes:
        nid = n["node_id"]
        eff_q = [q for q in n.get("open_blocking_questions", []) or [] if q not in answered]
        dep_blocked = [d for d in n.get("depends_on", []) if blocked.get(d)]
        blocked[nid] = bool(eff_q) or bool(dep_blocked)
        if eff_q:
            block_reason[nid] = eff_q
        elif dep_blocked:
            block_reason[nid] = [f"(waits on blocked {d})" for d in dep_blocked]

    sched_ids = [n["node_id"] for n in nodes if not blocked[n["node_id"]]]
    sched_set = set(sched_ids)

    def complexity(n: dict[str, Any]) -> float:
        return float(((n.get("impact") or {}).get("factors") or {}).get("complexity", 0) or 0)

    effort = {nid: round(base.get(by_id[nid]["kind"], 1.0) * (1 + cw * complexity(by_id[nid])), 2) for nid in sched_ids}
    gate_wait = {nid: round(sum(float(gate_sla.get(g, 0.0)) for g in by_id[nid].get("gates", [])), 2) for nid in sched_ids}

    def sched_deps(nid: str) -> list[str]:
        return [d for d in by_id[nid].get("depends_on", []) if d in sched_set]

    # 2) Dependency-only (infinite-resource) pass -> critical path + min duration.
    avail_inf: dict[str, float] = {}
    crit_dep: dict[str, str | None] = {}
    for nid in sched_ids:  # build_order is a valid topological order
        deps = sched_deps(nid)
        start = 0.0
        cd = None
        for d in deps:
            if avail_inf[d] > start or cd is None:
                if avail_inf[d] >= start:
                    cd = d
                start = max(start, avail_inf[d])
        avail_inf[nid] = round(start + effort[nid] + gate_wait[nid], 2)
        crit_dep[nid] = cd if deps else None
    makespan_inf = round(max(avail_inf.values()), 2) if avail_inf else 0.0

    critical: set[str] = set()
    if avail_inf:
        cur: str | None = max(avail_inf, key=lambda k: avail_inf[k])
        while cur is not None:
            critical.add(cur)
            cur = crit_dep.get(cur)
    critical_path = [nid for nid in sched_ids if nid in critical]  # in topo order

    # 3) Resource-constrained pass under `concurrency` workers -> wall-clock calendar.
    workers = [0.0] * max(1, concurrency)
    start: dict[str, float] = {}
    build_end: dict[str, float] = {}
    avail: dict[str, float] = {}
    for nid in sched_ids:  # topo order guarantees deps scheduled first
        dep_ready = max([avail[d] for d in sched_deps(nid)], default=0.0)
        w = min(range(len(workers)), key=lambda i: workers[i])
        s = round(max(dep_ready, workers[w]), 2)
        start[nid] = s
        build_end[nid] = round(s + effort[nid], 2)
        workers[w] = build_end[nid]  # implementer is free once the DRAFT is built (gate is a wait)
        avail[nid] = round(build_end[nid] + gate_wait[nid], 2)
    makespan = round(max(avail.values()), 2) if avail else 0.0

    # dependents map (for the gate register: what each gate unblocks)
    dependents: dict[str, list[str]] = {n["node_id"]: [] for n in nodes}
    for n in nodes:
        for d in n.get("depends_on", []):
            if d in dependents:
                dependents[d].append(n["node_id"])

    scheduled = [
        {
            "node_id": nid,
            "kind": by_id[nid]["kind"],
            "title": by_id[nid].get("title", nid),
            "unit": _unit_key(nid),
            "effort": effort[nid],
            "gate_wait": gate_wait[nid],
            "depends_on": by_id[nid].get("depends_on", []),
            "start": start[nid],
            "build_end": build_end[nid],
            "available": avail[nid],
            "critical": nid in critical,
            "gates": by_id[nid].get("gates", []),
            "status": by_id[nid].get("status"),
            "dependents": dependents.get(nid, []),
            "modules": by_id[nid].get("modules") or [],
            "shared": bool(by_id[nid].get("shared_across_modules")),
        }
        for nid in sched_ids
    ]
    blocked_list = [
        {"node_id": n["node_id"], "kind": n["kind"], "title": n.get("title", n["node_id"]),
         "reason": block_reason.get(n["node_id"], [])}
        for n in nodes if blocked[n["node_id"]]
    ]

    return {
        "concurrency": concurrency,
        "unit": profile["unit"],
        "makespan": makespan,
        "makespan_infinite": makespan_inf,
        "critical_path": critical_path,
        "scheduled": scheduled,
        "blocked": blocked_list,
        "scheduled_count": len(scheduled),
        "blocked_count": len(blocked_list),
        "scoped_to_module": (plan.get("summary") or {}).get("scoped_to_module"),
    }


# ------------------------------------------------------------------ render


def _san(text: str) -> str:
    """Sanitize a label for a Mermaid gantt task name (':' is the field delimiter)."""
    return text.replace(":", " -").replace("(", "[").replace(")", "]").replace("#", "no.").strip()


def _mermaid(result: dict[str, Any]) -> str:
    hours = result["unit"] == "hours"
    lines = ["```mermaid", "gantt",
             f"    title Build timeline ({result['concurrency']} parallel build lane(s), ~{result['makespan']} {result['unit']})",
             "    dateFormat YYYY-MM-DD HH:mm" if hours else "    dateFormat YYYY-MM-DD",
             "    axisFormat %d %Hh" if hours else "    axisFormat %m-%d"]
    base_dt = dt.datetime.combine(_GANTT_BASE, dt.time(9, 0))
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in result["scheduled"]:
        by_kind.setdefault(row["kind"], []).append(row)
    for kind in _KIND_ORDER:
        rows = by_kind.get(kind)
        if not rows:
            continue
        lines.append(f"    section {_KIND_SECTION[kind]}")
        for i, row in enumerate(rows):
            tag = "crit, " if row["critical"] else ""
            tid = row["node_id"].replace(":", "_").replace("-", "_")
            if hours:
                start = (base_dt + dt.timedelta(hours=row["start"])).strftime("%Y-%m-%d %H:%M")
                dur = f"{max(1, int(math.ceil(row['effort'])))}h"
            else:
                start = (_GANTT_BASE + dt.timedelta(days=int(math.floor(row["start"])))).isoformat()
                dur = f"{max(1, int(math.ceil(row['effort'])))}d"
            lines.append(f"    {_san(row['title'])} :{tag}{tid}, {start}, {dur}")
    lines.append("```")
    return "\n".join(lines)


def _ascii_gantt(result: dict[str, Any], width: int = 48) -> str:
    span = result["makespan"] or 1.0
    scale = width / span
    lines = ["```", f"{'Task':<26}{'|':<1} timeline (0 .. %.1f %s)" % (span, result["unit"])]
    for row in result["scheduled"]:
        s = int(round(row["start"] * scale))
        b = max(1, int(round(row["effort"] * scale)))
        g = int(round(row["gate_wait"] * scale))
        bar = " " * s + "#" * b + "*" * g
        bar = bar[:width]
        label = row["title"][:24]
        flag = "  <= critical" if row["critical"] else ""
        lines.append(f"{label:<26}|{bar}{flag}")
    lines.append("")
    lines.append("  # = build effort    * = gate/approval wait")
    lines.append("```")
    return "\n".join(lines)


def render_markdown(result: dict[str, Any], profile: dict[str, Any]) -> str:
    u = result["unit"]
    out: list[str] = []
    out.append("# Build timeline")
    out.append("")
    out.append(f"> Generated {now_iso()} · **{result['concurrency']} parallel build lane(s)** · unit **{u}** · "
               f"**read-only — builds nothing.** Durations assume **AI-agent implementation** (the agent builds; "
               f"humans answer questions / confirm logic / approve publishes at the gates — the gate SLAs dominate "
               f"the calendar). Use `--preset manual` or a custom `effort_profile.json` for human implementation; "
               f"calibrate against one real build.")
    out.append("")
    out.append(f"- **Critical path:** ~**{result['makespan_infinite']} {u}** (minimum possible, unlimited parallelism)")
    out.append(f"- **Wall-clock @ {result['concurrency']} build lane(s):** ~**{result['makespan']} {u}**")
    out.append(f"- **Scheduled nodes:** {result['scheduled_count']}   ·   **Blocked (unscheduled):** {result['blocked_count']}")
    all_modules = sorted({m for row in result["scheduled"] for m in row.get("modules", [])})
    if result.get("scoped_to_module"):
        out.append(f"- **Scoped to module:** {result['scoped_to_module']} — shared nodes that also "
                   "serve other modules are included and marked (shared)")
    elif len(all_modules) > 1:
        out.append(f"- **Modules:** {', '.join(all_modules)}")
    out.append("")

    if not result.get("scoped_to_module") and len(all_modules) > 1:
        out.append("## Module rollup")
        out.append(f"| Module | Nodes | Shared with others | Finishes (~{u}) |")
        out.append("|---|--:|--:|--:|")
        for m in all_modules:
            rows = [r for r in result["scheduled"] if m in r.get("modules", [])]
            shared_n = sum(1 for r in rows if r.get("shared"))
            finish = max((r["available"] for r in rows), default=0.0)
            out.append(f"| {m} | {len(rows)} | {shared_n} | {finish} |")
        out.append("")
        out.append("_Shared nodes are built with the FIRST module that needs them and inherited "
                   "by the rest — design them for all their consumers up front._")
        out.append("")

    out.append("## Gantt (Mermaid)")
    out.append(_mermaid(result))
    out.append("")
    out.append("## Gantt (ASCII fallback)")
    out.append(_ascii_gantt(result))
    out.append("")

    out.append("## Schedule")
    out.append(f"| Task | Kind | Module | Effort ({u}) | Depends on | Start | Build end | Available | Critical | Gates |")
    out.append("|---|---|---|--:|---|--:|--:|--:|:-:|---|")
    for row in result["scheduled"]:
        deps = ", ".join(row["depends_on"]) or "—"
        module = " + ".join(row.get("modules", [])) or "—"
        if row.get("shared"):
            module += " (shared)"
        out.append(f"| {row['title']} | {row['kind']} | {module} | {row['effort']} | {deps} | {row['start']} | "
                   f"{row['build_end']} | {row['available']} | {'✓' if row['critical'] else ''} | "
                   f"{', '.join(row['gates'])} |")
    out.append("")

    out.append(f"## Critical path (~{result['makespan_infinite']} {u})")
    out.append(" → ".join(result["critical_path"]) if result["critical_path"] else "_(none)_")
    out.append("")

    out.append("## Work breakdown (by unit)")
    units: dict[str, list[str]] = {}
    for row in result["scheduled"]:
        units.setdefault(row["unit"], []).append(f"{row['kind']} ({row['effort']}{u[0]})")
    for unit, items in units.items():
        out.append(f"- **{unit}** — {', '.join(items)}")
    out.append("")

    out.append("## Gate register")
    out.append("| Node | Gate(s) | Unblocks |")
    out.append("|---|---|---|")
    for row in result["scheduled"]:
        if row["gates"]:
            out.append(f"| {row['node_id']} | {', '.join(row['gates'])} | {', '.join(row['dependents']) or '—'} |")
    out.append("")

    out.append("## Blocked / unscheduled (resolve to schedule)")
    if result["blocked"]:
        out.append("| Node | Open blocking question(s) |")
        out.append("|---|---|")
        for b in result["blocked"]:
            out.append(f"| {b['node_id']} | {', '.join(b['reason']) or '—'} |")
        out.append("")
        out.append("_Answer these (record in `build_answers.jsonl`), re-run `/propose-build`, then `/build-timeline` — "
                   "the blocked nodes will slot into the schedule._")
    else:
        out.append("_None — every node is schedulable._")
    out.append("")
    return "\n".join(out)


def write_csv(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csvmod.writer(fh)
        w.writerow(["Task", "Kind", "Module", "Shared", "Effort", "Start", "Build_End", "Available",
                    "Predecessors", "Critical", "Gates"])
        for row in result["scheduled"]:
            w.writerow([row["node_id"], row["kind"], ";".join(row.get("modules", [])),
                        "yes" if row.get("shared") else "no", row["effort"], row["start"], row["build_end"],
                        row["available"], ";".join(row["depends_on"]), "yes" if row["critical"] else "no",
                        ";".join(row["gates"])])


# ----------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn build_plan.json into a reviewable implementation timeline (Gantt + critical "
        "path + schedule). Read-only: builds nothing."
    )
    parser.add_argument("--plan", default="workspace/build_plan.json")
    parser.add_argument("--answers", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--preset", default="ai", choices=sorted(PRESET_PROFILES),
                        help="Effort preset: 'ai' (default — agent builds, humans gate; hours) "
                             "or 'manual' (human implementers; days). --profile overrides merge on top.")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--output", default="workspace/build_timeline.md")
    parser.add_argument("--csv", default="workspace/build_timeline.csv")
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"Build plan not found: {plan_path} -- run /propose-build first.", file=sys.stderr)
        return 2

    plan = read_json(plan_path)
    profile = load_profile(Path(args.profile) if args.profile else None, preset=args.preset)
    concurrency = args.concurrency if args.concurrency is not None else int(profile.get("concurrency", 2))
    answered = answered_set(Path(args.answers) if args.answers else None)

    result = compute_schedule(plan, profile, concurrency, answered)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(result, profile), encoding="utf-8")
    if args.csv:
        write_csv(result, Path(args.csv))

    print(
        f"timeline: {result['scheduled_count']} scheduled, {result['blocked_count']} blocked; "
        f"critical path ~{result['makespan_infinite']} {result['unit']}, "
        f"wall-clock @ {concurrency} ~{result['makespan']} {result['unit']} -> {out}"
    )
    if result["blocked_count"]:
        print(f"  {result['blocked_count']} node(s) blocked on open questions -- resolve + re-run to schedule.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
