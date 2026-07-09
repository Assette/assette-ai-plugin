"""Stand-alone helper scripts bundled with the shim.

Each module is invokable as ``python -m assette_mcp_shim.scripts.<name>``
from inside the shim's venv. In the pre-unified-plugin layout these
lived under ``authoring/.claude/scripts/`` and were referenced by the
four authoring skills through ``Bash(python .claude/scripts/...)``
permission allowlist entries. Moving them inside the shim package
removes the host-level allowlist requirement (the plugin's
``mcp__assette`` permission covers everything Python invokes through
the venv) and makes the scripts importable, so the unit tests in
``../tests/`` can exercise them as modules instead of shelling out.
"""
