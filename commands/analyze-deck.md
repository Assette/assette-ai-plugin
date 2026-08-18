---
name: analyze-deck
description: Analyze a corpus of existing PPTX/Word/PDF deliverables (factsheets, pitchbooks, client reports) and return a structured, classified element inventory PLUS a deep structural understanding of every data-driven table/chart (dynamic columns/rows, row formatting, chart series — via multi-sample variance), a review queue, and an active clarifying-question queue. The "understand the source decks" front-end that feeds the assette authoring skills. Does NOT author Assette artifacts.
argument-hint: "<corpus path: a folder of source files, or a single .pptx/.docx/.pdf>"
---

# /analyze-deck

Reverse-engineer a firm's existing deliverables into a classified, deeply-understood
element inventory — the **input** the assette authoring skills (`assette-block-author`,
`assette-data-object-author`, `assette-pptx-authoring`) need to rebuild those decks in
Assette. Local phases, no hosted-server calls:

1. **Phase 1 — Ingest** (deterministic Python): parse the corpus into one
   `corpus_inventory.json`.
2. **Phase 1.5 — Render + visually segment** (the `slide-vision-segmenter` agent, vision):
   render each slide/page to an image and regroup the loose shapes a deck is built from into
   the components a viewer sees. **PDF decks render via bundled PyMuPDF on every platform —
   no Office needed**; only PPTX decks need PowerPoint (COM) for their render leg, and only
   that leg skips gracefully when unavailable.
3. **Phase 2a — Classify** (the `element-classifier` agent): tag every element + assign
   an investment-data-category and an Assette authoring-component.
4. **Phase 2c — Deep table/chart understanding** (the `table-chart-analyzer` agent):
   read the structural grammar of every data-driven table/chart across multiple samples,
   and raise active clarifying questions.

A deterministic post-pass (`merge_validate_analysis.py`) validates every element id the agents
emit against the inventory — the grounding guard — and produces the canonical workspace files.

**The format-agnostic principle.** A factsheet delivered as a PDF must analyze as richly as
its PPTX twin. Flattened structure (raster/vector pages, image charts, borderless tables) is
recovered by the vision pass — rendered, segmented, structurally read by the vision-capable
analyzer — always **provenance-marked** (`structure_provenance: vision|mixed`) and
**human-confirmed** (a non-blocking `vision-confirm` question + a `structure-verification`
gate in the build plan). It is never silently dropped, and a number read off pixels is never
data — values come from the bound sources at generation time.

**Corpus layout = implementation modules.** A typical implementation rolls out module by
module (Factsheets, Pitchbooks, Client Reports, …). Organize the corpus **one folder per
module** — `<corpus>/Factsheets/deck.pptx` — and the first-level folder name becomes each
deck's module downstream: `/propose-build` groups and can scope the plan per module, and
`/build-timeline` rolls the schedule up per module. A flat corpus is fine too (everything
lands in one `(ungrouped)` module); per-deck overrides go in `workspace/modules.json`
(`{"<deck_id or filename>": "<module>"}`). Tell the user this convention when they point you
at an unorganized corpus for a multi-module implementation.

## Setup — the analysis skills (one-time)

The six analysis/methodology skills this pipeline relies on (`content-ingestion`,
`element-classification`, `investment-data-categories`,
`authoring-component-identification`, `deep-table-chart-understanding`,
`assette-implementation-guide`) are **server-delivered, not shipped in the
plugin**: they are downloaded into `${CLAUDE_PLUGIN_ROOT}/skills/<name>/` by the
`assette-plugin` skill's *Initialize* / *Update* operations after B2C sign-in.
**Pre-flight check**: if `${CLAUDE_PLUGIN_ROOT}/skills/content-ingestion/SKILL.md`
does not exist, stop and tell the user to run **"initialize assette"** (first
install) or **"update assette"** (after a plugin upgrade) and start a new
session — do not proceed with the rubric skills missing, and do not improvise
their content.

## Setup — the analysis venv (one-time)

The analysis tools are Python and run in the **plugin's bundled shim venv** (the same one
the `assette-pptx-authoring` fabricator uses). Resolve the venv Python:

