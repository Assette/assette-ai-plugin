---
name: validate-system-data
description: Validate that the connected client tenant has the REQUIRED Assette system datasets — executes the 11 known Source_* data blocks (9 required, 2 optional, per the committed "Assette System Datasets v2.2" spec) via the assette MCP server, checks fields and value constraints deterministically, folds gaps into the pipeline (missing required data gates authoring), and writes a CLIENT-FACING gap report the implementer can send. The FIRST tenant-touching pipeline command — phase 0, runnable any time after sign-in, independent of deck analysis. Tenant-READ-only.
argument-hint: "[workspace path (default ./workspace)]"
---

# /validate-system-data

Check the tenant's **system data** — the datasets Assette needs for the platform to
operate (attributes, countries, accounts, account-attribute mappings, products,
offered countries, currencies, benchmarks, benchmark associations, plus two optional
group/permission datasets). Each is a named `Source_*` data block; this command
executes them all, validates the responses against the committed spec
(`${CLAUDE_PLUGIN_ROOT}/tools/system_datasets_spec.json`, v2.2), and produces:

- `workspace/system_data_validation.json` — machine state (the pipeline footer's
  `0 sysdata` row reads it),
- `workspace/system_data_report.md` — a **client-facing** readiness report: per gap,
  what the dataset is, why Assette needs it, what we found, and exactly what to
  provide (field layout with examples).

This is **phase 0** of the implementation pipeline and its only tenant-touching
command. Run it **early** — client data gaps take the longest to fix — and re-run it
after the client loads corrections. Missing REQUIRED datasets gate the final
*author* phase only; the local pipeline (`/analyze-deck` → `/build-timeline`)
proceeds regardless. (`/bind-sources` auto-runs this flow at its close when a cached
sign-in already exists, so binding and system-data findings land together.)

## Prerequisites

- **A configured tenant.** The first `mcp__assette__*` call triggers tenant discovery
  and, when no cached token exists, **opens the system browser for B2C sign-in — warn
  the user before the first call**. If a tool errors with
  `shim.config.client_code_missing`, route the user to the `assette-plugin` skill's
  Initialize op and stop.
- **Python** — the validator is stdlib-only: prefer the venv Python when it exists
  (Windows `${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`, macOS/Linux
  `${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`), otherwise any `python3 || python`
  >= 3.10 on PATH. No bootstrap needed.
- The workspace directory may not exist yet (this can legitimately run before
  `/analyze-deck`) — create it.

## What this command does

The default workspace is **`./workspace/`**; accept an optional argument.

1. **Read the spec** — `${CLAUDE_PLUGIN_ROOT}/tools/system_datasets_spec.json` lists
   the 11 dataset names in order.
2. **Execute each dataset's block** — for each spec name, in order, call
   `mcp__assette__execute_data_block` with `blockName: "<name>"` and **no
   `parametersJson`**. The reply is a `{cached: true, path, ...}` pointer envelope
   (the shim wrote the full response to disk). **Immediately** record the envelope's
   `path` into `workspace/system_data_map.json` under that dataset name, along with
   `executed_block_name`, before making the next call — the cache filenames do NOT
   carry the block name, so order is the only way to keep the mapping honest:
   ```json
   {"client_code": "<from shim_status or sign-in>", "started_at": "<UTC ISO-8601>",
    "runs": {"Source_CountryList": {"cache_file": "<envelope path>",
                                     "executed_block_name": "Source_CountryList"}}}
   ```
   Do **NOT** Read the cache files — they can be large; the validator reads them.
3. **Not-found fallback** — if a call errors in a way that suggests the block does
   not exist in this tenant (a Data Preparation error status/body rather than data),
   call `mcp__assette__search_blocks` once with `blockSystemType: "system"` (and once
   without, if needed) — block names vary per tenant; the spec names are the map, not
   the territory. Record up to 5 near-name candidates in the map entry as
   `{"cache_file": null, "status_hint": "not-found", "error": "<short error>",
   "candidates": [...]}`. **Never auto-substitute a candidate** — surface it for the
   human to confirm with the client. Other failures record
   `"status_hint": "execution-failed"` with the error text.
4. **Validate deterministically**:
   ```
   <python> "${CLAUDE_PLUGIN_ROOT}/tools/validate_system_data.py" --map <ws>/system_data_map.json --workspace <ws>
   ```
   Embed its one-line stdout summary in your report. It writes both artifacts and
   never exits non-zero.
5. **Present the findings** — REQUIRED gaps first (dataset / status / one-line
   finding / candidates when not-found), then optional gaps. Point at
   `workspace/system_data_report.md` as the client-facing deliverable — **tell the
   implementer to review it before sending** (it is a draft). State plainly: required
   gaps gate the *author* phase only; analysis, binding, planning, and scheduling all
   proceed. The resolution loop is: send the report → the client fixes/loads data →
   re-run `/validate-system-data`.
6. **Close with the pipeline footer**, appended verbatim:
   ```
   <python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace <ws>
   ```
   (Methodology reference: the `assette-implementation-guide` skill.)

## Error handling

- **`shim.config.client_code_missing`** → the tenant is not configured; route to the
  `assette-plugin` skill's Initialize op and stop.
- **Sign-in declined / browser closed** → stop cleanly; note that the footer's
  `0 sysdata` row will keep showing "not run" until validation completes.
- **Execution fails with a parameter-looking error** — the spec assumes the
  `Source_*` blocks are parameterless; if one demands parameters, record
  `execution-failed` and tell the implementer to inspect the block via
  `mcp__assette__get_block` and re-run once understood.
- **Empty is not failure** — a dataset that executes but returns zero rows is a
  legitimate finding (`empty`) and appears in the client report as a gap.

## Boundaries

- **Tenant-READ-only.** Calls only `execute_data_block` and `search_blocks`; creates,
  updates, publishes, and deletes nothing.
- **The report is a draft** for the implementer to review and send — this command
  never communicates with the client directly.
- **Re-runnable.** Re-running overwrites `system_data_map.json`,
  `system_data_validation.json`, and `system_data_report.md` with fresh results.
