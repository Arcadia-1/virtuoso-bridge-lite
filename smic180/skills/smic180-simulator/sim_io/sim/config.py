"""
Simulation Deck Configuration — Data structures, LLM I/O, Maestro parser.

Defines SimDeckConfig (the complete simulation configuration) and provides:
  - summarize_netlist()    — extract circuit structure from si netlist
  - write_sim_config_input — write LLM input JSON
  - load_sim_config()      — load LLM-generated sim_config.json
  - parse_active_state()   — parse Maestro active.state XML
  - sim_config_from_legacy — convert old SimConfig
  - sim_config_from_site() — build from SiteConfig defaults
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


TSMC180_MODEL_FILE = (
    "/home/IC/EDA/tsmc18/models/spectre/cr018gpii_v1d0.scs"
)
TSMC180_SECTIONS = [
    "tt", "tt_rfres_sa", "tt_rfmvar", "tt_rtmom", "tt_disres",
    "tt_rfesd", "tt_mim", "tt_fmom", "tt_rfres_rpo", "tt_m",
    "tt_rfmim", "tt_res", "tt_bip", "tt_bip3", "tt_dio3",
    "tt_3v", "tt_na", "tt_dio", "tt_dio_m", "tt_rfind",
    "tt_dio_3m", "tt_bbmvar", "tt_rfres_hri", "tt_3vna",
    "tt_dio_dnw", "tt_3m", "tt_rfjvar", "tt_rfmos", "tt_rfsbd",
    "tt_rfmvar_33", "tt_rfmos33", "tt_rffmom", "stat_noise",
]
# SMIC180 IO pad library (tpd018bcdnv5) not installed; set spectre.io_model_include in site.yaml when available.
TSMC180_IO_MODEL_FILE = ""


# ── PDK Constants ───────────────────────────────────────────────

# ── Data Structures ─────────────────────────────────────────────

@dataclass
class ModelInclude:
    path: str
    section: str


@dataclass
class DesignVar:
    name: str
    expression: str


@dataclass
class SweepSpec:
    param: str
    sweep_type: str = "Design Variable"
    range_type: str = "Start-Stop"
    start: str = ""
    stop: str = ""
    lin: str = ""
    dec: str = ""


@dataclass
class AnalysisSpec:
    name: str
    enabled: bool = True
    errpreset: str = "moderate"
    stop: str = ""
    sweep: Optional[SweepSpec] = None
    extra_options: dict[str, str] = field(default_factory=dict)


@dataclass
class SaveSignal:
    signal: str


@dataclass
class PinMeasurement:
    """LLM-specified measurement intent for a single pin.

    The LLM decides WHAT to measure; code decides the OCEAN
    expression syntax, eval_type, and save level.
    """
    measures: list[str] = field(default_factory=list)   # ["voltage","current","power","custom"]
    spec: dict[str, str] = field(default_factory=dict)  # {"i_max":"0.1","vmax_above":"0.9*VDD"}
    custom_expr: str = ""
    custom_name: str = ""


@dataclass
class OutputExpression:
    name: str
    expression: str
    eval_type: str = "point"


@dataclass
class InfoStatement:
    info_type: str
    what: str
    where: str = "rawfile"


@dataclass
class SimOptions:
    reltol: float = 1e-4
    vabstol: float = 1e-6
    iabstol: float = 1e-12
    gmin: float = 1e-12
    temp: float = 27.0
    tnom: float = 27.0
    pivrel: float = 1e-3
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class SimDeckConfig:
    lib: str = ""
    cell: str = ""
    global_ground: str = "0"
    design_vars: list[DesignVar] = field(default_factory=list)
    model_includes: list[ModelInclude] = field(default_factory=list)
    analyses: list[AnalysisSpec] = field(default_factory=list)
    save_signals: list[SaveSignal] = field(default_factory=list)
    outputs: list[OutputExpression] = field(default_factory=list)
    pin_measurements: dict[str, PinMeasurement] = field(default_factory=dict)
    info_statements: list[InfoStatement] = field(default_factory=list)
    sim_options: SimOptions = field(default_factory=SimOptions)
    save_default: str = "allpub"
    source: str = ""  # "llm" | "active_state" | "manual"


# ── Netlist Summarizer ──────────────────────────────────────────

def summarize_netlist(netlist_text: str) -> dict:
    """Extract circuit structure from a Spectre-format netlist.

    Returns dict with:
        top_cell  — name of the top-level cell (last subcircuit defined)
        subckts   — list of {name, pins, instance_names}
        instances — list of {name, cell, nets} at top level
    """
    subckts = []
    current_subckt = None
    top_instances = []

    for line in netlist_text.splitlines():
        s = line.strip()
        if s.startswith("//"):
            continue

        m = re.match(r"subckt\s+(\S+)\s+(.*)", s)
        if m:
            name = m.group(1)
            pins = m.group(2).split()
            current_subckt = {"name": name, "pins": pins, "instances": []}
            subckts.append(current_subckt)
            continue

        if s.startswith("ends "):
            current_subckt = None
            continue

        m = re.match(r"([A-Za-z]\w*)\s*\(([^)]*)\)\s*(\S+)", s)
        if m:
            inst_name = m.group(1)
            nets = m.group(2).split()
            cell = m.group(3)
            inst = {"name": inst_name, "cell": cell, "nets": nets}
            if current_subckt:
                current_subckt["instances"].append(inst)
            else:
                top_instances.append(inst)

    top_cell = subckts[-1]["name"] if subckts else ""

    return {
        "top_cell": top_cell,
        "subckts": [
            {
                "name": sc["name"],
                "pins": sc["pins"],
                "instance_names": [i["name"] for i in sc["instances"]],
            }
            for sc in subckts
        ],
        "instances": [
            {"name": i["name"], "cell": i["cell"], "nets": i["nets"]}
            for i in top_instances
        ],
    }


# ── LLM Input Writer ────────────────────────────────────────────

def write_sim_config_input(
    netlist_summary: dict,
    pin_classifications: list[dict] | None,
    user_intent: str,
    lib: str,
    cell: str,
    vdd_value: float,
    path: str | Path,
) -> None:
    """Write sim_config_input.json for the LLM to read."""
    data = {
        "lib": lib,
        "cell": cell,
        "vdd_value": vdd_value,
        "user_intent": user_intent,
        "netlist_summary": netlist_summary,
        "pin_classifications": pin_classifications or [],
        "pdk_info": {
            "model_file": os.getenv("SIM_PDK_CORE_SPECTRE_INCLUDE", ""),
            "available_sections": [
                section.strip()
                for section in os.getenv("SIM_PDK_CORE_SPECTRE_SECTIONS", "").split(",")
                if section.strip()
            ],
        },
    }
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── LLM Output Loader ──────────────────────────────────────────

def load_sim_config(path: str | Path) -> SimDeckConfig:
    """Load LLM-generated sim_config.json into SimDeckConfig."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError) as e:
        raise ValueError(f"Failed to load sim_config from {path}: {e}") from e
    return _dict_to_deck_config(data, source="llm")


