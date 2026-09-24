---
name: propose-build
description: >-
  Turn a finished /analyze-deck workspace into a PROPOSED build plan — a topo-sorted,
  impact-ranked Data Block → Data Object → Smart Shell → Smart Page DAG, plus the up-front
  blocking-question batch a human must resolve before building. READ-ONLY: it proposes and ranks
  the work; it builds NOTHING, calls no MCP tool, and needs no tenant. Phase 3 of the five-phase
  implementation pipeline (methodology: the assette-implementation-guide skill).
argument-hint: "<workspace path (default ./workspace) — a finished /analyze-deck workspace>"
---

# /propose-build

Compile the output of `/analyze-deck` into a **build plan**: the ordered list of Assette
artifacts (Data Blocks, Data Objects, Smart Shells, Smart Pages) that rebuilding the analyzed
decks would require, **sequenced by impact and dependency** — so you never have to decide "what
do I build first." This is the **read-only phase 3** of the five-phase implementation
pipeline (`/analyze-deck` → `/bind-sources` → `/propose-build` → `/build-timeline` →
author): it **proposes and ranks** the work and surfaces the clarifying questions that
gate it. It does **not** build anything, call any `mcp__assette__*`
tool, or touch a tenant — it only reads the workspace and writes `build_plan.json`.

Run this AFTER `/analyze-deck` has produced a workspace. It is safe to run repeatedly; it is
deterministic (same workspace in → same plan out).

## Setup — the analysis venv

`plan_build.py` is stdlib-only Python and runs in the **plugin's bundled shim venv** (the same
one `/analyze-deck` and the `assette-pptx-authoring` fabricator use). Resolve the venv Python:

- Windows: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`
- macOS / Linux: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`

It needs no extra dependencies beyond what `/analyze-deck` already requires (it imports only the
shared `_inventory_common.py` helper). Build/refresh the venv via the `assette-plugin` skill's
"Initialize" op or `mcp__assette__bootstrap` if it does not yet exist.

## What this command does

The default workspace is **`./workspace/`** in the current working directory (the same place
`/analyze-deck` writes). Accept an optional argument to point at a different workspace.

1. **Verify the workspace** — confirm `corpus_inventory.json` and `classifications.jsonl` exist
   in the target workspace. If not, tell the user to run `/analyze-deck` first and stop.
   (`element_understanding.jsonl`, `table_chart_groups.json`, and `question_queue.jsonl` are
   used when present and degrade gracefully when absent — the plan is just coarser without them.)
