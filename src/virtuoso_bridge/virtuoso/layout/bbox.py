"""Read-only extent diagnosis and explicitly opted-in OA BBox recomputation."""

from __future__ import annotations

import math
from typing import Sequence

from virtuoso_bridge.virtuoso.layout.ops import _layout_read_cellview
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import is_single_complete_skill_list, parse_sexpr


def layout_diagnose_bbox(lib: str, cell: str, *, view: str = "layout") -> str:
    """Snapshot extents, prBoundary and top-level geometry/logical contributors.

No recompute, save, deletion, visibility change or connectivity extraction.
Unavailable collections are reported, not silently treated as empty.
"""
    body = (
        'unless(cv~>cellViewType == "maskLayout" error("not a layout cellview")) '
        'rows = nil unavailable = nil '
        'prResult = if(member(\'prBoundary cv~>?) then errset(cv~>prBoundary~>bBox nil) else nil) '
        'unless(prResult unavailable = cons("prBoundary" unavailable)) '
    )
    for collection in ("shapes", "vias", "instances", "mosaics", "steiners", "markers",
                       "guides", "routes", "figGroups"):
        body += (
            f'probe = if(member(\'{collection} cv~>?) then errset(cv~>{collection} nil) else nil) '
            f'if(probe then foreach(obj car(probe) '
            'rows = cons(list('
            f'"{collection}" obj~>objType car(errset(obj~>name nil)) '
            'car(errset(obj~>bBox nil)) car(errset(obj~>lpp nil))) rows)) '
            f'else unavailable = cons("{collection}" unavailable)) '
        )
    body += 'sprintf(nil "%L" list("VB_LAYOUT_BBOX_V1" cv~>bBox car(prResult) reverse(unavailable) reverse(rows)))'
    return _layout_read_cellview(lib, cell, view, "maskLayout", "rows unavailable prResult probe", body)


def layout_recompute_bbox(
    lib: str, cell: str, *, view: str = "layout", dry_run: bool = True, save: bool = False,
) -> str:
    """Build an explicit recompute; defaults to read-only diagnosis.

A writable existing maskLayout is required for execution. Unsaved changes
and read-only borrowed views are refused, never saved/promoted automatically.
Owned references close even on failure. No retry/cleanup/oaScan is performed.
"""
    if not isinstance(dry_run, bool) or not isinstance(save, bool):
        raise ValueError("dry_run and save must be booleans")
    if dry_run:
        if save:
            raise ValueError("save=True contradicts dry_run=True")
        return layout_diagnose_bbox(lib, cell, view=view)
    names = " ".join('"' + escape_skill_string(n) + '"' for n in (lib, cell, view))
    lib_name, cell_name, view_name = ('"' + escape_skill_string(n) + '"' for n in (lib, cell, view))
    # An empty view type prevents append mode from creating a missing view.
    lookup = (
        "if(isCallable('dbFindOpenCellViewByName) then "
        f'cv = dbFindOpenCellViewByName({names}) else '
        f'libId = ddGetObj({lib_name}) '
        f'when(libId cv = dbFindOpenCellView(libId {cell_name} {view_name}))) '
    )
    verify = (
        'unless(dbSave(cv) error("BBox save failed; inspect outcome before retry")) '
        'unless(equal(cv~>bBox after) error("BBox changed during save")) '
        'verification = "session" '
        'when(ownsCv '
        'unless(dbClose(cv) error("BBox close failed")) cv = nil ownsCv = nil '
        f'cv = dbOpenCellViewByType({names} "" "r") '
        'unless(cv error("BBox saved but reopen verification failed")) ownsCv = cv '
        'unless(equal(cv~>bBox after) error("BBox saved but reopened extent differs")) '
        'verification = "reopened") '
    ) if save else 'verification = "memory_only" '
    return (
        'prog((cv ownsCv libId before after verification) '
        + lookup
        + f'unless(cv cv = dbOpenCellViewByType({names} "" "a") ownsCv = cv) '
        + 'unless(cv error("cannot open existing layout for BBox recompute")) '
        + 'return(unwindProtect(progn('
        + 'unless(cv~>cellViewType == "maskLayout" error("not a layout cellview")) '
        + 'unless(member(cv~>mode list("a" "w")) error("layout is not editable; no mode change performed")) '
        + 'when(dbIsCellViewModified(cv) error("layout has unsaved edits; save or discard explicitly first")) '
        + 'before = cv~>bBox '
        + 'unless(dbComputeBBox(cv) error("BBox recompute failed")) after = cv~>bBox '
        + verify
        + 'sprintf(nil "%L" list("VB_BBOX_RECOMPUTE_V1" before after '
        + ('t' if save else 'nil') + ' verification))) '
        + 'when(ownsCv && cv unless(dbClose(cv) error("BBox reference close failed"))))))'
    )