def _dict_to_deck_config(data: dict, source: str = "") -> SimDeckConfig:
    """Convert a plain dict (from JSON or parsed XML) to SimDeckConfig."""
    design_vars = [
        DesignVar(name=v["name"], expression=str(v["expression"]))
        for v in data.get("design_vars", [])
    ]

    model_includes = [
        ModelInclude(path=mi["path"], section=mi.get("section", ""))
        for mi in data.get("model_includes", [])
    ]

    analyses = []
    for a in data.get("analyses", []):
        sweep = None
        if "sweep" in a and a["sweep"]:
            sw = a["sweep"]
            sweep = SweepSpec(
                param=sw.get("param", ""),
                sweep_type=sw.get("sweep_type", "Design Variable"),
                range_type=sw.get("range_type", "Start-Stop"),
                start=sw.get("start", ""),
                stop=sw.get("stop", ""),
                lin=sw.get("lin", ""),
                dec=sw.get("dec", ""),
            )
        analyses.append(AnalysisSpec(
            name=a["name"],
            enabled=a.get("enabled", True),
            errpreset=a.get("errpreset", "moderate"),
            stop=a.get("stop", ""),
            sweep=sweep,
            extra_options=a.get("extra_options", {}),
        ))

    save_signals = [
        SaveSignal(signal=s["signal"])
        for s in data.get("save_signals", [])
    ]

    outputs = [
        OutputExpression(
            name=o["name"],
            expression=o["expression"],
            eval_type=o.get("eval_type", "point"),
        )
        for o in data.get("outputs", [])
    ]

    pin_measurements = {}
    for pin_name, pm_data in data.get("pin_measurements", {}).items():
        if isinstance(pm_data, dict):
            pin_measurements[pin_name] = PinMeasurement(
                measures=pm_data.get("measures", []),
                spec=pm_data.get("spec", {}),
                custom_expr=pm_data.get("custom_expr", ""),
                custom_name=pm_data.get("custom_name", ""),
            )

    info_statements = [
        InfoStatement(
            info_type=i.get("info_type", i.get("type", "")),
            what=i["what"],
            where=i.get("where", "rawfile"),
        )
        for i in data.get("info_statements", [])
    ]

    opts_data = data.get("sim_options", {})
    sim_options = SimOptions(
        reltol=float(opts_data.get("reltol", 1e-4)),
        vabstol=float(opts_data.get("vabstol", 1e-6)),
        iabstol=float(opts_data.get("iabstol", 1e-12)),
        gmin=float(opts_data.get("gmin", 1e-12)),
        temp=float(opts_data.get("temp", 27.0)),
        tnom=float(opts_data.get("tnom", 27.0)),
        pivrel=float(opts_data.get("pivrel", 1e-3)),
        extra=opts_data.get("extra", {}),
    )

    return SimDeckConfig(
        lib=data.get("lib", ""),
        cell=data.get("cell", ""),
        global_ground=data.get("global_ground", "0"),
        design_vars=design_vars,
        model_includes=model_includes,
        analyses=analyses,
        save_signals=save_signals,
        outputs=outputs,
        pin_measurements=pin_measurements,
        info_statements=info_statements,
        sim_options=sim_options,
        save_default=data.get("save_default", "allpub"),
        source=source,
    )


