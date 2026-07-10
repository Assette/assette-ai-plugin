---
name: assette-plugin
description: Invoke as `assette:assette-plugin` (Claude Code namespaces every plugin skill as `<plugin>:<skill>`; the bare `assette-plugin` does not resolve). Initialize, authenticate, and manage the Assette MCP plugin. Drives first-time setup (capture client code + server URL, open the browser for B2C sign-in, then download the author-facing skills from the server), updates/syncs those downloaded skills to the latest versions, switches the Assette tenant (clientCode), changes the hosted MCP server URL — including by environment alias (`local`, `dev`/`development`, `qa`, `prd`/`production`) which resolve to the canonical Assette URLs — raises or lowers the upload size cap, clears cached B2C credentials, wipes the local DataObjects / SmartPages / DataBlocks cache folders, or just shows the current effective config. Triggers on phrases like "initialize Assette plugin", "init assette", "set up Assette", "first-time Assette setup", "connect me to Assette", "update Assette plugin", "update assette", "sync Assette skills", "download the Assette skills", "sign in to Assette", "authenticate me", "log in", "change my Assette client code", "switch Assette tenant", "set Assette server to dev", "set Assette MCP server url to production", "update Assette MCP server to use local server url", "use the qa MCP server", "point at the production server", "increase upload limit to N MB", "clear Assette credentials", "sign out from Assette", "wipe my Assette caches", "reset my Assette config", "show current Assette settings", "what's my Assette tenant".
---

# Assette MCP plugin — initialization + configuration

This skill is the **single source of truth for mutating** the
assette-mcp-shim's local config AND for driving the lifecycle
operations the user might ask for:

- **initialize the plugin** on a fresh install (capture client code,
  open the browser for B2C sign-in),
- **sign in to Assette** when re-auth is needed,
- switch which Assette tenant the shim talks to (`clientCode`),
- point at a different hosted MCP server (`serverUrl`),
- raise or lower the upload-size cap (`maxFileSizeMb`),
- clear cached B2C credentials (force re-sign-in),
- wipe local content caches, OR
- inspect the current effective configuration.

**The shim hot-reloads its config on every tool call.** A change to
`shim-config.json` is picked up by the very next `mcp__assette__*`
call — the shim notices the file's mtime advanced, tears down the
cached tenant / token / upstream client triple, and rebuilds against
the new settings. **No host restart needed** for any of the three
user-facing settings.

**Always mutate through this skill's tools — never edit the config
file by hand.** The skill writes through the
`mcp__assette__set_client_code` synthetic tool which validates inputs,
preserves unrelated keys, and atomically updates the file. Manual
edits skip the validation and can produce a config the shim then
silently rejects.

## Where things live

The unified plugin keeps **user-scoped state** outside the plugin
install dir so a plugin upgrade never blows away the user's tenant
config or downloaded content. All paths live under one per-OS root:

| OS | `<runtimeRoot>` |
|---|---|
| Windows | `%LOCALAPPDATA%\Assette\authoring\` |
| macOS | `~/Library/Application Support/Assette/authoring/` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/assette/authoring/` |

Under that root:

| Path | Contents |
|---|---|
| `<runtimeRoot>/shim-config.json` | The three user-facing settings (`clientCode`, `serverUrl`, `maxFileSizeMb`). Per-developer. **Written exclusively through `mcp__assette__set_client_code`** — never edit by hand. |
| `<runtimeRoot>/skills.json` | Local manifest of the downloaded author skills + their installed versions. Written by this skill (Read/Write tools) during Initialize / Update. Its shape mirrors the server's catalog. |
| `<runtimeRoot>/msal/<CODE>.json` | MSAL token cache, one file per tenant. Plain JSON (`tokenCache.serialize()` output) with mode 0o600. Lives in the per-user-private LOCALAPPDATA / Library / XDG dir; no OS-level encryption layer in v0.2.0 (the pre-v0.2.0 Python shim used DPAPI/Keychain/libsecret via msal-extensions; the Node bundle dropped that to keep the bundle native-dep-free and cross-platform from a single .js). |
| `<runtimeRoot>/response_cache/*.json` | Bulky-response cache (timestamped). Auto-purged at >7 days on every shim start. |
| `<runtimeRoot>/DataObjects/<Name>.json` | Per-tenant cache of `get_data_object_metadata` responses. **Wiped on every shim start** AND on tenant / server-URL change. |
| `<runtimeRoot>/SmartPages/<Name>/` | Per-tenant cache of Smart Page `.pptx` + `metadata.json` + `.assette.json` triples. Same wipe rule. |
| `<runtimeRoot>/DataBlocks/<Name>.block.json` / `<Name>.blocks.json` | Per-tenant cache of `get_block` (`DPFBlockData`) and `get_data_block_with_dependent_blocks` (previewRows) results. Same wipe rule. |

