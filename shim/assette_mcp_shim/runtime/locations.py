"""Minimal location helpers for the fabricator helper scripts.

Pre-v0.2.0 this module was a full per-OS path resolver shared with the
MCP shim (now ported to ``shim-node/src/runtime/locations.ts``). The
Node binary owns its own path resolution; the Python scripts only need
two paths — the plugin root (for sibling-package imports) and the
per-user runtime root (for the wipe_tenant_caches helper).

Cross-platform: Windows + macOS + Linux. Stdlib-only — runs inside the
venv before any third-party deps load.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def plugin_root() -> Path:
    """The plugin install directory.

    Prefers ``$ASSETTE_PLUGIN_ROOT`` (set by the Node binary's
    ScriptsBridge before spawning the script). Falls back to
    ``parents[3]`` of this file, which is the correct relationship in
    the v0.2.0 layout: ``<plugin>/shim/assette_mcp_shim/runtime/locations.py``.
    """
    env = os.environ.get("ASSETTE_PLUGIN_ROOT", "").strip()
    if env and "${" not in env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def runtime_root() -> Path:
    """The per-user, per-OS root for all Assette authoring state.

    Survives plugin upgrades (lives outside the plugin install dir).
    Hosts the MSAL token cache, shim-config.json, and content caches.

      Windows:  %LOCALAPPDATA%\\Assette\\authoring\\
      macOS:    ~/Library/Application Support/Assette/authoring/
      Linux:    $XDG_DATA_HOME/assette/authoring/
                or ~/.local/share/assette/authoring/
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Assette" / "authoring"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Assette" / "authoring"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "assette" / "authoring"


def data_objects_dir() -> Path:
    """The per-user ``DataObjects/`` cache directory.

    Mirrors the Node binary's ``dataObjectsDir()`` (``<runtimeRoot>/DataObjects``),
    which writes the ``get_data_object_metadata`` cache to
    ``<runtimeRoot>/DataObjects/<Name>.json``. The fabricators' auto-fill
    (``assette_*page.data_objects.find_data_objects_dir``) reads the SAME
    directory so a cached data object populates table columns, row types, and
    runtime parameters without an explicit ``--data-objects-dir``. The path is
    returned whether or not it exists on disk; callers create it on demand.
    """
    return runtime_root() / "DataObjects"


def smart_pages_dir() -> Path:
    """The per-user ``SmartPages/`` cache directory.

    Mirrors the Node binary's ``smartPagesDir()`` (``<runtimeRoot>/SmartPages``).
    The default offload dir for fabricated Smart Page ``(.pptx, .metadata.json)``
    pairs when the caller omits ``--out-dir`` / ``outputDir``. Returned whether
    or not it exists on disk; callers create it on demand.
    """
    return runtime_root() / "SmartPages"