# ── active.state Parser ─────────────────────────────────────────

def parse_active_state(path: str | Path) -> SimDeckConfig:
    """Parse a Maestro active.state XML into SimDeckConfig."""
    tree = ET.parse(Path(path))
    root = tree.getroot()
    test = root.find(".//Test")
    if test is None:
        raise ValueError("No <Test> found in active.state")

    config = SimDeckConfig(
        global_ground="0",
        source="active_state",
    )

    for component in test.findall("component"):
        name = component.get("Name", "")
        if name == "modelSetup":
            config.model_includes = _parse_model_setup(component)
        elif name == "variables":
            config.design_vars = _parse_variables(component)
        elif name == "analyses":
            config.analyses = _parse_analyses(component)
        elif name == "simulatorOptions":
            config.sim_options = _parse_simulator_options(component)
        elif name == "outputs":
            signals, expressions, infos = _parse_outputs(component)
            config.save_signals = signals
            config.outputs = expressions
            if not config.info_statements:
                config.info_statements = infos

    return config


def _get_field(component: ET.Element, field_name: str) -> str:
    """Get a field's text value from a component/partition."""
    for f in component.iter("field"):
        if f.get("Name") == field_name:
            text = (f.text or "").strip('"')
            return text
    return ""


def _parse_model_setup(component: ET.Element) -> list[ModelInclude]:
    """Extract model includes from modelSetup component."""
    model_files_text = _get_field(component, "modelFiles")
    if not model_files_text:
        return []

    results = []
    for m in re.finditer(r'\("([^"]+)"\s+"([^"]+)"\)', model_files_text):
        results.append(ModelInclude(path=m.group(1), section=m.group(2)))
    return results


