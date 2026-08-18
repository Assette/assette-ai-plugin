"""Phase 0 BUILD PLANNER for the autonomous build orchestrator (`/propose-build`).

Deterministic, no-LLM, fully-offline pass that compiles a finished `/analyze-deck`
workspace into a topo-sorted, impact-ranked **build plan** — the Block -> Object ->
Shell -> Page DAG a human (or, later, the `/build-deck` conductor) would walk to rebuild
the deck's data-driven content as Assette artifacts.

It is the read-only first step of the design in `docs/build-orchestrator-design.md`:
it PROPOSES a plan and surfaces the up-front blocking-question batch. It BUILDS NOTHING,
calls no MCP tool, needs no tenant — so it is safe to run and fully testable offline.

It is a deterministic sibling of `merge_validate_analysis.py` and reuses the same
conventions: stdlib only, `read_jsonl`, the latest-per-id classification reducer, and the
GROUNDING GUARD — every `element_id` the plan references is validated against the real
`corpus_inventory.json`; a fabricated id is DROPPED and counted, never planned.

--------------------------------------------------------------------------------
Inputs (all from a finished /analyze-deck workspace):
  corpus_inventory.json       (Phase 1 — ground truth element ids + deck/slide context)
  classifications.jsonl       (Phase 2a — latest record per element_id; the actionable set)
  element_understanding.jsonl (Phase 2c — deep table/chart structure; complexity + reuse signals) [optional]
  table_chart_groups.json     (Phase 2c pre-pass — cross-deck dedup => frequency + deck spread) [optional]
  question_queue.jsonl        (Phase 2c — open clarifying questions; blocking => readiness gate) [optional]
  category_weights.json       (firm-supplied {data_category: weight} for criticality)          [optional]

Output:
  build_plan.json   { schema_version, weights, summary, build_order[], blocking_question_batch[], dropped_unknown_ids[] }

Node model (one node per artifact to build):
  data-block  -> data-object -> smart-shell -> smart-page   (strict upstream->downstream chain)
  footnote / disclosure                                     (standalone, reuse-first)
A data-driven UNIT is one such Block/Object/Shell chain. Repeated table/chart elements are
DEDUPED into one unit via table_chart_groups.json (sample_count => frequency, distinct decks
=> deck spread). One shell can feed MANY pages (page->shell is many-to-many).

Impact (deterministic, computed here — NOT in agent reasoning):
  score = w_freq*frequency + w_spread*deck_spread + w_unblock*downstream_unblock
        + w_crit*criticality - w_complex*complexity + w_reuse*reuse_discount
Every factor is tied to a real analysis field (see _unit_signals / _complexity / build_order).
Open BLOCKING questions are NOT a score term — they are a readiness gate (a blocked node is
not "ready"), but they are surfaced highest-leverage-first so the human unblocks the most
impactful work first.

CLI:
  py tools/plan_build.py \
      --inventory workspace/corpus_inventory.json \
      --classifications workspace/classifications.jsonl \
      --understanding workspace/element_understanding.jsonl \
      --groups workspace/table_chart_groups.json \
      --question-queue workspace/question_queue.jsonl \
      --category-weights workspace/category_weights.json \
      --output workspace/build_plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _inventory_common import load_vision_regions, now_iso

PLAN_SCHEMA_VERSION = "0.2.0"

# Default impact weights. Overridable per-factor via CLI (--w-*). Documented in
# docs/build-orchestrator-design.md §1.4. Tuned so leverage (downstream unblock) and
# corpus frequency dominate, complexity is a mild penalty, reuse a mild discount.
DEFAULT_WEIGHTS: dict[str, float] = {
    "frequency": 3.0,
    "deck_spread": 2.0,
    "downstream_unblock": 4.0,
    "criticality": 2.0,
    "complexity": 1.5,
    "reuse_discount": 1.0,
}

# Data-driven authoring components that get a Block -> Object -> Shell chain.
SMART_SHELL_COMPONENTS = frozenset(
    {
        "smart-shell:table",
        "smart-shell:zigzag",
        "smart-shell:chart",
        "smart-shell:performance-history",
        "smart-shell:text",
        "smart-shell:image",
    }
)

# node.kind -> (stage tier for topo order, the skill that would build it).
STAGE = {"data-block": 1, "footnote": 1, "disclosure": 1, "data-object": 2, "smart-shell": 3, "smart-page": 4}
SKILL = {
    "data-block": "assette:assette-block-author",
    "data-object": "assette:assette-data-object-author",
    "smart-shell": "assette:assette-pptx-authoring",
    "smart-page": "assette:assette-pptx-authoring",
    "footnote": "assette:assette-data-object-author (footnote library; reuse-first)",
    "disclosure": "assette:assette-classifications / fixed-content",
}


# --------------------------------------------------------------------------- io


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a .jsonl file into a list of dicts; skip blank/malformed lines."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def latest_classifications(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce append-only classifications to the latest record per element_id.

    Last-write-wins by `timestamp` then file order — the same reducer the review queue
    and group_table_chart_samples.py use, so human-corrections (written last) win.
    """
    latest: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for i, rec in enumerate(records):
        eid = rec.get("element_id")
        if not eid:
            continue
        prev = latest.get(eid)
        if prev is None or rec.get("timestamp", "") >= prev.get("timestamp", "") or i >= order.get(eid, -1):
            latest[eid] = rec
            order[eid] = i
    return latest