- Windows: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/Scripts/python.exe`
- macOS / Linux: `${CLAUDE_PLUGIN_ROOT}/shim/.venv/bin/python`

Build/refresh it via the `assette-plugin` skill's "Initialize" op (or the
`mcp__assette__bootstrap` tool); the pinned deps cover ingest (`python-pptx`, `pdfplumber`,
`python-docx`) and the vision render (`pymupdf`, plus `pywin32` on Windows).

**Phase 1.5 (vision) needs Microsoft PowerPoint only for PPTX decks** — `render_slides.py`
drives it via COM (Windows-only) for the pptx→pdf leg. **PDF decks render with the bundled
PyMuPDF on every platform** — for a PDF corpus there is no Office dependency at all, and the
vision pass is REQUIRED, not optional (see step 3). Word decks have no render path — flattened
Word charts stay unreadable; ask the firm for the PPTX/PDF original. See the
`content-ingestion` skill for the inventory schema.

## What this command does

Write all workspace files to a **`./workspace/`** directory in the current working
directory (create it if absent) — this is project-scoped analysis state.

When dispatching the `element-classifier` / `table-chart-analyzer` agents below, give them
**absolute** paths (resolve `./workspace/…` against the current working directory) — a
subagent may not share your relative cwd, and it must read/write the real files, not guess.

1. **Verify the corpus path** exists. If not, return an error and stop.
2. **Ingest** — run the dispatcher with the venv Python:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/ingest_content.py" --input <corpus_path> --output ./workspace/corpus_inventory.json
   ```
   It routes each file by extension (`.pptx`/`.docx`/`.pdf`) to the right ingester and
   emits one unified inventory with a per-deck `source_format`. Deterministic — no LLM
   calls. Report the file/element counts and the per-format breakdown. Then **slice the
   inventory into lean per-deck payloads** — the agents read these small files, never the
   monolithic inventory (a multi-MB inventory blows past the Read token cap, so the model
   gets lost paging through it and drops/fabricates):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/slice_payloads.py" --inventory ./workspace/corpus_inventory.json --output-dir ./workspace/payloads
   ```
   This writes `workspace/payloads/deck_<deck_id>.json` (only that deck's elements, bulky text
   capped, each well under the single-Read budget). If it warns a payload exceeds ~20K tokens,
   note that deck is unusually large.
3. **Render + visually segment (Phase 1.5 — REQUIRED for PDF / flattened corpora).** The
   object model is blind to how a slide is *drawn* — a "Sector Weightings chart" is often
   freeform bars + loose text boxes with no chart object, and a PDF factsheet page is often
   one flat image. Render the pixels and let a vision agent regroup those loose shapes into
   the components a viewer sees; for flattened content the vision pass is the ONLY source of
   structure. Create `./workspace/batches/`, then:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/render_slides.py" --inventory ./workspace/corpus_inventory.json --output-dir ./workspace/renders
   ```
   PPTX decks render via the installed **PowerPoint** (COM) → PDF → PyMuPDF; **PDF decks
   render directly via PyMuPDF (no Office)** → `workspace/renders/<deck_id>/slide_<index>.png`.
   **Branch on the per-format breakdown step 2 reported** when the tool exits 3 or 4:
   - **The corpus contains any PDF deck** (or ingest reported `needs_vision` /
     `likely_chart_image` elements): exit 3/4 here means a BROKEN ENVIRONMENT (the venv is
     missing `pymupdf`), NOT a graceful skip — run the `assette-plugin` skill's Initialize op
     (or `mcp__assette__bootstrap`) and retry. Do NOT silently proceed structure-only: the
     flattened tables/charts would be invisible to the whole pipeline. If the user explicitly
     accepts structure-only after being told that, proceed — otherwise stop here.
   - **pptx-only corpus + exit 4** (PowerPoint COM unavailable — non-Windows, no Office,
     missing `pywin32`): the documented graceful skip. Continue at step 4 structure-only;
     flattened PPTX charts will surface as unreadable with a "provide the native file"
     question — the explicit exception, not the rule.
   - **docx-only corpus + exit 3**: no render path exists for Word — skip, and note that
     flattened Word charts stay unreadable (ask for the PPTX/PDF originals).

   Otherwise, for **each rendered deck**, dispatch the `slide-vision-segmenter` agent (decks
   are independent — dispatch in parallel) with:
   - `renders_dir` = `./workspace/renders/<deck_id>`
   - `payload_path` = `./workspace/payloads/deck_<deck_id>.json`
   - `output_path` = `./workspace/batches/segment_<deck_id>.jsonl`

   The agent reads the slide PNGs + the deck's lean payload (element bboxes) and writes one
   visual-component record per line (contract: `agents/slide-vision-segmenter.schema.json`). Then consolidate:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/merge_validate_analysis.py" --mode segment --inventory ./workspace/corpus_inventory.json --batches-dir ./workspace/batches --components ./workspace/slide_components.jsonl --vision-regions ./workspace/vision_regions.json
   ```
   This hard-validates every component's ids against the inventory (same grounding guard),
   **checks each component's bbox against its members' real positions** (a mismatched bbox is
   nulled + flagged `bbox_suspect` so nothing ever crops the wrong region), **materializes one
   synthetic `vision-region` element per data component whose members carry no parsed
   structure** (code-minted ids `<deck_id>:<page>:v<n>` — this is what lets several flattened
   tables/charts on ONE page each get their own analysis chain), and writes
   `workspace/slide_components.jsonl` + `workspace/vision_regions.json`. It also prints the
   **per-page component inventory** — capture it; step 8 surfaces it for eyeball QA.
4. **Classify (Phase 2a)** — create `./workspace/batches/`. For **each per-deck payload** in
   `./workspace/payloads/`, dispatch the `element-classifier` agent (decks are independent —
   dispatch them in parallel) with two values:
   - `payload_path` = `./workspace/payloads/deck_<deck_id>.json`
   - `output_path` = `./workspace/batches/classify_<deck_id>.jsonl`

   The agent **reads its one lean payload** (single Read, no paging), classifies every element
   in it, and writes one record per element (the contract in `agents/element-classifier.schema.json`).
   It returns only a status line — the data is in the file it wrote, never in its reply.
5. **Merge + validate (the grounding guard)** — run (drop `--components` if Phase 1.5 was skipped):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/merge_validate_analysis.py" --mode classify --inventory ./workspace/corpus_inventory.json --batches-dir ./workspace/batches --classifications ./workspace/classifications.jsonl --review-queue ./workspace/review_queue.jsonl --components ./workspace/slide_components.jsonl --vision-regions ./workspace/vision_regions.json
   ```
   This concatenates every deck's output into `workspace/classifications.jsonl` (stamping a
   UTC `timestamp` + `"source": "element-classifier"`), **hard-validates every `element_id`
   against the inventory ∪ the vision-region registry and drops anything else** (the
   deterministic anti-hallucination guard — a fabricated id like `sample:0:0` can never reach
   the canonical file). When `slide_components.jsonl` exists it then **folds each vision
   component's loose members into one component-level classification** — object-model
   outranking vision: a component with a parsed table/chart member keeps that member's
   classifier record (merged, never overwritten); a purely-flattened data component is
   promoted onto its synthetic vision-region id (its primitive defaulting from the
   component_type when the segmenter abstained); the other members become `none` + a
   `part-of-component` hint. This is what pulls faux-chart fragments out of the review queue
   AND what carries flattened PDF tables/charts forward instead of dropping them. It preserves
   any prior `human-correction` records, and derives `workspace/review_queue.jsonl` (latest
   record per `element_id` with `tag == "ambiguous"` or `confidence < 0.7`). **Report the
   validated count and any dropped ids** — a non-zero drop count means an agent fabricated
   instead of reading; investigate, don't ignore.