def _parse_variables(component: ET.Element) -> list[DesignVar]:
    """Extract design variables from variables component."""
    results = []
    for f in component.iter("field"):
        if f.get("Name", "").startswith("saveComponent_"):
            name = ""
            expression = ""
            for child in f.iter("field"):
                cname = child.get("Name", "")
                if cname == "name":
                    name = (child.text or "").strip('"')
                elif cname == "expression":
                    expression = (child.text or "").strip('"')
            if name:
                results.append(DesignVar(name=name, expression=expression))
    return results


def _parse_analyses(component: ET.Element) -> list[AnalysisSpec]:
    """Extract analyses from analyses component.

    The XML structure is:
      <component Name="analyses">
        <analyses Name="analysis">
          <analysis Name="dc"> ... </analysis>
          <analysis Name="tran"> ... </analysis>
        </analyses>
      </component>
    """
    results = []
    # Find all <analysis> elements — they may be nested under <analyses Name="analysis">
    analysis_elems = list(component.findall("analysis"))
    if not analysis_elems:
        for analyses_wrapper in component.findall("analyses"):
            analysis_elems.extend(analyses_wrapper.findall("analysis"))

    for analysis_elem in analysis_elems:
        aname = analysis_elem.get("Name", "")
        if not aname:
            continue

        enabled = True
        errpreset = "moderate"
        stop = ""
        extra_options = {}
        # Collect all fields across partitions, then build sweep
        collected: dict[str, str] = {}

        for partition in analysis_elem.findall("partition"):
            pname = partition.get("Name", "")

            for f in partition.findall("field"):
                fname = f.get("Name", "")
                fval = (f.text or "").strip('"')

                if fname == "enable":
                    enabled = "(t)" in (f.text or "")
                elif fname == "errpreset":
                    errpreset = fval
                elif fname == "stop" and pname != "fields":
                    stop = fval
                elif pname == "fields":
                    collected[fname] = fval

        # Build sweep from collected fields (if there's a param to sweep)
        sweep = None
        if collected.get("param") or collected.get("designVar"):
            sweep = SweepSpec(
                param=collected.get("designVar") or collected.get("param", ""),
                sweep_type=collected.get("sweep", "Design Variable"),
                range_type=collected.get("rangeType", "Start-Stop"),
                start=collected.get("start", ""),
                stop=collected.get("stop", ""),
                lin=collected.get("lin", ""),
                dec=collected.get("dec", ""),
            )
        elif collected.get("stop"):
            # No param but has stop (e.g. tran stop=1u)
            stop = collected["stop"]

        # Clean up: remove empty sweep specs (no meaningful param)
        if sweep and not sweep.param:
            sweep = None

        results.append(AnalysisSpec(
            name=aname,
            enabled=enabled,
            errpreset=errpreset,
            stop=stop,
            sweep=sweep,
            extra_options=extra_options,
        ))

    return results


def _parse_simulator_options(component: ET.Element) -> SimOptions:
    """Extract simulator options from simulatorOptions component."""
    opts = SimOptions()
    for partition in component.findall("partition"):
        for f in partition.findall("field"):
            fname = f.get("Name", "")
            fval = (f.text or "").strip('"')
            if not fval:
                continue
            try:
                if fname == "reltol":
                    opts.reltol = float(fval)
                elif fname == "vabstol":
                    opts.vabstol = float(fval)
                elif fname == "iabstol":
                    opts.iabstol = float(fval)
                elif fname == "gmin":
                    opts.gmin = float(fval)
                elif fname == "temp":
                    opts.temp = float(fval)
                elif fname == "tnom":
                    opts.tnom = float(fval)
                elif fname == "pivrel":
                    opts.pivrel = float(fval)
                elif fname not in (
                    "noiseOnType", "noiseOffType", "noiseSeverity",
                    "spSeverity", "dcSeverity", "dcOpSeverity",
                    "acSeverity", "tranSeverity", "pzSeverity",
                    "checklimitdest", "sensfile", "cols", "digits",
                    "scale", "scalem", "rforce", "maxnotes", "maxwarns",
                    # Spectre-specific filter flags (clutter)
                    "pzDisableAll", "noiseEnableAll", "dcDisableAll",
                    "dcFilterNone", "spEnableAll", "acFilterExtreme",
                    "generalnoiseinst", "pzFilterExtreme", "dcOpFilterNone",
                    "dcFilterExtreme", "noiseDisableAll", "dcOpFilterExtreme",
                    "dcEnableAll", "dcOpEnableAll", "noiseFilterExtreme",
                    "spFilterExtreme", "tranFilterNone", "enable_dcsweep_op_info",
                    "tranFilterExtreme", "pzEnableAll", "spFilterNone",
                    "acDisableAll", "spDisableAll", "pzFilterNone",
                    "tranFilterExtremeInitByCdsenv", "noiseFilterNone",
                    "acEnableAll", "tranEnableAll", "acFilterNone",
                    "dcOpDisableAll", "tranDisableAll",
                ):
                    opts.extra[fname] = fval
            except ValueError:
                pass
    return opts


