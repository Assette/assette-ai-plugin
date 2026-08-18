"""Part 2 (Phase 0 of source-augmentation) — DATA SOURCE ingestion.

Deterministic, no-LLM sibling of the deck ingesters (`ingest_content.py`). Absorbs
human-provided DATA SOURCES — supplied alongside a deck so the build planner can bind deck
columns to real source columns instead of asking the source-family question from scratch —
into ONE normalized `source_catalog.json`.

It does NOT touch the deck `corpus_inventory.json` (deck element kinds are presentation
primitives; a source catalog is a different altitude: sources -> tables -> columns), so it
carries its OWN `SOURCE_SCHEMA_VERSION`. It reuses the deck ingesters' spine verbatim:
`_inventory_common.sha256_file` / `now_iso`, the lazy per-format dispatcher, the
`--input/--output` CLI, and sha256-based stable ids at a new altitude
(`source_id = sha256(file)[:12]`, `table_id = <source_id>:<i>`, `column_id = <source_id>:<i>:<j>`).

Supported source formats (routed by extension):
  .csv   -> a data file (header + sample rows) OR a Snowflake/SQL INFORMATION_SCHEMA CSV dump
  .json  -> an INFORMATION_SCHEMA JSON dump OR an array-of-objects data file
  .xlsx  -> each sheet is a table (openpyxl, read-only; lazy import — only this path needs it)
  .sql   -> raw DDL: CREATE TABLE statements parsed by a small hand-rolled tokenizer (no dep)

Per source we record a `source_family_hint` ("file" for CSV/Excel data => block-author Family E;
"database" for an INFORMATION_SCHEMA dump / DDL => Family A/SQL) — a HINT the build step confirms;
the exact connector + credentials are never inferable from a schema dump and stay human-confirmed.

CLI:
    py tools/ingest_sources.py --input <dir-or-file> --output workspace/source_catalog.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

from _inventory_common import now_iso, sha256_file

SOURCE_SCHEMA_VERSION = "0.1.0"

# Extension -> routing label. Mirrors _inventory_common.SUPPORTED_FORMATS for the deck side.
SUPPORTED_SOURCE_FORMATS = {".csv": "csv", ".json": "json", ".xlsx": "xlsx", ".sql": "ddl"}

SAMPLE_VALUE_CAP = 5      # distinct sample values kept per data-file column (privacy + size)
SAMPLE_VALUE_LEN = 80     # max chars per sample value
DATA_ROW_SCAN_CAP = 200   # rows scanned to gather samples from a data file

# INFORMATION_SCHEMA.COLUMNS canonical field names (case-insensitive match).
_IS_REQUIRED = ("table_name", "column_name", "data_type")


# --------------------------------------------------------------- type inference


def bucket_from_sql_type(raw: str | None) -> str:
    """Normalize a declared SQL/dump data_type to a coarse bucket."""
    t = (raw or "").lower()
    if any(k in t for k in ("numeric", "decimal", "number", "float", "double", "real", "money")):
        return "number"
    if any(k in t for k in ("bigint", "smallint", "tinyint", "serial", "int")):
        return "integer"
    if any(k in t for k in ("timestamp", "datetime", "date", "time")):
        return "date"
    if any(k in t for k in ("bool", "bit")):
        return "boolean"
    if any(k in t for k in ("char", "text", "string", "varchar", "nvarchar", "clob", "uuid", "variant", "json")):
        return "text"
    return "unknown"


def _looks_int(v: str) -> bool:
    try:
        int(v.replace(",", ""))
        return True
    except ValueError:
        return False


def _looks_float(v: str) -> bool:
    try:
        float(v.replace(",", "").rstrip("%"))
        return True
    except ValueError:
        return False


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")


def bucket_from_values(values: list[str]) -> str:
    """Infer a coarse bucket from sample cell values (data files carry no declared type)."""
    vals = [str(v).strip() for v in values if str(v).strip() != ""]
    if not vals:
        return "unknown"
    if all(_looks_int(v) for v in vals):
        return "integer"
    if all(_ISO_DATE.match(v) for v in vals):
        return "date"
    if all(v.lower() in {"true", "false", "yes", "no"} for v in vals):
        return "boolean"
    if all(_looks_float(v) for v in vals):
        return "number"
    return "text"


def infer_value_format(name: str, bucket: str, values: list[str]) -> str | None:
    """A modest value-format HINT from the column name + bucket (downstream confirms)."""
    n = (name or "").lower()
    if bucket == "date" or any(k in n for k in ("date", "asof", "as_of", "_dt", "period")):
        return "date"
    if any(k in n for k in ("bps", "basis_point")):
        return "bps"
    if any(k in n for k in ("pct", "percent", "weight", "_wt", "%", "ratio", "yield", "return")):
        return "percent"
    if any(k in n for k in ("amount", "value", "mktval", "market_value", "_mv", "price", "nav", "aum", "balance", "cost", "_usd")):
        return "currency"
    if bucket == "integer":
        return "integer"
    if bucket == "number":
        return "decimal"
    if bucket == "text":
        return "text"
    return None


# ------------------------------------------------------------------ records


def _column(name: str, ordinal: int, source_id: str, table_index: int, col_index: int, *,
            data_type: str | None, bucket: str, nullable: Any, is_pk: bool, fk_ref: str | None,
            samples: list[str]) -> dict[str, Any]:
    capped = [str(v)[:SAMPLE_VALUE_LEN] for v in samples[:SAMPLE_VALUE_CAP]]
    return {
        "column_id": f"{source_id}:{table_index}:{col_index}",
        "name": name,
        "ordinal": ordinal,
        "data_type": data_type,
        "data_type_bucket": bucket,
        "nullable": nullable,
        "is_pk": is_pk,
        "fk_ref": fk_ref,
        "sample_values": capped,
        "inferred_value_format": infer_value_format(name, bucket, capped),
    }


def _table(name: str, source_id: str, table_index: int, columns: list[dict[str, Any]],
           *, schema: str | None = None, row_sample_count: int = 0) -> dict[str, Any]:
    return {
        "table_id": f"{source_id}:{table_index}",
        "name": name,
        "schema": schema,
        "row_sample_count": row_sample_count,
        "columns": columns,
    }


# ---------------------------------------------------------- INFORMATION_SCHEMA


def _is_information_schema(field_names: list[str]) -> bool:
    lower = {f.lower() for f in field_names}
    return all(req in lower for req in _IS_REQUIRED)


def _ci_get(row: dict[str, Any], key: str) -> Any:
    for k, v in row.items():
        if k.lower() == key:
            return v
    return None


def _tables_from_information_schema(rows: list[dict[str, Any]], source_id: str) -> list[dict[str, Any]]:
    """Group INFORMATION_SCHEMA.COLUMNS-style rows into tables -> columns."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        schema = str(_ci_get(row, "table_schema") or "")
        table = str(_ci_get(row, "table_name") or "")
        key = (schema, table)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    tables: list[dict[str, Any]] = []
    for ti, key in enumerate(order):
        schema, table = key
        rows_for = grouped[key]
        rows_for.sort(key=lambda r: int(_ci_get(r, "ordinal_position") or 0) or 1_000_000)
        columns = []
        for ci, r in enumerate(rows_for):
            raw_type = str(_ci_get(r, "data_type") or "")
            nullable_raw = _ci_get(r, "is_nullable")
            nullable = None if nullable_raw is None else str(nullable_raw).lower() in {"yes", "true", "y", "1"}
            ct = str(_ci_get(r, "constraint_type") or "").lower()
            columns.append(
                _column(
                    str(_ci_get(r, "column_name") or ""),
                    ci, source_id, ti, ci,
                    data_type=raw_type, bucket=bucket_from_sql_type(raw_type), nullable=nullable,
                    is_pk="primary" in ct, fk_ref=None, samples=[],
                )
            )
        tables.append(_table(table, source_id, ti, columns, schema=schema or None))
    return tables


