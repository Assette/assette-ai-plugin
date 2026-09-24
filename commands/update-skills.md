---
name: update-skills
description: >-
  Download and install every server-delivered Assette skill the hosted MCP server currently
  offers — the eleven author-facing skills plus the six deck-analysis / implementation rubrics —
  into the installed plugin's skills directory, removing any skill the server has retired and
  rewriting the local skills.json manifest. Run this after EVERY plugin upgrade: the skills
  install into a version-specific directory, so a freshly upgraded plugin has none of them until
  this runs. Needs a signed-in tenant; changes no config (tenant, server URL, upload cap and
  credentials are untouched). The explicit, no-guessing equivalent of asking to "update assette".
argument-hint: "[--force to re-download every skill even when it looks current]"
---

# /assette:update-skills

Sync the **server-delivered** skills to what the hosted server currently offers.

This command exists so the sync has a **name**. The `assette:assette-plugin` skill covers the
same operation (its Op 8) plus tenant/URL/credential management, but reaching it depends on a
natural-language phrase matching a skill description — and a skill that fails to load, or that
simply doesn't get advertised, is a skill that never triggers. A slash command the user types is
deterministic. Prefer this command whenever the intent is *only* "get my skills up to date".

**This file is self-contained on purpose.** Do not delegate to `assette:assette-plugin` to
perform the sync — the whole point is that this works when that skill is unavailable. (The skill
remains the authority for everything else: initialize, sign-in, tenant switching, cache wiping.)

## Prerequisites

- The plugin must be **initialized** — `shim-config.json` must exist. If it doesn't, this
  command stops and points at initialization; there is no signed-in identity to download with.
- The user must be **signed in**. They may not be yet: the first upstream call triggers the
  browser sign-in on its own, so tell them what's about to happen rather than treating it as an
  error.

## Arguments

| Argument | Effect |
|---|---|
| *(none)* | Install every skill that is missing from disk or whose version differs from the server's. |
| `--force` | Re-download **every** catalogued skill regardless of what is on disk or in the manifest. Use after a corrupted extract, or to prove the local copies match the server. |

## What this command does

Work through these in order. Report at the end, not step by step.

1. **Probe with `mcp__assette__shim_status`.** Capture `config.fileExists`,
   `runtime.pluginSkillsDir`, `runtime.localSkillsManifest`, `runtime.localSkillsManifestExists`
   and `config.serverUrl`.

   - If `config.fileExists` is false → stop. Tell the user the plugin isn't initialized yet and
     that `assette:assette-plugin` handles first-time setup (client code + sign-in). Downloading
     skills before that is impossible.
   - **Show `config.serverUrl` in the final report.** Skills come from whichever server is
     configured; a `localhost` URL means they came from a local build, not DEV/QA/PRD, and that
     is worth the user knowing without having to ask.

2. **Enumerate what is actually on disk.** List the directories under
   `runtime.pluginSkillsDir` and keep those containing a `SKILL.md` (Bash `ls -1`, or the
   PowerShell tool's `Get-ChildItem -Directory` on Windows). This set is the **authority on what
   is installed**.

3. **Read the local manifest** with the Read tool when `runtime.localSkillsManifestExists` is
   true, else treat every skill as not installed. Parse
   `{ "skills": [ { "skill", "latestVersion" }, … ] }` into a `{ skill → installedVersion }` map.
   This gives you VERSIONS only.

   **The manifest is not evidence that a skill is present.** It lives in the version-independent
   runtime root while skills install under the version-specific
   `…/plugins/cache/<marketplace>/assette/<version>/skills/`, so after a plugin upgrade it
   cheerfully claims all seventeen are current while the directory holds only `assette-plugin`.
   Both inputs are required, and **disk wins**.

4. **Fetch the catalog** with `mcp__assette__get_skill_versions`. A non-200 `statusCode` means
   the server has no catalog — stop and report it verbatim; do not guess at a skill list.

5. **Retire what the server no longer serves.** A skill is retired when it is in the manifest map
   from step 3 but is either absent from the catalog, or present with `latestVersion`
   missing / `null` / empty. For each — **except `assette-plugin`, which ships inside the plugin
   install and must never be deleted** — delete `<pluginSkillsDir>/<skill>` recursively
   (`rm -rf "<path>"`, or `Remove-Item -Recurse -Force "<path>"` on Windows), drop it from the
   in-memory map, and note it for the report. An already-missing folder is a silent no-op, not
   an error.

6. **Decide what to download.** For every remaining catalog entry with a non-empty
   `latestVersion`, **skip it only when BOTH hold** — its folder is in the on-disk set from
   step 2, **and** its manifest version equals `latestVersion`. Anything else downloads,
   including a version that differs in either direction. With `--force`, skip nothing.

   A skill missing from disk is **always** re-downloaded no matter how current the manifest
   claims it is. Never shortcut to a version comparison alone: the presence check is the half
   that survives a plugin upgrade.

7. **Download each one** with
   `mcp__assette__get_skill(skill=<skill>, path="<pluginSkillsDir>/<skill>")`. Build `path` by
   joining `runtime.pluginSkillsDir` + the OS separator + `<skill>`; pass it explicitly rather
   than relying on the default, so the target is unambiguous. The shim wipes that directory and
   extracts into it. Record the `version` from the `{ installed, skill, version, path, files,
   bytes }` reply — that, not the catalog value, is what goes in the manifest.

   On a `shim.skill.extract_failed` error, **stop**. Report the exact `path` from the error and
   that the directory has to be writable (a read-only plugin store, or a plugin installed
   somewhere the user can't write). Do not silently continue with the remaining skills.

8. **Write the refreshed manifest** to `runtime.localSkillsManifest` with the Write tool, in the
   catalog's shape. Include every skill installed after this sync — downloaded, plus the ones
   confirmed already current — and exclude everything retired in step 5. The manifest must
   describe THIS install directory, not a previous one's.

9. **Report**, as a short table of `skill | was | now | action`
   (`installed` / `updated` / `up to date` / `removed`), then:
   - the server the skills came from (`config.serverUrl` from step 1),
   - the install directory,
   - and: **start a new Claude Code session** so the host discovers the new skills and drops the
     removed ones. `/reload-plugins` sometimes picks it up mid-session; a new session is the
     reliable path. Say this even when only one skill changed.

   If nothing changed at all, say so plainly — *"All 17 server-delivered skills are already
   current; nothing downloaded."* — and skip the restart advice.

## What this command does NOT do

Change the tenant (`clientCode`), the server URL, or the upload cap; clear credentials or sign
out; wipe the DataObjects / SmartPages / DataBlocks caches; update the **plugin** itself (that's
`/plugin marketplace update` + Update in the plugin UI — and you should run this command
straight afterwards); or remove `assette-plugin`. For any of those, use
`assette:assette-plugin`.
