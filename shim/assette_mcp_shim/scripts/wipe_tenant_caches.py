#!/usr/bin/env python3
"""Wipe the per-tenant working caches under the user-scoped runtime root.

Bounded, parameterized replacement for ``rm -rf
%LOCALAPPDATA%\\Assette\\authoring\\DataObjects\\* …`` that the
``assette-plugin`` skill runs on a tenant or server-URL switch. Its
destructiveness is *structural*, not argument-dependent:

  * The runtime root is resolved through
    ``runtime.locations.runtime_root()`` — the same per-OS path every
    other shim component reads / writes (``%LOCALAPPDATA%\\Assette\\authoring``
    on Windows, the equivalent user-scoped location on macOS / Linux).
    There is deliberately NO ``--project-dir`` flag, so the script
    cannot be aimed at an arbitrary path.
  * It only ever clears the CONTENTS of a fixed allowlist of subdirectories
    (``SmartPages``, ``DataObjects``, ``DataBlocks``), leaving the parent
    directories in place — mirroring the per-process housekeeping in
    ``runtime.housekeeping.wipe_session_caches`` (which runs at every
    shim subprocess start).
  * ``--dirs`` may only name members of that allowlist; anything else is a
    hard error.

Usage (from inside the shim's venv):
    python -m assette_mcp_shim.scripts.wipe_tenant_caches [--dirs A,B] [--dry-run]

Exit code is 0 on success, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from assette_mcp_shim.runtime.locations import runtime_root

# The only subdirectories this script is ever allowed to touch. Keep in
# sync with ``runtime.housekeeping._WIPE_DIRS``.
ALLOWED_DIRS = ("SmartPages", "DataObjects", "DataBlocks")


def clear_dir_contents(target: Path, dry_run: bool) -> int:
    """Delete every entry inside *target*, keeping *target* itself.

    Returns the number of top-level entries removed (or that would be removed
    under --dry-run). Missing target -> 0.
    """
    if not target.is_dir():
        return 0
    count = 0
    for entry in target.iterdir():
        count += 1
        if dry_run:
            print(f"    would remove {entry.name}")
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:  # best-effort, mirror the hook's silent-ish wipe
            print(f"    WARN could not remove {entry.name}: {exc}", file=sys.stderr)
    return count


def parse_dirs(raw: str | None) -> list[str]:
    if not raw:
        return list(ALLOWED_DIRS)
    requested = [d.strip() for d in raw.split(",") if d.strip()]
    invalid = [d for d in requested if d not in ALLOWED_DIRS]
    if invalid:
        allowed = ", ".join(ALLOWED_DIRS)
        raise SystemExit(
            f"error: --dirs may only contain {allowed}; got invalid: {', '.join(invalid)}"
        )
    return requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wipe per-tenant working caches (SmartPages / DataObjects / DataBlocks) "
        "under the user-scoped runtime root, preserving the folders themselves.",
    )
    parser.add_argument(
        "--dirs",
        help="Comma-separated subset of "
        f"{','.join(ALLOWED_DIRS)} to wipe. Default: all three.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without deleting anything.",
    )
    args = parser.parse_args(argv)

    dirs = parse_dirs(args.dirs)
    root = runtime_root()

    label = "DRY RUN - nothing will be deleted" if args.dry_run else "wiping"
    print(f"{label}; runtime root: {root}")

    total = 0
    for name in dirs:
        target = root / name
        removed = clear_dir_contents(target, args.dry_run)
        total += removed
        state = "missing" if not target.is_dir() else f"{removed} entr{'y' if removed == 1 else 'ies'}"
        verb = "would clear" if args.dry_run else "cleared"
        print(f"  {name:<12} {verb}: {state}")

    summary = "would clear" if args.dry_run else "cleared"
    print(f"Done - {summary} {total} top-level entr{'y' if total == 1 else 'ies'} across {len(dirs)} folder(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
