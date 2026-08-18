"""Phase 2 merge + GROUNDING GUARD for /analyze-deck.

Deterministic, no-LLM post-pass that turns the per-deck / per-group machine outputs
the `element-classifier` and `table-chart-analyzer` agents write into the canonical
workspace files — and, critically, HARD-VALIDATES every `element_id` against the real
`corpus_inventory.json` id set. Any record whose id is not in the inventory is DROPPED
and counted. This is the mechanical anti-hallucination guard: a fabricated id like
`sample:0:0` (which an agent inventing data instead of reading the file would emit)
can never reach the canonical files, because it is not in the inventory.

The agents now READ the real inventory and WRITE their own per-slice output; this tool
only concatenates + validates + derives. It never calls an LLM and never trusts an id
it cannot find in the inventory.

--------------------------------------------------------------------------------
MODE: classify
  Inputs : corpus_inventory.json, workspace/batches/classify_*.jsonl,
           (optional) existing classifications.jsonl (to preserve human corrections)
  Outputs: classifications.jsonl   (validated machine records + preserved corrections),
           review_queue.jsonl       (latest record per id with tag==ambiguous OR conf<thr)

MODE: understand
  Inputs : corpus_inventory.json, workspace/batches/understand_*.json,
           (optional) question_answers.jsonl (to drop answered questions)
  Outputs: element_understanding.jsonl   (validated machine records),
           question_queue.jsonl          (one line per OPEN question, keyed id+question_id)

MODE: bind   (Phase 2d — source binding; runs only when sources were supplied)
  Inputs : element_understanding.jsonl (valid element ids + deck column counts),
           source_catalog.json (valid column ids — the grounding truth for bindings),
           workspace/batches/bind_*.jsonl (one file per source, from source-binding-analyzer)
  Outputs: source_bindings.jsonl  (one line per element: candidates merged across sources,
           ranked, canonicalized from the catalog; fabricated element/column ids DROPPED;
           each record carries structure_provenance from the understanding)

MODE: segment (Phase 1.5 — vision components; runs only when decks were rendered)
  Inputs : corpus_inventory.json, workspace/batches/segment_*.jsonl
  Outputs: slide_components.jsonl (validated components; each bbox checked against the
           union of its members' positions — a bbox that misses its members is nulled
           and flagged bbox_suspect so nothing ever crops the wrong region),
           vision_regions.json    (SYNTHETIC vision-region elements, one per validated
           DATA component whose members carry no parsed object-model structure —
           element ids <deck_id>:<slide_index>:v<n> minted DETERMINISTICALLY BY CODE,
           never by an LLM, so the grounding guard stays intact. These give each
           flattened table/chart its own anchor: without them, every component on a
           raster page would collide on the page's single picture element id.)
  Also prints the per-page component inventory (deterministic eyeball QA).

VISION REGIONS + THE GUARD: classify / understand modes accept --vision-regions and
validate ids against inventory ∪ registry. Object-model structure ALWAYS outranks
vision: a component whose members include a parsed table/chart keeps the parsed
element as its analysis unit (no synthetic region, no overwrite of a good classifier
record). Vision-derived understanding records are confidence-clamped below the review
threshold and receive a deterministic, NON-blocking 'vision-confirm' question.
--------------------------------------------------------------------------------

CLI:
  py tools/merge_validate_analysis.py --mode classify \
      --inventory workspace/corpus_inventory.json \
      --batches-dir workspace/batches \
      --classifications workspace/classifications.jsonl \
      --review-queue workspace/review_queue.jsonl

  py tools/merge_validate_analysis.py --mode understand \
      --inventory workspace/corpus_inventory.json \
      --batches-dir workspace/batches \
      --understanding workspace/element_understanding.jsonl \
      --question-queue workspace/question_queue.jsonl \
      --answers workspace/question_answers.jsonl
"""

from __future__ import annotations

import argparse
import json
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

REVIEW_THRESHOLD_DEFAULT = 0.7

# Vision/mixed-provenance understanding records are clamped just below the review
# threshold until a human confirms the structure (never trust model self-capping).
VISION_CONFIDENCE_CAP = 0.69

# A component bbox must overlap the union of its members' positions by at least this
# containment ratio, else it is nulled + flagged bbox_suspect (never crop a wrong box).
BBOX_OVERLAP_MIN = 0.2

VISION_CONFIRM_QUESTION_ID = "vision-confirm"


def _tag_for(authoring_component: str | None) -> str:
    """Derive a Level-1 tag for a vision-segmented component from its primitive."""
    ac = authoring_component or "none"
    if ac.startswith("smart-shell:"):
        return "data-driven-qualitative" if ac == "smart-shell:text" else "data-driven-quantitative"
    return {"parameter": "parameterized", "disclosure": "conditional"}.get(ac, "static")


