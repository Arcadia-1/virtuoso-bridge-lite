"""Read schematic data — unified reader with optional geometry.

Usage:
    from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

    # Full read (topology + positions + notes)
    data = read_schematic(client, "myLib", "myCell")

    # Topology only (no xy/orient/bBox — notes still included)
    data = read_schematic(client, "myLib", "myCell", include_positions=False)

    # Custom param filters
    data = read_schematic(client, "myLib", "myCell", param_filters="my.yaml")

    # No param filtering (return all CDF params)
    data = read_schematic(client, "myLib", "myCell", param_filters=None)

Legacy API (read_placement, read_connectivity, read_instance_params) is
preserved below for backward compatibility.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.ops import escape_skill_string

_DEFAULT_FILTERS_PATH = Path(__file__).parent / "cdf_param_filters.yaml"
_DEFAULT_TIMEOUT_S = 300


# =======================================================================
# Param filter config
# =======================================================================

def _load_filters(path: str | Path) -> dict:
    """Load a cdf_param_filters.yaml and return parsed dict."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _match_filter(config: dict, lib: str, cell: str) -> list[str] | None:
    """Return param whitelist for a given lib/cell, or None for 'all'."""
    for rule in config.get("filters", []):
        m = rule["match"]
        if fnmatch.fnmatch(lib, m.get("lib", "*")) and fnmatch.fnmatch(cell, m.get("cell", "*")):
            return rule["params"]
    fallback = config.get("fallback", "all")
    return None if fallback == "all" else fallback


# =======================================================================
# SKILL expressions
# =======================================================================

_SKILL_TOPOLOGY = r'''
let((cv result)
  {owned_read_start}
  cv = {cv_expr}
  if(cv
    then progn(
  result = "INSTANCES\n"
  foreach(inst cv~>instances
    when(inst~>purpose != "pin"
      result = strcat(result sprintf(nil "INST|%s|%s|%s" inst~>name inst~>libName inst~>cellName))
      {geometry_inst}
      result = strcat(result "\n")
      ; nlAction (set by shift+delete "ignore" in the schematic editor)
      let((nla)
        nla = nil
        foreach(p inst~>prop
          when(p~>name == "nlAction" nla = p~>value))
        when(nla
          result = strcat(result sprintf(nil "NLACTION|%s\n" nla))))
      ; terminals
      foreach(it inst~>instTerms
        when(it~>net
          result = strcat(result sprintf(nil "TERM|%s|%s\n" it~>name it~>net~>name))))
      ; CDF params - only attempt if the cell exists in the database
      ; (ddGetObj is a silent lookup, avoids CIW warnings for missing masters)
      when(ddGetObj(inst~>libName inst~>cellName)
        let((cdf)
          cdf = cdfGetInstCDF(inst)
          when(cdf
            foreach(p cdf~>parameters
              when(p~>value != nil && p~>value != ""
                && strlen(sprintf(nil "%L" p~>value)) <= 120
                result = strcat(result sprintf(nil "PARAM|%s|%L\n" p~>name p~>value)))))))))
  result = strcat(result "NETS\n")
  foreach(net cv~>nets
      result = strcat(result sprintf(nil "NET|%s|%d|%s|%s"
      net~>name if(net~>numBits net~>numBits 1)
      if(net~>sigType net~>sigType "signal")
      if(net~>isGlobal "t" "nil")))
    foreach(it net~>instTerms
      result = strcat(result sprintf(nil "|%s.%s" it~>inst~>name it~>name)))
    result = strcat(result "\n"))
  result = strcat(result "PINS\n")
  foreach(term cv~>terminals
    result = strcat(result sprintf(nil "PIN|%s|%s|%d\n"
      term~>name
      if(term~>direction term~>direction "inputOutput")
      if(term~>numBits term~>numBits 1))))
  {notes_section}
  result = strcat(result "END\n")
  result)
    else "ERROR")
  {owned_read_cleanup})
'''

_OWNED_READ_START = r'''unwindProtect(
    progn('''