def _parse_outputs(
    component: ET.Element,
) -> tuple[list[SaveSignal], list[OutputExpression], list[InfoStatement]]:
    """Extract save signals, output expressions, and info statements."""
    signals = []
    expressions = []
    infos = []

    for f in component.iter("field"):
        fname = f.get("Name", "")
        if not fname.startswith("outputList_"):
            continue

        oname = ""
        osignal = ""
        oexpr = ""
        oeval = ""
        otype = ""

        for child in f.iter("field"):
            cname = child.get("Name", "")
            cval = (child.text or "").strip('"')
            if cname == "name":
                oname = cval
            elif cname == "signal":
                osignal = cval
            elif cname == "expression":
                # Expression is a list like "(VOUT / (VIP - VIN))"
                raw = child.text or ""
                m = re.match(r"\((.+)\)", raw.strip())
                oexpr = m.group(1).strip() if m else raw.strip()
            elif cname == "evalType":
                oeval = cval
            elif cname == "type" or cname == "type2":
                if child.text and child.text.strip() not in ("nil", ""):
                    otype = child.text.strip()

        if not oname:
            continue

        # Classify based on eval_type and presence of expression
        has_real_expr = oexpr and oexpr != "nil"
        has_signal = osignal and osignal not in ("nil", "")

        if has_real_expr:
            # It's an output expression (e.g. Av_big = VOUT / (VIP - VIN))
            expressions.append(OutputExpression(
                name=oname,
                expression=oexpr,
                eval_type=oeval or "point",
            ))
        elif has_signal:
            # It's a signal to save (e.g. input_p → /VIP)
            sig_name = osignal
            if not sig_name.startswith("/"):
                sig_name = "/" + sig_name
            signals.append(SaveSignal(signal=sig_name))
        # else: unnamed/empty output, skip

    # Default info statements (standard for Spectre decks)
    if not infos:
        infos = [
            InfoStatement(info_type="modelParameter", what="models"),
            InfoStatement(info_type="element", what="inst"),
            InfoStatement(info_type="outputParameter", what="output"),
            InfoStatement(info_type="designParamVals", what="parameters"),
            InfoStatement(info_type="primitives", what="primitives"),
            InfoStatement(info_type="subckts", what="subckts"),
        ]

    return signals, expressions, infos


# ── Legacy / SiteConfig Constructors ────────────────────────────

def sim_config_from_legacy(config) -> SimDeckConfig:
    """Convert old SimConfig (from sim_deck_template) to SimDeckConfig."""
    model_includes = []
    if config.model_include:
        model_includes.append(
            ModelInclude(path=config.model_include, section=config.model_section)
        )

    analyses = [
        AnalysisSpec(
            name=config.analysis,
            stop=config.stop,
            errpreset=config.errpreset,
        )
    ]

    return SimDeckConfig(
        global_ground="0",
        design_vars=[],
        model_includes=model_includes,
        analyses=analyses,
        save_signals=[],
        outputs=[],
        info_statements=[],
        sim_options=SimOptions(
            reltol=config.reltol,
            vabstol=config.vabstol,
            iabstol=config.iabstol,
            gmin=config.gmin,
            temp=config.temperature,
            tnom=config.temperature,
        ),
        save_default=config.save_signals,
        source="legacy",
    )