6. **Deep table/chart understanding (Phase 2c)** — first group the data-driven table/chart
   elements into cross-deck sample sets. In scope: elements the classifier tagged
   `smart-shell:table|zigzag|chart|performance-history` **plus every vision-identified data
   component** (table / chart / kpi-grid / performance-history regions — including synthetic
   vision-region anchors), even where the classifier said none/ambiguous. When
   `slide_components.jsonl` exists the grouping keys off the **visual component** (type +
   date-stripped label) so faux-charts group across decks and monthly factsheet variants
   land in ONE group:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/group_table_chart_samples.py" --inventory ./workspace/corpus_inventory.json --classifications ./workspace/classifications.jsonl --components ./workspace/slide_components.jsonl --vision-regions ./workspace/vision_regions.json --output ./workspace/table_chart_groups.json
   ```
   Then **slice per-group payloads** (the analyzer reads these, not the inventory):
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/slice_payloads.py" --mode understand --inventory ./workspace/corpus_inventory.json --groups ./workspace/table_chart_groups.json --renders-dir ./workspace/renders --vision-regions ./workspace/vision_regions.json --output-dir ./workspace/payloads
   ```
   This writes `workspace/payloads/understand_<group_id>.json` (the prior classification + the
   group's already-resolved member records). For samples covered by a vision component it also
   attaches the **vision block**: a deterministic **pre-cropped PNG** of exactly that component
   (`payloads/crops/`, rendered from the PDF via PyMuPDF using the validated bbox), the full
   page render as fallback, and — for pages with a text layer — **`region_text`, the REAL
   pdfplumber-extracted characters inside the bbox** (vision locates, code reads → the most
   trustworthy flattened structure, provenance `mixed`). At most 3 samples per group carry
   images (deterministic pick, representative first, distinct decks preferred). For **each
   group**, dispatch the `table-chart-analyzer` agent (groups are independent — dispatch in
   parallel) with two values:
   - `payload_path` = `./workspace/payloads/understand_<group_id>.json`
   - `output_path` = `./workspace/batches/understand_<group_id>.json`

   The agent **reads its one lean payload**, reasons about cross-sample variance, and writes one
   `element_understanding` JSON (contract: `agents/table-chart-analyzer.schema.json`). It returns
   only a status line.
   See the `deep-table-chart-understanding` skill (authoritative for the rubric, the
   multi-sample variance method, and the ASK-vs-infer rule).
7. **Merge + validate (Phase 2c)** — run:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/merge_validate_analysis.py" --mode understand --inventory ./workspace/corpus_inventory.json --batches-dir ./workspace/batches --understanding ./workspace/element_understanding.jsonl --question-queue ./workspace/question_queue.jsonl --answers ./workspace/question_answers.jsonl --vision-regions ./workspace/vision_regions.json
   ```
   This folds every group's output into `workspace/element_understanding.jsonl` (stamping a
   UTC `timestamp` + `"source"`), **hard-validates `element_id` + `member_element_ids`
   against the inventory ∪ the vision-region registry**, and derives
   `workspace/question_queue.jsonl` (one line per OPEN question, keyed by `element_id` +
   `question_id`; questions already answered in `question_answers.jsonl` drop out). For
   vision/mixed-provenance records it also applies the deterministic guards: `verification`
   forced to `needs-confirmation`, confidence clamped below the 0.7 review threshold, and a
   NON-blocking `vision-confirm` question auto-queued (stable id — an answer survives
   re-runs) pointing at the crop image so a human verifies the read against the pixels.
8. **Summarize — and surface the blocking questions PROGRESSIVELY, never as a wall.**
   Present total elements, per-format breakdown, per-tag counts, per-data-category counts
   (data-driven elements), review-queue size + a few items with rationales, and the
   deep-understanding count. When Phase 1.5 ran, also present the **per-page component
   inventory** (the segment merge printed it; it derives from `slide_components.jsonl`
   grouped by deck + page) — this is the eyeball-QA hook: **every table, chart, label and
   disclosure a human sees on a page must appear here**; invite the user to compare it
   against their own ground-truth list before binding. Add one line for vision-derived
   structures: `vision-derived structures: <n> (needs confirmation — non-blocking
   'vision-confirm' questions in the queue)`. Then surface the BLOCKING clarifying questions from
   `workspace/question_queue.jsonl`. The analyzer is a headless subagent and cannot ask you
   mid-run; it can only write its questions to the queue, so it is the orchestrator's job to
   put them in front of the human here — but state UP FRONT: **each blocking question gates
   ONLY its own component chain (that element's Data Block → Data Object → Smart Shell and
   its owning Smart Page) — nothing else. Answer them AS YOU BUILD, in plan order; answering
   everything up front is NOT required.** Then:
   - Present the **top 5** open blocking questions IN FULL (facet, element_id,
     `data_category`, the question, its `options` + `default`, and `why` it gates that
     component), ranked by the fixed facet priority — compliance first: `gross-net`,
     `benchmark`, `period-basis`, `units`; then structure: `dynamic-columns`,
     `dynamic-rows`, `top-n-vs-full`; then all other facets — ties broken by `element_id`
     then `question_id`. Invite answers to these now (they are the highest-priority
     decisions), via the "Handling implementer answers" flow.
   - Summarize the REST as grouped one-line counts **by facet AND by `data_category`**
     (queue lines carry both; group missing values from older queues as `uncategorized`).
   - Offer the full list on demand: the implementer can say **"show me all open
     questions"** at any time (the queue lives in `workspace/question_queue.jsonl`).
   Non-blocking questions are a count only (safe defaults exist; they may be deferred).
   The run is **not "done"** while blocking questions sit unsurfaced in the queue file —
   surfacing means the contract + top 5 + grouped counts, not an exhaustive dump.
9. **Close with the handoff + the pipeline footer.** Analysis is phase 1 of a five-phase
   implementation pipeline (`/analyze-deck` → `/bind-sources` → `/propose-build` →
   `/build-timeline` → author). **NEVER close by saying the decks are "ready to author"** —
   the firm's DATA SOURCES are still unanalyzed, and readiness is only ever declared by
   `/propose-build`. The required next phase is **`/bind-sources <path to the firm's
   CSV / Excel / Snowflake-schema sources>`**. Only if the user explicitly states that no
   data sources are available may they go straight to `/propose-build` — and say plainly
   that every Data Block then keeps an open "where does this data come from?" question in
   the plan. End the report with the standard footer, appended verbatim:
   ```
   <venv-python> "${CLAUDE_PLUGIN_ROOT}/tools/pipeline_status.py" --workspace ./workspace
   ```
   The `assette-implementation-guide` skill is the canonical methodology reference.

## What this command does NOT do

- It does not **author** the Assette artifacts — and a finished analysis is NOT the cue to
  start authoring. The pipeline continues with **`/bind-sources`** (ground the analysis in
  the firm's real data), then **`/propose-build`** (the ranked Block → Object → Shell → Page
  plan), then **`/build-timeline`** (the reviewable schedule). Authoring happens after that,
  with `assette-block-author`, `assette-data-object-author`, and `assette-pptx-authoring`
  (the deep-understanding record + the `data_category` map straight onto their domain
  fragments).
- It does not look at the firm's **data sources** — deck analysis alone cannot say where
  the data comes from. That is `/bind-sources`, the required next phase.
- It does not canonicalize / dedup across the whole corpus. *(Phase 2c's
  `table_chart_groups.json` is a narrow, analysis-time grouping of like table/chart
  samples for variance only — not corpus-wide canonicalization or parameter detection.)*
- It does not call the hosted Assette MCP server — analysis is entirely local (ingest =
  Python, classify/understand = these agents).

## Confidence threshold

The threshold separating auto-accept from review-queue is **0.7**. For Phase 2c, an open
**blocking** question (not a low single-sample confidence) is what gates downstream
authoring of that component.

## Handling implementer corrections

If the implementer corrects a classification (e.g. "`abc123def456:3:2` should be
`parameterized`"):

1. Append a new record to `workspace/classifications.jsonl` with `"source":
   "human-correction"`, `confidence: 1.0`, and a current UTC ISO-8601 timestamp:

```json
{"element_id": "<id>", "tag": "<corrected>", "data_category": "<or null>", "authoring_component": "<primitive or none>", "confidence": 1.0, "rationale": "<their reason>", "routing_hint": "<optional>", "timestamp": "<UTC ISO-8601>", "source": "human-correction"}
```

2. Re-derive the queue by re-running `merge_validate_analysis.py --mode classify` (it
preserves your `human-correction` record and regenerates `review_queue.jsonl`). 3. Confirm the
correction. Corrections are append-only; latest record per `element_id` wins.

## Handling implementer answers

The Phase 2c question queue is **active** — the implementer answers it the same way they
correct a classification. When they answer a clarifying question (e.g. "for
`abc123def456:3:2` q1, it's a Top-N display, N=10"):

1. Append to `workspace/question_answers.jsonl` with `"source": "human-answer"` and a UTC
   timestamp:

```json
{"element_id": "<id>", "question_id": "<qid>", "answer": "<their answer>", "timestamp": "<UTC ISO-8601>", "source": "human-answer"}
```

2. Re-derive the queue by re-running `merge_validate_analysis.py --mode understand` so
   answered questions drop out (open = the `questions[]` in `element_understanding.jsonl`
   with no matching answer in `question_answers.jsonl`).
3. Confirm. Answers are append-only (last-write-wins per `element_id` + `question_id`); a
   **blocking** question left unanswered holds back authoring of that component.

## Output format

After running, present:

```
Analyzed <N> documents [<a> pptx, <b> docx, <c> pdf], <M> elements.

Classification distribution (tag):
  static                       <count>
  data-driven-quantitative     <count>
  data-driven-qualitative      <count>
  parameterized                <count>
  conditional                  <count>
  ambiguous                    <count>

Data category (data-driven elements):
  performance <c>   attribution <c>   holdings <c>   transactions <c>
  characteristics-risk <c>   allocation-exposure <c>   cash-flows <c>   fees <c>

Review queue: <K> elements (mostly ambiguous/decorative — skim, don't triage all).
  - <element_id> (<tag>/<data_category>, conf=<x.xx>): <rationale>
  - ...

Per-page components (vision):                     [when Phase 1.5 ran]
  <deck_id> p0: table 'Portfolio Characteristics' (0.88); chart 'Sector Weightings' (0.80); ...
  <deck_id> p1: ...
  (compare against what YOUR eye sees on each page — anything missing means the
   segmenter missed it; say so and I'll re-dispatch that deck)

Deep understanding: <D> table/chart elements analyzed (<G> sample groups, <Gm> multi-sample).
  vision-derived structures: <V> (needs confirmation — non-blocking 'vision-confirm'
  questions in the queue; structure read from renders, values never)

══════════════════════════════════════════════════════════════════════
⚠  BLOCKING QUESTIONS — <B> open. Each gates ONLY its own component
   chain (Block → Object → Shell + its Smart Page) — answer AS YOU
   BUILD, in plan order. Answering everything up front is NOT required.
══════════════════════════════════════════════════════════════════════
Top 5 by priority (compliance facets first):
  1. [<facet>] <element_id> (<data_category>)
     <question>
     options: <option a> | <option b>     default: <default>
     why: <the downstream Block/Object/disclosure decision this gates>
  2. ... (5 max)

Remaining <B − 5> blocking questions, grouped:
  by facet:         gross-net <c> | dynamic-columns <c> | benchmark <c> | ...
  by data category: performance <c> | holdings <c> | uncategorized <c> | ...
(say "show me all open questions" for the full list — it lives in
 workspace/question_queue.jsonl)

Other open questions (non-blocking, safe defaults exist, can defer): <Q − B>

Readiness:
  ❌ <B> blocking question(s) are open. Answer the top ones now or as you build
     (reply e.g. `for <element_id> q1: <choice>`); I'll record each in
     question_answers.jsonl and re-derive the queue. A component with an open
     blocking question cannot be built — the rest of the deck is unaffected.
  — or, when <B> == 0 —
  ✅ No open blocking questions.
  Either way: analysis alone never makes a deck ready to author — your DATA SOURCES are
  still unanalyzed. Next: /bind-sources <path to your CSV / Excel / Snowflake sources>.
  (No sources available? Say so explicitly and run /propose-build — it still works, but
  every Data Block keeps an open source question.)

<the pipeline footer — the verbatim output of pipeline_status.py (step 9)>
```

## Error handling

- **Corpus path missing**: return error, do not create workspace files.
- **No supported files found** (`ingest_content.py` exit code 3): tell the implementer the
  corpus has no `.pptx`/`.docx`/`.pdf` files.
- **Venv Python missing / `ModuleNotFoundError` for pdfplumber or python-docx**: prompt the
  one-time install (see Setup) and retry.
- **`ingest_content.py` exits non-zero otherwise**: report stderr; do not proceed.
- **`slice_payloads.py` exits non-zero**: report stderr and stop — the agents depend on the
  per-deck payloads; without them, do not fall back to feeding the monolithic inventory (that
  is the exact failure this slicer prevents). A payload-too-large *warning* is non-fatal (proceed,
  but expect that one oversized deck may still page).
- **`render_slides.py` exits 3 or 4** — branch on the corpus (see step 3):
  - corpus contains **PDF decks** (or `needs_vision` elements): a BROKEN ENV (venv missing
    `pymupdf`) — bootstrap and retry; never silently run structure-only, the flattened
    content would vanish from the analysis. Proceed structure-only only with the user's
    explicit go-ahead after telling them what will be lost.
  - **pptx-only + exit 4** (PowerPoint COM unavailable — non-Windows, no Office, missing
    `pywin32`): the expected graceful skip. No `slide_components.jsonl`; omit `--components`
    / `--vision-regions` from the classify/group calls and run structure-only. Flattened
    PPTX charts then surface as unreadable + a "provide the native file" source question —
    the documented exception.
  - **docx-only + exit 3**: no render path for Word — skip, note the Word asymmetry.
- **A workspace analyzed before the vision upgrade** (no `vision_regions.json`, renders
  possibly stale): re-run `/analyze-deck` fresh — old inventories lack the raster-page
  anchors (`needs_vision` placeholders), so flattened content cannot be recovered by
  re-running only the later phases. Missing render/crop files are stat-checked and simply
  omitted from payloads (the analyzer falls back to readable:false — never a crash).
- **A per-deck `segment_<deck_id>.jsonl` is missing/malformed**: the segment merge skips it
  (reports a count) and proceeds; re-dispatch that deck's segmenter if you want it covered.
- **A per-deck `classify_<deck_id>.jsonl` is missing or empty** (the agent errored): log it
  and continue — `merge_validate_analysis.py` processes the decks that did produce output;
  one failed deck must not abort the run. Re-dispatch that deck if you want it covered.
- **The grounding guard drops ids** (`merge_validate_analysis.py` prints a non-zero "dropped
  for unknown element_id" count on stderr): an agent emitted an id not in the inventory — it
  fabricated rather than read the file. The bad records are already excluded from the
  canonical files; surface the count to the implementer and **re-dispatch the affected
  deck/group** rather than trusting that run.
- **A per-group `understand_<group_id>.json` is missing/malformed**: same handling — the
  helper skips it (reports a count) and processes the rest; re-dispatch if needed.
- **`group_table_chart_samples.py` exits non-zero**: report stderr and skip Phase 2c; the
  inventory + classifications still stand.

## Workspace state

`./workspace/` holds: `corpus_inventory.json`, `payloads/` (the lean per-deck
`deck_<deck_id>.json` + per-group `understand_<group_id>.json` files the agents actually Read,
plus `payloads/crops/` — the deterministic component pre-crops the analyzer views),
`renders/<deck_id>/slide_*.png` (Phase 1.5 slide images + `deck.pdf`), `batches/` (the per-deck
`segment_<deck_id>.jsonl` +
`classify_<deck_id>.jsonl` and per-group `understand_<group_id>.json` machine outputs the
agents write), `slide_components.jsonl` (Phase 1.5 visual component map),
`vision_regions.json` (the code-minted synthetic elements for flattened data components),
`classifications.jsonl`,
`review_queue.jsonl`, and (Phase 2c) `table_chart_groups.json`, `element_understanding.jsonl`,
`question_queue.jsonl`, `question_answers.jsonl`.

The agents write **only** into `batches/` (and the renderer into `renders/`);
`merge_validate_analysis.py` produces the canonical `slide_components.jsonl` /
`classifications.jsonl` / `element_understanding.jsonl` (id-validated) and the
derived `review_queue.jsonl` / `question_queue.jsonl`. Re-running a full analysis regenerates
`batches/` and the canonical files, **preserving** any appended `human-correction` records
(in `classifications.jsonl`) and `human-answer` records (in `question_answers.jsonl`) —
latest record per key wins (`element_id`; `element_id`+`question_id` for answers). To start
fresh, delete `./workspace/`.
