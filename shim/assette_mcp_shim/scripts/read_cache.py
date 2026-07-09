#!/usr/bin/env python3
"""Summarize / filter a cached Assette MCP response file.

The assette-mcp-shim auto-caches six read tools' responses to disk as
``response_cache/<tool>_<UTC-ts>.json`` envelopes of the shape::

    { "statusCode": 200, "body": "<JSON string>" }

(``body`` is occasionally an already-parsed object/array rather than a string;
both are handled.) This is the bounded, reusable replacement for the ad-hoc
``python -c "import json; ..."`` one-liners — it READS ONLY and never writes
or deletes anything.

It is generic across any cached list-returning tool (search_data_objects,
search_contents, search_smart_pages, …) AND across the larger
``tool-results/*.txt`` files the harness writes when a raw tool response is too
big to inline (e.g. ``search_blocks``): both share the ``{statusCode, body}``
shape, so give it any such file and it unwraps the envelope, then counts /
filters / renders the rows.

Usage examples:
    python -m assette_mcp_shim.scripts.read_cache --file response_cache/search_data_objects_X.json --count
    python -m assette_mcp_shim.scripts.read_cache --file <f> --keys
    python -m assette_mcp_shim.scripts.read_cache --file <f> --columns id,name,outputType,status --sort name
    python -m assette_mcp_shim.scripts.read_cache --file <f> --where outputType=Table --name-not-contains TableDataObject
    python -m assette_mcp_shim.scripts.read_cache --file <f> --where category=Interface --limit 5
    python -m assette_mcp_shim.scripts.read_cache --file <f> --where id=2 --json     # full record(s) as JSON

Exit code 0 on success, 1 if the cached statusCode is non-2xx, 2 on usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_COLUMNS = ["id", "name", "outputType", "status", "latestVersion"]


def load_body(path: Path):
    """Read the cache file and return (statusCode, parsed_body)."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON: {exc}")

    status = envelope.get("statusCode") if isinstance(envelope, dict) else None
    body = envelope.get("body") if isinstance(envelope, dict) else envelope

    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            # Leave as a raw string — caller will just see it printed.
            pass
    return status, body


def apply_filters(rows, wheres, contains, not_contains):
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        ok = True
        for field, val in wheres:
            if str(r.get(field)) != val:
                ok = False
                break
        if ok and contains and contains.lower() not in str(r.get("name", "")).lower():
            ok = False
        if ok and not_contains and not_contains.lower() in str(r.get("name", "")).lower():
            ok = False
        if ok:
            out.append(r)
    return out


def render_table(rows, columns):
    widths = {c: len(c) for c in columns}
    cells = []
    for r in rows:
        row = {c: ("" if r.get(c) is None else str(r.get(c))) for c in columns}
        for c in columns:
            widths[c] = max(widths[c], len(row[c]))
        cells.append(row)
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in cells:
        print("  ".join(row[c].ljust(widths[c]) for c in columns))


def parse_where(values):
    out = []
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"error: --where expects field=value, got: {item}")
        field, val = item.split("=", 1)
        out.append((field.strip(), val.strip()))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only summarizer/filter for cached Assette MCP response files.",
    )
    parser.add_argument("--file", required=True, help="Path to a response_cache/*.json file.")
    parser.add_argument("--count", action="store_true", help="Print the row count and exit.")
    parser.add_argument("--keys", action="store_true", help="Print the keys of the first row and exit.")
    parser.add_argument("--columns", help="Comma-separated columns to render. Default: a sensible subset.")
    parser.add_argument("--where", action="append", help="Exact-match filter field=value (repeatable).")
    parser.add_argument("--name-contains", dest="name_contains", help="Keep rows whose name contains S.")
    parser.add_argument("--name-not-contains", dest="name_not_contains", help="Drop rows whose name contains S.")
    parser.add_argument("--sort", help="Field to sort rows by (case-insensitive for strings).")
    parser.add_argument("--limit", type=int, help="Render at most N rows.")
    parser.add_argument("--json", action="store_true", help="Print the (filtered) rows as full JSON instead of a table.")
    args = parser.parse_args(argv)

    status, body = load_body(Path(args.file))

    if status is not None and not (200 <= int(status) < 300):
        print(f"statusCode {status} (non-success). Body:")
        print(json.dumps(body, indent=2) if not isinstance(body, str) else body)
        return 1

    if not isinstance(body, list):
        # Not a list payload — just pretty-print it.
        print(f"statusCode {status}; body is {type(body).__name__} (not a list):")
        print(json.dumps(body, indent=2) if not isinstance(body, str) else body)
        return 0

    rows = apply_filters(body, parse_where(args.where), args.name_contains, args.name_not_contains)

    if args.sort:
        rows = sorted(
            rows,
            key=lambda r: (str(r.get(args.sort, "")).lower() if isinstance(r, dict) else ""),
        )

    if args.count:
        print(len(rows))
        return 0

    if args.keys:
        first = next((r for r in rows if isinstance(r, dict)), None)
        print(", ".join(first.keys()) if first else "(no dict rows)")
        return 0

    if args.limit is not None:
        rows = rows[: args.limit]

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    columns = [c.strip() for c in args.columns.split(",")] if args.columns else DEFAULT_COLUMNS
    print(f"statusCode {status}; {len(rows)} row(s)" + (f" of {len(body)} after filtering" if len(rows) != len(body) else ""))
    if rows:
        render_table([r for r in rows if isinstance(r, dict)], columns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
