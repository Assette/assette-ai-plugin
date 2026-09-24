---
name: build-timeline
description: >-
  Turn a build_plan.json (from /propose-build) into a reviewable implementation TIMELINE — a Gantt
  chart, the critical path, a schedule table, a CSV/MS-Project export, a work-breakdown, and a
  gate register — so a human can review and sign off on the timeline before any building starts.
  READ-ONLY: it schedules and renders; it builds NOTHING, calls no MCP tool, and needs no tenant.
  Phase 4 of the five-phase implementation pipeline (methodology: the assette-implementation-guide
  skill).
argument-hint: "<workspace path (default ./workspace) — must contain build_plan.json from /propose-build>"
---

# /build-timeline

Generate the **implementation timeline** for a proposed build. After `/analyze-deck` →
`/propose-build` produce `build_plan.json` (the topo-sorted, impact-ranked Block → Object →
Shell → Page DAG), this command supplies durations and schedules the plan into a **Gantt chart +
critical path + calendar** a human can review. It is the read-only **phase 4** of the five-phase
implementation pipeline (`/analyze-deck` → `/bind-sources` → `/propose-build` →
`/build-timeline` → author): it **schedules and renders** — it does
**not** build anything, call any `mcp__assette__*` tool, or touch a tenant.

Run it AFTER `/propose-build`. It is deterministic (same plan + profile → same timeline) and safe
to re-run — in particular, re-run it after the human answers blocking questions to watch the
gated work slot into the schedule.

## Setup — the analysis venv

`plan_schedule.py` is stdlib-only Python and runs in the plugin's bundled shim venv (the same one
`/analyze-deck` and `/propose-build` use). Resolve the venv Python:

- Windows: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`
- macOS / Linux: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`

No extra dependencies beyond what `/analyze-deck` already needs (it imports only the shared
`_inventory_common.py` helper).

## What this command does

The default workspace is **`./workspace/`** in the current working directory. Accept an optional
argument to point at a different one.

1. **Verify** `build_plan.json` exists in the target workspace. If not, tell the user to run
   `/propose-build` first and stop.
2. **Run the scheduler** with the venv Python:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/plan_schedule.py" \
       --plan <ws>/build_plan.json \
       --answers <ws>/build_answers.jsonl \
       --profile <ws>/effort_profile.json \
       --concurrency 2 \
       --output <ws>/build_timeline.md \
       --csv <ws>/build_timeline.csv
   ```
   - `--answers` (optional) is a `build_answers.jsonl` of `{ "element_id", "question_id" }` records
     (the same shape `merge_validate_analysis.py` consumes). A node whose blocking questions are all
     answered here becomes schedulable. **The intended loop:** answer the blocking questions from
     `/propose-build` → re-run `/propose-build` (so the planner unblocks the nodes) → re-run
     `/build-timeline`.
   - **The default estimates assume AI-BASED implementation** — the `/build-deck` conductor + the
     authoring skills do the building (agent-hours per artifact), and humans appear only at the
     gates. The wall-clock is therefore dominated by the **gate SLAs** (publish-approval turnaround,
     source-family / composition confirmation), not build effort. Add `--preset manual` only if the
     firm will implement by hand (human-implementer days).
   - `--profile` (optional) is a firm-supplied `effort_profile.json` that deep-merges over the
     preset, e.g. `{ "base_effort": { "data-block": 1, "data-object": 0.75, "smart-shell": 0.75,
     "smart-page": 0.5 }, "complexity_weight": 0.25, "gate_sla": { "publish": 4, "source-family": 2,
     "composition": 2 }, "concurrency": 1, "unit": "hours" }` (the AI-preset defaults). Tune the
     `gate_sla` to the firm's real approval turnaround — that is the number that moves the calendar.
   - `--concurrency` overrides the number of parallel build lanes (concurrent agent build sessions —
     or implementers, under `--preset manual`) assumed for the wall-clock schedule.
3. **Read `build_timeline.md`** and present it — do NOT dump raw JSON. Show the **Gantt** (the file
   carries a Mermaid `gantt` block that renders in most markdown viewers, plus an ASCII fallback),
   then summarize:
   - **The two durations, clearly distinguished:** the **critical path** (minimum possible with
     unlimited parallelism) vs. the **wall-clock** under the stated concurrency. Always state the
     concurrency assumption.
   - **The critical path** — the chain that determines the floor on delivery.
   - **The gate register** — every publish/approval + confirmation gate and what it unblocks; these
     are the human touchpoints on the timeline.
   - **Blocked / unscheduled nodes** — anything still gated on an open blocking question, with the
     question ids. Point the user back to the resolve → re-plan → re-timeline loop.
   - **The module rollup** — when the plan spans multiple implementation modules, the timeline
     carries a "Module rollup" table (per-module node counts, shared-node counts, finish times)
     and every schedule row a Module column (shared plumbing marked `(shared)`). A plan scoped
     with `/propose-build --module <name>` schedules just that module's rollout.
   - Mention the **CSV / MS-Project export** (`build_timeline.csv`) for import into the firm's PM tool
     (it now carries Module + Shared columns).
4. **Close with what happens after sign-off + the pipeline footer.** Once the human signs off on
   the timeline, authoring starts bottom-up from the top of `build_order` in `build_plan.json`:
   Data Blocks (`assette-block-author`) → Data Objects (`assette-data-object-author`) → Smart
   Shells / Smart Pages (`assette-pptx-authoring` / `assette-xlsx-authoring`). Still-blocked nodes
   follow the answer → re-run `/propose-build` → re-run `/build-timeline` loop. End the report
   with the standard footer, appended verbatim:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace <ws>
   ```
   (Methodology reference: the `assette-implementation-guide` skill.)

## Boundaries

- **Read-only.** Schedules and renders a plan. Creates/updates/publishes nothing; invokes no
  `mcp__assette__*` tool.
- **Durations are model estimates**, derived from each node's complexity — NOT calibrated to a
  team's real velocity. Say so when presenting. The default assumes **AI-agent implementation**;
  the biggest lever on the calendar is the firm's real gate turnaround (`gate_sla`), so recommend
  calibrating `effort_profile.json` once against a known build before treating the wall-clock as
  commitment-grade.
- **Critical path ≠ wall-clock**, and **gates are waits, not work** (approval turnaround / answer
  latency), modeled via the gate SLA. A timeline that assumes instant gates understates the calendar.
