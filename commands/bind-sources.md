---
name: bind-sources
description: >-
  Connect the deck analysis to the firm's ACTUAL DATA — ingest human-provided data sources (CSV /
  Excel data files, a Snowflake INFORMATION_SCHEMA dump, or .sql DDL), then match every
  data-driven deck element's columns against the source tables/columns and emit ranked,
  catalog-grounded binding proposals (deck "Weight %" ↔ HOLDINGS.WEIGHT_PCT). Turns the Data Block
  source question in /propose-build from an open ask into a human confirmation. Runs AFTER
  /analyze-deck (needs element_understanding.jsonl); the binding itself is fully local — no tenant
  needed — but it CLOSES by auto-running the /validate-system-data check (phase 0) when a
  signed-in tenant is already available, so system-data gaps surface alongside the bindings. Phase
  2 of the implementation pipeline (methodology: the assette-implementation-guide skill).
argument-hint: "<sources path: a folder (or single file) of .csv/.xlsx/.json/.sql sources> [workspace path, default ./workspace]"
---

# /bind-sources

Bind the **output side** of a deck analysis (what each table/chart needs — resolved by
`/analyze-deck` Phase 2c into `element_understanding.jsonl`) to the **input side** (the data the
firm actually has — CSV/Excel extracts, a Snowflake `INFORMATION_SCHEMA` dump, or DDL). The result,
`workspace/source_bindings.jsonl`, carries ranked per-column candidates that `/propose-build`
consumes automatically — each bound Data Block node gains a `proposed_source` so the human
**confirms** a proposal instead of answering "where does this data come from?" from scratch.

The binding itself is local: deterministic Python + one LLM subagent per source file,
with a grounding guard that drops any fabricated binding. The only tenant touch is the
closing system-data auto-check (step 7) — tenant-READ-only, and only when a cached
sign-in already exists.

## Prerequisites

- A finished `/analyze-deck` workspace — `element_understanding.jsonl` must exist (Phase 2c ran).
  If it doesn't, tell the user to run `/analyze-deck` first and stop.
- The venv Python (same as `/analyze-deck`):
  Windows `${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`, macOS/Linux `${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`.
- Prefer an **`INFORMATION_SCHEMA` dump (CSV or JSON)** over raw `.sql` DDL when the user has a
  choice — the dump needs no parser and carries nullability/ordinal cleanly.

## What this command does

The default workspace is **`./workspace/`**. Use **absolute paths** when dispatching agents.

1. **Verify** the sources path exists and `element_understanding.jsonl` is present in the workspace.
2. **Ingest the sources** (deterministic — tables/columns/types/keys + capped sample values):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/ingest_sources.py" --input <sources_path> --output <ws>/source_catalog.json
   ```
3. **Slice** into lean payloads (per-source catalogs + the deck-side elements payload):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/slice_sources.py" --catalog <ws>/source_catalog.json --understanding <ws>/element_understanding.jsonl --output-dir <ws>/source_payloads
   ```
   This writes `source_<source_id>.json` per source plus `bind_elements.json` (every readable
   data-driven element with its resolved columns).
4. **Dispatch ONE `source-binding-analyzer` agent per source payload** (they run in parallel; each
   sees ALL elements but only ITS source). Give each agent absolute paths in the dispatch prompt:
   - `elements_path`: `<ws>/source_payloads/bind_elements.json`
   - `source_path`: `<ws>/source_payloads/source_<source_id>.json`
   - `output_path`: `<ws>/batches/bind_<source_id>.jsonl`
5. **Merge + ground** (deterministic — fabricated element/column ids DROPPED, names canonicalized
   from the catalog, candidates merged across sources and ranked):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/merge_validate_analysis.py" --mode bind --understanding <ws>/element_understanding.jsonl --catalog <ws>/source_catalog.json --batches-dir <ws>/batches --bindings <ws>/source_bindings.jsonl
   ```
6. **Report** — per element: columns **confirmed** (top candidate ≥ 0.7, shown as
   `deck column → TABLE.COLUMN (file)`), columns **to clarify** (ambiguous candidates — list them
   with their notes so the human can pick), **derived** columns (computed — no source column by
   design), and **no-match** columns. State the proposed **source family** per element
   (`database` → SQL/Snowflake block family; `file` → CSV/Excel block family). If the grounding
   guard dropped ids, say so — it means an agent fabricated instead of reading.
   **For `file`-family elements, add the build shape:** the block is a **staged chain** —
   Content-Service staging (a content type + filename pattern the human must confirm) → a Type-24
   byte producer → a CSV/Excel reader (→ a transform when the deck's layout differs from the
   file's, e.g. periods as columns over a long-format file). The decision guide is the
   block-author skill's `recipes/source/file-sourcing-playbook.md`.
   Close with the next step: **re-run `/propose-build`** — it picks up `source_bindings.jsonl`
   automatically and attaches `proposed_source` (including the staging gate and chain shape for
   file-family blocks) to the bound Data Block nodes.
7. **Auto-check the tenant's system data.** Binding connects the decks to the firm's data — the
   natural moment to confirm the TENANT side of that data too. If
   `<ws>/system_data_validation.json` is absent or older than 7 days:
   - Call `mcp__assette__shim_status` (side-effect-free — no sign-in, no tenant call). If a
     client code is configured AND an MSAL token cache exists, **run the full
     `/validate-system-data` flow now** (per `commands/validate-system-data.md`: execute the
     11 `Source_*` blocks, map, validate, report — no browser prompt is expected with a cached
     token) and fold its one-line summary + any required gaps into this report.
   - Otherwise (no client code, or sign-in would be interactive) do NOT trigger it — binding
     is a local operation and must not surprise the user with a browser. Say plainly: *"the
     tenant's system data has not been validated yet — run `/validate-system-data` (it opens
     browser sign-in)"*.
   - A failure in this step never fails the bind run — the bindings stand on their own.
8. **End the report with the standard pipeline footer**, appended verbatim (after step 7, so a
   fresh validation shows up in the `0 sysdata` row):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace <ws>
   ```
   Run the footer even when this command stops at the prerequisite gate above — it shows the
   user exactly where they are. (Methodology: the `assette-implementation-guide` skill.)

## Boundaries

- **Proposals, not truth.** A name+format match is not proof — every bind is confirmed by a human
  (and ultimately against live preview rows at build time). Derived columns, FK-less joins, and
  compliance facts (gross-vs-net, benchmark identity) stay clarifying questions by design.
- **Privacy.** `source_catalog.json` may carry a few real sample values from data files — treat the
  workspace as session-scoped working state; use a schema-only dump when data is sensitive.
- **Re-runnable.** Deterministic ingest/merge; re-run after adding source files. Re-running
  overwrites `source_bindings.jsonl` with the fresh merge.