_OWNED_READ_CLEANUP = r''')
    progn(
      when(cv
        unless(dbClose(cv)
          error("schematic reader close failed"))
        cv = nil)))'''


def _build_cellview_read_skill(
    template: str,
    lib: str | None,
    cell: str | None,
) -> str:
    """Bind a read template to either an owned named CV or the current CV."""
    if lib is None and cell is None:
        cv_expr = "geGetEditCellView()"
        owned_read_start = ""
        owned_read_cleanup = ""
    elif not lib or not cell:
        raise ValueError("lib and cell must both be non-empty when provided")
    else:
        cv_expr = (
            f'dbOpenCellViewByType("{escape_skill_string(lib)}" '
            f'"{escape_skill_string(cell)}" "schematic" "schematic" "r")'
        )
        owned_read_start = _OWNED_READ_START
        owned_read_cleanup = _OWNED_READ_CLEANUP

    return (
        template.replace("{owned_read_start}", owned_read_start)
        .replace("{owned_read_cleanup}", owned_read_cleanup)
        .replace("{cv_expr}", cv_expr)
    )


_GEOMETRY_INST_EXPR = r'''
      result = strcat(result sprintf(nil "|%L|%s|%L|%d|%s"
        inst~>xy
        if(inst~>orient inst~>orient "R0")
        inst~>bBox
        if(inst~>numInst inst~>numInst 1)
        if(inst~>viewName inst~>viewName "symbol")))
'''

_NOTES_SECTION_EXPR = r'''
  result = strcat(result "NOTES\n")
  foreach(shape cv~>shapes
    when(shape~>objType == "label" && shape~>purpose == "drawing"
      && shape~>layerName == "text" && shape~>theLabel
      result = strcat(result sprintf(nil "NOTE|%s|%L|%s|%g|%s|%s\n"
        shape~>theLabel shape~>xy
        if(shape~>font shape~>font "stick")
        if(shape~>height shape~>height 0.1)
        if(shape~>orient shape~>orient "R0")
        if(shape~>justify shape~>justify "lowerCenter")))))
'''


# =======================================================================
# Main API
# =======================================================================

