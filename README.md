# Authoring Smart Pages for Assette

Welcome. This folder is your workspace for **creating, updating, and
publishing Smart Pages** — the PowerPoint templates that the Assette
Generation Engine turns into live, data-driven client reports.

You don't write code or YAML here. You **talk to Claude** (in Claude
Code or Claude Cowork), and Claude does the work using five skills
that ship together as the **Assette plugin**:

- The **`assette-plugin`** skill initializes the plugin on first use
  (installs the local Python helpers, captures your tenant code, signs
  you in) and handles tenant switches, server-URL changes, and cache
  management afterwards.
- The **`assette-pptx-authoring`** skill builds and verifies a
  **PowerPoint** Smart Page (`.pptx` + metadata).
- The **`assette-xlsx-authoring`** skill builds and verifies an
  **Excel** Smart Page (`.xlsx` + metadata) — the same save / preview /
  publish flow with `format="xlsx"`.
- The **`assette-block-author`** skill creates, updates, and wires the
  data blocks the Smart Page depends on (Python / SQL / Snowflake /
  Settings, etc.).
- The **`assette-data-object-author`** skill creates, updates, and
  dry-runs **data objects** — the catalogue records that wrap a
  `DataObjectDefinitionV2` YAML and expose Table / Text / Values /
  Bytes / List output to Smart Pages. It also manages **Dynamic Fields**
  — the shared, reusable input parameters (AccountCode, AsOfDate,
  Currency, …) data objects bind to — and reuses existing ones when
  authoring a new object's parameters.
- The **`assette-classifications`** skill reads and writes **content
  classification** — the editor's "Manage Content Classification"
  dialog — on any content type (Smart Pages, Smart Shells, Data Objects,
  Data Blocks, Brand Themes, …): buckets, limitations, compliance tags,
  and data ingredients.

The rest of this README walks you through:

