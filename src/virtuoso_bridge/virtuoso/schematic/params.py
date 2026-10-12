"""Set CDF parameters on schematic instances with callback refresh.

Usage:
    from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

    # MOS transistors (shorthands resolved against the instance's CDF)
    set_instance_params(client, "MP0", w="500n", l="30n", nf="4", m="2")

    # Any component via **kwargs
    set_instance_params(client, "I0", idc="100u")           # current source
    set_instance_params(client, "V0", vdc="1.8")            # voltage source
    set_instance_params(client, "R0", r="10k")              # resistor
    set_instance_params(client, "C0", c="1p")               # capacitor

    # Optional CDF filter allowlist + strict mode
    set_instance_params(client, "L0", l="500p", r="0", strict=True)

Note:
    Names are checked against the instance's CDF before anything is changed;
    an unknown name raises ``ValueError``. MOS shorthands map to the first
    candidate CDF parameter that exists and is editable: ``nf`` -> ``nf`` or
    ``fingers``, ``m`` -> ``m`` or ``simM``, ``wf`` (finger width) -> ``Wfg``.
    ``w`` and ``l`` are the PDK's own ``w``/``l``: some kits define ``w`` as
    total width, others as width per finger, so read the CDF prompt.

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
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr

# MOS shorthands -> candidate CDF names, first existing editable one wins.
# Kits differ: TSMC keeps nf read-only and edits "fingers", some kits expose
# the multiplier as "simM", and "wf" is the finger width only where the kit
# calls it "Wfg" (elsewhere a parameter named "wf" can be the *total* width).
_SHORTHAND_CANDIDATES = {
    "w": ("w",),
    "wf": ("Wfg",),
    "l": ("l",),
    "nf": ("nf", "fingers"),
    "m": ("m", "simM"),
}
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


def _read_instance_cdf(
    client: VirtuosoClient,
    schematic_lib: str,
    schematic_cell: str,
    inst_name: str,
) -> dict[str, tuple[str, bool]]:
    """Return {parameter: (prompt, editable)} from the instance's CDF."""
    skill = (
        "let((cv inst cdf) "
        f'cv = dbOpenCellViewByType("{escape_skill_string(schematic_lib)}" "{escape_skill_string(schematic_cell)}" "schematic" "schematic" "r") '
        f'inst = car(setof(x cv~>instances x~>name == "{escape_skill_string(inst_name)}")) '
        "cdf = when(inst cdfGetInstCDF(inst)) "
        "when(cdf mapcar(lambda((p) list(p~>name p~>prompt p~>editable)) cdf~>parameters)))"
    )
    result = client.execute_skill(skill)
    if result.errors:
        raise RuntimeError(f"read CDF failed for {inst_name}: {result.errors[0]}")
    parsed = parse_sexpr(result.output or "")
    if not isinstance(parsed, list):
        raise ValueError(f"instance has no CDF parameters: {inst_name}")
    params: dict[str, tuple[str, bool]] = {}
    for entry in parsed:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            prompt = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
            # Unset (nil) means editable, as in analogLib; kits that lock a
            # parameter set the expression string "nil" (e.g. TSMC's nf).
            editable = entry[2] if len(entry) > 2 else None
            params[entry[0]] = (prompt, editable != "nil")
    return params


def _resolve_names(
    requested: dict[str, str],
    shorthands: dict[str, str],
    cdf: dict[str, tuple[str, bool]],
    inst_lib: str,
    inst_cell: str,
) -> dict[str, str]:
    """Map shorthands and kwargs to CDF names, refusing anything absent."""
    resolved: dict[str, str] = {}
    for short, value in shorthands.items():
        candidates = _SHORTHAND_CANDIDATES[short]
        name = next((c for c in candidates if c in cdf and cdf[c][1]), None)
        if name is None:
            widths = ", ".join(
                f"{n} ({cdf[n][0]!r})" for n in sorted(cdf) if n in ("w", "wf", "Wfg", "W", "wt", "wr")
            )
            hint = f"; width parameters: {widths}" if widths else ""
            raise ValueError(
                f"{inst_lib}/{inst_cell} has no editable CDF parameter for {short}= "
                f"(tried {', '.join(candidates)}){hint}. Pass the CDF name explicitly."
            )
        resolved[name] = value
    unknown = sorted(name for name in requested if name not in cdf)
    if unknown:
        raise ValueError(f"{inst_lib}/{inst_cell} has no CDF parameter(s): {unknown}")
    resolved.update(requested)
    return resolved


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

    MOS shorthand args (w, wf, l, nf, m) are resolved against the instance's
    CDF; any other parameter can be passed via kwargs. Every name must exist
    in the CDF, otherwise ``ValueError`` is raised before anything changes.

    Args:
        w: The PDK's CDF ``w``. Total width on some kits, width per finger on
            others (check the CDF prompt).
        wf: Finger width; requires a ``Wfg`` CDF parameter.
        l: Channel length (e.g. "30n").
        nf: Number of fingers; sets ``nf`` if editable, else ``fingers``.
        m: Multiplier; sets ``m`` if editable, else ``simM``.
        param_filters: path to YAML allowlist rules. Set to None to disable filtering.
        strict: if True, reject filtered-out parameters with ValueError.
        **kwargs: Any CDF parameter name=value, e.g. idc="100u", vdc="1.8",
                  r="10k", c="1p", freq="1G", etc.

    Returns:
        The parameters that were applied, keyed by CDF name.
    """
    if w is not None and wf is not None:
        raise ValueError("Specify w or wf (finger width), not both")

    shorthands = {
        name: value
        for name, value in (("w", w), ("wf", wf), ("l", l), ("nf", nf), ("m", m))
        if value is not None
    }
    if not shorthands and not kwargs:
        return {}

    schematic_lib, schematic_cell = _resolve_active_schematic_lib_cell(client)
    inst_lib, inst_cell = _resolve_instance_master(client, schematic_lib, schematic_cell, inst_name)
    cdf = _read_instance_cdf(client, schematic_lib, schematic_cell, inst_name)
    params = _resolve_names(dict(kwargs), shorthands, cdf, inst_lib, inst_cell)

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
