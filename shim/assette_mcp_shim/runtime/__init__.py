"""Host-agnostic runtime support for the assette-mcp-shim.

Three responsibilities — each in its own module so they stay testable
without pulling in mcp / msal / httpx:

  * ``locations``   — per-OS path resolution for everything the shim
                      writes (config, cache, venv).
  * ``bootstrap``   — one-shot venv creation + dep install. Replaces
                      the Windows-only ``setup-shim.ps1``.
  * ``housekeeping``— periodic cache cleanup. Replaces the wipe loop in
                      ``check-shim-setup.ps1``.

Everything here is stdlib-only so it can run BEFORE the shim's pinned
deps (mcp, msal, msal-extensions, httpx) are importable.
"""