1. [What you need before starting](#what-you-need-before-starting)
2. [First-time setup: connecting to your tenant](#first-time-setup-connecting-to-your-tenant)
3. [Creating your first Smart Page](#creating-your-first-smart-page)
4. [Updating an existing Smart Page](#updating-an-existing-smart-page)
5. [Searching the Assette application](#searching-the-assette-application)
6. [Switching tenants, signing out, or wiping caches](#switching-tenants-signing-out-or-wiping-caches)
7. [If something goes wrong](#if-something-goes-wrong)
8. [Where to go for more detail](#where-to-go-for-more-detail)

---

## What you need before starting

| Requirement | How to get it |
|---|---|
| **Claude Code or Claude Cowork** installed on your machine | Download Claude Code from [claude.com/claude-code](https://claude.com/claude-code), or use Cowork through the Claude desktop app. |
| **Python 3.10 or newer** on your PATH | Install from [python.org/downloads](https://www.python.org/downloads/). On Windows, choose the installer from python.org — **not** the Microsoft Store version (its sandbox breaks the credential cache). Make sure "Add Python to PATH" is checked. On macOS, the python.org installer or `brew install python@3.12` both work. |
| **The Assette plugin installed in your host** | Through the plugin marketplace, or via your IT contact's local install instructions. |
| **An Assette user account** | Same one you use for the Assette web application. Your IT contact can confirm. |
| **Your Assette client code** | A 4-letter identifier (e.g. `MODL`, `ACME`) for your tenant. Ask your IT contact or check an existing Assette URL — it's the four letters near the end. |

That's it. You do **not** need to install:
- Microsoft PowerPoint
- The Assette VSTO plugin
- Any Python libraries (the plugin installs them for you automatically)
- Anything Azure-related (sign-in happens through your normal browser)

---

## First-time setup: connecting to your tenant

After the plugin is installed in your host, start a new conversation
and say:

> *"initialize Assette plugin"*

The `assette-plugin` skill takes over and walks you through three
steps end-to-end:

1. **Verifies Python is ready.** If Python 3.10+ isn't on your PATH,
   you'll get a clear "install Python first" message with a link.
2. **Sets up the local Python environment** inside the plugin's own
   directory (~30–60 seconds, one time only). You don't have to do
   anything — the skill calls `mcp__assette__bootstrap` which creates
   the venv and installs the pinned dependencies in the background.
   **No host restart needed.**
3. **Asks for your 4-letter client code**, validates it (e.g. `MODL`,
   `ACME`), saves it via `mcp__assette__set_client_code`, then opens
   your default web browser to the Assette sign-in page. Sign in with
   your normal Assette credentials. The browser will say
   *"Authentication complete, you can close this tab"* — close it and
   return to your host.

Your settings land in a per-user location managed by the plugin:

- **Windows**: `%LOCALAPPDATA%\Assette\authoring\`
- **macOS**: `~/Library/Application Support/Assette/authoring/`

That folder holds `shim-config.json` (your tenant + server URL),
`msal/` (your encrypted refresh token), and the working caches
(`DataObjects/`, `SmartPages/`, `DataBlocks/`, `response_cache/`).
Plugin upgrades never touch it, so your settings survive every
upgrade.

After that one-time setup, you stay signed in across all future
sessions. You only re-sign-in if you switch tenants, change machines,
or explicitly sign out (see the [Switching tenants section](#switching-tenants-signing-out-or-wiping-caches) below).

---

## Creating your first Smart Page

Just ask Claude in plain English. The magic phrase is to include both
**"PowerPoint"** (or **"pptx"**) AND **"Smart Page"** (or
**"SmartPage"**). That tells Claude to use the fabricator skill rather
than treating it as a generic file question.

### Examples that work well

> "Create a Smart Page pptx with a title slide that shows the account
> name, and a second slide with a holdings table grouped by sector."

> "Build a SmartPage PowerPoint for our quarterly review. Slide 1 has
> the client name and report date. Slide 2 shows a returns chart for
> the last 4 quarters. Slide 3 is a one-page summary of the top 10
> holdings."

> "Fabricate a Smart Page .pptx that uses the Performance data object
> with a line chart over the last 12 months."

### Examples that don't trigger the skill

> *"Make me a PowerPoint template."*   → too generic
>
> *"Generate a pptx."*  → missing "Smart Page" / "SmartPage"
>
> *"Create a smart PowerPoint."*  → wrong word order ("Smart Page" must appear as two words or as "SmartPage")

If Claude looks confused about a Smart Page request, double-check that
both required words are in your message.

### What happens after you ask

Claude will:

1. Ask any clarifying questions it needs — typically *"which data
   objects should this use?"*, *"what columns do you want in the
   table?"*, *"any specific theme or layout?"*
2. If a data object isn't already cached locally, Claude fetches its
   shape (column names, types, parameters) from the Assette server.
   This is automatic and takes a second.
3. Generate the spec, build the `.pptx` and its companion
   `metadata.json` file, then verify they're consistent.
4. Save the pair under `SmartPages/<Name>/` so you (and Claude) can
   open them later.
5. **Ask whether you want to publish it to Assette**. If yes, Claude
   uploads it to the application; if no, the files stay on your
   machine until you say so. You can re-trigger the upload any time:
   *"Publish the Smart Page I just made to Assette."*

That's it. No YAML, no command line, no manual file editing.

---

## Updating an existing Smart Page

Two flavours: editing a Smart Page that's already in Assette, or
tweaking one you just created locally.

### Editing one that's already in Assette

Tell Claude the name (or ask it to search if you don't remember exactly):

> "Find the SmartPage pptx called Quarterly Review and add a new slide
> with a sector-breakdown chart from the Allocations object."

> "Update the holdings table in the existing SmartPage pptx
> 'Year-End Statement' to also show the cost basis column."

Claude will:

1. Search the Assette application for the matching Smart Page.
2. Download it into `SmartPages/<Name>/` on your local machine.
3. Make the change you asked for, regenerating the `.pptx` and
   `metadata.json` as needed.
4. Verify the change.
5. Ask whether you want to publish the updated version back to Assette.

### Tweaking one you just created (still local)

Just keep talking to Claude in the same conversation:

> "On slide 2, change the column order to put MarketValue first."
>
> "Make the title font bigger on slide 1."
>
> "Remove the disclosure label from slide 3."

Claude re-runs the fabrication and overwrites the local files.
Whenever you're ready, ask it to publish.

---

## Searching the Assette application

You can ask Claude to look things up without making changes:

> "Show me all the SmartPage pptx files I've published."
>
> "Which Smart Pages use the Holdings data object?"
>
> "What's the layout of the SmartPage called 'Monthly Performance Review'?"

Claude uses the `assette-mcp-shim` to query the Assette server and shows
you the results. Useful for reconnaissance before editing.

---

## Switching tenants, signing out, or wiping caches

The same `assette-plugin` skill that handled first-time setup also
handles lifecycle changes. Just talk to Claude:

| What you say | What happens |
|---|---|
| *"Switch my Assette tenant to ABCD."* | Claude updates your client code, clears your old credentials, and wipes the local DataObjects / SmartPages / DataBlocks caches (because they were specific to the old tenant). **No restart needed** — the change is picked up on the very next Assette action. |
| *"Point at the Assette staging server."* | Claude asks for the staging URL, saves it, clears cached credentials, wipes the data caches. **No restart needed.** |
| *"Increase my upload limit to 500 MB."* | Claude saves the new cap. **No restart needed.** |
| *"Sign me out of Assette."* | Claude clears your saved credentials. Next Assette action will pop the browser sign-in again. No restart needed. |
| *"Wipe my Assette caches."* | Claude clears the local DataObjects, SmartPages, and DataBlocks folders — useful if you suspect stale data. No restart needed. (Those three folders are also wiped automatically at the start of every session.) |
| *"Show me my current Assette settings."* | Claude prints your tenant, server URL, upload cap, and whether you're signed in. Read-only — nothing changes. |
| *"Reset my Assette config."* | Claude wipes everything: settings, credentials, caches. Next time you sign in, you'll set up from scratch. Useful after a botched setup. |

You don't need to memorise these phrases — Claude understands a wide
range of similar requests. *"Change my Assette tenant"*, *"switch
clients to ABCD"*, *"sign me out"*, *"my data looks stale"* all do the
right thing.

---

## If something goes wrong

| Symptom | What to try |
|---|---|
| Claude says it can't connect to the Assette MCP server. | Ask Claude to *"initialize Assette plugin"* — that re-runs the Python check, the bootstrap, and the sign-in. Most common cause: Python isn't on PATH. Reinstall from python.org and tick "Add to PATH". |
| The browser sign-in page shows an error code like `AADB2C90006`. | Your IT contact needs to pre-register `http://localhost:8080/callback` on the Assette B2C app for your tenant. Ask them to add it. |
| The browser pops the sign-in window every single Assette action. | The credential cache isn't persisting. On Windows, this usually means you installed Python from the Microsoft Store instead of python.org — reinstall from python.org and ask Claude to *"reset my Assette config"*. |
| Smart Page upload fails with a size error. | Ask Claude to *"increase my Assette upload limit to 500 MB"* (or whatever size you need). |
| Data looks stale. | Ask Claude to *"wipe my Assette caches"* — your next action will refetch from the server. |
| A search or `execute_data_block` reply came back as `{ "cached": true, "path": "...", ... }` instead of the actual data. | That's normal — bulky responses from `search_contents`, `search_smart_pages`, `search_data_objects`, `search_blocks`, `execute_data_block`, and `preview_data_block` are auto-cached to `response_cache/<tool>_<timestamp>.json` under your runtime root. Claude reads the file on demand. Files older than 7 days are purged automatically. |
| `get_data_object_metadata` returned a `{ "cached": true, "path": ".../DataObjects/<Name>.json", ... }` envelope. | Also normal — the metadata document is written to `DataObjects/<Name>.json` (under your runtime root) so the Smart Page fabricator can read it from disk. The envelope tells Claude where to look. The folder is wiped at every fresh shim start, so a new session always re-fetches live metadata. |
| `get_data_block_with_dependent_blocks` returned a `{ "cached": true, "path": ".../DataBlocks/<Name>.blocks.json", ... }` envelope. | Also normal — the block and all its dependencies are written to `DataBlocks/<Name>.blocks.json` in exactly the format `preview_data_block` accepts, so Claude can edit the file and preview a change before uploading it. The folder is wiped at every fresh shim start. |
| `get_block` returned a `{ "cached": true, "path": ".../DataBlocks/<Name>.block.json", ... }` envelope. | Also normal — the full block definition (`DPFBlockData`) is written to `DataBlocks/<Name>.block.json` under your runtime root so Claude can edit it on disk and hand it back to `update_block` with just the filename (e.g. `blockPath="MyBlock.block.json"`). The folder is wiped at every fresh shim start. |
| You're not sure which tenant you're on. | Ask Claude to *"show my Assette config"*. |
| Everything's broken and you want a clean start. | Ask Claude to *"reset my Assette config"*. You'll be prompted for your client code again like a fresh install. No host restart needed. |

If none of those help, the [skill's troubleshooting section](./skills/assette-pptx-authoring/creating-smart-pages.md#troubleshooting) has more detailed diagnostics.

---

## Where to go for more detail

The friendly README ends here, but everything that powers it lives
one level down. Four paths in if you want to learn more:

- **[`skills/assette-plugin/`](./skills/assette-plugin/)** — the init / config skill.
  - [`SKILL.md`](./skills/assette-plugin/SKILL.md) — the eight operations Claude follows when you ask to initialize, switch tenants, sign out, reset, etc.
- **[`skills/assette-pptx-authoring/`](./skills/assette-pptx-authoring/)** — the Smart Page builder.
  - [`README.md`](./skills/assette-pptx-authoring/README.md) — overview of the skill folder.
  - [`creating-smart-pages.md`](./skills/assette-pptx-authoring/creating-smart-pages.md) — the full end-user guide with the YAML spec format, all three workflows, and troubleshooting.
  - [`smart-page-spec-reference.md`](./skills/assette-pptx-authoring/smart-page-spec-reference.md) — field-by-field reference for the spec YAML.
- **[`skills/assette-xlsx-authoring/`](./skills/assette-xlsx-authoring/)** — the Excel Smart Page builder.
  - [`SKILL.md`](./skills/assette-xlsx-authoring/SKILL.md) — entry point + workflows (fabricate / convert / edit) for Excel Smart Pages.
  - [`README.md`](./skills/assette-xlsx-authoring/README.md) — overview of the skill folder.
  - [`reference/`](./skills/assette-xlsx-authoring/reference/) — editor vocabulary, spec reference, `ExcelMetaData` schema, generation semantics.
- **[`skills/assette-block-author/`](./skills/assette-block-author/)** — the data-block author.
  - [`SKILL.md`](./skills/assette-block-author/SKILL.md) — entry point + workflows for creating, editing, and wiring data blocks.
  - [`creating-data-blocks.md`](./skills/assette-block-author/creating-data-blocks.md) — categories, output types, dependencies, secrets.
  - [`block-type-recipes.md`](./skills/assette-block-author/block-type-recipes.md) — per-block-type recipe cards.
- **[`skills/assette-data-object-author/`](./skills/assette-data-object-author/)** — the data-object author.
  - [`SKILL.md`](./skills/assette-data-object-author/SKILL.md) — entry point + workflows (create / edit / dry-run).
  - [`managing-data-objects.md`](./skills/assette-data-object-author/managing-data-objects.md) — the eight MCP tools and the author lifecycle.
  - [`definition-anatomy.md`](./skills/assette-data-object-author/definition-anatomy.md) — every field of `DataObjectDefinitionV2`.
  - [`flavour-recipes.md`](./skills/assette-data-object-author/flavour-recipes.md) — per-output-type recipes (data / text / values / bytes / list).
- **[`skills/assette-classifications/`](./skills/assette-classifications/)** — content classification.
  - [`SKILL.md`](./skills/assette-classifications/SKILL.md) — the four facets (Buckets / Limitation / ComplianceTag / Ingredients), object types & levels, exact label-value formats, the dialog-vs-tools vocabulary map, and the discover → set → verify workflow.
- **[`shim/`](./shim/)** — the local Python helper that connects Claude to Assette.
  - [`README.md`](./shim/README.md) — how the bootstrap, auth, and tool-proxy layers work.

You don't need to read either of those to use the workflows above. They
exist for the day you want to understand or extend what's happening
under the hood.

---

Happy authoring. If you build a Smart Page worth sharing, drop it in
the `SmartPages/` folder so your teammates can see how it's wired
together.