The plugin install dir (typically a Cowork-managed or Claude Code
plugin-marketplace location) holds:

| Path | Contents |
|---|---|
| `<plugin>/bin/assette-mcp-shim.bundle.js` | The Node.js MCP shim. Single-file esbuild bundle (~7 MB), no native deps, runs on Node 22+ (Claude Code's own runtime). This is what `plugin.json` spawns. |
| `<plugin>/shim/.venv/` | Python venv used **only by the Smart Page fabricator skill** for the pptx-helper scripts. Created on first `mcp__assette__bootstrap` call from the fabricator skill. Not needed for sign-in or any of the 19 `mcp__assette__*` upstream tools. |
| `<plugin>/shim/assette_mcp_shim/scripts/` | The five fabricator helper scripts (Python). |
| `<plugin>/skills/assette-plugin/` | This skill — the ONLY skill shipped in the plugin. |
| `<plugin>/skills/<other>/` | The six author-facing skills, **downloaded on demand** by Initialize / Update (not shipped). Extracted here by `get_skill` so Claude Code auto-discovers them next session. |

`shim_status` reports the resolved values of all of these paths so you
can confirm where the shim is reading from before mutating anything.

## `shim-config.json` file shape

A single JSON object with three keys. All three are optional; missing
keys fall back to the baked-in defaults:

```json
{
  "clientCode": "MODL",
  "serverUrl": "https://app.assette.com/mcp",
  "maxFileSizeMb": 200
}
```

| Key | Default | Notes |
|---|---|---|
| `clientCode` | *(empty — required for any real tool call)* | 4-letter tenant identifier. Empty / missing → first `mcp__assette__*` call returns `shim.config.client_code_missing`. |
| `serverUrl` | `https://app.assette.com/mcp` | Trailing slashes are stripped. |
| `maxFileSizeMb` | `200` | Positive int ≤ 10240; cap above which `*Path` inputs are rejected before the upstream call. |

## Synthetic tools this skill calls

All three are always available, even before any client code is
configured or a venv exists:

| Tool | Purpose |
|---|---|
| `mcp__assette__shim_status` | Side-effect-free diagnostic. Returns the effective config, venv state, MSAL cache state, runtime paths, and node version. **Always safe to call.** |
| `mcp__assette__bootstrap` | **Fabricator-only.** Ensures the Smart Page fabricator's `<plugin>/shim/.venv/` exists and the pinned pptx deps (python-pptx, lxml, pydantic, pyyaml, deepdiff) import cleanly. Idempotent. Returns `OK_ALREADY` / `OK_INSTALLED` / `ERR_NO_PYTHON` / `ERR_VENV_FAILED` / `ERR_PIP_FAILED` plus diagnostic tail. ~20-30s on a fresh install. **This skill (assette-plugin) does NOT call bootstrap as part of initialize** — the fabricator skill calls it on its own first op. Authors who only use the 19 `mcp__assette__*` upstream tools never need this. |
| `mcp__assette__set_client_code` | Atomically writes the per-user `shim-config.json`. Accepts `clientCode` (required, validated `^[A-Z]{4}$`), `serverUrl` (optional), `maxFileSizeMb` (optional). Preserves unsupplied fields. Returns the absolute path written. |

## Credential cache (`<runtimeRoot>/msal/`)

The shim's MSAL token cache is one file per tenant, keyed by client
code (e.g. `MODL.json`, `ACME.json`). To clear the cache for a specific
tenant, delete the corresponding `.json` file using the **Bash tool**
(cross-platform; `rm -f <path>`) or the **PowerShell tool** on
Windows (`Remove-Item -Force <path>`). Always use `-Force` / `rm -f`
so a missing file doesn't fail the call.

**v0.1.x → v0.2.0 upgrade note:** the pre-v0.2.0 Python shim wrote
`<CODE>.bin` (DPAPI/Keychain/libsecret-encrypted) at the same path.
The Node bundle writes `<CODE>.json` (plain JSON, mode 0o600) and
ignores any `.bin` files left over. On the first sign-in after
upgrade, the user gets one interactive flow; from then on the new
`.json` cache handles silent refresh. If a user complains about
`.bin` files cluttering the msal/ directory, it's safe to delete
them — nothing reads them anymore.

## Wiping data caches

The `python -m assette_mcp_shim.scripts.wipe_tenant_caches` helper is
the bounded, parameterized replacement for ad-hoc `rm -rf`. It only
ever clears the **contents** of the fixed allowlist `SmartPages`,
`DataObjects`, `DataBlocks` (it has no `--project-dir` flag, so it
can't be aimed elsewhere). It keeps the parent folders, prints a
per-folder count of what it removed, and supports:

- `--dirs DataObjects,SmartPages` — wipe only a subset (names must be
  in the allowlist).
- `--dry-run` — list what would be removed without deleting anything.

Use it everywhere this skill says "run the wipe command" below. Do
**not** hand-roll an `rm -rf` — the script is the single source of
truth for the wipe.

## Server-delivered skills

Only **this** skill (`assette-plugin`) ships inside the installed plugin. The
six **author-facing** skills —

- `assette-general`
- `assette-block-author`
- `assette-classifications`
- `assette-data-object-author`
- `assette-pptx-authoring`
- `assette-xlsx-authoring`

— expose Assette internals and are therefore **not** shipped in the public
plugin. They live inside the hosted, B2C-gated MCP server and are **downloaded
on demand** by the *Initialize* / *Update* operations below, into the installed
plugin's own `skills/` directory, only after the user has signed in.

Two **upstream** `mcp__assette__*` tools drive this (they ride the normal B2C
gate, so they only work once the user is authenticated):

| Tool | Purpose |
|---|---|
| `mcp__assette__get_skill_versions` | Returns the server catalog — `{ statusCode, body }` where body is `{ "skills": [ { "skill", "latestVersion" }, … ] }`. |
| `mcp__assette__get_skill` | Downloads one skill and installs it. Takes `skill` (required) and `path` (optional extract dir). The **shim** decodes the returned zip, **wipes the target dir, and extracts** into it; the reply is a small `{ installed, skill, version, path, files, bytes }` envelope (never the zip bytes) — `version` is the current on-disk version reported by the server. Default target when `path` is omitted: `<pluginSkillsDir>/<skill>`. |

Paths come from `mcp__assette__shim_status`:

- `runtime.pluginSkillsDir` — where downloaded skills are extracted
  (`<pluginRoot>/skills`). The per-skill target is `<pluginSkillsDir>/<skill>`.
- `runtime.localSkillsManifest` — the **local** `skills.json`
  (`<runtimeRoot>/skills.json`, beside `shim-config.json`) tracking which
  versions are already installed. `runtime.localSkillsManifestExists` reports
  whether it's there yet.

The local `skills.json` has the same shape as the server's:

```json
{ "skills": [ { "skill": "assette-block-author", "latestVersion": "1.0" }, … ] }
```

### Sync skills (shared subroutine)

Both *Initialize* and *Update* run this. It assumes the user is already signed
in (an authenticated `mcp__assette__*` call has succeeded).

1. Read `mcp__assette__shim_status`; capture `runtime.pluginSkillsDir` and
   `runtime.localSkillsManifest`.
2. Read the local manifest with the **Read tool** if
   `runtime.localSkillsManifestExists` is true; otherwise treat every skill as
   "not installed". Parse it into a `{ skill → installedVersion }` map.
3. Call `mcp__assette__get_skill_versions`. If it returns a non-200
   (`statusCode` in `body`), stop and report — the server has no catalog.
4. For each `{ skill, latestVersion }` in the server catalog, download when the
   skill is **missing locally** OR `latestVersion` differs from the installed
   version (treat any difference as "update available"):
   - Call `mcp__assette__get_skill(skill=<skill>,
     path="<pluginSkillsDir>/<skill>")`. Build the `path` by joining
     `runtime.pluginSkillsDir` + the OS separator + `<skill>` (the shim wipes
     that dir and extracts into it). Passing `path` explicitly (rather than
     relying on the default) keeps the target unambiguous. The server always
     packs the current on-disk copy; the version comes back in the response
     envelope (`version`), which you record in the local manifest in step 6.
   - On `installed: true`, record the new version.
   - On a `shim.skill.extract_failed` error (e.g. the plugin lives in a
     read-only store), report the exact `path` from the error and stop — the
     author must make that directory writable (or the plugin must be installed
     somewhere writable). Do **not** silently continue.
5. Skip skills already at the latest version (report them as "up to date").
6. Write the refreshed manifest back to `runtime.localSkillsManifest` with the
   **Write tool** — the full server catalog (all six skills + their
   `latestVersion`s that were successfully installed/confirmed).
7. Tell the user which skills were installed / updated / skipped, then instruct
   them to **start a new Claude Code session** so the host discovers the
   freshly-installed skills (`/reload-plugins` may also pick them up
   mid-session, but a new session is the reliable path).

## Operations

### -1. Initialize the Assette plugin (first-time setup)

Trigger phrases: *"initialize Assette plugin"*, *"set up Assette"*,
*"first-time Assette setup"*, *"connect me to Assette"* on a fresh
install. Also fall through to this op from any other op when
`shim_status` reports `config.fileExists == false` — there's nothing
useful to do until the plugin is initialized.

**Background (v0.2.0+).** The shim is a Node.js single-file bundle.
Claude Code itself runs on Node, so the runtime is guaranteed
present — there is **no Python prerequisite for sign-in or any of
the 19 `mcp__assette__*` upstream tools.** Python is only needed
later if the user invokes the Smart Page fabricator skill (which
calls `mcp__assette__bootstrap` itself before its first op). This
operation MUST NOT trigger bootstrap — wasting 20-30s on a venv
install that most authors never use is bad first-run UX.

**Procedure.**

1. **Probe with `mcp__assette__shim_status`.** Side-effect free. Read:
   - `runtime.root` — where things will live
   - `runtime.nodeVersion` — Node runtime version (informational)
   - `config.fileExists` — has the user been through init before?
   - `config.clientCode` — if non-empty, the user has already initialized
   - `auth.cacheFileExists` — is there already a cached refresh token?

   `runtime.venvReady` is also reported, but **do not act on it**
   here. False is the normal expected state at this stage; the
   fabricator skill triggers bootstrap on demand if and when the
   user invokes it. Mentioning the venv at all here just confuses
   the user.

2. **Capture the Assette `clientCode`.** Decision tree:

   - If `shim_status.config.clientCode` is already non-empty: ask
     *"You're configured for tenant `<CODE>`. Re-initialize? This
     will clear that tenant's credentials and re-prompt sign-in."*
     If the user confirms, proceed; if not, just call `whoami` (step
     4) so they sign in and stop.
   - Otherwise: ask *"What's your 4-letter Assette client code?
     (Tenant identifier — exactly 4 letters, e.g. MODL or ACME.
     This is registered with the hosted server.)"*
   - Validate the answer client-side (trim → uppercase → must match
     `^[A-Z]{4}$`). Re-prompt on invalid input, naming the rule.

3. **Capture the MCP server URL.** Read `shim_status.config.serverUrl`.
   - If the config file does **not** exist yet (`config.fileExists == false`),
     the server URL is only the baked-in default — **ask** the user which
     environment to use: *"Which Assette environment? Say `prd` (production,
     the default), `qa`, `dev`, or paste a full server URL."* Accept an
     environment alias (`local` / `dev` / `qa` / `prd`) or a full `http(s)://`
     URL — pass it through verbatim (see Op 2; the shim resolves aliases). If
     the user just wants the default, use `prd`.
   - If the config file already exists, keep the configured `serverUrl` — don't
     re-prompt.

4. **Persist via `mcp__assette__set_client_code`.** Call it with the captured
   client code (and `serverUrl` when you prompted for one). It returns the
   absolute file path written — echo it back so the user knows where their
   config lives.

   ```
   mcp__assette__set_client_code(clientCode="MODL", serverUrl="prd")
   → {path: "C:\\Users\\<you>\\AppData\\Local\\Assette\\authoring\\shim-config.json",
       clientCode: "MODL", serverUrl: "https://app.assette.com/mcp",
       maxFileSizeMb: 200}
   ```

5. **Initiate the B2C sign-in.** Call `mcp__assette__whoami` (no
   arguments). The first call:
   - Loads the freshly-written `shim-config.json`.
   - Runs tenant discovery against `settings.serverUrl`.
   - Tries silent MSAL acquire — will fail on first install (no
     cached refresh token), falling through to interactive.
   - Opens the system browser; MSAL Node's built-in loopback receiver
     listens on `http://localhost:8080/callback` for the B2C
     interactive sign-in response.

   While the browser opens, tell the user once:

   *"Your browser should open for the Assette sign-in window.
   Complete the B2C sign-in there — I'll see the result once you're
   done."*

   Do NOT loop or re-call `whoami`. The shim's call blocks until
   the user finishes (or closes the tab) — when control returns,
   the response is either the identity payload (success) or a
   `shim.auth.*` error.

6. **Handle the sign-in outcome.**
   - On success: **continue to step 7** (download the skills) — do NOT declare
     "done" yet.
   - On `shim.auth.no_account` (user closed the browser): tell them
     *"The sign-in window was closed before the flow finished. Run
     the same request again when you're ready, and complete the
     sign-in in the browser."* Stop.
   - On `shim.auth.token_refused` or `shim.auth.interactive_failed`:
     surface the structured error and point at Op 4 / Op 1 as
     appropriate. Stop.

7. **Download the author-facing skills.** Run the **Sync skills** subroutine
   (see "Server-delivered skills" above): read the local manifest, call
   `get_skill_versions`, download each missing/outdated skill into
   `<pluginSkillsDir>/<skill>`, and write the refreshed local `skills.json`.

8. **Report and prompt a restart.** On a clean sync:
   *"Initialized! Signed in as `<name>` (`<email>`) for tenant `<CODE>`, and
   downloaded the Assette author skills (<list of skills>). **Start a new Claude
   Code session** to load them. (If you later use the Smart Page fabricator,
   that skill installs its own Python venv on first use — nothing to do now.)"*
   If a skill failed with `shim.skill.extract_failed`, report which one and the
   target path, and that the directory must be writable.

**What this op explicitly does NOT do (v0.2.0+):**

- **Does NOT verify Python is on PATH.** The Node bundle doesn't
  need Python for sign-in or any of the 19 upstream tools.
- **Does NOT call `mcp__assette__bootstrap`.** The fabricator venv
  is the fabricator skill's responsibility; building it here would
  waste 20-30s on most users who never touch the fabricator.
- **Does NOT mention `runtime.venvReady`** in the user-facing output.
  Its value at this stage is meaningless — false is normal.

### 0. Sign in to Assette (re-auth on demand)



Trigger phrases: *"sign me in to Assette"*, *"log in to Assette"*,
*"authenticate with Assette"*, *"open the Assette sign-in window"*,
*"can I sign in?"*.

If `shim_status.config.fileExists == false`, **fall through to Op -1**
— the user needs to initialize first.

**Background.** The shim authenticates lazily — the first time ANY
`mcp__assette__*` tool is called, the shim runs tenant discovery,
then either silently acquires a token from the OS-encrypted MSAL
cache OR opens the system browser for B2C interactive sign-in. The
user usually never has to ask for sign-in explicitly: it happens on
demand.

But sometimes a user wants to sign in *up front* — to confirm their
credentials work before kicking off a long task, to refresh after
travelling between identity providers, or just to see "am I signed
in?". This operation provides that on-demand path.

**Procedure.**

1. **Probe with `mcp__assette__shim_status`.** If
   `auth.cacheFileExists == true`, the user has a cached refresh
   token for this tenant. Surface that fact (*"Already signed in
   for tenant `<CODE>` — cache at `<auth.cacheFile>`. To force a
   re-sign-in, use Op 4 (clear credentials) first."*) and skip the
   rest of this operation.

2. **Kick off the sign-in flow.** Call `mcp__assette__whoami` (no
   arguments). Same flow as Op -1 step 4.

3. **Report the outcome.** Same as Op -1 step 5.

**What this operation does NOT do:**

- It does NOT change the tenant — sign-in always uses the
  currently-configured `clientCode`. To sign in *as a different
  tenant*, switch tenants via Op 1 (which clears the old tenant's
  MSAL cache, so the next sign-in re-prompts).
- It does NOT bypass the local config check. A missing client code
  surfaces as a `shim.config.client_code_missing` error; the user
  has to fix that first (Op -1 or Op 1).

### 1. Change the Assette client code (tenant)

1. **Ask the user** for the new client code if they didn't already
   supply it. Phrase it as: *"What's the new Assette client code?
   (Tenant identifier — exactly 4 letters, e.g. MODL or ACME.)"*
2. **Validate** client-side: trim → uppercase → must match
   `^[A-Z]{4}$`. Re-prompt on invalid input, naming the rule.
3. **Capture the OLD client code** by reading
   `shim_status.config.clientCode` BEFORE the write — you'll need
   it to clean up the old credential cache.
4. **Write via `mcp__assette__set_client_code`** with only the
   `clientCode` argument. The synthetic tool preserves `serverUrl`
   and `maxFileSizeMb` unchanged.
5. **Clean up the old tenant's credential cache.** Delete
   `<runtimeRoot>/msal/<OLD_CODE>.json` via Bash (or PowerShell on
   Windows). Skip silently if there was no old code (first-time
   setup) or the file isn't there.
6. **Wipe the data caches** — they're scoped to the old tenant. Run:

   ```bash
   python -m assette_mcp_shim.scripts.wipe_tenant_caches
   ```

   The fabricator and `mcp__assette__*` tools repopulate on demand.
   The script no-ops harmlessly if the folders are missing.

7. **Tell the user**: *"Saved new client code `<NEW>`. Old tenant's
   credentials and local DataObjects / SmartPages / DataBlocks
   caches cleared. The shim will pick up the new tenant on the next
   `mcp__assette__*` call — no restart needed. You'll be prompted to
   sign in to `<NEW>` on the first call."*

### 2. Change the MCP server URL

1. **Pass the alias straight through — the shim resolves it.** The
   `mcp__assette__set_client_code` tool now resolves environment
   aliases **in code** (this table is mirrored from
   `ENV_SERVER_URLS` in the shim, which is the authoritative source):

   | Alias the user says | Resolved `serverUrl` |
   |---|---|
   | `local` | `https://localhost:7038/mcp` |
   | `dev` / `development` | `https://app.dev01.assette.com/mcp` |
   | `qa` | `https://app.qa01.assette.com/mcp` |
   | `prd` / `prod` / `production` | `https://app.assette.com/mcp` |

   So for *"set assette server to dev"* you pass
   `serverUrl="dev"` and the shim writes
   `https://app.dev01.assette.com/mcp`. **Do not hand-derive a URL**
   (e.g. from an Azure resource name) — pass the alias and let the
   shim resolve it.

   If the user supplies a **full URL** (starts with `http://` or
   `https://`), pass it verbatim. The shim accepts it, but if it
   matches no known environment the reply carries a `warning` — relay
   that to the user (they may have pasted the wrong host). If the user
   gives some **other word** that isn't an alias and isn't a URL (e.g.
   "uat"), don't guess — ask for the exact URL.

2. **Write via `mcp__assette__set_client_code`** with `clientCode`
   (re-supply the current value from `shim_status.config.clientCode`)
   and `serverUrl` (the alias or full URL). The tool requires
   `clientCode` so you must include it; supply the current value to
   keep it unchanged. It validates `serverUrl` server-side
   (`shim.config.invalid_server_url` on a bare word that's neither
   alias nor URL) and echoes back the resolved `serverUrl` plus any
   `warning`.
3. **Clear the current tenant's credential cache** — delete
   `<runtimeRoot>/msal/<current_code>.json`. Rationale: a new server
   URL may resolve to a different B2C authority, in which case the
   old cached tokens are useless. Better a clean re-auth than a
   confusing "shim.auth.token_refused" error on the first call.
4. **Wipe the data caches** — content fetched from the *old* server
   is no longer valid. Same `python -m
   assette_mcp_shim.scripts.wipe_tenant_caches` command as Op 1
   step 6.
5. **Tell the user**: *"Saved new server URL. Credential cache and
   local data caches cleared. The shim will pick up the new URL on
   the next `mcp__assette__*` call — no restart needed. You'll be
   prompted to sign in on the first call."*

### 3. Change the upload size cap

1. **Ask** for the new cap in MB if not provided. Accept either bare
   integer ("500") or with a unit ("500 MB", "500MB").
2. **Validate** client-side: positive integer between 1 and 10240
   (`set_client_code` re-validates server-side).
3. **Write via `mcp__assette__set_client_code`** with the current
   `clientCode` (so the required field is supplied) and the new
   `maxFileSizeMb` as an integer.
4. **No cache action** — this setting is shim-internal, doesn't
   touch auth.
5. **Tell the user**: *"Saved upload cap of `<N>` MB. The shim will
   pick up the new limit on the next `mcp__assette__*` call — no
   restart needed."*

### 4. Clear credentials / sign out

When the user explicitly asks to clear credentials, sign out, or
re-authenticate:

1. Read `shim_status.config.clientCode` to find the current tenant.
2. Delete `<runtimeRoot>/msal/<current_code>.json` via Bash /
   PowerShell. Surface a clear "no cache to clear" message if the
   file doesn't exist.
3. **Do not** touch `shim-config.json` — the user wants to re-auth,
   not change tenants.
4. Tell the user: *"Credential cache cleared. The next
   `mcp__assette__*` tool call will open the browser for sign-in."*

### 5. Reset config (delete shim-config.json)

When the user asks to reset their config / start fresh:

1. Read `shim_status.config.clientCode` and capture it (you'll need
   it to delete the credential cache afterward). If the file doesn't
   exist, there's no client code to capture — skip straight to
   step 4.
2. Delete `<runtimeRoot>/shim-config.json` via Bash / PowerShell.
3. Delete the captured tenant's `<runtimeRoot>/msal/<CODE>.json` cache file (and any leftover `.bin` from a pre-v0.2.0 upgrade).
4. Wipe the data caches: `python -m
   assette_mcp_shim.scripts.wipe_tenant_caches`. A "reset" means a
   clean local state, including any cached data from the previous
   tenant.
5. Tell the user: *"Reset complete: shim-config.json, credentials,
   and local data caches all cleared. Run me again with the
   initialize Assette plugin request when you're ready — no host
   restart needed."*

### 6. Wipe data caches only

Use when the user wants to force a refresh of `DataObjects/`,
`SmartPages/`, and `DataBlocks/` content WITHOUT changing tenant,
server URL, or credentials. Typical phrasing: *"my Holdings data
looks stale, refresh the cache"*, *"wipe my Assette caches"*, *"force
a re-fetch from the server"*.

1. Confirm with the user that this will delete every cached file
   under those three folders, with no undo. Skip the confirmation
   only if the user used unambiguous language like "yes, delete all
   my caches".
2. Run `python -m assette_mcp_shim.scripts.wipe_tenant_caches`. Use
   `--dry-run` first if you want to preview the counts before
   deleting.
3. Report what was deleted — the script prints a per-folder count of
   the top-level entries it removed.
4. **No restart needed** — these caches are read on demand by the
   fabricator and the `mcp__assette__*` tools; they'll be
   repopulated from the upstream services on next use.

### 7. Show current config

This is **read-only** — never use it as a side effect of another
operation; only when the user explicitly asks "what's my config" /
"show settings" / "what tenant am I on" / "show Assette plugin
configuration".

Show **only the contents of `<runtimeRoot>/shim-config.json`** as a
table — nothing else. Do **not** surface runtime paths, Node version,
venv state, or credential-cache status here.

1. Read `<runtimeRoot>/shim-config.json` directly with the Read tool
   (resolve `<runtimeRoot>` per the OS table under "Where things
   live" — on Windows `%LOCALAPPDATA%\Assette\authoring\`).
2. Present the three keys as a two-column table, e.g.:

   | Setting | Value |
   |---|---|
   | `clientCode` | MODL |
   | `serverUrl` | https://app.assette.com/mcp |
   | `maxFileSizeMb` | 200 |

3. Stop there. No additional commentary, status, or diagnostics.

### 8. Update the Assette plugin (sync skills to latest)

Trigger phrases: *"update Assette plugin"*, *"update assette"*, *"sync Assette
skills"*, *"download the latest Assette skills"*, *"are my Assette skills up to
date?"*.

This refreshes the five **server-delivered** author skills to the versions the
hosted server currently offers. It does **not** change tenant, server URL, or
credentials — it only downloads skills.

**Procedure.**

1. **Probe with `mcp__assette__shim_status`.**
   - If `config.fileExists == false` → **fall through to Op -1** (the user
     hasn't initialized; there's nothing signed-in to download with).
   - Capture `runtime.pluginSkillsDir` and `runtime.localSkillsManifest`.

2. **Ensure signed in.** If `auth.cacheFileExists == false`, the first
   `get_skill_versions` call will trigger the browser sign-in itself — tell the
   user *"Your browser should open for Assette sign-in; complete it and I'll
   continue."* (The upstream skills tools ride the normal lazy-auth path.)

3. **Run the Sync skills subroutine** (see "Server-delivered skills" above):
   read the local manifest, call `get_skill_versions`, download each
   missing/outdated skill into `<pluginSkillsDir>/<skill>`, write the refreshed
   local `skills.json`.

4. **Report and prompt a restart.**
   - If any skills were installed/updated: *"Updated <list> (now at <versions>).
     <others> were already current. **Start a new Claude Code session** to load
     the changes."*
   - If everything was already current: *"All Assette author skills are already
     at the latest version — nothing to download."*
   - On `shim.skill.extract_failed`: report the skill + target path and that the
     directory must be writable.

**What this operation does NOT do:** change tenant / server URL / upload cap,
clear credentials, or touch the DataObjects / SmartPages / DataBlocks caches.

## Validation rules (defence in depth)

The `mcp__assette__set_client_code` synthetic tool enforces these
server-side. The skill re-validates client-side so the user gets an
immediate re-prompt instead of waiting for a tool round-trip:

| Key | Rule |
|---|---|
| `clientCode` | Trim → uppercase → must match exactly `^[A-Z]{4}$`. Returns `shim.config.invalid_client_code` on failure. |
| `serverUrl` | Must start with `http://` or `https://`; trailing `/` stripped. Returns `shim.config.invalid_server_url` on failure. |
| `maxFileSizeMb` | Positive integer between 1 and 10240. Returns `shim.config.invalid_max_file_size` on failure. |

## "No restart required" — confirming the new contract

Mutating any of the three settings is **hot-reloaded** by the shim
on the next tool call. The shim notices the file's mtime advanced,
tears down the cached tenant / token / upstream client triple if
`clientCode` or `serverUrl` changed, and rebuilds against the new
settings. `maxFileSizeMb` doesn't even need a teardown — it's read
fresh on every call.

For a credentials-only clear (Op 4), nothing needs to restart either
— the next call just re-auths through the browser.

## What this skill is NOT for

- **Adding new Assette tools** — that's a shim code change, not a
  config change.
- **Editing the upstream cloud server** — out of scope; this skill
  only touches the local shim's config.
- **Resetting the fabricator venv / re-installing pptx deps** — that's
  the fabricator skill's domain. Delete `<plugin>/shim/.venv/`
  manually and the next fabricator op will call
  `mcp__assette__bootstrap` to recreate it.
- **Editing the plugin manifest itself** — that's
  `<plugin>/.claude-plugin/plugin.json`; changes there are package /
  release operations, not user-facing config.
