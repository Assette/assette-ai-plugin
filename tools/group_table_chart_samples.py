"""Phase 2c (pre-pass) — group samples of the same data-driven table/chart across decks.

Deterministic, no-LLM grouping that gathers every instance of "the same" table or
chart across the corpus so the `table-chart-analyzer` subagent can reason about
VARIANCE across samples (what varies = dynamic; what is constant = structural).

This is a NARROW, 2c-local pre-pass — NOT the v0.2 Phase 2b canonicalizer
(`cluster_elements.py`), which dedups the WHOLE corpus (all element kinds, parameters,
brownfield reuse). This tool only groups data-driven table/chart elements by a
structural signature, purely to feed deep-understanding variance. When Phase 2b lands,
2c can consume `workspace/clusters.json` instead of this file.

Input:
    workspace/corpus_inventory.json     (Phase 1)
    workspace/classifications.jsonl     (Phase 2a; latest record per element_id wins)
    workspace/slide_components.jsonl    (Phase 1.5 vision components, if rendered)
    workspace/vision_regions.json       (Phase 1.5 synthetic region elements, if any)
Output:
    workspace/table_chart_groups.json   (groups of element_ids that are the same
                                         table/chart across decks, with deck context;
                                         members covered by a vision component carry a
                                         `vision` block — component id/type/label/bbox —
                                         so the slicer can attach render/crop images)

Selection — an element is admitted when EITHER:
  - its latest classification has an authoring_component of smart-shell:table |
    smart-shell:zigzag | smart-shell:chart | smart-shell:performance-history, OR
  - it is the ANCHOR (representative or synthetic vision-region element) of a vision
    component whose component_type is a DATA type (table/chart/kpi-grid/
    performance-history) — even when the classifier said none/ambiguous. This is how
    flattened PDF tables/charts reach deep understanding. Anchor-only: non-anchor
    members were demoted to part-of-component by the classify merge and stay out
    (no duplicate groups per faux-chart fragment).
Static / parameter / disclosure / text elements are out of scope here.

Signature (what makes two elements "the same" structure):
  - parsed table  -> (data_category, "table", col_count, normalized header-row cell texts)
  - parsed chart  -> (data_category, "chart", chart_type, normalized series names)
  - vision comp   -> (data_category, "component", component_type, DATE-STRIPPED label)
                     (headings embed dates — "... as of June 30, 2025" — which would
                     fragment monthly factsheets into single-sample groups; strip them);
                     an element with parsed structure prefers the STRUCTURAL signature
                     (headers are stabler than headings); an unlabeled vision component
                     falls back to (component_type, category, slide_index, bbox
                     quantized to a 0.25 grid) — same-template decks put the same
                     table in the same place.
  - other         -> (data_category, kind, name)   # defensive fallback

CLI:
    py tools/group_table_chart_samples.py \
        --inventory workspace/corpus_inventory.json \
        --classifications workspace/classifications.jsonl \
        --output workspace/table_chart_groups.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from _inventory_common import (
    DATA_COMPONENT_TYPES,
    VISION_COMPONENT_DEFAULT_PRIMITIVE,
    has_parsed_structure,
    load_vision_regions,
    now_iso,
)

# The data-driven table/chart authoring components Phase 2c deep-analyzes.
TABLE_CHART_COMPONENTS = frozenset(
    {
        "smart-shell:table",
        "smart-shell:zigzag",
        "smart-shell:chart",
        "smart-shell:performance-history",
    }
)

_WS = re.compile(r"\s+")

# Date/period tokens stripped from vision-component labels before keying: headings
# like "Sector Weightings (%) as of June 30, 2025" must group with their March twin.
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_LABEL_STRIP = [
    re.compile(r"\bas\s+of\b.*$", re.IGNORECASE),                      # "as of ..." to end
    re.compile(rf"\b({_MONTHS})\.?\s+\d{{1,2}}\s*,?\s*\d{{4}}\b", re.IGNORECASE),
    re.compile(rf"\b({_MONTHS})\.?\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\bq[1-4]\s*(19|20)\d{2}\b", re.IGNORECASE),
    re.compile(r"\b(19|20)\d{2}\b"),
    re.compile(r"\(\s*%\s*\)"),
]


def _norm(text: Any) -> str:
    """Normalize a cell/label for signature comparison: str, strip, lower, collapse WS."""
    return _WS.sub(" ", str(text if text is not None else "").strip()).lower()


def _norm_label(text: Any) -> str:
    """_norm plus date/period/unit-token stripping — for vision-component labels only."""
    out = str(text if text is not None else "")
    for pattern in _LABEL_STRIP:
        out = pattern.sub(" ", out)
    return _norm(out)


def _quantized_bbox(bbox: Any) -> tuple:
    """Quantize a normalized bbox to a coarse 0.25 grid (position-keyed grouping for
    unlabeled vision components — same-template decks draw the same table in the same
    place). Returns () when the bbox is unusable."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return ()
    try:
        return tuple(round(float(v) * 4) / 4 for v in bbox)
    except (TypeError, ValueError):
        return ()


