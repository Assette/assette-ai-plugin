"""Offline test for ingest_sources.py + slice_sources.py (Part 2 source-ingest foundation).

Writes synthetic data sources (a CSV data file, a Snowflake INFORMATION_SCHEMA dump in both
CSV and JSON, and a .sql DDL file), runs the ingester, and asserts on the normalized
source_catalog.json — table/column extraction, type buckets, PK/FK, value-format hints,
source_family_hint, stable ids — then slices it and checks the lean per-source payloads.

The .xlsx path uses a lazy openpyxl import; that case is only exercised when openpyxl is
importable (skipped otherwise). Runs under pytest OR as a plain script.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import ingest_sources  # noqa: E402
import slice_sources  # noqa: E402

ID_RE = re.compile(r"^[0-9a-f]{12}:\d+:\d+$")


def build_sources(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "holdings.csv").write_text(
        "SECURITY,WEIGHT_PCT,MARKET_VALUE\nApple,5.2,1000000\nMicrosoft,4.8,950000\n", encoding="utf-8"
    )
    (d / "infoschema.csv").write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE,ORDINAL_POSITION\n"
        "PUBLIC,HOLDINGS,ACCOUNT_ID,VARCHAR,NO,1\n"
        "PUBLIC,HOLDINGS,WEIGHT,NUMBER,YES,2\n"
        "PUBLIC,SECURITY,SEC_ID,VARCHAR,NO,1\n",
        encoding="utf-8",
    )
    (d / "infoschema.json").write_text(
        json.dumps([
            {"TABLE_SCHEMA": "PUBLIC", "TABLE_NAME": "PERF", "COLUMN_NAME": "RETURN_PCT", "DATA_TYPE": "NUMBER", "IS_NULLABLE": "YES", "ORDINAL_POSITION": 1},
            {"TABLE_SCHEMA": "PUBLIC", "TABLE_NAME": "PERF", "COLUMN_NAME": "AS_OF_DATE", "DATA_TYPE": "DATE", "IS_NULLABLE": "NO", "ORDINAL_POSITION": 2},
        ]),
        encoding="utf-8",
    )
    (d / "schema.sql").write_text(
        "CREATE TABLE PUBLIC.ACCOUNT (\n"
        "  ACCOUNT_ID VARCHAR(20) NOT NULL,\n"
        "  NAME VARCHAR(100),\n"
        "  PRIMARY KEY (ACCOUNT_ID)\n"
        ");\n"
        "CREATE TABLE IF NOT EXISTS HOLDING (\n"
        "  HOLDING_ID INT PRIMARY KEY,\n"
        "  ACCOUNT_ID VARCHAR(20) NOT NULL REFERENCES ACCOUNT(ACCOUNT_ID),\n"
        "  WEIGHT_PCT NUMBER(18,6),\n"
        "  AS_OF_DATE DATE\n"
        ");\n",
        encoding="utf-8",
    )


def _by_filename(catalog: dict) -> dict:
    return {s["filename"]: s for s in catalog["sources"]}


def _table(source: dict, name: str) -> dict:
    return next(t for t in source["tables"] if t["name"] == name)


def _col(table: dict, name: str) -> dict:
    return next(c for c in table["columns"] if c["name"] == name)


def test_ingest_sources(tmp_path: Path) -> None:
    src = tmp_path / "sources"
    build_sources(src)
    out = tmp_path / "source_catalog.json"
    rc = ingest_sources.main(["--input", str(src), "--output", str(out)])
    assert rc == 0
    catalog = json.loads(out.read_text(encoding="utf-8"))

    assert catalog["schema_version"] == ingest_sources.SOURCE_SCHEMA_VERSION
    assert catalog["catalog"]["source_count"] == 4
    by = _by_filename(catalog)

    # --- CSV data file: one table, inferred buckets + value-format hints, family "file" ---
    csv_src = by["holdings.csv"]
    assert csv_src["parsed_as"] == "data-file" and csv_src["source_family_hint"] == "file"
    holdings = _table(csv_src, "holdings")
    assert len(holdings["columns"]) == 3
    assert _col(holdings, "SECURITY")["data_type_bucket"] == "text"
    assert _col(holdings, "WEIGHT_PCT")["data_type_bucket"] == "number"
    assert _col(holdings, "WEIGHT_PCT")["inferred_value_format"] == "percent"
    assert _col(holdings, "MARKET_VALUE")["inferred_value_format"] == "currency"
    assert _col(holdings, "SECURITY")["sample_values"] == ["Apple", "Microsoft"]

    # --- INFORMATION_SCHEMA CSV: multiple tables, declared types -> buckets, nullability, family "database" ---
    is_csv = by["infoschema.csv"]
    assert is_csv["parsed_as"] == "information-schema" and is_csv["source_family_hint"] == "database"
    assert {t["name"] for t in is_csv["tables"]} == {"HOLDINGS", "SECURITY"}
    holdings_is = _table(is_csv, "HOLDINGS")
    assert _col(holdings_is, "ACCOUNT_ID")["data_type_bucket"] == "text"
    assert _col(holdings_is, "ACCOUNT_ID")["nullable"] is False
    assert _col(holdings_is, "WEIGHT")["data_type_bucket"] == "number"
    assert _col(holdings_is, "WEIGHT")["nullable"] is True

    # --- INFORMATION_SCHEMA JSON: same machinery, JSON dump ---
    is_json = by["infoschema.json"]
    assert is_json["parsed_as"] == "information-schema"
    perf = _table(is_json, "PERF")
    assert _col(perf, "RETURN_PCT")["inferred_value_format"] == "percent"
    assert _col(perf, "AS_OF_DATE")["data_type_bucket"] == "date"

    # --- DDL: PK (table + inline), FK, types, family "database" ---
    ddl = by["schema.sql"]
    assert ddl["parsed_as"] == "ddl" and ddl["source_family_hint"] == "database"
    assert {t["name"] for t in ddl["tables"]} == {"ACCOUNT", "HOLDING"}
    account = _table(ddl, "ACCOUNT")
    assert account["schema"] == "PUBLIC"
    assert _col(account, "ACCOUNT_ID")["is_pk"] is True          # from the PRIMARY KEY (...) constraint
    assert _col(account, "ACCOUNT_ID")["nullable"] is False
    holding = _table(ddl, "HOLDING")
    assert _col(holding, "HOLDING_ID")["is_pk"] is True          # inline PRIMARY KEY
    assert _col(holding, "HOLDING_ID")["data_type_bucket"] == "integer"
    assert _col(holding, "ACCOUNT_ID")["fk_ref"] == "ACCOUNT.ACCOUNT_ID"
    assert _col(holding, "WEIGHT_PCT")["data_type_bucket"] == "number"
    assert _col(holding, "WEIGHT_PCT")["inferred_value_format"] == "percent"
    assert _col(holding, "AS_OF_DATE")["data_type_bucket"] == "date"

    # --- stable, addressable ids everywhere ---
    for s in catalog["sources"]:
        assert re.match(r"^[0-9a-f]{12}$", s["source_id"])
        for t in s["tables"]:
            for c in t["columns"]:
                assert ID_RE.match(c["column_id"]), c["column_id"]

    # --- slice into lean per-source payloads ---
    payload_dir = tmp_path / "source_payloads"
    rc = slice_sources.main(["--catalog", str(out), "--output-dir", str(payload_dir)])
    assert rc == 0
    payloads = list(payload_dir.glob("source_*.json"))
    assert len(payloads) == 4
    lean = json.loads((payload_dir / f"source_{ddl['source_id']}.json").read_text(encoding="utf-8"))
    assert {t["name"] for t in lean["tables"]} == {"ACCOUNT", "HOLDING"}
    # binding-relevant facts survive the slice
    lean_holding = next(t for t in lean["tables"] if t["name"] == "HOLDING")
    assert next(c for c in lean_holding["columns"] if c["name"] == "ACCOUNT_ID")["fk_ref"] == "ACCOUNT.ACCOUNT_ID"


def test_xlsx_when_available(tmp_path: Path) -> None:
    try:
        import openpyxl
    except ImportError:
        print("SKIP test_xlsx_when_available (openpyxl not installed)")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Holdings"
    ws.append(["SECURITY", "WEIGHT_PCT"])
    ws.append(["Apple", 5.2])
    ws.append(["Microsoft", 4.8])
    src = tmp_path / "x"
    src.mkdir()
    wb.save(str(src / "book.xlsx"))
    out = tmp_path / "xcat.json"
    assert ingest_sources.main(["--input", str(src), "--output", str(out)]) == 0
    catalog = json.loads(out.read_text(encoding="utf-8"))
    s = catalog["sources"][0]
    assert s["source_format"] == "xlsx" and s["source_family_hint"] == "file"
    t = s["tables"][0]
    assert t["name"] == "Holdings"
    assert {c["name"] for c in t["columns"]} == {"SECURITY", "WEIGHT_PCT"}


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="ingest_sources_test_"))
    try:
        test_ingest_sources(tmp / "a")
        test_xlsx_when_available(tmp / "b")
        print("\nALL ASSERTIONS PASSED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
