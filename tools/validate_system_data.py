"""Validate a tenant's system datasets against the committed dataset spec.

The deterministic half of /validate-system-data. The command executes each
spec-named data block via mcp__assette__execute_data_block (the shim caches
every response to disk and returns a pointer envelope), recording which cache
file belongs to which dataset in workspace/system_data_map.json as it goes
(cache filenames do NOT carry the block name). This tool then reads those
cached responses and checks each dataset against tools/system_datasets_spec.json:

  executed ok  ->  2xx statusCode, parseable body, success, data[] is a list
  non-empty    ->  data[] has rows
  fields       ->  every spec field present (case-insensitive, trimmed) in the
                   union of keys across the first 200 rows
  constraints  ->  the spec's value constraints (attribute-type coverage,
                   allowed-value lists, per-account attribute coverage);
                   scans capped at 200,000 rows, offender samples capped at 5

Outputs:
  workspace/system_data_validation.json  - machine state; pipeline_status.py
                                           reads only summary + validated_at
  workspace/system_data_report.md        - CLIENT-FACING gap report (plain
                                           language, "dataset" never "block")

Statuses (worst wins): not-found > execution-failed > not-executed > empty
> missing-fields > constraint-violation > ok. Optional datasets get the same
statuses but only ever count toward optional_gaps.

Always exits 0 once arguments parse - this runs inside a command's report and
must never break one. Output is pure ASCII.

Usage:
  python validate_system_data.py --map ./workspace/system_data_map.json
      [--spec <script_dir>/system_datasets_spec.json]
      [--workspace ./workspace] [--format summary|json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"
FIELD_SAMPLE_ROWS = 200
CONSTRAINT_SCAN_CAP = 200_000
SAMPLE_CAP = 5
ERROR_SNIPPET_CHARS = 200

STATUS_ORDER = [
    "not-found", "execution-failed", "not-executed",
    "empty", "missing-fields", "constraint-violation", "ok",
]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fold(value: Any) -> str:
    """Case-insensitive, trimmed comparison form."""
    return str(value).strip().lower() if value is not None else ""


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _folded_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {str(k).strip().lower(): v for k in row for v in [row[k]]}


# ---------------------------------------------------------------- constraints


def _check_constraint(constraint: dict, rows: list[dict], notes: list[str]) -> dict:
    """Evaluate one spec constraint over (folded) rows. Returns a constraint
    result: {id, kind, passed, skipped, detail, samples}."""
    kind = constraint.get("kind")
    result = {"id": constraint.get("id"), "kind": kind, "passed": True,
              "skipped": False, "detail": "", "samples": []}
    scan = rows[:CONSTRAINT_SCAN_CAP]
    if len(rows) > CONSTRAINT_SCAN_CAP:
        notes.append(f"constraint scan capped at {CONSTRAINT_SCAN_CAP} rows for '{constraint.get('id')}'")

    def _column_present(col: str) -> bool:
        return any(_fold(col) in r for r in scan[:FIELD_SAMPLE_ROWS])

    if kind == "column-values-include":
        col = _fold(constraint.get("column"))
        if not _column_present(col):
            result.update(skipped=True, detail=f"column '{constraint.get('column')}' not present - not evaluated")
            return result
        found = {_fold(r.get(col)) for r in scan if r.get(col) is not None}
        missing = [m for m in constraint.get("must_include", []) if _fold(m) not in found]
        if missing:
            result.update(passed=False, samples=missing,
                          detail=f"values not found in '{constraint.get('column')}': {', '.join(missing)}")
        return result

    if kind == "conditional-column-allowed-values":
        when_col = _fold(constraint.get("when_column"))
        col = _fold(constraint.get("column"))
        if not _column_present(when_col) or not _column_present(col):
            result.update(skipped=True, detail="constraint columns not present - not evaluated")
            return result
        allowed = {_fold(a) for a in constraint.get("allowed", [])}
        when_equals = _fold(constraint.get("when_equals"))
        offenders: list[str] = []
        offender_count = 0
        seen: set[str] = set()
        for r in scan:
            if _fold(r.get(when_col)) != when_equals:
                continue
            v = r.get(col)
            if _fold(v) not in allowed:
                offender_count += 1
                key = _fold(v)
                if key not in seen and len(offenders) < SAMPLE_CAP:
                    seen.add(key)
                    offenders.append(str(v))
        if offender_count:
            result.update(passed=False, samples=offenders,
                          detail=(f"{offender_count} row(s) where {constraint.get('when_column')}="
                                  f"{constraint.get('when_equals')} carry a value outside "
                                  f"{constraint.get('allowed')} (examples: {', '.join(offenders)})"))
        return result

    if kind == "column-allowed-values":
        col = _fold(constraint.get("column"))
        if not _column_present(col):
            result.update(skipped=True, detail=f"column '{constraint.get('column')}' not present - not evaluated")
            return result
        allowed = {_fold(a) for a in constraint.get("allowed", [])}
        offenders = []
        offender_count = 0
        seen = set()
        for r in scan:
            v = r.get(col)
            if _fold(v) not in allowed:
                offender_count += 1
                key = _fold(v)
                if key not in seen and len(offenders) < SAMPLE_CAP:
                    seen.add(key)
                    offenders.append("(empty)" if _fold(v) == "" else str(v))
        if offender_count:
            result.update(passed=False, samples=offenders,
                          detail=(f"{offender_count} row(s) carry a '{constraint.get('column')}' value outside "
                                  f"the allowed set {constraint.get('allowed')} (examples: {', '.join(offenders)})"))
        return result

    if kind == "per-key-values-include":
        key_col = _fold(constraint.get("key_column"))
        col = _fold(constraint.get("column"))
        if not _column_present(key_col) or not _column_present(col):
            result.update(skipped=True, detail="constraint columns not present - not evaluated")
            return result
        must = [_fold(m) for m in constraint.get("must_include", [])]
        per_key: dict[str, set[str]] = {}
        display: dict[str, str] = {}
        for r in scan:
            k = _fold(r.get(key_col))
            if not k:
                continue
            per_key.setdefault(k, set()).add(_fold(r.get(col)))
            display.setdefault(k, str(r.get(key_col)))
        offenders = [display[k] for k, vals in sorted(per_key.items()) if any(m not in vals for m in must)]
        if offenders:
            result.update(passed=False, samples=offenders[:SAMPLE_CAP],
                          detail=(f"{len(offenders)} of {len(per_key)} {constraint.get('key_column')}(s) are not "
                                  f"mapped to all of {constraint.get('must_include')} "
                                  f"(examples: {', '.join(offenders[:SAMPLE_CAP])})"))
        return result

    result.update(skipped=True, detail=f"unknown constraint kind '{kind}' - not evaluated")
    notes.append(f"unknown constraint kind '{kind}' in spec - update validate_system_data.py")
    return result


# ----------------------------------------------------------------- validation


def _extract_rows(cache_path: Path) -> tuple[list[dict] | None, str | None]:
    """Read a cached execute_data_block response. Returns (rows, error)."""
    doc = _read_json(cache_path)
    if doc is None:
        return None, f"cached response unreadable: {cache_path.name}"
    doc = _as_dict(doc)
    status_code = doc.get("statusCode")
    body = doc.get("body")
    if isinstance(body, str):  # double-encoded body guard
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass
    body = _as_dict(body)
    if isinstance(status_code, int) and not (200 <= status_code < 300):
        return None, f"execution returned HTTP {status_code}"
    if body.get("success") is False:
        errors = body.get("errors")
        return None, f"execution reported failure: {str(errors)[:ERROR_SNIPPET_CHARS]}"
    data = body.get("data")
    if not isinstance(data, list):
        return None, "response carries no 'data' array"
    return [r for r in data if isinstance(r, dict)], None


def validate(spec: dict, map_doc: dict) -> dict:
    """Pure validation: spec + map -> the validation document."""
    runs = _as_dict(map_doc.get("runs"))
    datasets_out: list[dict] = []
    by_status: dict[str, int] = {}
    counters = {"required_total": 0, "required_ok": 0, "required_gaps": 0,
                "optional_total": 0, "optional_ok": 0, "optional_gaps": 0}

    for ds in spec.get("datasets", []):
        name = ds.get("name")
        required = bool(ds.get("required"))
        notes: list[str] = []
        entry = _as_dict(runs.get(name))
        out = {"name": name, "required": required, "status": "not-executed",
               "executed_block_name": entry.get("executed_block_name") or name,
               "cache_file": entry.get("cache_file"), "row_count": 0,
               "missing_fields": [], "present_fields": [],
               "constraint_results": [], "candidates": entry.get("candidates") or [],
               "error": None, "notes": notes}

        if not entry:
            out["status"] = "not-executed"
        elif entry.get("status_hint") == "not-found":
            out["status"] = "not-found"
            out["error"] = str(entry.get("error") or "")[:ERROR_SNIPPET_CHARS] or None
        elif entry.get("status_hint") == "execution-failed" or not entry.get("cache_file"):
            out["status"] = "execution-failed"
            out["error"] = str(entry.get("error") or "")[:ERROR_SNIPPET_CHARS] or None
        else:
            rows_raw, error = _extract_rows(Path(str(entry.get("cache_file"))))
            if rows_raw is None:
                out["status"] = "execution-failed"
                out["error"] = error
            else:
                rows = [_folded_row(r) for r in rows_raw]
                out["row_count"] = len(rows)
                if not rows:
                    out["status"] = "empty"
                else:
                    sample_keys: set[str] = set()
                    for r in rows[:FIELD_SAMPLE_ROWS]:
                        sample_keys.update(r.keys())
                    spec_fields = [f.get("name") for f in ds.get("fields", []) if isinstance(f, dict)]
                    missing = [f for f in spec_fields if _fold(f) not in sample_keys]
                    out["missing_fields"] = missing
                    out["present_fields"] = sorted(sample_keys)
                    out["constraint_results"] = [
                        _check_constraint(c, rows, notes) for c in ds.get("constraints", [])
                    ]
                    violated = any(
                        not c["passed"] and not c["skipped"] for c in out["constraint_results"]
                    )
                    if missing:
                        out["status"] = "missing-fields"
                    elif violated:
                        out["status"] = "constraint-violation"
                    else:
                        out["status"] = "ok"

        by_status[out["status"]] = by_status.get(out["status"], 0) + 1
        bucket = "required" if required else "optional"
        counters[f"{bucket}_total"] += 1
        if out["status"] == "ok":
            counters[f"{bucket}_ok"] += 1
        else:
            counters[f"{bucket}_gaps"] += 1
        datasets_out.append(out)

    return {
        "schema_version": SCHEMA_VERSION,
        "spec_version": spec.get("spec_version"),
        "validated_at": _now_iso(),
        "client_code": map_doc.get("client_code"),
        "summary": {**counters, "by_status": by_status},
        "datasets": datasets_out,
    }


# --------------------------------------------------------------- the report


_STATUS_LABEL = {
    "ok": "OK",
    "empty": "ACTION REQUIRED - dataset is empty",
    "missing-fields": "ACTION REQUIRED - fields missing",
    "constraint-violation": "ACTION REQUIRED - invalid values",
    "not-found": "ACTION REQUIRED - dataset not found",
    "execution-failed": "CHECK FAILED - could not be retrieved",
    "not-executed": "NOT CHECKED in this run",
}


def _found_text(ds_out: dict) -> list[str]:
    status = ds_out["status"]
    lines: list[str] = []
    if status == "not-found":
        lines.append("No dataset with this name exists in your environment.")
        if ds_out["candidates"]:
            lines.append("Closest existing names: " + ", ".join(map(str, ds_out["candidates"])) +
                         " - if one of these is the intended dataset, let your Assette implementer know.")
    elif status == "execution-failed":
        lines.append("The dataset could not be retrieved (technical error" +
                     (f": {ds_out['error']}" if ds_out.get("error") else "") + ").")
    elif status == "not-executed":
        lines.append("The dataset was not checked in this run.")
    elif status == "empty":
        lines.append("The dataset exists but returned no rows.")
    elif status == "missing-fields":
        lines.append(f"{ds_out['row_count']} row(s) received, but the following required "
                     f"fields are missing: {', '.join(ds_out['missing_fields'])}.")
    if status in ("missing-fields", "constraint-violation"):
        for c in ds_out["constraint_results"]:
            if not c["passed"] and not c["skipped"]:
                lines.append(f"{c['detail']}.")
    return lines or ["See your Assette implementer for details."]


def _field_table(ds_spec: dict) -> list[str]:
    lines = ["| Field | Example |", "|-------|---------|"]
    for f in ds_spec.get("fields", []):
        if isinstance(f, dict):
            lines.append(f"| {f.get('name')} | {f.get('example', '')} |")
    return lines


def render_report(spec: dict, validation: dict) -> str:
    spec_by_name = {d.get("name"): d for d in spec.get("datasets", [])}
    s = validation["summary"]
    lines: list[str] = []
    lines.append("# System Data Readiness Report")
    lines.append("")
    if validation.get("client_code"):
        lines.append(f"Prepared for tenant: {validation['client_code']}")
    lines.append(f"Validated: {str(validation.get('validated_at', ''))[:10]} (UTC)")
    lines.append(f"Specification: Assette System Datasets v{validation.get('spec_version')}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| # | Dataset | Required | Status |")
    lines.append("|---|---------|----------|--------|")
    for i, ds in enumerate(validation["datasets"], start=1):
        req = "Yes" if ds["required"] else "Optional"
        lines.append(f"| {i} | {ds['name']} | {req} | {_STATUS_LABEL.get(ds['status'], ds['status'])} |")
    lines.append("")
    lines.append(f"{s['required_ok']} of {s['required_total']} required datasets are ready.")
    if s["required_gaps"]:
        lines.append(f"{s['required_gaps']} required dataset(s) need your attention before "
                     "report automation can begin. Details and exactly what to provide follow.")
    else:
        lines.append("All required datasets are in place.")
    lines.append("")

    required_gaps = [d for d in validation["datasets"] if d["required"] and d["status"] != "ok"]
    optional_gaps = [d for d in validation["datasets"] if not d["required"] and d["status"] != "ok"]

    if required_gaps:
        lines.append("## Action required")
        lines.append("")
        for ds in required_gaps:
            ds_spec = _as_dict(spec_by_name.get(ds["name"]))
            lines.append(f"### {ds['name']}")
            lines.append("")
            lines.append(f"**What it is:** {ds_spec.get('purpose', '')}")
            lines.append("")
            lines.append(f"**Why Assette needs it:** {ds_spec.get('why_needed', '')}")
            lines.append("")
            lines.append("**What we found:** " + " ".join(_found_text(ds)))
            lines.append("")
            lines.append("**What to provide:** data conforming to the field layout below.")
            lines.append("")
            lines.extend(_field_table(ds_spec))
            lines.append("")

    if optional_gaps:
        lines.append("## Optional datasets")
        lines.append("")
        for ds in optional_gaps:
            ds_spec = _as_dict(spec_by_name.get(ds["name"]))
            note = ds_spec.get("note") or ds_spec.get("why_needed") or ""
            lines.append(f"- **{ds['name']}** - {_STATUS_LABEL.get(ds['status'], ds['status'])}. "
                         f"{ds_spec.get('purpose', '')} {note}".rstrip())
        lines.append("")

    lines.append("## Appendix: full field reference")
    lines.append("")
    for ds in spec.get("datasets", []):
        lines.append(f"### {ds.get('name')} ({'required' if ds.get('required') else 'optional'})")
        lines.append("")
        lines.append(ds.get("purpose", ""))
        lines.append("")
        lines.extend(_field_table(ds))
        lines.append("")
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


# --------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate cached system-dataset responses against the committed spec; "
        "writes system_data_validation.json + the client-facing system_data_report.md."
    )
    parser.add_argument("--map", default="workspace/system_data_map.json")
    parser.add_argument("--spec", default=str(Path(__file__).resolve().parent / "system_datasets_spec.json"))
    parser.add_argument("--workspace", default="./workspace")
    parser.add_argument("--format", choices=("summary", "json"), default="summary")
    args = parser.parse_args(argv)

    try:
        spec = _as_dict(_read_json(Path(args.spec)))
        if not spec.get("datasets"):
            print(f"(system-data validation unavailable - spec unreadable: {args.spec})")
            return 0
        map_doc = _as_dict(_read_json(Path(args.map)))
        validation = validate(spec, map_doc)

        ws = Path(args.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "system_data_validation.json").write_text(
            json.dumps(validation, indent=2), encoding="utf-8")
        (ws / "system_data_report.md").write_text(render_report(spec, validation), encoding="utf-8")

        if args.format == "json":
            print(json.dumps(validation, indent=2))
        else:
            s = validation["summary"]
            print(f"system data: {s['required_ok']}/{s['required_total']} required ok, "
                  f"{s['required_gaps']} required gap(s), {s['optional_gaps']} optional gap(s); "
                  "wrote system_data_validation.json + system_data_report.md")
    except Exception as exc:  # noqa: BLE001 - runs inside a command report; never break it
        print((f"(system-data validation unavailable - {exc.__class__.__name__}: {exc})"
               ).encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