# --------------------------------------------------------------------------- io


def inventory_element_ids(
    inventory: dict[str, Any],
    vision_regions: list[dict[str, Any]] | None = None,
) -> set[str]:
    """The set of every real element_id in the corpus — the ground truth for ids.

    `vision_regions` (the records of workspace/vision_regions.json) extends the set
    with the code-minted synthetic vision-region ids: they are deterministic output of
    run_segment, never LLM-authored, so admitting them keeps the guard intact.
    """
    ids: set[str] = set()
    for deck in inventory.get("decks", []):
        for container in deck.get("slides", []):
            for element in container.get("elements", []):
                eid = element.get("element_id")
                if eid:
                    ids.add(eid)
    for region in vision_regions or []:
        rid = region.get("element_id")
        if rid:
            ids.add(rid)
    return ids


def inventory_element_lookup(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """element_id -> element record, for structure checks and geometry."""
    lookup: dict[str, dict[str, Any]] = {}
    for deck in inventory.get("decks", []):
        for container in deck.get("slides", []):
            for element in container.get("elements", []):
                eid = element.get("element_id")
                if eid:
                    lookup[eid] = element
    return lookup


def _normalized_rect(element: dict[str, Any], page_w_emu: float, page_h_emu: float) -> tuple[float, float, float, float] | None:
    """Element position (EMU) -> normalized [l, t, r, b] over the page box; None when
    the position or page dimensions are unusable (e.g. Word's empty positions)."""
    pos = element.get("position") or {}
    if page_w_emu <= 0 or page_h_emu <= 0:
        return None
    try:
        left = float(pos.get("left_emu", 0))
        top = float(pos.get("top_emu", 0))
        width = float(pos.get("width_emu", 0))
        height = float(pos.get("height_emu", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (left / page_w_emu, top / page_h_emu, (left + width) / page_w_emu, (top + height) / page_h_emu)


def _valid_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    """Parse a component bbox; None unless it is 4 numbers with l<r, t<b in ~[0,1]."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        left, top, right, bottom = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not (left < right and top < bottom):
        return None
    if left < -0.05 or top < -0.05 or right > 1.05 or bottom > 1.05:
        return None
    return (left, top, right, bottom)


def _containment(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection area over the SMALLER of the two rects (0 when disjoint)."""
    il, it = max(a[0], b[0]), max(a[1], b[1])
    ir, ib = min(a[2], b[2]), min(a[3], b[3])
    if ir <= il or ib <= it:
        return 0.0
    inter = (ir - il) * (ib - it)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    denom = min(area_a, area_b)
    return inter / denom if denom > 0 else 0.0


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


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")


def _load_machine_files(batches_dir: Path, glob: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Load every machine-output file matching `glob`. Returns (records, skipped_files).

    `classify_*.jsonl`   -> each file is JSONL, many records.
    `understand_*.json`  -> each file is one JSON object (the group understanding).
    """
    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not batches_dir.exists():
        return records, skipped
    for path in sorted(batches_dir.glob(glob)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            skipped.append(path.name)
            continue
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
        else:
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                skipped.append(path.name)
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                skipped.append(path.name)
    return records, skipped


# --------------------------------------------------- component-aware collapse


def _component_primitive(comp: dict[str, Any]) -> str:
    """The component's authoring primitive, with the deterministic fallback: a DATA
    component whose segmenter abstained ("none"/missing/non-data primitive) gets the
    component_type default — code enforces promotion, prose obedience is not enough."""
    ac = comp.get("proposed_authoring_component") or "none"
    ctype = comp.get("component_type")
    if ctype in DATA_COMPONENT_TYPES and not ac.startswith("smart-shell:"):
        return VISION_COMPONENT_DEFAULT_PRIMITIVE[ctype]
    return ac


def apply_components(
    kept: dict[str, dict[str, Any]],
    components: list[dict[str, Any]],
    valid_ids: set[str],
    stamp: str,
    element_lookup: dict[str, dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """Fold vision-segmented components into the per-element classifications.

    One element carries the component-level classification (so the component flows
    downstream as one shell); the other members are demoted to authoring_component
    "none" with a part-of-component routing hint — which pulls faux-chart fragments out
    of the review queue. Only real ids are touched (grounding guard already applied
    upstream). Returns (components_applied, members_folded).

    Which element carries it — object-model ALWAYS outranks vision:
    1. A member with parsed structure (table cells / chart series) anchors the
       component regardless of the segmenter's representative choice, and its existing
       data-driven classifier record is MERGED (hint + category fill + max confidence),
       never overwritten — a deterministic parse is never replaced by a vision guess.
    2. Otherwise a DATA component uses its synthetic vision-region element
       (vision_region_element_id, minted by run_segment) so multiple components on one
       page never collide on the page's single picture/text anchor.
    3. Otherwise (non-data component, or legacy slide_components without regions) the
       representative element is promoted, as before.
    A data component's primitive defaults from its component_type when the segmenter
    abstained, and an existing classifier data_category is never nulled by a null
    vision proposal.
    """
    lookup = element_lookup or {}
    reps = members_folded = 0
    for comp in components:
        rep = comp.get("representative_element_id")
        if not rep or rep not in valid_ids:
            continue
        cid = comp.get("component_id")
        label = comp.get("label") or ""
        ctype = comp.get("component_type")
        members = [m for m in (comp.get("member_element_ids") or []) if m in valid_ids]

        parsed = sorted(m for m in members if has_parsed_structure(lookup.get(m)))
        synthetic = comp.get("vision_region_element_id")

        if parsed:
            # 1) Object-model anchor: merge, never replace.
            anchor = rep if rep in parsed else parsed[0]
            ac = _component_primitive(comp)
            comp_dc = comp.get("proposed_data_category")
            existing = kept.get(anchor)
            if (
                existing
                and str(existing.get("tag", "")).startswith("data-driven")
                and str(existing.get("authoring_component", "")).startswith("smart-shell:")
            ):
                if existing.get("data_category") is None and comp_dc:
                    existing["data_category"] = comp_dc
                existing["confidence"] = max(
                    float(existing.get("confidence", 0.0) or 0.0),
                    float(comp.get("confidence", 0.0) or 0.0),
                )
                existing["routing_hint"] = f"component:{cid}"
                existing["timestamp"] = stamp
            else:
                tag = _tag_for(ac)
                prior_dc = (existing or {}).get("data_category") if existing and str(
                    (existing or {}).get("tag", "")
                ).startswith("data-driven") else None
                kept[anchor] = {
                    "element_id": anchor,
                    "tag": tag,
                    "data_category": (comp_dc or prior_dc) if tag.startswith("data-driven") else None,
                    "authoring_component": ac,
                    "confidence": float(comp.get("confidence", 0.85) or 0.85),
                    "rationale": f"Vision-segmented component '{label}' ({ctype}) anchored on its parsed {lookup.get(anchor, {}).get('kind', 'element')}; {len(members)} member shape(s).",
                    "routing_hint": f"component:{cid}",
                    "source": "slide-vision-component",
                    "timestamp": stamp,
                }
        elif ctype in DATA_COMPONENT_TYPES and synthetic and synthetic in valid_ids:
            # 2) Synthetic vision-region anchor — the component's own element id.
            anchor = synthetic
            ac = _component_primitive(comp)
            tag = _tag_for(ac)
            kept[anchor] = {
                "element_id": anchor,
                "tag": tag,
                "data_category": comp.get("proposed_data_category") if tag.startswith("data-driven") else None,
                "authoring_component": ac,
                "confidence": float(comp.get("confidence", 0.85) or 0.85),
                "rationale": f"Vision-segmented component '{label}' ({ctype}); synthetic vision region for {len(members)} member shape(s).",
                "routing_hint": f"component:{cid}",
                "source": "slide-vision-component",
                "timestamp": stamp,
            }
        else:
            # 3) Legacy/non-data path: the representative carries the classification.
            anchor = rep
            ac = _component_primitive(comp)
            tag = _tag_for(ac)
            existing_dc = (kept.get(anchor) or {}).get("data_category") if str(
                (kept.get(anchor) or {}).get("tag", "")
            ).startswith("data-driven") else None
            kept[anchor] = {
                "element_id": anchor,
                "tag": tag,
                "data_category": (comp.get("proposed_data_category") or existing_dc) if tag.startswith("data-driven") else None,
                "authoring_component": ac,
                "confidence": float(comp.get("confidence", 0.85) or 0.85),
                "rationale": f"Vision-segmented component '{label}' ({ctype}); {len(members)} member shape(s).",
                "routing_hint": f"component:{cid}",
                "source": "slide-vision-component",
                "timestamp": stamp,
            }
        reps += 1

        for m in members:
            if m == anchor:
                continue
            kept[m] = {
                "element_id": m,
                "tag": "static",
                "data_category": None,
                "authoring_component": "none",
                "confidence": 0.9,
                "rationale": f"Part of vision-segmented component '{label}' ({cid}); handled with that component.",
                "routing_hint": f"part-of-component:{cid}",
                "source": "slide-vision-component-member",
                "timestamp": stamp,
            }
            members_folded += 1
    return reps, members_folded


# ----------------------------------------------------------------- classify mode


def run_classify(args: argparse.Namespace) -> int:
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    regions = load_vision_regions(Path(args.vision_regions))
    valid_ids = inventory_element_ids(inventory, regions)
    element_lookup = inventory_element_lookup(inventory)

    machine, skipped = _load_machine_files(Path(args.batches_dir), "classify_*.jsonl")

    kept: dict[str, dict[str, Any]] = {}
    dropped_unknown: list[str] = []
    dropped_noid = 0
    stamp = now_iso()
    for rec in machine:
        eid = rec.get("element_id")
        if not eid:
            dropped_noid += 1
            continue
        if eid not in valid_ids:
            dropped_unknown.append(str(eid))  # GROUNDING GUARD: fabricated id -> dropped
            continue
        rec.setdefault("source", "element-classifier")
        rec.setdefault("timestamp", stamp)
        kept[eid] = rec  # last machine record per id wins (defensive against dup batches)

    # Preserve human corrections from any prior classifications.jsonl (they must survive
    # re-runs). Written LAST so the downstream "latest per id" reducer lets them win.
    corrections = [r for r in read_jsonl(Path(args.classifications)) if r.get("source") == "human-correction"]

    # Component-aware collapse: fold vision-segmented component members so faux-chart
    # fragments leave the review queue and each component flows downstream as one shell.
    comp_reps = comp_members = 0
    comps_path = Path(args.components)
    if comps_path.exists():
        comp_reps, comp_members = apply_components(
            kept, read_jsonl(comps_path), valid_ids, stamp, element_lookup
        )

    machine_records = [kept[eid] for eid in sorted(kept)]
    write_jsonl(Path(args.classifications), machine_records + corrections)

    # review_queue: latest record per id, selecting ambiguous OR low-confidence.
    latest: dict[str, dict[str, Any]] = {}
    for rec in machine_records + corrections:  # corrections last -> win
        eid = rec.get("element_id")
        if eid:
            latest[eid] = rec
    thr = args.review_threshold
    review = [
        rec
        for eid in sorted(latest)
        for rec in [latest[eid]]
        if rec.get("tag") == "ambiguous" or float(rec.get("confidence", 0.0) or 0.0) < thr
    ]
    write_jsonl(Path(args.review_queue), review)

    print(
        f"classify: {len(machine_records)} validated record(s) "
        f"({len(corrections)} human correction(s) preserved), "
        f"{len(dropped_unknown)} dropped for unknown element_id, "
        f"{dropped_noid} dropped for missing id; review queue = {len(review)}."
    )
    if comp_reps:
        print(
            f"  folded {comp_reps} vision component(s) ({comp_members} member shape(s)) — "
            f"collapsed out of the review queue and carried as components."
        )
    if dropped_unknown:
        sample = ", ".join(dropped_unknown[:5])
        print(
            f"  GROUNDING GUARD dropped {len(dropped_unknown)} fabricated/unknown id(s): {sample}"
            f"{' ...' if len(dropped_unknown) > 5 else ''}",
            file=sys.stderr,
        )
    if skipped:
        print(f"  skipped {len(skipped)} unreadable batch file(s): {', '.join(skipped[:5])}", file=sys.stderr)
    return 0


# --------------------------------------------------------------- understand mode


def run_understand(args: argparse.Namespace) -> int:
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    regions = load_vision_regions(Path(args.vision_regions))
    valid_ids = inventory_element_ids(inventory, regions)

    machine, skipped = _load_machine_files(Path(args.batches_dir), "understand_*.json")

    kept: dict[str, dict[str, Any]] = {}
    dropped_unknown: list[str] = []
    dropped_noid = 0
    clamped = 0
    stamp = now_iso()
    for rec in machine:
        eid = rec.get("element_id")
        if not eid:
            dropped_noid += 1
            continue
        if eid not in valid_ids:
            dropped_unknown.append(str(eid))  # GROUNDING GUARD
            continue
        # member_element_ids must also be real, when present.
        members = rec.get("member_element_ids")
        if isinstance(members, list):
            rec["member_element_ids"] = [m for m in members if m in valid_ids]
        rec.setdefault("source", "table-chart-analyzer")
        rec.setdefault("timestamp", stamp)
        # Provenance defaults: records written before these fields existed were only
        # ever readable via the object model.
        rec.setdefault("structure_provenance", "object-model")
        rec.setdefault("verification", "deterministic")
        if rec["structure_provenance"] in ("vision", "mixed"):
            # Deterministic guards for vision-derived structure — never trust the
            # model to self-cap: verification is forced, confidence is clamped below
            # the review threshold, and a NON-blocking confirm question is queued.
            rec["verification"] = "needs-confirmation"
            conf = float(rec.get("confidence", 0.0) or 0.0)
            if conf > VISION_CONFIDENCE_CAP:
                rec["confidence"] = VISION_CONFIDENCE_CAP
                clamped += 1
            questions = rec.get("questions")
            if not isinstance(questions, list):
                questions = []
                rec["questions"] = questions
            if not any(q.get("question_id") == VISION_CONFIRM_QUESTION_ID for q in questions):
                images = rec.get("vision_source_images") or []
                see = f" (see {images[0]})" if images else ""
                questions.append({
                    "question_id": VISION_CONFIRM_QUESTION_ID,
                    "facet": "structure-confirm",
                    "question": (
                        "This structure was read from the rendered page image, not a "
                        f"machine-readable object. Confirm the column/series structure is correct{see}."
                    ),
                    "options": ["Structure is correct as read", "Structure needs correction"],
                    "default": "Structure is correct as read",
                    "why": (
                        "Vision-derived structure must be human-confirmed before the Shell / "
                        "Data Object is finalized; numbers in the image are illustrative only."
                    ),
                    "blocking": False,
                })
        kept[eid] = rec

    understanding = [kept[eid] for eid in sorted(kept)]
    write_jsonl(Path(args.understanding), understanding)

    # question_queue: one line per question, minus answered ones.
    answered: set[tuple[str, str]] = set()
    if args.answers:
        for ans in read_jsonl(Path(args.answers)):
            eid, qid = ans.get("element_id"), ans.get("question_id")
            if eid and qid:
                answered.add((eid, qid))

    questions: list[dict[str, Any]] = []
    blocking = 0
    for rec in understanding:
        eid = rec.get("element_id")
        for q in rec.get("questions", []) or []:
            qid = q.get("question_id")
            if not qid or (eid, qid) in answered:
                continue
            # Carry the owning record's triage metadata so presenters can group /
            # rank questions without joining back to element_understanding.jsonl.
            entry = {"element_id": eid, **q,
                     "data_category": rec.get("data_category"),
                     "authoring_component": rec.get("authoring_component"),
                     "source": "table-chart-analyzer", "timestamp": rec.get("timestamp", stamp)}
            questions.append(entry)
            if q.get("blocking"):
                blocking += 1
    write_jsonl(Path(args.question_queue), questions)

    vision_count = sum(
        1 for r in understanding if r.get("structure_provenance") in ("vision", "mixed")
    )
    print(
        f"understand: {len(understanding)} validated understanding record(s), "
        f"{len(dropped_unknown)} dropped for unknown element_id, "
        f"{dropped_noid} dropped for missing id; "
        f"open questions = {len(questions)} ({blocking} blocking)."
    )
    if vision_count:
        print(
            f"  {vision_count} vision/mixed-provenance structure(s) — needs-confirmation "
            f"({clamped} confidence-clamped to {VISION_CONFIDENCE_CAP}); non-blocking "
            f"'{VISION_CONFIRM_QUESTION_ID}' questions queued."
        )
    if dropped_unknown:
        sample = ", ".join(dropped_unknown[:5])
        print(
            f"  GROUNDING GUARD dropped {len(dropped_unknown)} fabricated/unknown id(s): {sample}"
            f"{' ...' if len(dropped_unknown) > 5 else ''}",
            file=sys.stderr,
        )
    if skipped:
        print(f"  skipped {len(skipped)} unreadable batch file(s): {', '.join(skipped[:5])}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------- bind mode


CONFIRM_SCORE_DEFAULT = 0.7
CANDIDATE_CAP = 5


def run_bind(args: argparse.Namespace) -> int:
    from slice_sources import binding_columns_for  # deck-side column counts (same definition the slicer uses)

    understanding = read_jsonl(Path(args.understanding))
    if not understanding:
        print(f"No element understanding at {args.understanding} -- run /analyze-deck first.", file=sys.stderr)
        return 2
    valid_eids: set[str] = set()
    total_cols: dict[str, int] = {}
    u_category: dict[str, Any] = {}
    u_provenance: dict[str, str] = {}
    for rec in understanding:
        eid = rec.get("element_id")
        if not eid:
            continue
        valid_eids.add(eid)
        total_cols[eid] = len(binding_columns_for(rec))
        u_category[eid] = rec.get("data_category")
        u_provenance[eid] = rec.get("structure_provenance") or "object-model"
        for m in rec.get("member_element_ids") or []:
            valid_eids.add(m)

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print(f"Source catalog not found: {catalog_path} -- run ingest_sources.py first.", file=sys.stderr)
        return 2
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    # column_id -> canonical facts. The catalog is the TRUTH for names/families.
    col_index: dict[str, dict[str, Any]] = {}
    for src in catalog.get("sources", []):
        for tbl in src.get("tables", []):
            for col in tbl.get("columns", []):
                cid = col.get("column_id")
                if cid:
                    col_index[cid] = {
                        "table": tbl.get("name"),
                        "column": col.get("name"),
                        "source_id": src.get("source_id"),
                        "source_file": src.get("filename"),
                        "source_family": src.get("source_family_hint"),
                    }

    machine, skipped = _load_machine_files(Path(args.batches_dir), "bind_*.jsonl")

    dropped_eids: list[str] = []
    dropped_cids: list[str] = []
    stamp = now_iso()
    # (element_id, deck_column) -> {"candidates": [...], "resolutions": [...], "notes": [...]}
    merged: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in machine:
        eid = rec.get("element_id")
        if not eid or eid not in valid_eids:
            dropped_eids.append(str(eid))  # GROUNDING GUARD: fabricated element
            continue
        per_el = merged.setdefault(eid, {})
        for b in rec.get("bindings") or []:
            dc = b.get("deck_column")
            if not dc:
                continue
            slot = per_el.setdefault(dc, {"candidates": {}, "resolutions": [], "notes": []})
            for cand in b.get("candidates") or []:
                cid = cand.get("column_id")
                canon = col_index.get(cid)
                if canon is None:
                    dropped_cids.append(str(cid))  # GROUNDING GUARD: fabricated column
                    continue
                score = float(cand.get("score", 0.0) or 0.0)
                prev = slot["candidates"].get(cid)
                if prev is None or score > prev["score"]:
                    slot["candidates"][cid] = {
                        "column_id": cid,
                        **canon,  # canonical table/column/source_file/source_id/source_family
                        "match_signal": cand.get("match_signal"),
                        "score": round(score, 3),
                    }
            if b.get("resolution"):
                slot["resolutions"].append(b["resolution"])
            if b.get("note"):
                slot["notes"].append(b["note"])

    # Reduce to canonical per-element records.
    out_records: list[dict[str, Any]] = []
    thr = float(args.confirm_score)
    for eid in sorted(merged):
        bindings = []
        bound = clarify = 0
        family_votes: dict[str, int] = {}
        for dc in sorted(merged[eid]):
            slot = merged[eid][dc]
            cands = sorted(slot["candidates"].values(), key=lambda c: -c["score"])[:CANDIDATE_CAP]
            if "derived" in slot["resolutions"]:
                resolution = "derived"
            elif cands and cands[0]["score"] >= thr:
                resolution = "confirm"
            elif cands:
                resolution = "clarify"
            else:
                resolution = "no-match"
            if resolution == "confirm":
                bound += 1
                fam = cands[0].get("source_family")
                if fam:
                    family_votes[fam] = family_votes.get(fam, 0) + 1
            elif resolution == "clarify":
                clarify += 1
            entry: dict[str, Any] = {"deck_column": dc, "resolution": resolution, "candidates": cands}
            if slot["notes"]:
                entry["note"] = " | ".join(dict.fromkeys(slot["notes"]))
            bindings.append(entry)
        family = max(family_votes, key=lambda k: family_votes[k]) if family_votes else None
        out_records.append({
            "element_id": eid,
            "data_category": u_category.get(eid),
            "structure_provenance": u_provenance.get(eid, "object-model"),
            "source_family_proposal": family,
            "total_columns": total_cols.get(eid),
            "bound_columns": bound,
            "clarify_columns": clarify,
            "bindings": bindings,
            "source": "source-binding-analyzer",
            "timestamp": stamp,
        })

    write_jsonl(Path(args.bindings), out_records)

    total_bound = sum(r["bound_columns"] for r in out_records)
    total_clarify = sum(r["clarify_columns"] for r in out_records)
    print(
        f"bind: {len(out_records)} element(s) with merged bindings "
        f"({total_bound} column(s) confirmed >= {thr}, {total_clarify} to clarify), "
        f"{len(dropped_eids)} record(s) dropped for unknown element_id, "
        f"{len(dropped_cids)} candidate(s) dropped for unknown column_id -> {args.bindings}"
    )
    if dropped_eids or dropped_cids:
        sample = ", ".join((dropped_eids + dropped_cids)[:5])
        print(
            f"  GROUNDING GUARD dropped {len(dropped_eids)} element(s) / {len(dropped_cids)} column candidate(s) "
            f"with fabricated/unknown ids: {sample}",
            file=sys.stderr,
        )
    if skipped:
        print(f"  skipped {len(skipped)} unreadable batch file(s): {', '.join(skipped[:5])}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ segment mode


def _deck_page_dims(inventory: dict[str, Any]) -> dict[str, tuple[float, float]]:
    dims: dict[str, tuple[float, float]] = {}
    for deck in inventory.get("decks", []):
        did = deck.get("deck_id")
        meta = deck.get("metadata") or {}
        if did:
            dims[did] = (
                float(meta.get("slide_width_emu", 0) or 0),
                float(meta.get("slide_height_emu", 0) or 0),
            )
    return dims


def _validate_component_bbox(
    comp: dict[str, Any],
    element_lookup: dict[str, dict[str, Any]],
    page_dims: dict[str, tuple[float, float]],
) -> None:
    """Null + flag a bbox that misses the union of its members' positions.

    Coordinate spaces: the bbox is normalized 0..1 over the rendered page; element
    positions are EMU over the same page box — both normalize to the same space (see
    _inventory_common's coordinate-transform note). A component with no positioned
    members (or an unknown page size) cannot be judged and keeps its bbox.
    """
    bbox = _valid_bbox(comp.get("bbox"))
    if bbox is None:
        comp["bbox_suspect"] = comp.get("bbox") is not None  # malformed -> suspect
        comp["bbox"] = None
        return
    w_emu, h_emu = page_dims.get(comp.get("deck_id"), (0.0, 0.0))
    rects = [
        r
        for m in comp.get("member_element_ids") or []
        for r in [_normalized_rect(element_lookup.get(m) or {}, w_emu, h_emu)]
        if r is not None
    ]
    if not rects:
        comp["bbox_suspect"] = False
        return
    union = (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )
    if _containment(bbox, union) < BBOX_OVERLAP_MIN:
        comp["bbox_suspect"] = True
        comp["bbox"] = None
    else:
        comp["bbox_suspect"] = False


def _materialize_vision_regions(
    kept: list[dict[str, Any]],
    element_lookup: dict[str, dict[str, Any]],
    page_dims: dict[str, tuple[float, float]],
    stamp: str,
) -> list[dict[str, Any]]:
    """Mint one synthetic vision-region element per validated DATA component whose
    members carry NO parsed object-model structure. Ids are deterministic
    (<deck_id>:<slide_index>:v<n>, n by sorted component_id per page) and CODE-minted —
    the guard never admits an LLM-authored id. Components anchored on a parsed
    table/chart skip materialization: object-model outranks vision. Each materialized
    component gains vision_region_element_id so the classify merge promotes onto it."""
    regions: list[dict[str, Any]] = []
    by_page: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for comp in kept:
        if comp.get("component_type") not in DATA_COMPONENT_TYPES:
            continue
        if any(has_parsed_structure(element_lookup.get(m)) for m in comp.get("member_element_ids") or []):
            continue
        key = (comp.get("deck_id") or "", int(comp.get("slide_index", 0) or 0))
        by_page.setdefault(key, []).append(comp)

    for (deck_id, slide_index) in sorted(by_page):
        comps = sorted(by_page[(deck_id, slide_index)], key=lambda c: str(c.get("component_id")))
        w_emu, h_emu = page_dims.get(deck_id, (0.0, 0.0))
        for n, comp in enumerate(comps):
            bbox = _valid_bbox(comp.get("bbox"))
            if bbox is not None and w_emu > 0 and h_emu > 0:
                position = {
                    "left_emu": int(bbox[0] * w_emu),
                    "top_emu": int(bbox[1] * h_emu),
                    "width_emu": int((bbox[2] - bbox[0]) * w_emu),
                    "height_emu": int((bbox[3] - bbox[1]) * h_emu),
                }
            else:
                position = {"left_emu": 0, "top_emu": 0, "width_emu": int(w_emu), "height_emu": int(h_emu)}
            rid = f"{deck_id}:{slide_index}:v{n}"
            comp["vision_region_element_id"] = rid
            regions.append({
                "element_id": rid,
                "kind": "vision-region",
                "deck_id": deck_id,
                "slide_index": slide_index,
                "position": position,
                "content": {
                    "label": comp.get("label") or "",
                    "component_type": comp.get("component_type"),
                    "needs_vision": True,
                },
                "component_id": comp.get("component_id"),
                "member_element_ids": comp.get("member_element_ids") or [],
                "source": "vision-region-materializer",
                "timestamp": stamp,
            })
    return regions


def _print_page_inventory(kept: list[dict[str, Any]], max_pages: int = 60) -> None:
    """The deterministic per-page component inventory — the eyeball-QA hook: every
    table/chart/label a human sees on a page should appear here."""
    by_page: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for comp in kept:
        key = (comp.get("deck_id") or "?", int(comp.get("slide_index", 0) or 0))
        by_page.setdefault(key, []).append(comp)
    if not by_page:
        return
    print("per-page component inventory:")
    for i, key in enumerate(sorted(by_page)):
        if i >= max_pages:
            print(f"  ... +{len(by_page) - max_pages} more page(s)")
            break
        deck_id, slide_index = key
        parts = []
        for comp in sorted(by_page[key], key=lambda c: str(c.get("component_id"))):
            label = comp.get("label") or "(unlabeled)"
            conf = comp.get("confidence")
            flag = " BBOX-SUSPECT" if comp.get("bbox_suspect") else ""
            conf_s = f" ({float(conf):.2f})" if isinstance(conf, (int, float)) else ""
            parts.append(f"{comp.get('component_type')} '{label}'{conf_s}{flag}")
        print(f"  {deck_id} p{slide_index}: " + "; ".join(parts))


def run_segment(args: argparse.Namespace) -> int:
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    valid_ids = inventory_element_ids(inventory)
    element_lookup = inventory_element_lookup(inventory)
    page_dims = _deck_page_dims(inventory)

    machine, skipped = _load_machine_files(Path(args.batches_dir), "segment_*.jsonl")

    kept: list[dict[str, Any]] = []
    dropped_unknown: list[str] = []
    dropped_members = 0
    stamp = now_iso()
    for comp in machine:
        rep = comp.get("representative_element_id")
        if not rep or rep not in valid_ids:
            dropped_unknown.append(str(rep))  # GROUNDING GUARD: fabricated component
            continue
        members = comp.get("member_element_ids") or []
        valid_members = [m for m in members if m in valid_ids]
        dropped_members += len(members) - len(valid_members)
        if rep not in valid_members:
            valid_members.append(rep)
        comp["member_element_ids"] = valid_members
        comp.setdefault("source", "slide-vision-segmenter")
        comp.setdefault("timestamp", stamp)
        _validate_component_bbox(comp, element_lookup, page_dims)
        kept.append(comp)

    regions = _materialize_vision_regions(kept, element_lookup, page_dims, stamp)
    write_jsonl(Path(args.components), kept)
    Path(args.vision_regions).parent.mkdir(parents=True, exist_ok=True)
    Path(args.vision_regions).write_text(
        json.dumps(
            {"generated_at": stamp, "region_count": len(regions), "regions": regions},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    suspect = sum(1 for c in kept if c.get("bbox_suspect"))
    print(
        f"segment: {len(kept)} component(s) validated, "
        f"{len(dropped_unknown)} dropped for unknown representative id, "
        f"{dropped_members} member id(s) filtered -> {args.components}"
    )
    print(
        f"  {len(regions)} synthetic vision region(s) materialized -> {args.vision_regions}"
        f"{f'; {suspect} bbox(es) flagged suspect and nulled' if suspect else ''}"
    )
    _print_page_inventory(kept)
    if dropped_unknown:
        sample = ", ".join(dropped_unknown[:5])
        print(
            f"  GROUNDING GUARD dropped {len(dropped_unknown)} component(s) with fabricated/unknown "
            f"representative id: {sample}{' ...' if len(dropped_unknown) > 5 else ''}",
            file=sys.stderr,
        )
    if skipped:
        print(f"  skipped {len(skipped)} unreadable batch file(s): {', '.join(skipped[:5])}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge per-slice analysis outputs into the canonical workspace files, "
        "hard-validating every element_id against the inventory (grounding guard)."
    )
    parser.add_argument("--mode", required=True, choices=["classify", "understand", "segment", "bind"])
    parser.add_argument("--inventory", default="workspace/corpus_inventory.json")
    parser.add_argument("--batches-dir", default="workspace/batches")
    # classify
    parser.add_argument("--classifications", default="workspace/classifications.jsonl")
    parser.add_argument("--review-queue", default="workspace/review_queue.jsonl")
    parser.add_argument("--review-threshold", type=float, default=REVIEW_THRESHOLD_DEFAULT)
    # segment (writes these) / classify + understand (read them if present)
    parser.add_argument("--components", default="workspace/slide_components.jsonl")
    parser.add_argument("--vision-regions", default="workspace/vision_regions.json")
    # understand (bind also reads --understanding for valid ids + column counts)
    parser.add_argument("--understanding", default="workspace/element_understanding.jsonl")
    parser.add_argument("--question-queue", default="workspace/question_queue.jsonl")
    parser.add_argument("--answers", default="workspace/question_answers.jsonl")
    # bind
    parser.add_argument("--catalog", default="workspace/source_catalog.json")
    parser.add_argument("--bindings", default="workspace/source_bindings.jsonl")
    parser.add_argument("--confirm-score", type=float, default=CONFIRM_SCORE_DEFAULT)
    args = parser.parse_args(argv)

    if args.mode == "bind":
        return run_bind(args)  # grounds against the understanding + catalog, not the inventory

    inv = Path(args.inventory)
    if not inv.exists():
        print(f"Inventory not found: {inv}", file=sys.stderr)
        return 2

    if args.mode == "classify":
        return run_classify(args)
    if args.mode == "segment":
        return run_segment(args)
    return run_understand(args)


if __name__ == "__main__":
    raise SystemExit(main())
