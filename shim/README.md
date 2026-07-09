# assette-plugin / shim

This directory holds **the implementation helper scripts** that ship inside
the Assette plugin. As of v0.2.0 it is NOT the MCP shim —
the MCP shim is a Node.js esbuild bundle at
`<plugin>/bin/assette-mcp-shim.bundle.js` (source under `../../shim-node/`
in the ast-mcp-server monorepo).

## Layout

```
shim/
├── pyproject.toml                        # editable-install spec for the venv
├── README.md                              # this file
└── assette_mcp_shim/
    ├── __init__.py                        # __version__ = "0.2.0"
    ├── runtime/
    │   ├── __init__.py
    │   └── locations.py                   # plugin_root(), runtime_root() — 2 functions
    └── scripts/                           # the 5 fabricator helpers
        ├── __init__.py
        ├── pptx_extract_tags.py
        ├── pptx_add_table_slide.py
        ├── smartpage_decode_pptx.py
        ├── wipe_tenant_caches.py
        └── read_cache.py
```

The venv lives at `<plugin>/shim/.venv/` (gitignored). The Node binary's
`ScriptsBootstrap.ensureVenvReady` creates it on first fabricator op.

## Why scripts/* stay Python

The Smart Page fabricator uses `python-pptx` + `lxml` for OOXML manipulation
— mature libraries with no direct JavaScript equivalent at the same fidelity.
Rather than re-implement OOXML in JS we keep the scripts in Python and the
Node binary spawns them on-demand via
`shim-node/src/runtime/scriptsBridge.ts`.

This means the 80% of authors who only use blocks / content / data-object
tools never trigger a venv install. Authors who DO use the fabricator pay
the venv install cost (~20-30s for 5 deps: python-pptx, lxml, pydantic,
pyyaml, deepdiff) on first fabricator op, inside a tool call where there's
no MCP-handshake timeout.

## When the venv gets created

Three entry points trigger venv creation:

1. **`mcp__assette__bootstrap` synthetic tool** — the Node binary's
   `SyntheticTools.handleBootstrap` calls `ScriptsBootstrap.ensureVenvReady`
   (port of the old Python `bootstrap.py`). Idempotent: returns
   `OK_ALREADY` instantly when the venv is healthy.

2. **`assette-pptx-authoring` skill's first op** — calls the
   bootstrap tool explicitly so the venv is ready before the skill runs
   `pptx_extract_tags` or `pptx_add_table_slide` via ScriptsBridge.

3. **Manual** — a developer running `python -m venv .venv && .venv/Scripts/pip
   install -e .` inside this directory for editable-install / IDE work.

The shim's PINNED_DEPS list lives in three places that MUST stay in sync:

- `assette-plugin/shim/pyproject.toml` — for `pip install -e .`
- `assette-pptx-authoring/pyproject.toml` — for fabricator package
- `shim-node/src/runtime/scriptsBootstrap.ts::PINNED_DEPS` — for the
  Node binary's bootstrap

## Path resolution

`runtime/locations.py` exposes two helpers (cut down from the pre-v0.2.0
full per-OS resolver, which moved into
`shim-node/src/runtime/locations.ts`):

- `plugin_root()` — `$ASSETTE_PLUGIN_ROOT` (set by the Node binary's
  ScriptsBridge before spawning the script). Falls back to `parents[3]`
  of this file.
- `runtime_root()` — `%LOCALAPPDATA%\Assette\authoring\` on Windows;
  `~/Library/Application Support/Assette/authoring/` on macOS;
  `$XDG_DATA_HOME/assette/authoring/` (or `~/.local/share/...`) on Linux.
  Used by the `wipe_tenant_caches.py` helper.

## What was here before v0.2.0

Pre-v0.2.0 this directory held the entire Python MCP shim:
`server.py`, `auth.py`, `tenant.py`, `upstream.py`, `augment.py`,
`tool_catalog.py`, `response_cache.py`, `config.py`, `errors.py`,
`tls.py`, `paths.py`, `__main__.py`, plus the runtime/ package
(`bootstrap.py`, `housekeeping.py`) and a full pytest tree. All of that
ported to TypeScript under `../../shim-node/`. The deletion landed in
the v0.1.x → v0.2.0 cutover commit on branch `shim-node-rewrite`.

The historical record: a parallel C# AOT port under
`Assette.McpService.Shim/` on branch `shim-aot-rewrite` reached
functional parity (3,300 LOC, 72 tests) but was abandoned in favour of
the Node port because the per-platform AOT publish pipeline +
binary-mirroring overhead outweighed the cold-start gain. Both branches
remain in the repo for reference.

If you need the historical Python shim source for any reason, check it
out at the commit before the cutover (`git log --diff-filter=D -- shim/`).