def sim_config_from_site(
    vdd_value: float = 1.8,
    model_file: str = "",
    sections: list[str] | None = None,
) -> SimDeckConfig:
    """Build SimDeckConfig from site defaults.

    Uses SiteConfig-exported environment values. If model paths or sections are
    missing, falls back to built-in SMIC180 defaults with explicit WARNING output.
    """
    mf = model_file or os.getenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "")
    if not mf:
        mf = TSMC180_MODEL_FILE
        print("[sim-config] WARNING: spectre.core_model_include is missing; using built-in SMIC180 core model fallback (TSMC180 cr018gpii_v1d0).")
    secs_str = os.getenv("SIM_PDK_CORE_SPECTRE_SECTIONS", "")
    if secs_str:
        secs = secs_str.split(",")
    else:
        secs = sections or TSMC180_SECTIONS
        if not sections:
            print("[sim-config] WARNING: spectre.core_sections is missing; using built-in SMIC180 section fallback (33 core sections from spectre.scs).")

    # IO pad model — only include if the env var is set (PDK-specific)
    io_model_path = os.getenv("SIM_PDK_IO_SPECTRE_INCLUDE", "")
    if not io_model_path:
        io_model_path = ""
        print("[sim-config] WARNING: spectre.io_model_include is missing; SMIC180 IO pad library not installed; skipping IO model include.")
    model_includes = [ModelInclude(path=mf, section=s) for s in secs]
    if io_model_path:
        model_includes.append(ModelInclude(path=io_model_path, section=""))

    return SimDeckConfig(
        global_ground="0",
        design_vars=[DesignVar(name="VDD", expression=str(vdd_value))],
        model_includes=model_includes,
        analyses=[
            AnalysisSpec(name="dc", sweep=SweepSpec(
                param="VDD", start="0", stop=str(vdd_value * 1.5), lin="50"
            )),
            AnalysisSpec(name="tran", stop="100n", errpreset="liberal"),
        ],
        save_signals=[],
        outputs=[],
        info_statements=[
            InfoStatement(info_type="modelParameter", what="models"),
            InfoStatement(info_type="element", what="inst"),
            InfoStatement(info_type="outputParameter", what="output"),
            InfoStatement(info_type="designParamVals", what="parameters"),
            InfoStatement(info_type="primitives", what="primitives"),
            InfoStatement(info_type="subckts", what="subckts"),
        ],
        sim_options=SimOptions(),
        save_default="allpub",
        source="site_default",
    )


# ── Sim Config Resolution ───────────────────────────────────────

def resolve_sim_config(
    run_dir: Path,
    lib: str = "",
    cell: str = "",
    vdd_value: float = 1.8,
    user_intent: str = "",
    legacy_config=None,
) -> SimDeckConfig:
    """Resolve SimDeckConfig with priority: LLM > active.state > legacy > site default.

    Parameters
    ----------
    run_dir : Path to search for sim_config.json and active.state
    lib, cell : design identification
    vdd_value : supply voltage for default config
    user_intent : passed through for LLM context (not used in resolution)
    legacy_config : old SimConfig to convert if no other source found
    """
    # 1. LLM-generated config
    llm_path = run_dir / "sim_config.json"
    if llm_path.exists():
        config = load_sim_config(llm_path)
        print(f"[sim-config] Loaded LLM config from {llm_path}")
        return config

    # 2. Maestro active.state (must be in run_dir — not scattered in parent dirs)
    state_path = run_dir / "active.state"
    if state_path.exists():
        try:
            config = parse_active_state(state_path)
            config.lib = lib or config.lib
            config.cell = cell or config.cell
            print(f"[sim-config] Loaded config from active.state ({state_path})")
            return config
        except Exception as e:
            print(f"[sim-config] WARNING: Failed to parse active.state: {e}")

    # 3. Legacy SimConfig conversion
    if legacy_config is not None:
        config = sim_config_from_legacy(legacy_config)
        config.lib = lib
        config.cell = cell
        print(f"[sim-config] Using legacy SimConfig")
        return config

    # 4. Site default
    config = sim_config_from_site(vdd_value=vdd_value)
    config.lib = lib
    config.cell = cell
    print(f"[sim-config] Using site default config")
    return config
