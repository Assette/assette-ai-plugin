"""Offline test for the system-dataset validator (validate_system_data.py).

Builds fake cached execute_data_block responses + the dataset->file map the
/validate-system-data command writes, runs validate() / render_report() /
main() against the COMMITTED spec (tools/system_datasets_spec.json), and
asserts on statuses, counters, the client report, and never-fail behavior.

Runs under pytest, OR as a plain script (`python test_validate_system_data.py`).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import validate_system_data as vsd  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parent.parent / "system_datasets_spec.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

REQUIRED_NAMES = [
    "Source_ListofAttributeTypeValues", "Source_CountryList",
    "Source_ExtractAccountsDetails", "Source_AccountAttributesValues",
    "Source_ProductMasterExtract", "Source_ProductOfferCountries",
    "Source_ListOfCurrencyCodes", "Source_IndexDetails",
    "Source_IndexAccountAssociation",
]
OPTIONAL_NAMES = ["Source_GetGroupAccountCode", "Source_SubAccountsRelationship"]


def _good_rows(name: str) -> list[dict]:
    if name == "Source_ListofAttributeTypeValues":
        return [
            {"attributetype": t, "attributetypecode": f"{t[:3].upper()}1", "attributetypevalue": v}
            for t, v in [("Strategy", "US Small Cap Equity"), ("Vehicle", "Mutual Fund"),
                         ("VehicleCategory", "Pooled"), ("VehicleCategory", "Segregated"),
                         ("AssetClass", "Equity")]
        ]
    if name == "Source_AccountAttributesValues":
        return [
            {"portfoliocode": "1106", "attributetype": t, "attributetypecode": "X", "attributetypevalue": "Y"}
            for t in ["Strategy", "Vehicle", "VehicleCategory", "AssetClass"]
        ]
    spec_ds = next(d for d in SPEC["datasets"] if d["name"] == name)
    return [{f["name"]: f.get("example", "x") for f in spec_ds["fields"]}]


def _cache(path: Path, rows: list | None, status: int = 200, success: bool = True,
           body_override=None, double_encode: bool = False) -> str:
    body = body_override if body_override is not None else {"data": rows, "success": success}
    if double_encode:
        body = json.dumps(body)
    path.write_text(json.dumps({"statusCode": status, "body": body}), encoding="utf-8")
    return str(path)


def _full_map(ws: Path, overrides: dict | None = None) -> dict:
    """Map with a good cache file for every dataset, then apply overrides."""
    runs = {}
    for name in REQUIRED_NAMES + OPTIONAL_NAMES:
        runs[name] = {"cache_file": _cache(ws / f"{name}.json", _good_rows(name)),
                      "executed_block_name": name}
    for name, entry in (overrides or {}).items():
        runs[name] = entry
    return {"client_code": "DEMO", "started_at": "2026-07-10T00:00:00Z", "runs": runs}


def _by_name(validation: dict, name: str) -> dict:
    return next(d for d in validation["datasets"] if d["name"] == name)


# ------------------------------------------------------------------- tests


def test_all_ok(tmp_path: Path) -> None:
    validation = vsd.validate(SPEC, _full_map(tmp_path))
    s = validation["summary"]
    assert (s["required_total"], s["required_ok"], s["required_gaps"]) == (9, 9, 0)
    assert (s["optional_total"], s["optional_ok"], s["optional_gaps"]) == (2, 2, 0)
    assert s["by_status"] == {"ok": 11}
    report = vsd.render_report(SPEC, validation)
    assert "All required datasets are in place." in report
    assert "## Action required" not in report


def test_empty_dataset_is_gap(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {"Source_CountryList": {
        "cache_file": _cache(tmp_path / "empty.json", []), "executed_block_name": "Source_CountryList"}})
    validation = vsd.validate(SPEC, m)
    assert _by_name(validation, "Source_CountryList")["status"] == "empty"
    assert validation["summary"]["required_gaps"] == 1
    assert "returned no rows" in vsd.render_report(SPEC, validation)


def test_field_match_is_case_insensitive(tmp_path: Path) -> None:
    rows = [{"Code": "us", " NAME ": "United States"}]
    m = _full_map(tmp_path, {"Source_CountryList": {
        "cache_file": _cache(tmp_path / "cc.json", rows), "executed_block_name": "Source_CountryList"}})
    validation = vsd.validate(SPEC, m)
    assert _by_name(validation, "Source_CountryList")["status"] == "ok"


def test_missing_fields(tmp_path: Path) -> None:
    rows = [{"code": "us"}]  # 'name' absent
    m = _full_map(tmp_path, {"Source_CountryList": {
        "cache_file": _cache(tmp_path / "cc.json", rows), "executed_block_name": "Source_CountryList"}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_CountryList")
    assert ds["status"] == "missing-fields"
    assert ds["missing_fields"] == ["name"]
    assert "fields are missing: name" in vsd.render_report(SPEC, validation)


def test_attribute_types_minimum(tmp_path: Path) -> None:
    rows = [r for r in _good_rows("Source_ListofAttributeTypeValues") if r["attributetype"] != "AssetClass"]
    m = _full_map(tmp_path, {"Source_ListofAttributeTypeValues": {
        "cache_file": _cache(tmp_path / "attr.json", rows),
        "executed_block_name": "Source_ListofAttributeTypeValues"}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_ListofAttributeTypeValues")
    assert ds["status"] == "constraint-violation"
    failed = [c for c in ds["constraint_results"] if not c["passed"] and not c["skipped"]]
    assert [c["id"] for c in failed] == ["attribute-types-minimum"]
    assert "AssetClass" in failed[0]["detail"]


def test_vehiclecategory_allowed_values(tmp_path: Path) -> None:
    rows = _good_rows("Source_ListofAttributeTypeValues")
    rows.append({"attributetype": "VehicleCategory", "attributetypecode": "X",
                 "attributetypevalue": "Commingled"})
    m = _full_map(tmp_path, {"Source_ListofAttributeTypeValues": {
        "cache_file": _cache(tmp_path / "attr.json", rows),
        "executed_block_name": "Source_ListofAttributeTypeValues"}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_ListofAttributeTypeValues")
    failed = {c["id"]: c for c in ds["constraint_results"] if not c["passed"] and not c["skipped"]}
    assert "vehiclecategory-allowed-values" in failed
    assert "Commingled" in failed["vehiclecategory-allowed-values"]["samples"]


def test_portfoliocategory_allowed_values_sample_cap(tmp_path: Path) -> None:
    good = _good_rows("Source_ExtractAccountsDetails")[0]
    rows = [good] + [dict(good, portfoliocode=str(i), portfoliocategory=f"Sleeve{i}") for i in range(8)]
    m = _full_map(tmp_path, {"Source_ExtractAccountsDetails": {
        "cache_file": _cache(tmp_path / "acc.json", rows),
        "executed_block_name": "Source_ExtractAccountsDetails"}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_ExtractAccountsDetails")
    assert ds["status"] == "constraint-violation"
    failed = next(c for c in ds["constraint_results"] if not c["passed"])
    assert len(failed["samples"]) <= 5
    assert "8 row(s)" in failed["detail"]


def test_per_key_coverage(tmp_path: Path) -> None:
    rows = _good_rows("Source_AccountAttributesValues")
    rows += [{"portfoliocode": "2900", "attributetype": t, "attributetypecode": "X", "attributetypevalue": "Y"}
             for t in ["Strategy", "Vehicle", "VehicleCategory"]]  # 2900 lacks AssetClass
    m = _full_map(tmp_path, {"Source_AccountAttributesValues": {
        "cache_file": _cache(tmp_path / "aav.json", rows),
        "executed_block_name": "Source_AccountAttributesValues"}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_AccountAttributesValues")
    assert ds["status"] == "constraint-violation"
    failed = next(c for c in ds["constraint_results"] if not c["passed"])
    assert "2900" in failed["samples"]
    assert "1 of 2" in failed["detail"]


def test_not_found_carries_candidates(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {"Source_IndexDetails": {
        "cache_file": None, "status_hint": "not-found", "error": "block not found",
        "candidates": ["Source_IndexMaster", "Source_IndexList"]}})
    validation = vsd.validate(SPEC, m)
    ds = _by_name(validation, "Source_IndexDetails")
    assert ds["status"] == "not-found"
    assert ds["candidates"] == ["Source_IndexMaster", "Source_IndexList"]
    report = vsd.render_report(SPEC, validation)
    assert "Closest existing names: Source_IndexMaster, Source_IndexList" in report


def test_execution_failed_variants(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {
        "Source_CountryList": {"cache_file": _cache(tmp_path / "e1.json", [{"code": "us", "name": "x"}], status=500),
                               "executed_block_name": "Source_CountryList"},
        "Source_IndexDetails": {"cache_file": _cache(tmp_path / "e2.json", [], success=False),
                                "executed_block_name": "Source_IndexDetails"},
        "Source_ListOfCurrencyCodes": {"cache_file": str(tmp_path / "does-not-exist.json"),
                                       "executed_block_name": "Source_ListOfCurrencyCodes"},
        "Source_ProductOfferCountries": {"cache_file": None, "status_hint": "execution-failed",
                                         "error": "boom"},
    })
    validation = vsd.validate(SPEC, m)
    for name in ["Source_CountryList", "Source_IndexDetails",
                 "Source_ListOfCurrencyCodes", "Source_ProductOfferCountries"]:
        assert _by_name(validation, name)["status"] == "execution-failed", name
    assert validation["summary"]["required_gaps"] == 4


def test_double_encoded_body(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {"Source_CountryList": {
        "cache_file": _cache(tmp_path / "de.json", [{"code": "us", "name": "United States"}],
                             double_encode=True),
        "executed_block_name": "Source_CountryList"}})
    validation = vsd.validate(SPEC, m)
    assert _by_name(validation, "Source_CountryList")["status"] == "ok"


def test_missing_map_entry_is_not_executed(tmp_path: Path) -> None:
    m = _full_map(tmp_path)
    del m["runs"]["Source_IndexAccountAssociation"]
    validation = vsd.validate(SPEC, m)
    assert _by_name(validation, "Source_IndexAccountAssociation")["status"] == "not-executed"
    assert validation["summary"]["required_gaps"] == 1


def test_optional_gap_never_counts_required(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {"Source_SubAccountsRelationship": {
        "cache_file": None, "status_hint": "not-found", "candidates": []}})
    validation = vsd.validate(SPEC, m)
    s = validation["summary"]
    assert (s["required_gaps"], s["optional_gaps"]) == (0, 1)
    report = vsd.render_report(SPEC, validation)
    assert "## Optional datasets" in report
    assert "## Action required" not in report


def test_main_exit_zero_on_garbage_map(tmp_path: Path) -> None:
    bad_map = tmp_path / "map.json"
    bad_map.write_text("{not json", encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vsd.main(["--map", str(bad_map), "--workspace", str(tmp_path / "ws")])
    assert rc == 0
    # A garbage map means nothing was executed - artifacts still written, all not-executed.
    validation = json.loads((tmp_path / "ws" / "system_data_validation.json").read_text(encoding="utf-8"))
    assert validation["summary"]["required_gaps"] == 9
    # And a garbage SPEC degrades to the apology line, still exit 0.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vsd.main(["--map", str(bad_map), "--spec", str(bad_map), "--workspace", str(tmp_path / "ws2")])
    assert rc == 0
    assert "unavailable" in buf.getvalue()


def test_main_writes_artifacts_and_summary(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _full_map(tmp_path)
    (ws / "system_data_map.json").write_text(json.dumps(m), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vsd.main(["--map", str(ws / "system_data_map.json"), "--workspace", str(ws)])
    assert rc == 0
    assert "system data: 9/9 required ok, 0 required gap(s)" in buf.getvalue()
    assert (ws / "system_data_validation.json").is_file()
    assert (ws / "system_data_report.md").is_file()


def test_spec_integrity() -> None:
    """Drift guard: the committed spec matches the validator's capabilities."""
    datasets = SPEC["datasets"]
    assert len(datasets) == 11
    assert [d["name"] for d in datasets if d["required"]] == REQUIRED_NAMES
    assert [d["name"] for d in datasets if not d["required"]] == OPTIONAL_NAMES
    assert SPEC["spec_version"] == "2.2"
    implemented = {"column-values-include", "conditional-column-allowed-values",
                   "column-allowed-values", "per-key-values-include"}
    for d in datasets:
        assert d.get("purpose") and d.get("why_needed"), d["name"]
        for f in d["fields"]:
            assert isinstance(f, dict) and f.get("name"), d["name"]
        for c in d.get("constraints", []):
            assert c["kind"] in implemented, f"{d['name']}: {c['kind']}"


def test_report_is_ascii_with_field_tables(tmp_path: Path) -> None:
    m = _full_map(tmp_path, {"Source_ExtractAccountsDetails": {
        "cache_file": _cache(tmp_path / "acc.json",
                             [dict(_good_rows("Source_ExtractAccountsDetails")[0],
                                   portfoliocategory="Sleeve")]),
        "executed_block_name": "Source_ExtractAccountsDetails"}})
    validation = vsd.validate(SPEC, m)
    report = vsd.render_report(SPEC, validation)
    report.encode("ascii")  # raises on any non-ASCII character
    assert "**Why Assette needs it:**" in report
    assert "| portfoliocode | 1106 |" in report  # the what-to-provide field table
    assert "## Appendix: full field reference" in report


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            if fn.__code__.co_argcount:
                fn(Path(td))
            else:
                fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} validate_system_data test(s) passed.")


if __name__ == "__main__":
    _run_all()
