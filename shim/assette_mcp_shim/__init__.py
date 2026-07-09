"""Smart Page fabricator helper scripts.

Pre-v0.2.0 this package hosted the MCP stdio shim itself (proxy + auth +
augmentation + 19-tool catalog). v0.2.0 moved all that to a Node.js
single-file esbuild bundle at ``<plugin>/bin/assette-mcp-shim.bundle.js``
(source under ``shim-node/`` in the ast-mcp-server monorepo). Only the
five fabricator helper scripts under ``assette_mcp_shim.scripts`` and a
tiny ``runtime.locations`` path resolver remain here — invoked by the
Node binary's ``ScriptsBridge`` on-demand when the Smart Page fabricator
skill needs pptx post-processing.
"""

__version__ = "0.2.0"