def inventory_element_ids(inventory: dict[str, Any]) -> set[str]:
    """Every real element_id in the corpus — the ground truth for the grounding guard."""
    ids: set[str] = set()
    for deck in inventory.get("decks", []):
        for container in deck.get("slides", []):
            for element in container.get("elements", []):
                eid = element.get("element_id")
                if eid:
                    ids.add(eid)
    return ids


def index_elements(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """element_id -> {deck_id, filename, slide_index, kind} for titles + page grouping."""
    index: dict[str, dict[str, Any]] = {}
    for deck in inventory.get("decks", []):
        deck_id = deck.get("deck_id")
        filename = deck.get("filename")
        for container in deck.get("slides", []):
            slide_index = container.get("slide_index")
            for element in container.get("elements", []):
                eid = element.get("element_id")
                if eid:
                    index[eid] = {
                        "deck_id": deck_id,
                        "filename": filename,
                        "slide_index": slide_index,
                        "kind": element.get("kind"),
                    }
    return index


def deck_modules(inventory: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, str]:
    """deck_id -> implementation module.

    A typical implementation is rolled out module by module (Factsheets,
    Pitchbooks, Client Reports, ...). The module comes from the corpus folder
    structure: organize the corpus one folder per module and the FIRST folder
    segment of each deck's relative_path is its module. Decks at the corpus
    root fall into '(ungrouped)'. An optional overrides map (workspace/
    modules.json, keyed by deck_id OR filename, values = module name) wins.
    """
    overrides = {str(k): str(v) for k, v in (overrides or {}).items()}
    modules: dict[str, str] = {}
    for deck in inventory.get("decks", []) or []:
        deck_id = deck.get("deck_id")
        if not deck_id:
            continue
        rel = str(deck.get("relative_path") or "")
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        module = parts[0] if len(parts) > 1 else "(ungrouped)"
        module = overrides.get(str(deck_id)) or overrides.get(str(deck.get("filename") or "")) or module
        modules[str(deck_id)] = module
    return modules


def _eid_module(element_id: Any, modules_by_deck: dict[str, str]) -> str:
    return modules_by_deck.get(str(element_id).split(":")[0], "(ungrouped)")


def understanding_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map every element_id (representative AND members) -> its understanding record."""
    index: dict[str, dict[str, Any]] = {}
    for rec in records:
        eid = rec.get("element_id")
        if eid:
            index.setdefault(eid, rec)
        for m in rec.get("member_element_ids") or []:
            index.setdefault(m, rec)
    return index


# ------------------------------------------------------------------ signals


def _complexity(understanding: dict[str, Any] | None) -> int:
    """Deterministic build-complexity from the deep-understanding record (0 when absent).

    Higher = harder to build. Every input is a real field in element_understanding.jsonl.
    """
    if not understanding:
        return 1  # base cost for an un-deep-analyzed data-driven element
    score = 0
    table = understanding.get("table") or {}
    for col in table.get("columns") or []:
        if col.get("growth") in {"grows-per-period", "grows-per-entity", "conditional"}:
            score += 1
    row_model = table.get("row_model") or {}
    if row_model.get("kind") in {"repeating-group", "mixed"}:
        score += 2
    if int(row_model.get("observed_row_count_max") or 0) > 25:
        score += 1
    score += len(table.get("conditional_formatting") or [])
    chart = understanding.get("chart") or {}
    if chart.get("combo"):
        score += 1
    if chart.get("secondary_axis"):
        score += 1
    if int(understanding.get("samples_analyzed") or 1) <= 1:
        score += 1  # single-sample: variance unknown -> riskier build
    return score


def _reuse_discount(understanding: dict[str, Any] | None) -> int:
    """More reuse candidates (System blocks + Dynamic Fields) = less bespoke build."""
    if not understanding:
        return 0
    return len(understanding.get("system_block_candidates") or []) + len(
        understanding.get("dynamic_field_candidates") or []
    )


# ------------------------------------------------------------------ units


def _unit(
    unit_id: str,
    element_ids: list[str],
    representative: str,
    data_category: Any,
    authoring_component: str | None,
    frequency: int,
    deck_spread: int,
    understanding: dict[str, Any] | None,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "element_ids": element_ids,
        "representative_element_id": representative,
        "data_category": data_category,
        "authoring_component": authoring_component,
        "frequency": frequency,
        "deck_spread": deck_spread,
        "complexity": _complexity(understanding),
        "reuse_discount": _reuse_discount(understanding),
        "label": label,
    }


def build_units(
    valid_ids: set[str],
    classifications: dict[str, dict[str, Any]],
    groups: list[dict[str, Any]],
    u_index: dict[str, dict[str, Any]],
    dropped: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build deduped data-driven / footnote / disclosure units. Returns (units, not_planned_by_component)."""
    units: list[dict[str, Any]] = []
    claimed: set[str] = set()

    # 1) Data-driven units from cross-deck groups (the dedup): one unit per group.
    for g in groups:
        members = [m for m in (g.get("members") or []) if m.get("element_id") in valid_ids]
        member_ids = [m["element_id"] for m in members]
        if not member_ids:
            continue
        claimed.update(member_ids)
        rep = g.get("representative_element_id")
        if rep not in valid_ids:
            rep = member_ids[0]
        deck_spread = len({m.get("deck_id") for m in members if m.get("deck_id")}) or 1
        units.append(
            _unit(
                unit_id=g.get("group_id") or f"grp_{rep}",
                element_ids=member_ids,
                representative=rep,
                data_category=g.get("data_category"),
                authoring_component=g.get("authoring_component"),
                frequency=int(g.get("sample_count") or len(member_ids)),
                deck_spread=deck_spread,
                understanding=u_index.get(rep) or (u_index.get(member_ids[0]) if member_ids else None),
                label=g.get("label"),
            )
        )

    # 2) Per-element units for everything actionable not already claimed by a group.
    not_planned: dict[str, int] = {}
    for eid in sorted(classifications):
        if eid in claimed:
            continue
        if eid not in valid_ids:
            dropped.append(eid)  # GROUNDING GUARD
            continue
        cls = classifications[eid]
        ac = cls.get("authoring_component") or "none"
        if ac in SMART_SHELL_COMPONENTS:
            units.append(
                _unit(
                    unit_id=f"el_{eid}",
                    element_ids=[eid],
                    representative=eid,
                    data_category=cls.get("data_category"),
                    authoring_component=ac,
                    frequency=1,
                    deck_spread=1,
                    understanding=u_index.get(eid),
                )
            )
        elif ac == "footnote":
            units.append({"unit_id": f"fn_{eid}", "kind_hint": "footnote", "element_ids": [eid],
                          "representative_element_id": eid, "data_category": cls.get("data_category"),
                          "authoring_component": ac, "frequency": 1, "deck_spread": 1,
                          "complexity": 1, "reuse_discount": 0, "label": None})
        elif ac == "disclosure":
            units.append({"unit_id": f"disc_{eid}", "kind_hint": "disclosure", "element_ids": [eid],
                          "representative_element_id": eid, "data_category": cls.get("data_category"),
                          "authoring_component": ac, "frequency": 1, "deck_spread": 1,
                          "complexity": 1, "reuse_discount": 0, "label": None})
        else:
            # fixed-content / brand-theme / parameter / none -> not a build-a-chain target.
            not_planned[ac] = not_planned.get(ac, 0) + 1

    return units, not_planned


# ------------------------------------------------------------------ nodes / DAG


def _title(unit: dict[str, Any], kind: str) -> str:
    cat = unit.get("data_category") or "uncategorized"
    label = unit.get("label")
    base = label or f"{cat} {unit.get('authoring_component') or ''}".strip()
    span = ""
    if unit.get("frequency", 1) > 1:
        span = f" ({unit['frequency']} samples / {unit['deck_spread']} decks)"
    return f"{kind}: {base}{span}"


def _source_proposal(binding_rec: dict[str, Any]) -> dict[str, Any]:
    """Condense a source_bindings.jsonl record into the block node's proposed_source.

    For the FILE family, also propose the build shape: file-backed blocks are a staged
    CHAIN (Content-Service staging -> Type-24 byte producer -> reader -> optional
    transform; see the block-author skill's file-sourcing playbook), and staging is a
    human decision (content type + filename pattern), surfaced as the `staging` gate.
    """
    confirmed = {
        b["deck_column"]: f"{b['candidates'][0]['table']}.{b['candidates'][0]['column']}"
        for b in binding_rec.get("bindings", [])
        if b.get("resolution") == "confirm" and b.get("candidates")
    }
    files = sorted({
        b["candidates"][0].get("source_file")
        for b in binding_rec.get("bindings", [])
        if b.get("resolution") == "confirm" and b.get("candidates") and b["candidates"][0].get("source_file")
    })
    proposal = {
        "source_family": binding_rec.get("source_family_proposal"),
        "source_files": files[:3],
        "columns_bound": binding_rec.get("bound_columns"),
        "columns_total": binding_rec.get("total_columns"),
        "columns_to_clarify": binding_rec.get("clarify_columns"),
        "bindings": dict(list(confirmed.items())[:8]),
    }
    if proposal["source_family"] == "file":
        proposal["staging"] = "content-service (confirm content type + naming pattern)"
        chain = ["content-service-read", "reader"]
        # Reshape hint: >=2 deck columns whose TOP candidate is the SAME source column
        # means the deck spreads one source measure across columns (periods/entities as
        # deck columns over a long-format file) -> a pivot transform is expected.
        top_targets: dict[str, int] = {}
        for b in binding_rec.get("bindings", []):
            if b.get("candidates"):
                key = f"{b['candidates'][0]['table']}.{b['candidates'][0]['column']}"
                top_targets[key] = top_targets.get(key, 0) + 1
        if any(n >= 2 for n in top_targets.values()):
            proposal["reshape_hint"] = "pivot-suspected"
            chain.append("transform")
        proposal["chain"] = chain
    return proposal


# Platform parameter convention: a Data Block is parameterized by AccountCode +
# AsofDate at minimum (EXACT spelling — lowercase "o" in AsofDate; these are the
# generation engine's RunTimeParameters keys). Period-window categories take a
# FromDate/ToDate range instead of a point; firm-level categories drop AccountCode.
# The authoritative statement lives in the assette-block-author skill; the plan
# stamps the proposal on every block node so the author phase inherits it.
_RANGE_CATEGORIES = frozenset({"transactions", "cash-flows"})
_FIRM_LEVEL_CATEGORIES = frozenset({"personnel", "reference-other"})


def _default_parameters(data_category: Any) -> list[str]:
    if data_category in _RANGE_CATEGORIES:
        return ["AccountCode", "FromDate", "ToDate"]
    if data_category in _FIRM_LEVEL_CATEGORIES:
        return ["AsofDate"]
    return ["AccountCode", "AsofDate"]


def make_nodes(
    units: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    blocking_by_eid: dict[str, list[dict[str, Any]]],
    bindings_by_eid: dict[str, dict[str, Any]] | None = None,
    needs_confirm_eids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Expand each unit into its build nodes + synthesize page nodes that compose shells."""
    bindings_by_eid = bindings_by_eid or {}
    needs_confirm_eids = needs_confirm_eids or set()
    nodes: list[dict[str, Any]] = []
    shell_node_by_unit: dict[str, str] = {}

    for unit in units:
        uid = unit["unit_id"]
        hint = unit.get("kind_hint")
        unit_qs = sorted({f"{q['element_id']}:{q['question_id']}" for eid in unit["element_ids"] for q in blocking_by_eid.get(eid, [])})
        # Structure-verification gate: the unit's understanding was vision-derived
        # (verification: needs-confirmation) — the chain builds, but a human confirms
        # the structure at build time (mirrors the source-family gate; blocks nothing).
        needs_sv = any(eid in needs_confirm_eids for eid in unit["element_ids"])

        if hint == "footnote":
            nodes.append(_node("footnote:" + uid, "footnote", _title(unit, "Footnote"), unit, [], unit_qs))
            continue
        if hint == "disclosure":
            nodes.append(_node("disclosure:" + uid, "disclosure", _title(unit, "Disclosure"), unit, [], unit_qs))
            continue

        # Data-driven chain: block -> object -> shell.
        block_id = "block:" + uid
        object_id = "object:" + uid
        shell_id = "shell:" + uid
        block_node = _node(block_id, "data-block", _title(unit, "Data Block"), unit, [], unit_qs, needs_structure_verification=needs_sv)
        block_node["default_parameters"] = _default_parameters(unit.get("data_category"))
        # Source binding (Phase 2d, when /bind-sources has run): the source-family ASK
        # becomes a proposal the human CONFIRMS. Attached to the block node only.
        binding = next((bindings_by_eid[eid] for eid in [unit.get("representative_element_id"), *unit["element_ids"]]
                        if eid and eid in bindings_by_eid), None)
        if binding:
            proposal = _source_proposal(binding)
            block_node["proposed_source"] = proposal
            if proposal.get("source_family") == "file":
                # File-family: staging (content type + naming pattern) is its own human
                # gate, and the node builds as a producer->reader(->transform) chain.
                block_node["gates"] = ["source-family", "staging", "publish"]
                block_node["title"] += " (file chain)"
        nodes.append(block_node)
        nodes.append(_node(object_id, "data-object", _title(unit, "Data Object"), unit, [block_id], unit_qs, needs_structure_verification=needs_sv))
        nodes.append(_node(shell_id, "smart-shell", _title(unit, "Smart Shell"), unit, [object_id], unit_qs, needs_structure_verification=needs_sv))
        shell_node_by_unit[uid] = shell_id

    # Page nodes: one per (deck_id, slide_index) holding data-driven elements; depends on
    # the shells of the units whose elements live on that slide (one shell can feed many).
    slide_shells: dict[tuple[str, int], set[str]] = {}
    slide_eids: dict[tuple[str, int], list[str]] = {}
    for unit in units:
        if unit.get("kind_hint"):
            continue
        shell_id = shell_node_by_unit.get(unit["unit_id"])
        if not shell_id:
            continue
        for eid in unit["element_ids"]:
            info = index.get(eid)
            if not info or info.get("slide_index") is None:
                continue
            key = (info["deck_id"], info["slide_index"])
            slide_shells.setdefault(key, set()).add(shell_id)
            slide_eids.setdefault(key, []).append(eid)

    for (deck_id, slide_index), shells in slide_shells.items():
        page_id = f"page:{deck_id}:{slide_index}"
        page_unit = {
            "unit_id": page_id, "element_ids": sorted(slide_eids[(deck_id, slide_index)]),
            "representative_element_id": None, "data_category": None, "authoring_component": None,
            "frequency": 1, "deck_spread": 1, "complexity": len(shells), "reuse_discount": 0,
            "label": f"slide {slide_index} of deck {deck_id[:8]}",
        }
        page_qs = sorted({f"{q['element_id']}:{q['question_id']}" for eid in page_unit["element_ids"] for q in blocking_by_eid.get(eid, [])})
        nodes.append(_node(page_id, "smart-page", f"Smart Page: slide {slide_index} ({len(shells)} shell(s))", page_unit, sorted(shells), page_qs))

    return nodes


def _node(node_id: str, kind: str, title: str, unit: dict[str, Any], depends_on: list[str], blocking_qs: list[str], *, needs_structure_verification: bool = False) -> dict[str, Any]:
    gates = {
        "data-block": ["source-family", "publish"],
        "data-object": ["publish"],
        "smart-shell": ["publish"],
        "smart-page": ["composition", "publish"],
        "footnote": ["reuse-first", "publish"],
        "disclosure": ["reuse-first"],
    }[kind]
    if needs_structure_verification:
        # Vision-derived structure: confirm-at-build-time, exactly like the
        # source-family gate — visible, never blocking.
        gates = gates[:-1] + ["structure-verification"] + gates[-1:] if gates and gates[-1] == "publish" else [*gates, "structure-verification"]
    if blocking_qs:
        status = "blocked-on-question"
    elif "source-family" in gates or "composition" in gates or "structure-verification" in gates:
        status = "needs-confirmation"
    else:
        status = "ready"
    return {
        "node_id": node_id,
        "kind": kind,
        "title": title,
        "skill": SKILL[kind],
        "stage": STAGE[kind],
        "data_category": unit.get("data_category"),
        "authoring_component": unit.get("authoring_component") if kind in {"smart-shell"} else None,
        "element_ids": unit["element_ids"],
        "representative_element_id": unit.get("representative_element_id"),
        "depends_on": depends_on,
        "gates": gates,
        "status": status,
        "open_blocking_questions": blocking_qs,
        # impact signals carried for the scorer (filled in score_nodes)
        "_frequency": unit.get("frequency", 1),
        "_deck_spread": unit.get("deck_spread", 1),
        "_complexity": unit.get("complexity", 0),
        "_reuse_discount": unit.get("reuse_discount", 0),
    }


def downstream_unblock(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Transitive count of nodes that DEPEND on each node (its leverage)."""
    dependents: dict[str, set[str]] = {n["node_id"]: set() for n in nodes}
    for n in nodes:
        for dep in n["depends_on"]:
            if dep in dependents:
                dependents[dep].add(n["node_id"])

    memo: dict[str, set[str]] = {}

    def descendants(nid: str, seen: set[str]) -> set[str]:
        if nid in memo:
            return memo[nid]
        acc: set[str] = set()
        for d in dependents.get(nid, ()):
            if d in seen:
                continue
            acc.add(d)
            acc |= descendants(d, seen | {d})
        memo[nid] = acc
        return acc

    return {n["node_id"]: len(descendants(n["node_id"], {n["node_id"]})) for n in nodes}


def score_nodes(nodes: list[dict[str, Any]], weights: dict[str, float], category_weights: dict[str, float]) -> None:
    """Attach an impact score + factor breakdown to every node, then strip the temp fields."""
    unblock = downstream_unblock(nodes)
    for n in nodes:
        crit = float(category_weights.get(n.get("data_category") or "", 1.0))
        factors = {
            "frequency": n.pop("_frequency"),
            "deck_spread": n.pop("_deck_spread"),
            "downstream_unblock": unblock[n["node_id"]],
            "criticality": crit,
            "complexity": n.pop("_complexity"),
            "reuse_discount": n.pop("_reuse_discount"),
        }
        score = (
            weights["frequency"] * factors["frequency"]
            + weights["deck_spread"] * factors["deck_spread"]
            + weights["downstream_unblock"] * factors["downstream_unblock"]
            + weights["criticality"] * factors["criticality"]
            - weights["complexity"] * factors["complexity"]
            + weights["reuse_discount"] * factors["reuse_discount"]
        )
        n["impact"] = {"score": round(score, 3), "factors": factors}


# ------------------------------------------------------------------ plan


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    valid_ids = inventory_element_ids(inventory)
    index = index_elements(inventory)

    # Synthetic vision-region elements (code-minted by the segment merge) are as real
    # as inventory ids to the guard, and carry deck/page context for page grouping.
    regions = load_vision_regions(Path(getattr(args, "vision_regions", "") or "workspace/vision_regions.json"))
    filenames_by_deck = {d.get("deck_id"): d.get("filename") for d in inventory.get("decks", [])}
    for region in regions:
        rid = region.get("element_id")
        if not rid:
            continue
        valid_ids.add(rid)
        index[rid] = {
            "deck_id": region.get("deck_id"),
            "filename": filenames_by_deck.get(region.get("deck_id")),
            "slide_index": region.get("slide_index"),
            "kind": region.get("kind", "vision-region"),
        }

    classifications = latest_classifications(read_jsonl(Path(args.classifications)))
    understanding = read_jsonl(Path(args.understanding))
    u_index = understanding_index(understanding)
    groups = (read_json(Path(args.groups)).get("groups") or []) if args.groups else []
    question_records = read_jsonl(Path(args.question_queue)) if args.question_queue else []
    category_weights = read_json(Path(args.category_weights)) if args.category_weights else {}
    bindings_by_eid: dict[str, dict[str, Any]] = {}
    if args.bindings:
        for rec in read_jsonl(Path(args.bindings)):
            eid = rec.get("element_id")
            if eid:
                bindings_by_eid[eid] = rec

    weights = dict(DEFAULT_WEIGHTS)
    for k in weights:
        v = getattr(args, f"w_{k}", None)
        if v is not None:
            weights[k] = float(v)

    # Open blocking questions, grounded. element_id must be real.
    dropped: list[str] = []
    blocking_by_eid: dict[str, list[dict[str, Any]]] = {}
    blocking_questions: list[dict[str, Any]] = []
    for q in question_records:
        eid = q.get("element_id")
        if not eid:
            continue
        if eid not in valid_ids:
            dropped.append(str(eid))  # GROUNDING GUARD
            continue
        if q.get("blocking"):
            blocking_by_eid.setdefault(eid, []).append(q)
            blocking_questions.append(q)

    units, not_planned = build_units(valid_ids, classifications, groups, u_index, dropped)
    # Vision-derived structures (verification: needs-confirmation) gate their chain
    # with structure-verification -> node status needs-confirmation, never blocking.
    needs_confirm_eids: set[str] = set()
    for rec in understanding:
        if rec.get("verification") == "needs-confirmation":
            for eid in [rec.get("element_id"), *(rec.get("member_element_ids") or [])]:
                if eid:
                    needs_confirm_eids.add(eid)
    nodes = make_nodes(units, index, blocking_by_eid, bindings_by_eid, needs_confirm_eids)
    score_nodes(nodes, weights, category_weights)

    # Implementation modules: from the corpus folder structure (one folder per
    # module) + optional workspace/modules.json overrides. A node's modules =
    # the modules of every deck its elements come from; >1 means shared
    # plumbing that also serves other modules (built with the FIRST module
    # that needs it, inherited by the rest).
    module_overrides = read_json(Path(args.modules_file)) if getattr(args, "modules_file", "") else {}
    modules_by_deck = deck_modules(inventory, module_overrides)
    for n in nodes:
        mods = sorted({_eid_module(eid, modules_by_deck) for eid in n["element_ids"]}) or ["(ungrouped)"]
        n["modules"] = mods
        n["shared_across_modules"] = len(mods) > 1

    # --module scoping: keep only the nodes serving that module (shared nodes
    # included — they serve it too). Chains stay closed: a node's dependencies
    # always carry a superset of its modules.
    scoped_module = None
    if getattr(args, "module", None):
        want = str(args.module).strip().lower()
        available = sorted({m for n in nodes for m in n["modules"]})
        scoped_module = next((m for m in available if m.lower() == want), None)
        if scoped_module is None:
            print(f"Unknown module '{args.module}'. Modules in this corpus: "
                  f"{', '.join(available)}", file=sys.stderr)
            raise SystemExit(2)
        nodes = [n for n in nodes if scoped_module in n["modules"]]
        kept_eids = {eid for n in nodes for eid in n["element_ids"]}
        blocking_questions = [q for q in blocking_questions if q.get("element_id") in kept_eids]

    # Deterministic build order: topo by stage, then highest impact, then id.
    nodes.sort(key=lambda n: (n["stage"], -n["impact"]["score"], n["node_id"]))

    # Up-front blocking-question batch: highest-leverage blocked node first.
    impact_by_eid: dict[str, float] = {}
    for n in nodes:
        for eid in n["element_ids"]:
            impact_by_eid[eid] = max(impact_by_eid.get(eid, 0.0), n["impact"]["score"])
    batch = sorted(
        (
            {
                "element_id": q["element_id"],
                "question_id": q.get("question_id"),
                "facet": q.get("facet"),
                "question": q.get("question"),
                "options": q.get("options"),
                "default": q.get("default"),
                "why": q.get("why"),
                "data_category": q.get("data_category"),
                "authoring_component": q.get("authoring_component"),
                "module": _eid_module(q["element_id"], modules_by_deck),
                "blocks_node_impact": round(impact_by_eid.get(q["element_id"], 0.0), 3),
            }
            for q in blocking_questions
        ),
        key=lambda b: (-b["blocks_node_impact"], str(b["element_id"]), str(b["question_id"])),
    )

    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_module: dict[str, dict[str, Any]] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
        by_status[n["status"]] = by_status.get(n["status"], 0) + 1
        for m in n["modules"]:
            bm = by_module.setdefault(m, {"node_count": 0, "by_status": {}, "blocking_question_count": 0})
            bm["node_count"] += 1
            bm["by_status"][n["status"]] = bm["by_status"].get(n["status"], 0) + 1
    for b in batch:
        bm = by_module.get(b["module"])
        if bm:
            bm["blocking_question_count"] += 1
    blocks_with_proposal = sum(1 for n in nodes if n.get("proposed_source"))
    file_staging_unconfirmed = sum(
        1 for n in nodes
        if str((n.get("proposed_source") or {}).get("staging", "")).startswith("content-service")
    )

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "source_inventory": str(Path(args.inventory)),
        "weights": weights,
        "summary": {
            "node_count": len(nodes),
            "unit_count": len(units),
            "deduped_from_groups": sum(1 for u in units if not u.get("kind_hint") and u.get("frequency", 1) > 1),
            "by_kind": by_kind,
            "by_status": by_status,
            "modules": sorted(by_module),
            "by_module": by_module,
            "shared_node_count": sum(1 for n in nodes if n["shared_across_modules"]),
            "scoped_to_module": scoped_module,
            "blocking_question_count": len(batch),
            "blocks_with_source_proposal": blocks_with_proposal,
            "file_blocks_staging_unconfirmed": file_staging_unconfirmed,
            "vision_structures_needing_confirmation": sum(
                1 for rec in understanding
                if rec.get("structure_provenance") in ("vision", "mixed")
            ),
            "elements_not_planned": not_planned,
            "dropped_unknown_id_count": len(dropped),
        },
        "build_order": nodes,
        "blocking_question_batch": batch,
        "dropped_unknown_ids": sorted(set(dropped)),
    }


# ----------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile a finished /analyze-deck workspace into a topo-sorted, impact-ranked "
        "build plan (Block->Object->Shell->Page). Read-only: proposes a plan + the up-front "
        "blocking-question batch; builds nothing."
    )
    parser.add_argument("--inventory", default="workspace/corpus_inventory.json")
    parser.add_argument("--classifications", default="workspace/classifications.jsonl")
    parser.add_argument("--understanding", default="workspace/element_understanding.jsonl")
    parser.add_argument("--vision-regions", default="workspace/vision_regions.json")
    parser.add_argument("--groups", default="workspace/table_chart_groups.json")
    parser.add_argument("--question-queue", default="workspace/question_queue.jsonl")
    parser.add_argument("--category-weights", default="")
    parser.add_argument("--bindings", default="workspace/source_bindings.jsonl",
                        help="source_bindings.jsonl from /bind-sources (optional) — attaches a "
                             "proposed_source to each bound Data Block node.")
    parser.add_argument("--module", default=None,
                        help="scope the plan to ONE implementation module (case-insensitive; "
                             "modules come from the corpus folder structure). Shared nodes that "
                             "also serve other modules are kept and flagged.")
    parser.add_argument("--modules-file", default="workspace/modules.json",
                        help="optional deck->module override map (keys: deck_id or filename).")
    parser.add_argument("--output", default="workspace/build_plan.json")
    for k in DEFAULT_WEIGHTS:
        parser.add_argument(f"--w-{k}", type=float, default=None, help=f"override impact weight for {k}")
    args = parser.parse_args(argv)

    inv = Path(args.inventory)
    if not inv.exists():
        print(f"Inventory not found: {inv}", file=sys.stderr)
        return 2
    cls = Path(args.classifications)
    if not cls.exists():
        print(f"Classifications not found: {cls} -- run /analyze-deck first.", file=sys.stderr)
        return 2

    plan = build_plan(args)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    s = plan["summary"]
    print(
        f"build plan: {s['node_count']} node(s) across {s['unit_count']} unit(s) "
        f"({s['deduped_from_groups']} deduped from cross-deck groups) -> {out}"
    )
    print(f"  by kind:   {s['by_kind']}")
    print(f"  by status: {s['by_status']}")
    if s.get("scoped_to_module"):
        print(f"  SCOPED to module: {s['scoped_to_module']} "
              f"({s['shared_node_count']} node(s) shared with other modules)")
    elif len(s.get("modules") or []) > 1:
        print(f"  modules:   {', '.join(s['modules'])} "
              f"({s['shared_node_count']} shared node(s))")
    print(f"  blocking questions to resolve up front: {s['blocking_question_count']}")
    if s.get("blocks_with_source_proposal"):
        print(f"  data-block nodes with a source proposal (from /bind-sources): {s['blocks_with_source_proposal']}")
    if s.get("file_blocks_staging_unconfirmed"):
        print(
            f"  file-family blocks awaiting a STAGING decision (content type + naming pattern): "
            f"{s['file_blocks_staging_unconfirmed']}"
        )
    if s["elements_not_planned"]:
        print(f"  not planned (fixed/parameter/etc.): {s['elements_not_planned']}")
    if s["dropped_unknown_id_count"]:
        print(
            f"  GROUNDING GUARD dropped {s['dropped_unknown_id_count']} fabricated/unknown id(s): "
            f"{', '.join(plan['dropped_unknown_ids'][:5])}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