def read_schematic(
    client: VirtuosoClient,
    lib: str | None = None,
    cell: str | None = None,
    *,
    include_positions: bool = False,
    param_filters: str | Path | None = _DEFAULT_FILTERS_PATH,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Read a schematic in one SKILL call.

    Args:
        lib, cell: library and cell name. A named read closes the cellview
            reference it opens. If omitted, uses the caller-owned current
            cellview (geGetEditCellView) and leaves it open.
        include_positions: if True, include xy/orient/bBox/numInst/view on each
            instance. False = pure topology + params only.
            Notes are always returned regardless of this flag.
        param_filters: path to a YAML filter config.  Default uses the
            built-in cdf_param_filters.yaml.  Pass None to return all
            CDF params unfiltered.
        timeout: Virtuoso SKILL execution timeout in seconds. Large schematics
            can take longer than the transport default, so the reader defaults
            to 300 seconds and lets callers override it.

    Returns:
        dict with keys: instances, nets, pins, notes.

        Each instance dict carries ``nlAction`` only when set — typically
        ``"ignore"`` when the designer shift+deleted the instance in the
        schematic editor to exclude it from netlisting.  Absent key = normal.
    """
    skill = _SKILL_TOPOLOGY.replace(
        "{geometry_inst}", _GEOMETRY_INST_EXPR if include_positions else ""
    )
    # Notes are always included
    skill = skill.replace("{notes_section}", _NOTES_SECTION_EXPR)
    skill = _build_cellview_read_skill(skill, lib, cell)

    r = client.execute_skill(skill, timeout=timeout)
    if getattr(r, "errors", None):
        raise RuntimeError(f"read_schematic SKILL error: {r.errors[0]}")
    raw = decode_skill_output(r.output)
    if raw.strip() == "ERROR":
        raise RuntimeError(f"read_schematic could not open schematic {lib or '(current)'}/{cell or '(current)'}")
    if not raw.strip():
        raise RuntimeError(f"read_schematic returned empty output for {lib or '(current)'}/{cell or '(current)'}")

    # Load filter config
    filter_config = None
    if param_filters is not None:
        filter_config = _load_filters(param_filters)

    return _parse_schematic(raw, include_positions=include_positions, filter_config=filter_config)


def _parse_schematic(
    raw: str,
    *,
    include_positions: bool,
    filter_config: dict | None,
) -> dict:
    """Parse the raw SKILL output into structured dict."""
    result: dict[str, Any] = {
        "instances": [],
        "nets": {},
        "pins": {},
        "notes": [],
    }

    section = None
    current_inst: dict | None = None
    allowed_params: list[str] | None = None  # per-instance param whitelist

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        if line in ("INSTANCES", "NETS", "PINS", "NOTES", "END"):
            # Flush current instance
            if current_inst is not None:
                result["instances"].append(current_inst)
                current_inst = None
            section = line.lower()
            continue

        if section == "instances":
            if line.startswith("INST|"):
                # Flush previous instance
                if current_inst is not None:
                    result["instances"].append(current_inst)

                parts = line.split("|")
                name, inst_lib, inst_cell = parts[1], parts[2], parts[3]

                current_inst = {
                    "name": name,
                    "lib": inst_lib,
                    "cell": inst_cell,
                }

                if include_positions and len(parts) >= 8:
                    current_inst["xy"] = _parse_point(parts[4])
                    current_inst["orient"] = parts[5]
                    current_inst["bBox"] = _parse_bbox(parts[6])
                    current_inst["numInst"] = int(parts[7]) if parts[7].isdigit() else 1
                    current_inst["view"] = parts[8] if len(parts) > 8 else "symbol"

                current_inst["params"] = {}
                current_inst["terms"] = {}

                # Determine param filter for this instance
                if filter_config is not None:
                    allowed_params = _match_filter(filter_config, inst_lib, inst_cell)
                else:
                    allowed_params = None

            elif line.startswith("NLACTION|") and current_inst is not None:
                parts = line.split("|", 1)
                if len(parts) > 1:
                    current_inst["nlAction"] = parts[1]

            elif line.startswith("TERM|") and current_inst is not None:
                parts = line.split("|")
                current_inst["terms"][parts[1]] = parts[2]

            elif line.startswith("PARAM|") and current_inst is not None:
                parts = line.split("|", 2)
                pname = parts[1]
                pval = parts[2].strip('"') if len(parts) > 2 else ""
                if allowed_params is None or pname in allowed_params:
                    current_inst["params"][pname] = pval

        elif section == "nets":
            if line.startswith("NET|"):
                parts = line.split("|")
                net_name = parts[1]
                num_bits = int(parts[2]) if parts[2].isdigit() else 1
                sig_type = parts[3] if len(parts) > 3 else "signal"
                is_global = parts[4] == "t" if len(parts) > 4 else False
                connections = parts[5:]
                result["nets"][net_name] = {
                    "connections": connections,
                    "numBits": num_bits,
                    "sigType": sig_type,
                    "isGlobal": is_global,
                }

        elif section == "pins":
            if line.startswith("PIN|"):
                parts = line.split("|")
                pin_name = parts[1]
                direction = parts[2]
                num_bits = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
                result["pins"][pin_name] = {
                    "direction": direction,
                    "numBits": num_bits,
                }

        elif section == "notes":
            if line.startswith("NOTE|"):
                parts = line.split("|")
                note: dict[str, Any] = {"text": parts[1]}
                if include_positions:
                    note["xy"] = _parse_point(parts[2]) if len(parts) > 2 else None
                    note["font"] = parts[3] if len(parts) > 3 else "stick"
                    note["height"] = float(parts[4]) if len(parts) > 4 else 0.1
                    note["orient"] = parts[5] if len(parts) > 5 else "R0"
                    note["justify"] = parts[6] if len(parts) > 6 else "lowerCenter"
                result["notes"].append(note)

    # Flush last instance
    if current_inst is not None:
        result["instances"].append(current_inst)

    return result


# =======================================================================
# Helpers
# =======================================================================

def _parse_point(s: str) -> list[float]:
    """Parse SKILL point '(1.5 -2.0)' → [1.5, -2.0]."""
    s = s.strip().strip("()")
    parts = s.split()
    return [float(x) for x in parts] if len(parts) == 2 else [0.0, 0.0]


def _parse_bbox(s: str) -> list[list[float]]:
    """Parse SKILL bBox '((x1 y1) (x2 y2))' → [[x1,y1],[x2,y2]]."""
    s = s.strip().strip("()")
    # After stripping outer parens: "(x1 y1) (x2 y2)"
    points = []
    for part in s.split(")"):
        part = part.strip().strip("(")
        if part:
            nums = part.split()
            if len(nums) == 2:
                points.append([float(nums[0]), float(nums[1])])
    return points if len(points) == 2 else [[0, 0], [0, 0]]


# =======================================================================
# Legacy API — kept for backward compatibility
# =======================================================================

_READ_PLACEMENT_SKILL = '''
let((cv instList pinList labelList wireList)
  {owned_read_start}
  cv = {cv_expr}
  if(cv
    then progn(
  instList = ""
  foreach(inst cv~>instances
    instList = strcat(instList sprintf(nil "%s|%s|%s|%L|%s\\n"
      inst~>name inst~>libName inst~>cellName inst~>xy inst~>orient)))
  pinList = ""
  foreach(term cv~>terminals
    pinList = strcat(pinList sprintf(nil "%s|%s\\n" term~>name term~>direction)))
  labelList = ""
  foreach(label cv~>shapes
    when(label~>objType == "label"
      labelList = strcat(labelList sprintf(nil "%s|%L\\n" label~>theLabel label~>xy))))
  wireList = ""
  foreach(shape cv~>shapes
    when(shape~>objType == "line"
      wireList = strcat(wireList sprintf(nil "%L\\n" shape~>points))))
  sprintf(nil "INSTANCES\\n%sPINS\\n%sLABELS\\n%sWIRES\\n%sEND" instList pinList labelList wireList))
    else "ERROR")
  {owned_read_cleanup})
'''


def read_placement(
    client: VirtuosoClient,
    lib: str | None = None,
    cell: str | None = None,
) -> dict:
    """Read placement: instance positions, pins, labels, wires."""
    skill = _build_cellview_read_skill(_READ_PLACEMENT_SKILL, lib, cell)
    r = client.execute_skill(skill, timeout=30)
    if getattr(r, "errors", None):
        raise RuntimeError(f"read_placement SKILL error: {r.errors[0]}")
    raw = decode_skill_output(r.output)
    if raw.strip() == "ERROR":
        raise RuntimeError(
            f"read_placement could not open schematic "
            f"{lib or '(current)'}/{cell or '(current)'}"
        )

    result: dict = {"instances": [], "pins": [], "labels": [], "wires": []}
    section = None
    for line in raw.splitlines():
        line = line.strip()
        if line in ("INSTANCES", "PINS", "LABELS", "WIRES"):
            section = line.lower()
        elif line == "END" or not line:
            continue
        elif section == "instances":
            parts = line.split("|")
            if len(parts) >= 5:
                result["instances"].append({
                    "name": parts[0], "lib": parts[1], "cell": parts[2],
                    "xy": parts[3], "orient": parts[4],
                })
        elif section == "pins":
            parts = line.split("|")
            if len(parts) >= 2:
                result["pins"].append({"name": parts[0], "direction": parts[1]})
        elif section == "labels":
            parts = line.split("|", 1)
            if len(parts) >= 2:
                result["labels"].append({"text": parts[0], "xy": parts[1]})
        elif section == "wires":
            result["wires"].append(line)
    return result


_READ_CONNECTIVITY_SKILL = '''
let((cv instList netList pinList)
  {owned_read_start}
  cv = {cv_expr}
  if(cv
    then progn(
  instList = ""
  foreach(inst cv~>instances
    instList = strcat(instList sprintf(nil "%s|%s|%s\\n"
      inst~>name inst~>libName inst~>cellName)))
  netList = ""
  foreach(net cv~>nets
    netList = strcat(netList sprintf(nil "%s" net~>name))
    foreach(it net~>instTerms
      netList = strcat(netList sprintf(nil "|%s.%s" it~>inst~>name it~>name)))
    netList = strcat(netList "\\n"))
  pinList = ""
  foreach(term cv~>terminals
    pinList = strcat(pinList sprintf(nil "%s|%s\\n" term~>name term~>direction)))
  sprintf(nil "INSTANCES\\n%sNETS\\n%sPINS\\n%sEND" instList netList pinList))
    else "ERROR")
  {owned_read_cleanup})
'''


def read_connectivity(
    client: VirtuosoClient,
    lib: str | None = None,
    cell: str | None = None,
) -> dict:
    """Read electrical connectivity: instances, nets, pins."""
    skill = _build_cellview_read_skill(_READ_CONNECTIVITY_SKILL, lib, cell)
    r = client.execute_skill(skill, timeout=30)
    if getattr(r, "errors", None):
        raise RuntimeError(f"read_connectivity SKILL error: {r.errors[0]}")
    raw = decode_skill_output(r.output)
    if raw.strip() == "ERROR":
        raise RuntimeError(
            f"read_connectivity could not open schematic "
            f"{lib or '(current)'}/{cell or '(current)'}"
        )

    result: dict = {"instances": [], "nets": [], "pins": []}
    section = None
    for line in raw.splitlines():
        line = line.strip()
        if line in ("INSTANCES", "NETS", "PINS"):
            section = line.lower()
        elif line == "END" or not line:
            continue
        elif section == "instances":
            parts = line.split("|")
            if len(parts) >= 3:
                result["instances"].append({
                    "name": parts[0], "lib": parts[1], "cell": parts[2],
                })
        elif section == "nets":
            parts = line.split("|")
            result["nets"].append({
                "name": parts[0],
                "connections": parts[1:],
            })
        elif section == "pins":
            parts = line.split("|")
            if len(parts) >= 2:
                result["pins"].append({"name": parts[0], "direction": parts[1]})
    return result


_READ_PARAMS_SKILL = '''
let((cv result)
  {owned_read_start}
  cv = {cv_expr}
  if(cv
    then progn(
  result = ""
  foreach(inst cv~>instances
    let((cdf paramStr)
      cdf = cdfGetInstCDF(inst)
      paramStr = ""
      when(cdf
        foreach(p cdf~>parameters
          when(p~>value != nil && p~>value != ""
            && strlen(sprintf(nil "%L" p~>value)) <= 120
            paramStr = strcat(paramStr sprintf(nil "|%s=%L" p~>name p~>value)))))
      result = strcat(result sprintf(nil "%s|%s|%s%s\\n"
        inst~>name inst~>libName inst~>cellName paramStr))))
  result)
    else "ERROR")
  {owned_read_cleanup})
'''


def read_instance_params(
    client: VirtuosoClient,
    lib: str | None = None,
    cell: str | None = None,
    filter_params: list[str] | None = None,
) -> list[dict]:
    """Read CDF parameters for all instances."""
    skill = _build_cellview_read_skill(_READ_PARAMS_SKILL, lib, cell)
    r = client.execute_skill(skill, timeout=30)
    if getattr(r, "errors", None):
        raise RuntimeError(f"read_instance_params SKILL error: {r.errors[0]}")
    raw = decode_skill_output(r.output)
    if raw.strip() == "ERROR":
        raise RuntimeError(
            f"read_instance_params could not open schematic "
            f"{lib or '(current)'}/{cell or '(current)'}"
        )

    result = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        inst = {"name": parts[0], "lib": parts[1], "cell": parts[2], "params": {}}
        for kv in parts[3:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                v = v.strip('"')
                if filter_params is None or k in filter_params:
                    inst["params"][k] = v
        result.append(inst)
    return result