# --------------------------------------------------------------- data files


def _columns_from_grid(header: list[str], data_rows: list[list[Any]], source_id: str, table_index: int) -> tuple[list[dict[str, Any]], int]:
    columns = []
    scanned = data_rows[:DATA_ROW_SCAN_CAP]
    for ci, name in enumerate(header):
        samples: list[str] = []
        for row in scanned:
            if ci < len(row):
                v = row[ci]
                sv = "" if v is None else str(v).strip()
                if sv and sv not in samples:
                    samples.append(sv)
            if len(samples) >= SAMPLE_VALUE_CAP:
                break
        bucket = bucket_from_values(samples)
        columns.append(
            _column(str(name).strip(), ci, source_id, table_index, ci,
                    data_type=None, bucket=bucket, nullable=None, is_pk=False, fk_ref=None, samples=samples)
        )
    return columns, len(scanned)


def _ingest_csv(path: Path, source_id: str) -> tuple[list[dict[str, Any]], str, str]:
    """Returns (tables, parsed_as, family_hint)."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return [], "data-file", "file"
    header = [c.strip() for c in rows[0]]
    if _is_information_schema(header):
        dict_rows = [dict(zip(header, r)) for r in rows[1:]]
        return _tables_from_information_schema(dict_rows, source_id), "information-schema", "database"
    columns, n = _columns_from_grid(header, rows[1:], source_id, 0)
    return [_table(path.stem, source_id, 0, columns, row_sample_count=n)], "data-file", "file"


def _ingest_json(path: Path, source_id: str) -> tuple[list[dict[str, Any]], str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # A wrapper like {"columns": [...]} or {"data": [...]} — unwrap a list if present.
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
        else:
            data = [data]
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return [], "data-file", "file"
    field_names = list(data[0].keys())
    if _is_information_schema(field_names):
        return _tables_from_information_schema(data, source_id), "information-schema", "database"
    # array-of-objects data file: union of keys (first-seen order) = columns.
    cols: list[str] = []
    for rec in data:
        for k in rec:
            if k not in cols:
                cols.append(k)
    grid = [[rec.get(c) for c in cols] for rec in data]
    columns, n = _columns_from_grid(cols, grid, source_id, 0)
    return [_table(path.stem, source_id, 0, columns, row_sample_count=n)], "data-file", "file"


def _ingest_xlsx(path: Path, source_id: str) -> tuple[list[dict[str, Any]], str, str]:
    import openpyxl  # lazy: only the .xlsx path needs it (already pinned in the venv)

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    tables = []
    for ti, ws in enumerate(wb.worksheets):
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
        if not rows:
            continue
        header = [("" if c is None else str(c).strip()) for c in rows[0]]
        columns, n = _columns_from_grid(header, rows[1:], source_id, ti)
        tables.append(_table(ws.title, source_id, ti, columns, row_sample_count=n))
    wb.close()
    return tables, "data-file", "file"


# ------------------------------------------------------------------- DDL


_IDENT_TRIM = '"`[]'
_CONSTRAINT_HEAD = ("primary", "foreign", "constraint", "unique", "check", "key", "index")
_TYPE_STOP = {"not", "null", "primary", "references", "default", "unique", "check", "constraint",
              "comment", "generated", "autoincrement", "identity", "collate", "auto_increment"}


def _strip_ident(tok: str) -> str:
    return tok.strip().strip(_IDENT_TRIM).strip()


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts, depth, buf = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _paren_block(s: str) -> str | None:
    start = s.find("(")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1:i]
    return None


def _parse_create_table(stmt: str, source_id: str, table_index: int) -> dict[str, Any] | None:
    m = re.match(r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:transient\s+|temporary\s+|temp\s+)?table\s+(?:if\s+not\s+exists\s+)?(?P<name>(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w$.]+))", stmt)
    if not m:
        return None
    raw_name = m.group("name")
    schema = None
    parts = [p for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw_name)]
    name = _strip_ident(parts[-1])
    if len(parts) >= 2:
        schema = _strip_ident(parts[-2])
    body = _paren_block(stmt)
    if body is None:
        return None

    columns: list[dict[str, Any]] = []
    pk_cols: set[str] = set()
    fk_map: dict[str, str] = {}
    col_index = 0
    for raw_def in _split_top_level(body):
        d = raw_def.strip()
        if not d:
            continue
        head = d.split("(")[0].split()[0].lower() if d.split() else ""
        if head in _CONSTRAINT_HEAD:
            up = d.upper()
            if "PRIMARY KEY" in up:
                inner = _paren_block(d)
                if inner:
                    pk_cols.update(_strip_ident(c) for c in _split_top_level(inner))
            if "FOREIGN KEY" in up and "REFERENCES" in up:
                fk_inner = _paren_block(d.split("(", 1)[1] and d)  # first paren group = local cols
                local = _paren_block(d)
                ref_m = re.search(r"(?is)references\s+(?P<rt>(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w$.]+))\s*\((?P<rc>[^)]*)\)", d)
                if local and ref_m:
                    local_cols = [_strip_ident(c) for c in _split_top_level(local)]
                    ref_table = _strip_ident(ref_m.group("rt").split(".")[-1])
                    ref_cols = [_strip_ident(c) for c in _split_top_level(ref_m.group("rc"))]
                    for i, lc in enumerate(local_cols):
                        rc = ref_cols[i] if i < len(ref_cols) else (ref_cols[0] if ref_cols else "")
                        fk_map[lc] = f"{ref_table}.{rc}"
            continue
        # column definition: first token = name, then the type tokens up to a stop keyword.
        toks = d.split()
        col_name = _strip_ident(toks[0])
        rest = toks[1:]
        type_toks: list[str] = []
        for t in rest:
            if t.lower().rstrip("(") in _TYPE_STOP and type_toks:
                break
            type_toks.append(t)
            if ")" in t:  # absorbed a (...) like NUMBER(18,6)
                break
        data_type = " ".join(type_toks) if type_toks else None
        up = d.upper()
        is_pk = "PRIMARY KEY" in up
        nullable = False if "NOT NULL" in up else None
        fk_ref = None
        ref_m = re.search(r"(?is)references\s+(?P<rt>(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w$.]+))\s*(?:\((?P<rc>[^)]*)\))?", d)
        if ref_m:
            rt = _strip_ident(ref_m.group("rt").split(".")[-1])
            rc = _strip_ident((ref_m.group("rc") or "id"))
            fk_ref = f"{rt}.{rc}"
        columns.append(
            _column(col_name, col_index, source_id, table_index, col_index,
                    data_type=data_type, bucket=bucket_from_sql_type(data_type), nullable=nullable,
                    is_pk=is_pk, fk_ref=fk_ref, samples=[])
        )
        col_index += 1

    for c in columns:
        if c["name"] in pk_cols:
            c["is_pk"] = True
        if c["name"] in fk_map and not c["fk_ref"]:
            c["fk_ref"] = fk_map[c["name"]]
    return _table(name, source_id, table_index, columns, schema=schema)


def _split_statements(text: str) -> list[str]:
    # Strip line + block comments, then split on ';'.
    text = re.sub(r"--[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [s for s in text.split(";") if s.strip()]


def _ingest_ddl(path: Path, source_id: str) -> tuple[list[dict[str, Any]], str, str]:
    tables: list[dict[str, Any]] = []
    ti = 0
    for stmt in _split_statements(path.read_text(encoding="utf-8")):
        if not re.match(r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:transient\s+|temporary\s+|temp\s+)?table\b", stmt):
            continue
        try:
            tbl = _parse_create_table(stmt, source_id, ti)
        except Exception:  # best-effort: one bad statement must not abort the catalog
            tbl = None
        if tbl:
            tables.append(tbl)
            ti += 1
    return tables, "ddl", "database"


# ------------------------------------------------------------------ dispatch


def ingest_source(path: Path, relative_to: Path) -> dict[str, Any]:
    source_format = SUPPORTED_SOURCE_FORMATS[path.suffix.lower()]
    source_id = sha256_file(path)[:12]
    error = None
    try:
        if source_format == "csv":
            tables, parsed_as, family = _ingest_csv(path, source_id)
        elif source_format == "json":
            tables, parsed_as, family = _ingest_json(path, source_id)
        elif source_format == "xlsx":
            tables, parsed_as, family = _ingest_xlsx(path, source_id)
        else:
            tables, parsed_as, family = _ingest_ddl(path, source_id)
    except Exception as exc:  # never let one unreadable source abort the whole catalog
        tables, parsed_as, family, error = [], "error", "unknown", f"{type(exc).__name__}: {exc}"

    try:
        rel = str(path.resolve().relative_to(relative_to))
    except ValueError:
        rel = path.name
    rec = {
        "source_id": source_id,
        "filename": path.name,
        "relative_path": rel,
        "source_format": source_format,
        "parsed_as": parsed_as,
        "source_family_hint": family,
        "table_count": len(tables),
        "tables": tables,
    }
    if error:
        rec["error"] = error
    return rec


def _discover(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in SUPPORTED_SOURCE_FORMATS else []
    paths: list[Path] = []
    for ext in SUPPORTED_SOURCE_FORMATS:
        paths.extend(input_path.rglob(f"*{ext}"))
    return sorted(paths)


def ingest_catalog(input_path: Path) -> dict[str, Any]:
    input_path = input_path.resolve()
    relative_to = input_path.parent if input_path.is_file() else input_path
    sources = [ingest_source(p, relative_to) for p in _discover(input_path)]

    formats: dict[str, int] = {}
    table_count = column_count = 0
    for s in sources:
        formats[s["source_format"]] = formats.get(s["source_format"], 0) + 1
        table_count += len(s["tables"])
        column_count += sum(len(t["columns"]) for t in s["tables"])
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "ingested_at": now_iso(),
        "catalog": {
            "input_path": str(input_path),
            "source_count": len(sources),
            "table_count": table_count,
            "column_count": column_count,
            "formats": formats,
        },
        "sources": sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest data sources (.csv/.json/.xlsx/.sql, incl. Snowflake INFORMATION_SCHEMA "
        "dumps) into one normalized source_catalog.json for source-aware build planning."
    )
    parser.add_argument("--input", required=True, help="A supported source file or a directory.")
    parser.add_argument("--output", required=True, help="Path to write source_catalog.json.")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input path does not exist: {input_path}", file=sys.stderr)
        return 2
    files = _discover(input_path)
    if not files:
        exts = ", ".join(sorted(SUPPORTED_SOURCE_FORMATS))
        print(f"No supported source files ({exts}) found under {input_path}", file=sys.stderr)
        return 3

    catalog = ingest_catalog(input_path)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    c = catalog["catalog"]
    fmt = ", ".join(f"{n} {f}" for f, n in sorted(c["formats"].items()))
    print(
        f"Ingested {c['source_count']} source(s) [{fmt}] -> {c['table_count']} table(s), "
        f"{c['column_count']} column(s) -> {out}"
    )
    errored = [s["filename"] for s in catalog["sources"] if s.get("error")]
    if errored:
        print(f"  {len(errored)} source(s) failed to parse: {', '.join(errored[:5])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