def _payload(raw: str, schema: str, count: int) -> list:
    text = raw.strip()
    if text.startswith('"'):
        text = parse_sexpr(text)
    if not isinstance(text, str) or not is_single_complete_skill_list(text):
        raise ValueError("invalid BBox output")
    value = parse_sexpr(text)
    if len(value) != count or value[0] != schema:
        raise ValueError("unsupported BBox output schema")
    return value


def _bbox(value):
    if value is None:
        return None
    try:
        if len(value) != 2 or any(len(p) != 2 for p in value):
            raise ValueError
        box = [[float(n) for n in p] for p in value]
        if any(not math.isfinite(n) for p in box for n in p):
            raise ValueError
        if box[0][0] > box[1][0] or box[0][1] > box[1][1]:
            raise ValueError
        return box
    except (ValueError, TypeError):
        raise ValueError("invalid BBox coordinates") from None


def _union(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return [[min(b[0][0] for b in boxes), min(b[0][1] for b in boxes)],
            [max(b[1][0] for b in boxes), max(b[1][1] for b in boxes)]]


def _inspect_bbox(value, location, invalid):
    try:
        return _bbox(value)
    except ValueError as exc:
        invalid.append(dict(location=location, raw_bbox=value, reason=str(exc)))
        return None


def parse_layout_bbox_output(
    raw: str, *, expected_bbox: Sequence[Sequence[float]] | None = None,
    tolerance: float = 1e-6,
) -> dict:
    """Analyze a diagnostic snapshot; candidates are never deletion advice.

An optional user-defined expected_bbox flags objects outside the intended
footprint. Without it, report exact edge contributors, not a guessed 'real'
boundary. prBoundary is reported separately, not assumed to enclose all shapes.
"""
    if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    _, cached, pr_boundary, unavailable, rows = _payload(raw, "VB_LAYOUT_BBOX_V1", 5)
    invalid = []
    cached = _inspect_bbox(cached, "cellview", invalid)
    pr_boundary = _inspect_bbox(pr_boundary, "prBoundary", invalid)
    expected = _bbox(expected_bbox)
    if unavailable is not None and (not isinstance(unavailable, list)
                                    or not all(isinstance(c, str) for c in unavailable)):
        raise ValueError("invalid unavailable collections")
    objects = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("invalid BBox object row")
        collection, obj_type, name, bbox, lpp = row
        objects.append(dict(collection=collection, obj_type=obj_type, name=name,
                            bbox=_inspect_bbox(bbox, f"objects[{len(objects)}]", invalid), lpp=lpp))
    union = _union(obj["bbox"] for obj in objects)
    geometry = _union(obj["bbox"] for obj in objects
                      if obj["collection"] in ("shapes", "vias", "instances", "mosaics"))
    for obj in objects:
        bbox = obj["bbox"]
        obj["cached_edges"] = []
        if bbox and cached:
            for side, p, axis in (("left", 0, 0), ("bottom", 0, 1), ("right", 1, 0), ("top", 1, 1)):
                if abs(bbox[p][axis] - cached[p][axis]) <= tolerance:
                    obj["cached_edges"].append(side)
        obj["outside_expected"] = bool(bbox and expected and (
            bbox[0][0] < expected[0][0] - tolerance or bbox[0][1] < expected[0][1] - tolerance
            or bbox[1][0] > expected[1][0] + tolerance or bbox[1][1] > expected[1][1] + tolerance))
    matches = None if cached is None or union is None else all(
        abs(cached[p][a] - union[p][a]) <= tolerance for p in (0, 1) for a in (0, 1))
    return dict(bbox=cached, pr_boundary_bbox=pr_boundary, geometry_bbox=geometry,
                observed_bbox=union, bbox_matches_observed=matches,
                expected_bbox=expected, objects=objects, unavailable_collections=unavailable or [],
                invalid_bboxes=invalid,
                warnings=["Top-level snapshots only; child masters may have stale extents. "
                          "A mismatch is a diagnostic clue, not proof of corruption. "
                          "Overlapping collections can report the same object more than once."])


def parse_layout_bbox_recompute_output(raw: str) -> dict:
    """Decode execution evidence; a successful call need not shrink the box."""
    _, before, after, saved, verification = _payload(raw, "VB_BBOX_RECOMPUTE_V1", 5)
    if saved not in (None, True) or verification not in ("memory_only", "session", "reopened"):
        raise ValueError("invalid BBox recompute evidence")
    if bool(saved) != (verification != "memory_only"):
        raise ValueError("inconsistent BBox save evidence")
    before, after = _bbox(before), _bbox(after)
    return dict(before=before, after=after, changed=before != after,
                saved=bool(saved), verification=verification)
