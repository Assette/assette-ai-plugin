"""Render the Assette implementation-pipeline status footer for a workspace.

The single deterministic renderer of the pipeline footer. All five pipeline
commands (/analyze-deck, /bind-sources, /propose-build, /build-timeline,
/implementation-status) append this tool's output VERBATIM to their final
report; never hand-compose the footer.

Phase completion is inferred purely from which workspace artifacts exist
(there is no pipeline-state file by design):

  0 sysdata   system_data_validation.json (from /validate-system-data,
              the tenant-touching phase - runnable any time after sign-in)
              -> ok / gaps (summary.required_gaps) / not-run
  1 analyze   corpus_inventory.json + classifications.jsonl
              + element_understanding.jsonl                     -> done
    questions question_queue.jsonl minus question_answers.jsonl (re-filtered
              at read time, keyed element_id+question_id). An open BLOCKING
              question gates only its own component chain, never the pipeline.
  2 bind      source_bindings.jsonl -> done; source_catalog.json only
              -> partial; neither but build_plan.json exists -> SKIPPED
  3 propose   build_plan.json -> done; stale when any plan input (bindings /
              queue / answers / classifications / understanding / inventory)
              is newer than the plan
  4 timeline  build_timeline.md -> done; stale when the plan is newer
  5 author    virtual: ready when propose + timeline are done (not stale)
              AND system data is validated with no required gaps

Always exits 0 once arguments parse — a missing workspace is the phase-0
state and corrupt files degrade to a notes[] warning, because this runs
appended to every command's report and must never break one. Output is pure
ASCII, <= 78 columns (piped Python on Windows can fall back to cp1252).

Usage:
  python pipeline_status.py [--workspace ./workspace] [--format footer|json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.2.0"
SYSDATA_STALE_DAYS = 7

WIDTH = 78
# " [x] " + label(11) + " " + command(16) + " " = 34 columns of row prefix.
_DESC_INDENT = 34
_DESC_WIDTH = WIDTH - _DESC_INDENT


# ------------------------------------------------------------------ reading


def _read_json(path: Path, notes: list[str]) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        notes.append(f"could not read {path.name}: {exc.__class__.__name__} - treated as absent")
        return None


def _read_jsonl(path: Path, notes: list[str]) -> list[dict] | None:
    if not path.is_file():
        return None
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                notes.append(f"skipped a malformed line in {path.name}")
                continue
            if isinstance(rec, dict):
                records.append(rec)
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"could not read {path.name}: {exc.__class__.__name__} - treated as absent")
        return None
    return records


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _as_dict(value: Any) -> dict:
    """Shape guard: workspace files are outside our control (some are even
    LLM-appended), so any nested field can be the wrong type."""
    return value if isinstance(value, dict) else {}


def _qkey(rec: dict) -> tuple[str, str]:
    """Hashable answer/question key even when a field has a wrong type."""
    return (str(rec.get("element_id")), str(rec.get("question_id")))


# ---------------------------------------------------------------- detection


def detect(workspace: Path) -> dict:
    """Inspect the workspace and return the machine-readable pipeline state."""
    notes: list[str] = []
    ws = workspace

    inv_path = ws / "corpus_inventory.json"
    cls_path = ws / "classifications.jsonl"
    und_path = ws / "element_understanding.jsonl"
    queue_path = ws / "question_queue.jsonl"
    answers_path = ws / "question_answers.jsonl"
    catalog_path = ws / "source_catalog.json"
    bindings_path = ws / "source_bindings.jsonl"
    plan_path = ws / "build_plan.json"
    timeline_path = ws / "build_timeline.md"
    timeline_csv_path = ws / "build_timeline.csv"
    sysdata_path = ws / "system_data_validation.json"

    # --- 0 sysdata (tenant system-data validation; independent of phases 1-4)
    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    sd_doc = _as_dict(_read_json(sysdata_path, notes)) if sysdata_path.is_file() else {}
    sd_summary = _as_dict(sd_doc.get("summary"))
    if not sd_summary:
        sd_state = "not-run"
    elif _int(sd_summary.get("required_gaps")) > 0:
        sd_state = "gaps"
    else:
        sd_state = "ok"
    sysdata = {"state": sd_state,
               "required_ok": _int(sd_summary.get("required_ok")),
               "required_total": _int(sd_summary.get("required_total")),
               "required_gaps": _int(sd_summary.get("required_gaps")),
               "optional_gaps": _int(sd_summary.get("optional_gaps")),
               "validated_at": sd_doc.get("validated_at")}
    if sd_state != "not-run":
        try:
            ts = dt.datetime.fromisoformat(str(sd_doc.get("validated_at")).replace("Z", "+00:00"))
            age_days = (dt.datetime.now(dt.timezone.utc) - ts).days
            if age_days > SYSDATA_STALE_DAYS:
                notes.append(f"system data validation is {age_days} days old - re-run "
                             "/validate-system-data to refresh")
        except (TypeError, ValueError):
            pass

    # --- 1 analyze -----------------------------------------------------------
    have_inv = inv_path.is_file()
    have_cls = cls_path.is_file()
    have_und = und_path.is_file()
    missing = [p.name for p, ok in ((inv_path, have_inv), (cls_path, have_cls), (und_path, have_und)) if not ok]
    decks = elements = understood = 0
    if have_inv:
        inv = _as_dict(_read_json(inv_path, notes))
        corpus = _as_dict(inv.get("corpus"))
        raw_decks = inv.get("decks")
        deck_list = [d for d in raw_decks if isinstance(d, dict)] if isinstance(raw_decks, list) else []
        decks = corpus.get("deck_count") or len(deck_list)
        elements = corpus.get("element_count") or sum(
            len(c.get("elements") or [])
            for d in deck_list
            for c in (d.get("slides") if isinstance(d.get("slides"), list) else [])
            if isinstance(c, dict)
        )
    if have_und:
        und_lines = _read_jsonl(und_path, notes)
        understood = len(und_lines) if und_lines is not None else 0
    if not have_inv:
        analyze_state = "not-started"
    elif have_cls and have_und:
        analyze_state = "done"
    else:
        analyze_state = "partial"
    analyze = {"state": analyze_state, "decks": decks, "elements": elements,
               "understood": understood, "missing": missing}

    # --- questions (open = queue minus answers, re-filtered at read time) ----
    queue = _read_jsonl(queue_path, notes)
    answers = _read_jsonl(answers_path, notes) or []
    questions = {"open_blocking": 0, "open_total": 0, "queue_total": 0, "known": queue is not None}
    if queue is not None:
        answered = {_qkey(a) for a in answers}
        open_qs = [q for q in queue if _qkey(q) not in answered]
        questions["queue_total"] = len(queue)
        questions["open_total"] = len(open_qs)
        questions["open_blocking"] = sum(1 for q in open_qs if q.get("blocking"))
        if len(open_qs) < len(queue):
            notes.append(
                "question_queue.jsonl still contains answered questions - re-run "
                "merge_validate_analysis.py --mode understand to re-derive it"
            )

    # --- 3 propose (needed before bind to detect the skipped state) ----------
    plan = _as_dict(_read_json(plan_path, notes)) if plan_path.is_file() else {}
    have_plan = plan_path.is_file()
    plan_summary = _as_dict(plan.get("summary"))
    by_status = _as_dict(plan_summary.get("by_status"))
    stale_reason = None
    if have_plan:
        # Every plan INPUT is watched: the plan must be re-compiled when any of
        # them changes underneath it (incl. the analyze-deck human-correction
        # flow, which rewrites classifications.jsonl without touching the queue).
        plan_mtime = _mtime(plan_path) or 0.0
        for p, why in ((bindings_path, "source_bindings.jsonl"),
                       (queue_path, "question_queue.jsonl"),
                       (answers_path, "question_answers.jsonl"),
                       (cls_path, "classifications.jsonl"),
                       (und_path, "element_understanding.jsonl"),
                       (inv_path, "corpus_inventory.json")):
            m = _mtime(p)
            if m is not None and m > plan_mtime:
                stale_reason = f"{why} is newer than the plan"
                break
    plan_modules = plan_summary.get("modules")
    propose = {
        "state": ("stale" if stale_reason else "done") if have_plan else "pending",
        "node_count": plan_summary.get("node_count", 0),
        "by_status": by_status,
        "blocking_question_count": plan_summary.get("blocking_question_count", 0),
        "modules": plan_modules if isinstance(plan_modules, list) else [],
        "scoped_to_module": plan_summary.get("scoped_to_module"),
        "stale_reason": stale_reason,
    }

    # --- 2 bind ---------------------------------------------------------------
    bindings = _read_jsonl(bindings_path, notes)
    have_catalog = catalog_path.is_file()
    sources = 0
    if have_catalog:
        catalog = _as_dict(_read_json(catalog_path, notes))
        sources = _as_dict(catalog.get("catalog")).get("source_count", 0)
    if bindings is not None:
        bind_state = "done"
    elif have_catalog:
        bind_state = "partial"
    elif have_plan:
        bind_state = "skipped"
    else:
        bind_state = "pending"
    bind = {"state": bind_state, "binding_records": len(bindings or []), "sources": sources}

    # --- 4 timeline -----------------------------------------------------------
    have_timeline = timeline_path.is_file()
    timeline_stale = None
    if have_timeline and have_plan:
        pm, tm = _mtime(plan_path), _mtime(timeline_path)
        if pm is not None and tm is not None and pm > tm:
            timeline_stale = "build_plan.json is newer than the timeline"
    timeline = {
        "state": ("stale" if timeline_stale else "done") if have_timeline else "pending",
        "csv": timeline_csv_path.is_file(),
        "stale_reason": timeline_stale,
    }

    # --- 5 author (virtual) ----------------------------------------------------
    # Open blocking questions do NOT gate authoring globally - each gates only
    # its own component chain (plan_build marks those nodes blocked-on-question).
    gates = []
    if propose["state"] != "done":
        gates.append("/propose-build")
    if timeline["state"] != "done":
        gates.append("/build-timeline")
    if sysdata["state"] == "not-run":
        gates.append("system data not validated (/validate-system-data)")
    elif sysdata["state"] == "gaps":
        gates.append(f"client data gaps ({sysdata['required_gaps']} required)")
    author = {"state": "ready" if not gates else "gated", "gates": gates}

    # --- Next: rule chain (first match wins) -----------------------------------
    ob = questions["open_blocking"]
    if analyze_state == "not-started":
        next_key, next_command = "analyze", "/analyze-deck <path to the deck corpus>"
    elif analyze_state == "partial":
        next_key = "analyze"
        next_command = f"re-run /analyze-deck (missing: {', '.join(missing)})"
    elif bind_state == "partial":
        next_key, next_command = "bind", "re-run /bind-sources <path-to-your-data-sources>"
    elif bind_state == "pending":
        next_key, next_command = "bind", "/bind-sources <path-to-your-data-sources>"
    elif propose["state"] in ("pending", "stale"):
        next_key, next_command = "propose", "/propose-build"
    elif timeline["state"] in ("pending", "stale"):
        next_key, next_command = "timeline", "/build-timeline"
    elif sysdata["state"] == "not-run":
        next_key = "sysdata"
        next_command = "/validate-system-data (checks the tenant's required system datasets)"
    elif sysdata["state"] == "gaps":
        next_key = "sysdata"
        next_command = (f"resolve the {sysdata['required_gaps']} required system-data gap(s) "
                        "with the client (send workspace/system_data_report.md), then re-run "
                        "/validate-system-data")
    else:
        next_key, next_command = "author", "author the top of build_order in build_plan.json"

    return {
        "schema_version": SCHEMA_VERSION,
        "workspace": str(ws),
        "phases": {"sysdata": sysdata, "analyze": analyze, "questions": questions,
                   "bind": bind, "propose": propose, "timeline": timeline, "author": author},
        "next_key": next_key,
        "next_command": next_command,
        "notes": notes,
    }


# --------------------------------------------------------------- rendering


def _row(marker: str, label: str, command: str, desc: str) -> list[str]:
    prefix = f" [{marker}] {label:<11} {command:<16} "
    # /validate-system-data overflows the 16-col command field; pad adaptively
    # so its description starts past the prefix while every other row keeps
    # the standard 34-column start.
    pad = max(_DESC_INDENT, len(prefix))
    # Never split command names / status words at their hyphens.
    wrapped = textwrap.wrap(desc, width=WIDTH - pad,
                            break_long_words=False, break_on_hyphens=False) or [""]
    lines = [f"{prefix}{wrapped[0]}".rstrip()]
    lines.extend(f"{' ' * pad}{cont}" for cont in wrapped[1:])
    return lines


def render_footer(state: dict) -> str:
    p = state["phases"]
    analyze, questions, bind = p["analyze"], p["questions"], p["bind"]
    propose, timeline, author = p["propose"], p["timeline"], p["author"]
    sysdata = p.get("sysdata") or {"state": "not-run", "required_ok": 0, "required_total": 0,
                                   "required_gaps": 0, "optional_gaps": 0}
    next_key = state["next_key"]
    ob, ot = questions["open_blocking"], questions["open_total"]

    def marker(phase_key: str, state_word: str) -> str:
        if phase_key == next_key:
            return ">"
        return {"done": "x", "partial": "~", "skipped": "!", "stale": "!"}.get(state_word, " ")

    lines = [f"=== Assette implementation pipeline {'=' * (WIDTH - 36)}"]

    # 0 sysdata (tenant-touching; independent of the local phases)
    sd_state = sysdata["state"]
    if sd_state == "ok":
        sd_desc = f"all {sysdata['required_ok']} required datasets ok"
        if sysdata["optional_gaps"]:
            sd_desc += f" ({sysdata['optional_gaps']} optional gap(s))"
    elif sd_state == "gaps":
        sd_desc = (f"{sysdata['required_ok']}/{sysdata['required_total']} required ok - "
                   f"{sysdata['required_gaps']} required gap(s)")
        if sysdata["optional_gaps"]:
            sd_desc += f", {sysdata['optional_gaps']} optional"
        sd_desc += "; client report: system_data_report.md"
    else:
        sd_desc = ("not run - checks the tenant's REQUIRED system datasets "
                   "(needs sign-in; run any time)")
    sd_marker = ">" if next_key == "sysdata" else {"ok": "x", "gaps": "!"}.get(sd_state, " ")
    lines += _row(sd_marker, "0 sysdata", "/validate-system-data", sd_desc)

    # 1 analyze
    a_state = analyze["state"]
    if a_state == "done":
        a_desc = (f"done: {analyze['decks']} decks, {analyze['elements']} elements, "
                  f"{analyze['understood']} understood")
    elif a_state == "partial":
        a_desc = f"partial - missing: {', '.join(analyze['missing'])}"
    else:
        a_desc = "not started - point it at a folder of .pptx/.docx/.pdf decks"
    lines += _row(marker("analyze", a_state), "1 analyze", "/analyze-deck", a_desc)

    # questions sub-row (only once the queue is known)
    if questions["known"]:
        if ob:
            q_marker = "!"
            q_desc = (f"{ob} blocking / {ot} open - each gates only its own "
                      "component chain; answer as you build (plan order)")
        elif ot:
            q_marker = "x"
            q_desc = f"0 blocking / {ot} open - non-blocking, may be deferred"
        else:
            q_marker = "x"
            q_desc = "none open - all clarifying questions answered"
        lines += _row(q_marker, "  questions", "", q_desc)

    # 2 bind
    b_state = bind["state"]
    if b_state == "done":
        b_desc = f"done: {bind['binding_records']} binding records from {bind['sources']} sources"
    elif b_state == "partial":
        b_desc = "sources ingested - bindings not merged; re-run /bind-sources"
    elif b_state == "skipped":
        b_desc = ("SKIPPED - no source bindings: Data Block source/family questions "
                  "remain open asks in the build plan (run /bind-sources any time, "
                  "then re-run /propose-build)")
    elif next_key == "bind":
        b_desc = "NEXT: point it at your CSV / Excel / Snowflake-schema data sources"
    else:
        b_desc = "pending - after /analyze-deck"
    lines += _row(marker("bind", b_state), "2 bind", "/bind-sources", b_desc)

    # 3 propose
    pr_state = propose["state"]
    if pr_state == "done":
        by_status = propose["by_status"]
        status_bits = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
        pr_desc = f"done: {propose['node_count']} nodes" + (f" ({status_bits})" if status_bits else "")
        if propose.get("scoped_to_module"):
            pr_desc += f" [module: {propose['scoped_to_module']}]"
        elif len(propose.get("modules") or []) > 1:
            pr_desc += f" across {len(propose['modules'])} modules"
    elif pr_state == "stale":
        pr_desc = f"STALE - {propose['stale_reason']}; re-run /propose-build"
    elif next_key == "propose":
        pr_desc = "NEXT: compile the ranked Block -> Object -> Shell -> Page plan"
        if bind["state"] != "done":
            pr_desc += " (no bindings: every Data Block keeps an open source question)"
    else:
        pr_desc = ("pending (works without bindings, but every Data Block keeps an "
                   "open source question)")
    lines += _row(marker("propose", pr_state), "3 propose", "/propose-build", pr_desc)

    # 4 timeline
    t_state = timeline["state"]
    if t_state == "done":
        t_desc = "done: build_timeline.md" + (" + .csv" if timeline["csv"] else "") + " await sign-off"
    elif t_state == "stale":
        t_desc = f"STALE - {timeline['stale_reason']}; re-run /build-timeline"
    elif next_key == "timeline":
        t_desc = "NEXT: schedule the plan for human review"
    else:
        t_desc = "pending - needs build_plan.json"
    lines += _row(marker("timeline", t_state), "4 timeline", "/build-timeline", t_desc)

    # 5 author
    if author["state"] == "ready":
        au_desc = "READY - build bottom-up: Data Block -> Data Object -> Smart Shell -> Smart Page"
        if ob:
            au_desc += f"; {ob} open question(s) block their own chains only"
    else:
        au_desc = "gated: " + " + ".join(author["gates"])
    lines += _row(">" if next_key == "author" else " ", "5 author", "(skills)", au_desc)

    lines.append(f" {'-' * (WIDTH - 1)}")

    def _tail(text: str, hang: str = "   ") -> None:
        wrapped = textwrap.wrap(text, width=WIDTH - 1, subsequent_indent=hang,
                                break_long_words=False, break_on_hyphens=False)
        lines.extend(f" {w}" for w in wrapped)

    _tail(f"Next: {state['next_command']}")
    if next_key == "bind":
        lines.append("   No data sources available? Say so explicitly and run /propose-build")
        lines.append("   anyway; Data Block source/family questions then remain OPEN ASKS in")
        lines.append("   the build plan.")
    elif next_key == "sysdata":
        if sysdata["state"] == "gaps":
            lines.append("   (send workspace/system_data_report.md to the client - it lists each")
            lines.append("   gap and exactly what to provide)")
        else:
            lines.append("   (the first mcp__assette__ call opens the browser sign-in; gaps become")
            lines.append("   a client-facing report at workspace/system_data_report.md)")
    elif next_key == "author":
        lines.append('   e.g. say "build the first data block from the build plan"')
        lines.append("   (assette-block-author picks it up). Methodology: ask \"how do I")
        lines.append('   implement these decks in Assette" any time.')
    if ob:
        _tail(f"Also: {ob} open blocking question(s) - each gates only its own component "
              "chain; answer as you build (reply: for <element_id> q1: <answer>).")
    if sysdata["state"] != "ok" and next_key != "sysdata":
        if sysdata["state"] == "gaps":
            _tail(f"Also: {sysdata['required_gaps']} required system-data gap(s) - send "
                  "workspace/system_data_report.md to the client; fixes take lead time.")
        else:
            _tail("Also: run /validate-system-data early (needs sign-in) - client data "
                  "gaps take lead time to fix.")
    for note in state["notes"]:
        note_lines = textwrap.wrap(f"Note: {note}", width=WIDTH - 1,
                                   break_long_words=False, break_on_hyphens=False)
        lines.extend(f" {nl}" for nl in note_lines)
    lines.append("=" * WIDTH)
    return "\n".join(lines)


# --------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the Assette implementation-pipeline status footer for a workspace. "
        "Deterministic and read-only; a missing workspace is simply the not-started state."
    )
    parser.add_argument("--workspace", default="./workspace")
    parser.add_argument("--format", choices=("footer", "json"), default="footer")
    args = parser.parse_args(argv)

    try:
        state = detect(Path(args.workspace))
        out = json.dumps(state, indent=2) if args.format == "json" else render_footer(state)
    except Exception as exc:  # noqa: BLE001 - the footer must NEVER break a command's report
        out = (f"(pipeline status unavailable - {exc.__class__.__name__}: {exc}; "
               f"workspace: {args.workspace})").encode("ascii", "replace").decode("ascii")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
