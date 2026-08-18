---
name: implementation-status
description: Report where a deck-implementation workspace stands — which pipeline phases (/analyze-deck → /bind-sources → /propose-build → /build-timeline → author) are done / partial / skipped / stale, how many blocking questions are open, and the exact next command. READ-ONLY — inspects workspace files via a deterministic helper; builds nothing, calls no MCP tool, needs no tenant. The resume-a-fresh-session entry point.
argument-hint: "[workspace path (default ./workspace)]"
---

# /implementation-status

Render the canonical pipeline status for an implementation workspace and say exactly
what to do next. This is the command to run when picking up a half-done implementation
in a fresh session — the workspace files ARE the state; nothing lives in conversation
memory. The methodology behind the phases is the `assette-implementation-guide` skill.

## Prerequisites

None worth blocking on. The helper is **stdlib-only**, so it does not need the plugin
venv: prefer the venv Python when it exists (Windows
`${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`, macOS/Linux
`${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`), otherwise fall back to any
`python3 || python` >= 3.10 on PATH — status must work on a fresh install before
`mcp__assette__bootstrap` has ever run. A missing workspace is NOT an error; it renders
as the not-started state.

## What this command does

1. **Resolve the workspace** — the optional argument, else `./workspace`.
2. **Run the canonical footer** and show its output **verbatim in a code fence** — never
   hand-compose or paraphrase it:
   ```
   <python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace <ws>
   ```
   (Add `--format json` for the machine-readable state when you need to reason over it.)
3. **Elaborate on whatever the footer flagged**, in this order:
   - **Open blocking questions — presented progressively, exactly as `/analyze-deck` step 8
     does.** State the gating rule first: each blocking question gates ONLY its own component
     chain — answer AS YOU BUILD, in plan order; answering everything up front is NOT
     required. Then the **top 5 IN FULL** (facet, element_id, `data_category`, question,
     `options` + `default`, `why`) — ranked by `blocks_node_impact` from `build_plan.json`'s
     `blocking_question_batch` when the plan exists and is not stale, otherwise by the fixed
     facet priority (`gross-net`, `benchmark`, `period-basis`, `units`, then
     `dynamic-columns`, `dynamic-rows`, `top-n-vs-full`, then the rest) — then grouped
     one-line counts by facet AND `data_category`, then the "show me all open questions"
     offer. Open = a `question_queue.jsonl` line with no matching
     `(element_id, question_id)` in `question_answers.jsonl`. Record answers per the
     "Handling implementer answers" flow in `analyze-deck.md`.
   - **System data** — when the footer's `0 sysdata` row shows gaps or not-run: restate the
     required-gap count, that required gaps gate the author phase only, and point at
     `workspace/system_data_report.md` (the client-facing ask) and `/validate-system-data`
     (to re-check after the client loads fixes).
   - **The plan**, when `build_plan.json` exists — `summary.node_count`,
     `summary.by_status`, and the top 3 `build_order` nodes with one-line whys. When
     `summary.modules` has more than one entry, add the per-module rollup
     (`summary.by_module`) and whether the plan is scoped (`summary.scoped_to_module`) —
     implementations roll out module by module, so "where are we?" is usually a per-module
     question.
   - **Bind skipped**, when the footer says SKIPPED — restate the consequence (every
     Data Block keeps an open source/family question in the plan) and offer
     `/bind-sources <sources>` followed by a `/propose-build` re-run.
   - **Stale phases** — name the command to re-run and why (what changed underneath it).
4. **Close by restating the footer's `Next:` line** as the single next action.

## Boundaries

- **Read-only.** Creates, edits, and answers nothing; invokes no `mcp__assette__*` tool.
  Recording question answers or re-running a phase happens through the owning command.
- **The footer is canonical.** If this command's narration and the footer ever disagree,
  the footer wins — fix the narration, not the footer.
- **Methodology questions** ("why bind before propose?", "what are the best practices?")
  belong to the `assette-implementation-guide` skill, not this command.
