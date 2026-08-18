"""Offline test for the pipeline-status footer renderer (pipeline_status.py).

Builds tiny SYNTHETIC workspaces (no tenant, no LLM) at each pipeline state the
footer must render — empty, partial analyze, blocking-open, bind partial/skipped,
stale plan, all complete, corrupt inputs — runs pipeline_status.detect() /
render_footer() / main(), and asserts on the state dict and the footer text.

Runs under pytest, OR as a plain script (`python test_pipeline_status.py`) so it
can be verified without pytest installed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/ on path for script mode
import pipeline_status  # noqa: E402


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _pin(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


T0 = 1_700_000_000.0  # arbitrary fixed epoch; later phases get T0 + n


def _question(i: int, blocking: bool) -> dict:
    return {
        "element_id": f"aaaaaaaaaaaa:0:{i}",
        "question_id": f"q{i}",
        "facet": "columns",
        "question": f"Question {i}?",
        "blocking": blocking,
    }


def build_analyze_done(ws: Path, blocking: int = 4, nonblocking: int = 3) -> None:
    """A finished /analyze-deck workspace: 3 decks, 214 elements, 12 understood."""
    ws.mkdir(parents=True, exist_ok=True)
    inventory = {
        "schema_version": "0.2.0",
        "corpus": {"deck_count": 3, "element_count": 214, "formats": {"pptx": 3}},
        "decks": [],
    }
    (ws / "corpus_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    _write_jsonl(ws / "classifications.jsonl", [
        {"element_id": "aaaaaaaaaaaa:0:0", "tag": "data-driven-quantitative"},
    ])
    _write_jsonl(ws / "element_understanding.jsonl", [
        {"element_id": f"aaaaaaaaaaaa:0:{i}"} for i in range(12)
    ])
    queue = [_question(i, blocking=i < blocking) for i in range(blocking + nonblocking)]
    _write_jsonl(ws / "question_queue.jsonl", queue)
    for name in ("corpus_inventory.json", "classifications.jsonl",
                 "element_understanding.jsonl", "question_queue.jsonl"):
        _pin(ws / name, T0)


def build_bind_done(ws: Path) -> None:
    catalog = {"schema_version": "0.1.0",
               "catalog": {"source_count": 6, "table_count": 9, "column_count": 40},
               "sources": []}
    (ws / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    _write_jsonl(ws / "source_bindings.jsonl",
                 [{"element_id": f"aaaaaaaaaaaa:0:{i}"} for i in range(5)])
    _pin(ws / "source_catalog.json", T0 + 10)
    _pin(ws / "source_bindings.jsonl", T0 + 10)


def build_plan(ws: Path, modules: list[str] | None = None, scoped: str | None = None) -> None:
    plan = {
        "schema_version": "0.1.0",
        "summary": {"node_count": 34,
                    "by_status": {"ready": 20, "needs-confirmation": 14},
                    "blocking_question_count": 0,
                    "modules": modules or [], "scoped_to_module": scoped},
        "build_order": [{"kind": "data-block", "status": "ready"}],
        "blocking_question_batch": [],
    }
    (ws / "build_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _pin(ws / "build_plan.json", T0 + 20)


def build_timeline(ws: Path) -> None:
    (ws / "build_timeline.md").write_text("# timeline\n", encoding="utf-8")
    (ws / "build_timeline.csv").write_text("task\n", encoding="utf-8")
    _pin(ws / "build_timeline.md", T0 + 30)
    _pin(ws / "build_timeline.csv", T0 + 30)


def build_sysdata(ws: Path, required_gaps: int = 0, optional_gaps: int = 0,
                  age_days: int = 0) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    validated = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
    doc = {
        "schema_version": "0.1.0", "spec_version": "2.2",
        "validated_at": validated.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "client_code": "DEMO",
        "summary": {"required_total": 9, "required_ok": 9 - required_gaps,
                    "required_gaps": required_gaps, "optional_total": 2,
                    "optional_ok": 2 - optional_gaps, "optional_gaps": optional_gaps,
                    "by_status": {}},
        "datasets": [],
    }
    (ws / "system_data_validation.json").write_text(json.dumps(doc), encoding="utf-8")
    _pin(ws / "system_data_validation.json", T0 + 40)


def _footer(ws: Path) -> tuple[dict, str]:
    state = pipeline_status.detect(ws)
    return state, pipeline_status.render_footer(state)


def _assert_ascii_and_width(text: str) -> None:
    text.encode("ascii")  # raises UnicodeEncodeError on any non-ASCII character
    for line in text.splitlines():
        assert len(line) <= pipeline_status.WIDTH, f"line over {pipeline_status.WIDTH} cols: {line!r}"


# ------------------------------------------------------------------- tests


def test_empty_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "does-not-exist"
    state, footer = _footer(ws)
    assert state["phases"]["analyze"]["state"] == "not-started"
    assert state["phases"]["bind"]["state"] == "pending"
    assert state["phases"]["propose"]["state"] == "pending"
    assert state["phases"]["timeline"]["state"] == "pending"
    assert state["phases"]["author"]["state"] == "gated"
    assert state["next_command"].startswith("/analyze-deck")
    assert "[>] 1 analyze" in footer
    assert "questions" not in footer  # no questions sub-row until the queue exists
    _assert_ascii_and_width(footer)


def test_partial_analyze(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "corpus_inventory.json").write_text(
        json.dumps({"corpus": {"deck_count": 1, "element_count": 5}, "decks": []}),
        encoding="utf-8")
    state, footer = _footer(ws)
    assert state["phases"]["analyze"]["state"] == "partial"
    assert set(state["phases"]["analyze"]["missing"]) == {
        "classifications.jsonl", "element_understanding.jsonl"}
    assert "re-run /analyze-deck" in state["next_command"]
    assert "[>] 1 analyze" in footer
    assert "partial - missing:" in footer
    _assert_ascii_and_width(footer)


def test_analyze_done_blocking_open(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=4, nonblocking=3)
    state, footer = _footer(ws)
    a = state["phases"]["analyze"]
    assert (a["state"], a["decks"], a["elements"], a["understood"]) == ("done", 3, 214, 12)
    q = state["phases"]["questions"]
    assert (q["open_blocking"], q["open_total"]) == (4, 7)
    assert state["next_key"] == "bind"
    assert state["next_command"] == "/bind-sources <path-to-your-data-sources>"
    assert "4 blocking / 7 open" in footer
    assert "each gates only its own" in footer  # per-chain framing on the row
    assert "[>] 2 bind" in footer
    # The bind-skip consequence couplet must ALWAYS accompany a bind NEXT.
    assert "No data sources available?" in footer
    assert "remain OPEN ASKS in" in footer
    # Blocking questions surface on an Also: line with the per-chain framing.
    assert "Also: 4 open blocking question(s)" in footer
    assert "answer as you build" in footer
    # And the early sysdata nudge appears while validation has never run.
    assert "Also: run /validate-system-data early" in footer
    _assert_ascii_and_width(footer)


def test_answers_filter_at_read_time(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=4, nonblocking=3)
    # Answer one BLOCKING question WITHOUT re-deriving the queue.
    _write_jsonl(ws / "question_answers.jsonl", [
        {"element_id": "aaaaaaaaaaaa:0:0", "question_id": "q0",
         "answer": "net", "source": "human-answer"},
    ])
    _pin(ws / "question_answers.jsonl", T0)
    state, footer = _footer(ws)
    q = state["phases"]["questions"]
    assert (q["open_blocking"], q["open_total"], q["queue_total"]) == (3, 6, 7)
    assert any("re-run" in n and "--mode understand" in n for n in state["notes"])
    assert "Note:" in footer
    _assert_ascii_and_width(footer)


def test_bind_partial(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    catalog = {"catalog": {"source_count": 2}}
    (ws / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    state, footer = _footer(ws)
    assert state["phases"]["bind"]["state"] == "partial"
    assert state["next_command"].startswith("re-run /bind-sources")
    assert "bindings not merged" in footer
    _assert_ascii_and_width(footer)


def test_bind_skipped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_plan(ws)  # plan exists, no catalog/bindings -> bind was explicitly skipped
    state, footer = _footer(ws)
    assert state["phases"]["bind"]["state"] == "skipped"
    assert state["phases"]["propose"]["state"] == "done"
    assert state["next_command"] == "/build-timeline"  # skipped bind never blocks the chain
    assert "SKIPPED - no source bindings" in footer
    assert "remain open asks" in footer  # full sentence may wrap across lines
    assert "[>] 4 timeline" in footer
    _assert_ascii_and_width(footer)


def test_stale_plan(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_plan(ws)
    build_bind_done(ws)
    _pin(ws / "source_bindings.jsonl", T0 + 100)  # bindings NEWER than the plan
    state, footer = _footer(ws)
    assert state["phases"]["propose"]["state"] == "stale"
    assert "source_bindings.jsonl" in state["phases"]["propose"]["stale_reason"]
    assert state["next_command"] == "/propose-build"
    assert "STALE" in footer
    assert "[>] 3 propose" in footer
    _assert_ascii_and_width(footer)


def test_stale_plan_after_classification_correction(tmp_path: Path) -> None:
    """The analyze-deck human-correction flow rewrites classifications.jsonl without
    touching the question queue — the plan must go stale from that input too."""
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    _pin(ws / "classifications.jsonl", T0 + 100)  # post-plan human correction
    state, footer = _footer(ws)
    assert state["phases"]["propose"]["state"] == "stale"
    assert "classifications.jsonl" in state["phases"]["propose"]["stale_reason"]
    assert state["next_command"] == "/propose-build"
    _assert_ascii_and_width(footer)


def test_stale_timeline(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    build_timeline(ws)
    _pin(ws / "build_plan.json", T0 + 100)  # plan re-compiled AFTER the timeline
    state, footer = _footer(ws)
    assert state["phases"]["timeline"]["state"] == "stale"
    assert state["phases"]["author"]["state"] == "gated"
    assert state["next_command"] == "/build-timeline"
    assert "STALE" in footer
    assert "newer than the timeline" in state["phases"]["timeline"]["stale_reason"]
    _assert_ascii_and_width(footer)


def test_bind_partial_beats_skipped(tmp_path: Path) -> None:
    """Catalog present + plan present + no bindings = an interrupted bind, NOT a
    deliberate skip — the footer must send the user back to /bind-sources."""
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    catalog = {"catalog": {"source_count": 2}}
    (ws / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    _pin(ws / "source_catalog.json", T0 + 5)
    build_plan(ws)
    state, footer = _footer(ws)
    assert state["phases"]["bind"]["state"] == "partial"
    assert state["next_command"].startswith("re-run /bind-sources")
    assert "SKIPPED" not in footer
    _assert_ascii_and_width(footer)


def test_blocking_questions_do_not_gate_author(tmp_path: Path) -> None:
    """All phases done + system data ok, but a blocking question still open:
    author is READY (questions gate only their own chains), the questions row
    keeps its [!], and the Also: line carries the per-chain framing."""
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=1, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    build_timeline(ws)
    build_sysdata(ws)
    state, footer = _footer(ws)
    assert state["phases"]["author"]["state"] == "ready"
    assert state["next_key"] == "author"
    assert "[!]   questions" in footer
    assert "READY - build bottom-up" in footer
    assert "block their own chains only" in footer  # the author-row clause
    assert "Also: 1 open blocking question(s)" in footer
    assert "answer as you build" in footer
    _assert_ascii_and_width(footer)


def test_sysdata_not_run_gates_author_and_becomes_next(tmp_path: Path) -> None:
    """All local phases done but validation never ran: author gated, sysdata Next."""
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    build_timeline(ws)
    state, footer = _footer(ws)
    assert state["phases"]["sysdata"]["state"] == "not-run"
    assert state["phases"]["author"]["state"] == "gated"
    assert "system data not validated" in " + ".join(state["phases"]["author"]["gates"])
    assert state["next_key"] == "sysdata"
    assert state["next_command"].startswith("/validate-system-data")
    assert "[>] 0 sysdata" in footer
    assert "browser sign-in" in footer  # the sysdata Next couplet
    _assert_ascii_and_width(footer)


def test_sysdata_gaps_gate_author_and_become_next(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    build_timeline(ws)
    build_sysdata(ws, required_gaps=2, optional_gaps=1)
    state, footer = _footer(ws)
    assert state["phases"]["sysdata"]["state"] == "gaps"
    assert state["phases"]["author"]["state"] == "gated"
    assert "client data gaps (2 required)" in " + ".join(state["phases"]["author"]["gates"])
    assert state["next_key"] == "sysdata"
    assert "system_data_report.md" in state["next_command"]
    assert "[!] 0 sysdata" not in footer  # it is the Next, so it carries [>]
    assert "[>] 0 sysdata" in footer
    assert "7/9 required ok" in footer
    _assert_ascii_and_width(footer)


def test_sysdata_gaps_also_line_mid_pipeline(tmp_path: Path) -> None:
    """Gaps known while the local pipeline is still running: [!] row + Also line."""
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_sysdata(ws, required_gaps=3)
    state, footer = _footer(ws)
    assert state["next_key"] == "bind"  # local flow still wins the Next
    assert "[!] 0 sysdata" in footer
    assert "Also: 3 required system-data gap(s)" in footer
    _assert_ascii_and_width(footer)


def test_sysdata_row_renders_on_empty_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "does-not-exist"
    state, footer = _footer(ws)
    assert state["phases"]["sysdata"]["state"] == "not-run"
    assert "[ ] 0 sysdata" in footer
    assert "not run - checks" in footer  # full sentence wraps at the 39-col start
    assert "sign-in" in footer
    _assert_ascii_and_width(footer)


def test_sysdata_stale_note(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_sysdata(ws, age_days=10)
    state, footer = _footer(ws)
    assert any("days old" in n and "refresh" in n for n in state["notes"])
    assert "days old - re-run" in footer  # the "to refresh" tail may wrap
    _assert_ascii_and_width(footer)


def test_propose_row_shows_modules(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_plan(ws, modules=["Factsheets", "Pitchbooks"])
    state, footer = _footer(ws)
    assert state["phases"]["propose"]["modules"] == ["Factsheets", "Pitchbooks"]
    assert "across 2 modules" in footer
    _assert_ascii_and_width(footer)
    build_plan(ws, modules=["Factsheets"], scoped="Factsheets")
    _, footer = _footer(ws)
    assert "[module: Factsheets]" in footer
    _assert_ascii_and_width(footer)


def test_sysdata_corrupt_treated_as_not_run(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "system_data_validation.json").write_text("{not json", encoding="utf-8")
    state, footer = _footer(ws)
    assert state["phases"]["sysdata"]["state"] == "not-run"
    assert any("system_data_validation.json" in n for n in state["notes"])
    _assert_ascii_and_width(footer)


def test_nonblocking_only_questions(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=2)
    state, footer = _footer(ws)
    q = state["phases"]["questions"]
    assert (q["open_blocking"], q["open_total"]) == (0, 2)
    assert "0 blocking / 2 open" in footer
    assert "non-blocking" in footer  # the may-be-deferred tail can wrap to the next line
    assert "[x]   questions" in footer  # non-blocking never gets the [!] marker
    assert "open blocking question(s)" not in footer  # no questions Also: line
    assert state["next_key"] == "bind"
    _assert_ascii_and_width(footer)


def test_wrong_shape_files_never_crash(tmp_path: Path) -> None:
    """Valid-JSON-but-wrong-shape workspace files (some are LLM-appended) must
    degrade, never raise — the footer is appended to every command's report."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "corpus_inventory.json").write_text(
        json.dumps({"corpus": [1, 2], "decks": ["a", {"slides": "nope"}, {"slides": [3]}]}),
        encoding="utf-8")
    (ws / "classifications.jsonl").write_text("{}\n", encoding="utf-8")
    _write_jsonl(ws / "element_understanding.jsonl", [{}])
    _write_jsonl(ws / "question_queue.jsonl", [
        {"element_id": ["a", "b"], "question_id": {"x": 1}, "blocking": True},
    ])
    _write_jsonl(ws / "question_answers.jsonl", [{"element_id": [1], "question_id": None}])
    (ws / "build_plan.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    (ws / "source_catalog.json").write_text(json.dumps({"catalog": "nope"}), encoding="utf-8")
    state, footer = _footer(ws)  # must not raise
    assert state["phases"]["propose"]["node_count"] == 0
    _assert_ascii_and_width(footer)
    # And the other wrong-shape plan variants:
    for bad_plan in ({"summary": "oops"}, {"summary": {"by_status": None}}):
        (ws / "build_plan.json").write_text(json.dumps(bad_plan), encoding="utf-8")
        state, footer = _footer(ws)
        assert isinstance(state["phases"]["propose"]["by_status"], dict)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pipeline_status.main(["--workspace", str(ws)])
    assert rc == 0 and buf.getvalue().strip()


def test_all_complete(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws, blocking=0, nonblocking=0)
    build_bind_done(ws)
    build_plan(ws)
    build_timeline(ws)
    build_sysdata(ws)
    state, footer = _footer(ws)
    assert state["phases"]["sysdata"]["state"] == "ok"
    assert "[x] 0 sysdata" in footer
    assert state["phases"]["bind"]["state"] == "done"
    assert state["phases"]["propose"]["state"] == "done"
    assert state["phases"]["timeline"]["state"] == "done"
    assert state["phases"]["author"]["state"] == "ready"
    assert "build_order" in state["next_command"]
    assert "READY - build bottom-up" in footer
    assert "[>] 5 author" in footer
    assert "5 binding records from 6 sources" in footer
    assert state["phases"]["propose"]["by_status"] == {"ready": 20, "needs-confirmation": 14}
    assert "done: 34 nodes" in footer
    assert "needs-confirmation" in footer  # exact "k v" pairs may wrap across lines
    assert "assette-block-author" in footer
    _assert_ascii_and_width(footer)


def test_corrupt_inventory_never_fails(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "corpus_inventory.json").write_text("{not json", encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pipeline_status.main(["--workspace", str(ws)])
    assert rc == 0
    assert "corpus_inventory.json" in buf.getvalue()  # surfaced as a Note: line
    state = pipeline_status.detect(ws)
    assert any("corpus_inventory.json" in n for n in state["notes"])


def test_cli_footer_and_json(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    build_analyze_done(ws)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pipeline_status.main(["--workspace", str(ws), "--format", "footer"])
    assert rc == 0
    _assert_ascii_and_width(buf.getvalue())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pipeline_status.main(["--workspace", str(ws), "--format", "json"])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    assert parsed["schema_version"] == pipeline_status.SCHEMA_VERSION
    assert parsed["phases"]["questions"]["open_blocking"] == 4
    assert parsed["next_command"] == "/bind-sources <path-to-your-data-sources>"


# ---------------------------------------------------------------- script mode


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
        print(f"  ok  {fn.__name__}")
    print(f"{len(tests)} pipeline_status test(s) passed.")


if __name__ == "__main__":
    _run_all()