def latest_classifications(path: Path) -> dict[str, dict[str, Any]]:
    """Reduce an append-only classifications.jsonl to the latest record per element_id.

    Last-write-wins: a later record (by `timestamp` when present, else file order)
    supersedes an earlier one for the same element_id — matching how analyze-deck and
    the review queue read this file.
    """
    latest: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = rec.get("element_id")
        if not eid:
            continue
        prev = latest.get(eid)
        if prev is None:
            latest[eid] = rec
            order[eid] = i
            continue
        # Prefer the record with the greater timestamp; fall back to later file order.
        if rec.get("timestamp", "") >= prev.get("timestamp", "") or i >= order[eid]:
            latest[eid] = rec
            order[eid] = i
    return latest


def index_elements(
    inventory: dict[str, Any],
    vision_regions: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map element_id -> {element, deck_id, filename, slide_index, layout_name}.

    Synthetic vision-region elements (workspace/vision_regions.json, code-minted by the
    segment merge) index exactly like inventory elements — their deck context comes
    from the registry record, the filename joins from the inventory deck."""
    index: dict[str, dict[str, Any]] = {}
    filenames: dict[str, Any] = {}
    for deck in inventory.get("decks", []):
        deck_id = deck.get("deck_id")
        filename = deck.get("filename")
        filenames[deck_id] = filename
        for container in deck.get("slides", []):
            slide_index = container.get("slide_index")
            layout_name = container.get("layout_name")
            for element in container.get("elements", []):
                eid = element.get("element_id")
                if not eid:
                    continue
                index[eid] = {
                    "element": element,
                    "deck_id": deck_id,
                    "filename": filename,
                    "slide_index": slide_index,
                    "layout_name": layout_name,
                }
    for region in vision_regions or []:
        rid = region.get("element_id")
        if not rid:
            continue
        deck_id = region.get("deck_id")
        slide_index = region.get("slide_index")
        index[rid] = {
            "element": {
                "element_id": rid,
                "kind": region.get("kind", "vision-region"),
                "position": region.get("position"),
                "name": (region.get("content") or {}).get("label"),
                "content": region.get("content") or {},
            },
            "deck_id": deck_id,
            "filename": filenames.get(deck_id),
            "slide_index": slide_index,
            "layout_name": f"page-{(slide_index or 0) + 1}",
        }
    return index


def signature(element: dict[str, Any], data_category: Any) -> tuple:
    """A structural signature: equal signatures => samples of the same table/chart."""
    kind = element.get("kind")
    content = element.get("content") or {}
    cat = data_category or "unknown"
    if kind == "table":
        cells = content.get("cells") or []
        header = tuple(_norm(c.get("text")) for c in cells[0]) if (content.get("has_header") and cells) else ()
        return (cat, "table", int(content.get("cols") or 0), header)
    if kind == "chart":
        series = content.get("series") or []
        names = tuple(sorted(_norm(s.get("name")) for s in series))
        return (cat, "chart", _norm(content.get("chart_type")), names)
    # Defensive fallback (an element classified as a shell but not a native table/chart).
    return (cat, str(kind), _norm(content.get("name") or element.get("name")))


def _group_id(sig: tuple) -> str:
    digest = hashlib.sha256(repr(sig).encode("utf-8")).hexdigest()[:10]
    return f"tcg_{digest}"


def load_components(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read slide_components.jsonl (if present) into two element-id -> component maps:

    - anchors: the ids a component may be ANALYZED under — its representative element
      and (when materialized) its synthetic vision-region element. These are the only
      ids the widened admission gate accepts.
    - covered: every member id -> its covering component (first-wins by sorted
      component_id on overlap), used to attach the `vision` block to member records.
    """
    anchors: dict[str, dict[str, Any]] = {}
    covered: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return anchors, covered
    comps: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(comp, dict):
            comps.append(comp)
    for comp in sorted(comps, key=lambda c: str(c.get("component_id"))):
        rep = comp.get("representative_element_id")
        if rep:
            anchors.setdefault(rep, comp)
        synthetic = comp.get("vision_region_element_id")
        if synthetic:
            anchors.setdefault(synthetic, comp)
            covered.setdefault(synthetic, comp)
        for m in comp.get("member_element_ids") or []:
            covered.setdefault(m, comp)
    return anchors, covered


def build_groups(
    inventory: dict[str, Any],
    classifications: dict[str, dict[str, Any]],
    anchors: dict[str, dict[str, Any]] | None = None,
    covered: dict[str, dict[str, Any]] | None = None,
    vision_regions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    anchors = anchors or {}
    covered = covered or {}
    index = index_elements(inventory, vision_regions)
    buckets: dict[tuple, dict[str, Any]] = {}

    for eid, info in index.items():
        element = info["element"]
        cls = classifications.get(eid)
        comp = covered.get(eid)
        component = cls.get("authoring_component") if cls else None
        if component in TABLE_CHART_COMPONENTS:
            data_category = cls.get("data_category")
            if data_category is None and comp is not None:
                data_category = comp.get("proposed_data_category")
        else:
            # Widened admission: a vision DATA component admits its ANCHOR even when
            # the classifier said none/ambiguous (or never saw the synthetic id).
            anchor_comp = anchors.get(eid)
            if anchor_comp is None or anchor_comp.get("component_type") not in DATA_COMPONENT_TYPES:
                continue
            # Anchor-only, one anchor per component: prefer the synthetic region when
            # it exists (the rep was demoted to part-of-component by the merge).
            preferred = anchor_comp.get("vision_region_element_id") or anchor_comp.get(
                "representative_element_id"
            )
            if eid != preferred:
                continue
            # Object-model outranks vision: when any member parses, the parsed element
            # is the analysis unit (admitted by the classifier gate above) — never a
            # second, vision-keyed duplicate.
            if any(
                has_parsed_structure((index.get(m) or {}).get("element"))
                for m in anchor_comp.get("member_element_ids") or []
            ):
                continue
            comp = anchor_comp
            ac = anchor_comp.get("proposed_authoring_component") or "none"
            component = (
                ac
                if ac in TABLE_CHART_COMPONENTS
                else VISION_COMPONENT_DEFAULT_PRIMITIVE[anchor_comp["component_type"]]
            )
            data_category = (cls.get("data_category") if cls else None) or anchor_comp.get(
                "proposed_data_category"
            )

        if comp is not None and not has_parsed_structure(element):
            # Vision signature: group by what the eye sees (type + date-stripped label),
            # so faux-charts and flattened regions group across decks. An element WITH
            # parsed structure prefers the structural signature below — headers are
            # stabler than headings.
            ctype = comp.get("component_type")
            stripped = _norm_label(comp.get("label"))
            if stripped:
                sig = (data_category or "unknown", "component", ctype, stripped)
            else:
                sig = (
                    data_category or "unknown",
                    "component",
                    ctype,
                    info.get("slide_index"),
                    _quantized_bbox(comp.get("bbox")),
                )
            shape = (
                "table"
                if ctype in ("table", "kpi-grid")
                else ("chart" if ctype in ("chart", "performance-history") else "other")
            )
            label = comp.get("label")
        else:
            sig = signature(element, data_category)
            shape = "table" if element.get("kind") == "table" else ("chart" if element.get("kind") == "chart" else "other")
            label = comp.get("label") if comp is not None else None
        bucket = buckets.setdefault(
            sig,
            {
                "group_id": _group_id(sig),
                "shape": shape,
                "label": label,
                "data_category": data_category,
                "authoring_components": set(),
                "members": [],
            },
        )
        bucket["authoring_components"].add(component)
        member: dict[str, Any] = {
            "element_id": eid,
            "deck_id": info["deck_id"],
            "filename": info["filename"],
            "slide_index": info["slide_index"],
            "layout_name": info["layout_name"],
        }
        if comp is not None:
            member["vision"] = {
                "component_id": comp.get("component_id"),
                "component_type": comp.get("component_type"),
                "label": comp.get("label"),
                "bbox": comp.get("bbox"),
                "bbox_suspect": bool(comp.get("bbox_suspect")),
            }
        bucket["members"].append(member)

    groups: list[dict[str, Any]] = []
    for sig, bucket in buckets.items():
        members = sorted(bucket["members"], key=lambda m: m["element_id"])
        observed = sorted(bucket["authoring_components"])
        # Refine to zigzag if any sample was tagged a full-list/zigzag shell.
        if "smart-shell:zigzag" in observed:
            component = "smart-shell:zigzag"
        else:
            component = observed[0] if observed else None
        groups.append(
            {
                "group_id": bucket["group_id"],
                "shape": bucket["shape"],
                "label": bucket.get("label"),
                "data_category": bucket["data_category"],
                "authoring_component": component,
                "authoring_components_observed": observed,
                "sample_count": len(members),
                "representative_element_id": members[0]["element_id"],
                "members": members,
            }
        )
    # Deterministic order: most-sampled groups first, then by group_id.
    groups.sort(key=lambda g: (-g["sample_count"], g["group_id"]))
    return groups


def build_output(inventory_path: Path, groups: list[dict[str, Any]]) -> dict[str, Any]:
    multi = sum(1 for g in groups if g["sample_count"] > 1)
    return {
        "schema_version": "0.2.0",
        "generated_at": now_iso(),
        "source_inventory": str(inventory_path),
        "group_count": len(groups),
        "multi_sample_group_count": multi,
        "element_count": sum(g["sample_count"] for g in groups),
        "groups": groups,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Group data-driven table/chart samples across decks for Phase 2c deep understanding."
    )
    parser.add_argument("--inventory", default="workspace/corpus_inventory.json")
    parser.add_argument("--classifications", default="workspace/classifications.jsonl")
    parser.add_argument("--components", default="workspace/slide_components.jsonl")
    parser.add_argument("--vision-regions", default="workspace/vision_regions.json")
    parser.add_argument("--output", default="workspace/table_chart_groups.json")
    args = parser.parse_args(argv)

    inventory_path = Path(args.inventory)
    classifications_path = Path(args.classifications)
    components_path = Path(args.components)
    output_path = Path(args.output)

    if not inventory_path.exists():
        print(f"Inventory not found: {inventory_path}", file=sys.stderr)
        return 2
    if not classifications_path.exists():
        print(f"Classifications not found: {classifications_path}", file=sys.stderr)
        return 2

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    classifications = latest_classifications(classifications_path)
    anchors, covered = load_components(components_path)
    vision_regions = load_vision_regions(Path(args.vision_regions))
    groups = build_groups(inventory, classifications, anchors, covered, vision_regions)
    output = build_output(inventory_path, groups)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(
        f"Grouped {output['element_count']} table/chart element(s) into "
        f"{output['group_count']} group(s) ({output['multi_sample_group_count']} multi-sample) "
        f"-> {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