2. **Run the planner** with the venv Python:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/plan_build.py" \
       --inventory <ws>/corpus_inventory.json \
       --classifications <ws>/classifications.jsonl \
       --understanding <ws>/element_understanding.jsonl \
       --groups <ws>/table_chart_groups.json \
       --question-queue <ws>/question_queue.jsonl \
       --output <ws>/build_plan.json
   ```
   Optionally pass `--category-weights <ws>/category_weights.json` (a firm-supplied
   `{ "<data_category>": <weight> }` table) to bias the ranking toward business-critical
   categories; without it every category weighs equally. The per-factor weights can be overridden
   with `--w-frequency`, `--w-downstream_unblock`, etc. (defaults in `plan_build.py::DEFAULT_WEIGHTS`).
   If `/bind-sources` has run, the planner picks up `<ws>/source_bindings.jsonl` automatically
   (or pass `--bindings` explicitly) and attaches a `proposed_source` to each bound Data Block node.
   **Module scoping**: implementations typically roll out module by module — when the user asks to
   plan one module (e.g. "plan the Factsheets module"), add `--module <name>` (case-insensitive;
   modules come from the corpus folder structure, see `/analyze-deck`, with `workspace/modules.json`
   overrides). The scoped plan keeps shared nodes that also serve other modules — they are built
   with the FIRST module that needs them and flagged `shared_across_modules`. An unknown module
   name exits 2 and lists the available modules.
3. **Read `build_plan.json`** and present it to the user — do NOT dump the raw JSON. Summarize:
   - **The build order** — the `build_order[]` is already topo-sorted (Data Block → Data Object →
     Smart Shell → Smart Page) and ranked by `impact.score` within each tier. Walk the top units
     and explain WHY they rank where they do (`impact.factors`: cross-deck `frequency`,
     `deck_spread`, `downstream_unblock` leverage, `complexity`, `reuse_discount`). Note which
     table/chart units were **deduped** from cross-deck groups (`summary.deduped_from_groups`).
   - **Modules** — when `summary.modules` has more than one entry, present the per-module rollup
     (`summary.by_module`: nodes, statuses, blocking questions per module) and call out the
     **shared plumbing** (`summary.shared_node_count`, nodes flagged `shared_across_modules` —
     "also serves modules X, Y"): shared components are built once, with the first module that
     needs them, and must be designed for all their consumers. When the plan is scoped
     (`summary.scoped_to_module`), say so up front.
   - **The up-front blocking-question batch — presented progressively, never as a wall.**
     `blocking_question_batch[]` is already ranked by `blocks_node_impact` (how much of the
     plan each answer unblocks). Present the **top 5 IN FULL** (facet / `data_category` /
     question / options / default / why / `blocks_node_impact`) and the rest as grouped
     one-line counts by facet AND by `data_category` (`uncategorized` when missing). State
     explicitly: **each question gates only its own node chain — the build starts on
     unblocked nodes regardless; answer these in `build_order` order as you reach the gated
     nodes. Answering everything up front is NOT required.** Full list on demand ("show me
     all open questions"). This is the HITL gate the design calls for — surface it, do not
     answer it yourself.
   - **What is gated vs. ready** — `summary.by_status`: `ready` (the autonomous middle band:
     Data Object / Smart Shell off a published block), `needs-confirmation` (Data Block
     source-family and Smart Page composition — un-inferable from the deck), `blocked-on-question`
     (has an open blocking question). Every stage transition is a publish-approval gate by
     construction (a node is ready only when its upstream is published).
   - **What was not planned** — `summary.elements_not_planned` (fixed-content / parameter /
     brand-theme / none) and `summary.dropped_unknown_id_count` (the grounding guard — fabricated
     ids that were refused).
4. **Declare readiness honestly, then close with the next step + the pipeline footer.**
   - If `source_bindings.jsonl` was NOT present: say plainly that `/bind-sources` has not run,
     so every Data Block's source/family question is an OPEN ASK in this plan — and that running
     `/bind-sources <sources>` then re-running `/propose-build` turns those asks into
     confirmations. Proceed only because the user explicitly chose to skip it.
   - Check `<ws>/system_data_validation.json` (written by `/validate-system-data`): if it is
     absent, note that the tenant's system data has not been validated yet — the plan stands,
     but authoring stays gated until `/validate-system-data` runs. If it shows required gaps
     (`summary.required_gaps > 0`), say the plan is buildable on paper but authoring is gated
     until the client closes the gaps — the client-facing ask is
     `<ws>/system_data_report.md`.
   - If sources are bound and the system-data check (previous bullet) shows no required gaps:
     this workspace is **ready to author bottom-up** (Data Block → Data Object → Smart Shell →
     Smart Page) once the schedule is reviewed — readiness is declared here, never by
     `/analyze-deck`. Open blocking questions do NOT block this declaration — they gate only
     their own node chains (those nodes are `blocked-on-question` in the plan); the build
     starts on the unblocked nodes.
   - The next step is **`/build-timeline`** — schedule the plan into a reviewable Gantt +
     critical path before any authoring starts. After the user answers blocking questions, the
     loop is: re-run `/propose-build` → re-run `/build-timeline`.
   - End the report with the standard footer, appended verbatim:
     ```
     <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace <ws>
     ```
     (Methodology reference: the `assette-implementation-guide` skill.)

## Boundaries

- **Read-only.** This command proposes a plan. It does not create, update, preview, publish, or
  lock anything, and it invokes no `mcp__assette__*` tool. Building the artifacts is a later,
  separate, human-gated step (the `/build-deck` conductor in the design doc — not yet built).
- **The plan is provisional against the live tenant.** Its reuse signals are offline (from the
  corpus); the authoritative reuse-vs-build decision happens at build time via `search_blocks` /
  `search_data_objects`. Treat `build_plan.json` as a proposal to review, not a commitment.
- The plan's **Data Block source-family and Smart Page composition nodes are `needs-confirmation`
  by design** — they cannot be inferred from the deck alone. Running **`/bind-sources`** with the
  firm's data (CSV/Excel/Snowflake dictionary) turns the source-family question from an open ask
  into a confirmation: bound block nodes carry a `proposed_source` (family, files,
  `deck column → TABLE.COLUMN` bindings) for the human to ratify. Present it when it's there.
- **Every Data Block node carries `default_parameters`** — the platform parameter
  convention the author phase must honor: `["AccountCode", "AsofDate"]` for
  account-scoped data (note the EXACT spelling — lowercase "o" in `AsofDate`; these
  are the generation engine's runtime-parameter keys), `["AccountCode", "FromDate",
  "ToDate"]` for period-window categories (transactions, cash-flows), and
  `["AsofDate"]` for firm-level reference data. Present it with the node; a block
  authored without these parameters serves exactly one account/period and cannot be
  bound into a Smart Page. The authoritative statement lives in the
  `assette-block-author` skill's "Parameter conventions" section.
- **File-family block nodes carry two extra facts — present both.** `proposed_source.staging`
  (default `content-service (confirm content type + naming pattern)`) is a **`staging` gate**: the
  file must be staged via the Content-Service fixed-content flow, which means a content type and a
  filename pattern the human confirms (or registers — a tenant-admin step). And
  `proposed_source.chain` shows the build shape (`content-service-read` producer → `reader` →
  optionally `transform`; a `reshape_hint: pivot-suspected` means the deck's layout differs from
  the file's and a pivot transform is expected). The knowledge lives in the block-author skill's
  `recipes/source/file-sourcing-playbook.md`.
