"""Set CDF parameters on schematic instances with callback refresh.

Usage:
    from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

    # MOS transistors (shorthand params)
    set_instance_params(client, "MP0", w="500n", l="30n", nf="4", m="2")
    set_instance_params(client, "MN0", wf="250n", nf="8")

    # Any component via **kwargs
    set_instance_params(client, "I0", idc="100u")           # current source
    set_instance_params(client, "V0", vdc="1.8")            # voltage source
    set_instance_params(client, "R0", r="10k")              # resistor
    set_instance_params(client, "C0", c="1p")               # capacitor

    # Optional CDF filter allowlist + strict mode
    set_instance_params(client, "L0", l="500p", r="0", strict=True)

Note:
    By default ``param_filters`` points at ``cdf_param_filters.yaml`` and
    params outside the per-cell allowlist are dropped with a warning
    (or raise ``ValueError`` under ``strict=True``).  Pass
    ``param_filters=None`` to apply every kwarg verbatim.  The function
    also runs ``schCheck`` + ``dbSave`` — changes persist immediately.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
import warnings

import yaml

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.ops import escape_skill_string

# nf is read-only in TSMC PDK, must use "fingers"
# wf maps to "Wfg" (finger width); w is total width = Wfg × fingers
_PARAM_MAP = {"nf": "fingers", "wf": "Wfg"}
_DEFAULT_FILTERS_PATH = Path(__file__).parent / "cdf_param_filters.yaml"

_RUN_CALLBACKS_ON_CV = '''
let((cv inst iCDF cCDF saved cdfgData cdfgForm p cb n)
  cv = dbOpenCellViewByType("{slib}" "{scell}" "schematic" "schematic" "a")
  inst = car(setof(x cv~>instances x~>name == "{inst}"))
  unless(inst error("instance not found"))
  iCDF = cdfGetInstCDF(inst)
  unless(iCDF error("instance has no CDF"))
  cCDF = cdfGetCellCDF(ddGetObj(inst~>libName inst~>cellName))
  unless(cCDF error("cell CDF not found"))
  saved = makeTable('s)
  foreach(p cCDF~>parameters setarray(saved p~>name p~>value))
  foreach(p cCDF~>parameters
    when(get(iCDF p~>name) putpropq(p get(iCDF p~>name)~>value value)))
  cdfgData = cCDF
  cdfgForm = cCDF
  foreach(n list({params})
    p = get(cCDF n)
    unless(p error(sprintf(nil "unknown CDF param: %s" n)))
    p~>value = arrayref(paramVals n))
  foreach(n list({params})
    p = get(cCDF n)
    cb = p~>callback
    when(cb && cb != "" errset(evalstring(cb) t)))
  cdfUpdateInstParam(inst)
  foreach(p cCDF~>parameters putpropq(p arrayref(saved p~>name) value))
  schCheck(cv)
  dbSave(cv)
  t)
'''


def _load_filters(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _match_filter(config: dict, lib: str, cell: str) -> list[str] | None:
    for rule in config.get("filters", []):
        match = rule.get("match", {})
        if fnmatch.fnmatch(lib, match.get("lib", "*")) and fnmatch.fnmatch(
            cell, match.get("cell", "*")
        ):
            return rule.get("params")
    fallback = config.get("fallback", "all")
    return None if fallback == "all" else fallback


def _resolve_active_schematic_lib_cell(client: VirtuosoClient) -> tuple[str, str]:
    skill = (
        "let((cv) "
        "cv = geGetEditCellView() "
        'if(cv sprintf(nil "%s|%s" cv~>libName cv~>cellName) ""))'
    )
    result = client.execute_skill(skill)
    if result.errors:
        raise RuntimeError(f"resolve active schematic failed: {result.errors[0]}")
    out = decode_skill_output(result.output).strip().strip('"')
    if not out or "|" not in out:
        raise ValueError("no active schematic cellview")
    return tuple(out.split("|", 1))  # type: ignore[return-value]


def _resolve_instance_master(
    client: VirtuosoClient,
    schematic_lib: str,
    schematic_cell: str,
    inst_name: str,
) -> tuple[str, str]:
    skill = (
        "let((cv inst) "
        f'cv = dbOpenCellViewByType("{escape_skill_string(schematic_lib)}" "{escape_skill_string(schematic_cell)}" "schematic" "schematic" "r") '
        f'inst = car(setof(x cv~>instances x~>name == "{escape_skill_string(inst_name)}")) '
        'if(inst sprintf(nil "%s|%s" inst~>libName inst~>cellName) ""))'
    )
    result = client.execute_skill(skill)
    if result.errors:
        raise RuntimeError(f"resolve instance master failed for {inst_name}: {result.errors[0]}")
    out = decode_skill_output(result.output).strip().strip('"')
    if not out or "|" not in out:
        raise ValueError(f"instance not found: {inst_name}")
    return tuple(out.split("|", 1))  # type: ignore[return-value]


def _run_batched_param_update(
    client: VirtuosoClient,
    schematic_lib: str,
    schematic_cell: str,
    inst_name: str,
    params: dict[str, str],
) -> dict[str, str]:
    if not params:
        return {}

    param_list = " ".join(f'"{escape_skill_string(name)}"' for name in params)
    param_value_bindings = " ".join(
        f'setarray(paramVals "{escape_skill_string(name)}" "{escape_skill_string(value)}")'
        for name, value in params.items()
    )

    skill = (
        "let((paramVals) "
        "paramVals = makeTable('paramVals) "
        f"{param_value_bindings} "
        + _RUN_CALLBACKS_ON_CV.format(
            slib=escape_skill_string(schematic_lib),
            scell=escape_skill_string(schematic_cell),
            inst=escape_skill_string(inst_name),
            params=param_list,
        )
        + ")"
    )

    result = client.execute_skill(skill, timeout=30)
    if result.errors:
        raise RuntimeError(f"CDF callback update failed for {inst_name}: {result.errors[0]}")
    return params


def set_instance_params(
    client: VirtuosoClient,
    inst_name: str,
    *,
    w: str | None = None,
    wf: str | None = None,
    l: str | None = None,
    nf: str | None = None,
    m: str | None = None,
    param_filters: str | Path | None = _DEFAULT_FILTERS_PATH,
    strict: bool = False,
    **kwargs: str,
) -> dict[str, str]:
    """Set CDF parameters on any instance, then trigger CDF callbacks.

    MOS shorthand args (w, wf, l, nf, m) are mapped to PDK CDF names.
    Any other parameter can be passed via kwargs.

    Args:
        w: Total width (e.g. "2u"). w = wf × nf.
        wf: Finger width (e.g. "500n"). Maps to CDF param "Wfg".
        l: Channel length (e.g. "30n").
        nf: Number of fingers (e.g. "4"). Maps to CDF param "fingers".
        m: Multiplier (e.g. "2").
        param_filters: path to YAML allowlist rules. Set to None to disable filtering.
        strict: if True, reject filtered-out parameters with ValueError.
        **kwargs: Any CDF parameter name=value, e.g. idc="100u", vdc="1.8",
                  r="10k", c="1p", freq="1G", etc.

    Returns:
        The parameters that were applied.
    """
    if w is not None and wf is not None:
        raise ValueError("Specify w (total width) or wf (finger width), not both")

    params: dict[str, str] = {}
    if w is not None:
        params["w"] = w
    if wf is not None:
        params[_PARAM_MAP["wf"]] = wf
    if l is not None:
        params["l"] = l
    if nf is not None:
        params[_PARAM_MAP["nf"]] = nf
    if m is not None:
        params["m"] = m
    params.update(kwargs)
    if not params:
        return {}

    schematic_lib, schematic_cell = _resolve_active_schematic_lib_cell(client)
    inst_lib, inst_cell = _resolve_instance_master(client, schematic_lib, schematic_cell, inst_name)

    allowed: list[str] | None = None
    if param_filters is not None:
        allowed = _match_filter(_load_filters(param_filters), inst_lib, inst_cell)

    rejected = [name for name in params if allowed is not None and name not in allowed]
    if strict and rejected:
        raise ValueError(f"params not allowed by filter for {inst_lib}/{inst_cell}: {rejected}")
    if rejected:
        warnings.warn(
            f"params ignored by filter for {inst_lib}/{inst_cell}: {rejected}",
            UserWarning,
            stacklevel=2,
        )

    apply_params = {name: value for name, value in params.items() if allowed is None or name in allowed}
    return _run_batched_param_update(
        client,
        schematic_lib,
        schematic_cell,
        inst_name,
        apply_params,
    )
