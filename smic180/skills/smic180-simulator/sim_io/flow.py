"""
sim_io.flow 鈥?Building blocks for the SIM-IO pipeline.

Step functions and dataclasses used by scripts/symbol_export.py, tb_builder.py, and maestro_runner.py.
Do not call run_symbol_export / run_tb_builder / run_maestro_runner from here 鈥?those live in the scripts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from virtuoso_bridge import VirtuosoClient
from sim_io.config import create_run_dir as _create_sim_run_dir
from sim_io.bridge.edit_patterns import (
    batch_ops,
    label_term,
    label_term_directed,
    create_inst,
)
from sim_io.symbol.layout_engine import (
    LayoutConfig,
    LayoutEngine,
    Side,
    generate_apply_skill,
    parse_symbol_info,
)
from sim_io.pin_types import (
    PinInfo,
    PinClassification,
    ClassificationResult,
    PinType,
    PAD_RULES,
    SIDE_CONFIGS,
    classify_pin_heuristic,
    load_pin_classifications,
    write_pin_info_json,
    build_classification_map,
    get_rule_for_pin,
)
from sim_io.bridge.skill_call import skill_exec, SkillExecutionError

_SIM_IO = Path(__file__).resolve().parent.parent
SKILL_DIR = _SIM_IO / "skill_code"

# Shared net label used to connect all digital output _CORE pins to one vpulse.
_DIG_OUT_CORE_NET = "DIG_OUT_CORE"

# Global ground net 鈥?all source/load MINUS terminals label "gnd!" directly.
# In Virtuoso "gnd!" is a built-in global net; no analogLib/gnd symbol needed.
_GND_NET = "gnd!"


def load_llm_result(run_dir: Path, *, cell: str = "") -> ClassificationResult | None:
    """Load pin_classifications.json, returning the full ClassificationResult or None."""
    run_path = run_dir / "pin_classifications.json"
    if not run_path.exists():
        print(f"[llm] No pin_classifications.json in {run_dir} 鈥?heuristic fallback.")
        return None
    result = load_pin_classifications(run_path)
    if cell and result.cell and result.cell != cell:
        print(f"[llm] WARNING: {run_path} has cell={result.cell!r}, "
              f"expected {cell!r} 鈥?skipping stale file")
        return None
    print(f"[llm] Loaded {len(result.pins)} pin classifications from {run_path}")
    return result


def load_llm_classifications(run_dir: Path, *, cell: str = "") -> dict[str, PinClassification]:
    """Backward-compat wrapper 鈥?returns name鈫扨inClassification dict."""
    r = load_llm_result(run_dir, cell=cell)
    if r:
        return build_classification_map(r)
    return {}


def classify_pin(pin: PinInfo, classifications: dict[str, PinClassification]) -> str:
    """Return pin type from LLM classifications, or heuristic fallback."""
    if pin.name in classifications:
        return classifications[pin.name].pin_type
    return classify_pin_heuristic(pin)


def create_run_dir() -> Path:
    """Create and return a timestamped simulator output directory.

    Returns path like ``${AMS_OUTPUT_ROOT}/simulation/20260430_153045/``.

    Also writes ``.latest_run`` under ``${AMS_OUTPUT_ROOT}/simulation`` so the skill
    can discover the current run directory without guessing.
    """
    return _create_sim_run_dir()


def log_skill_code(run_dir: Path, skill_path: str | Path) -> None:
    """Copy a skill file into ``run_dir/build/skill_code/`` for logging."""
    src = Path(skill_path)
    if not src.is_file():
        return
    dest_dir = run_dir / "build" / "skill_code"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)


# Spacing from DUT pin center to source/load center (schematic units)
_SRC_LOAD_OFFSET = 5.0
_LOAD_OFFSET = 8.0


@dataclass
class SimFlowResult:
    lib: str
    primary_cell: str
    tb_cell: str
    symbol_exported: bool
    redistributed: bool
    tb_created: bool
    dut_placed: bool
    pins: list[PinInfo]
    labels_added: list[str]
    sources_placed: list[str]
    sim_run_ok: Optional[bool] = None
    sim_verdict: Optional[str] = None
    run_dir: Optional[str] = None

    def save(self, run_dir: Path) -> None:
        """Serialize result to run_dir/result.json."""
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


@dataclass
class DutContext:
    """Result of symbol export: symbol generation, redistribution, and pin extraction.

    Symbol export ends after writing ``pin_info.json`` so pin intent can be authored.
    Pass this to later workflow steps after writing ``pin_classifications.json``
    to the run directory.

    ``tb_cell`` is always ``f"{primary_cell}_tb"`` 鈥?TB creation happens in
    the testbench build step.
    """
    lib: str
    primary_cell: str
    pins: list[PinInfo]
    run_dir: Path
    vdd_value: float
    symbol_exported: bool
    redistributed: bool

    @property
    def tb_cell(self) -> str:
        return f"{self.primary_cell}_tb"

    def save(self, run_dir: Path) -> None:
        """Serialize to run_dir/dut_context.json for cross-process use."""
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        (run_dir / "dut_context.json").write_text(text, encoding="utf-8")
        if os.getenv("SIM_IO_WRITE_LEGACY_PHASE_A") == "1":
            (run_dir / "phase_a_result.json").write_text(text, encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> DutContext:
        """Load from a DUT context checkpoint file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.pop("tb_cell", None)  # compat: tb_cell is now a property
        data["run_dir"] = Path(data["run_dir"])
        data["pins"] = [PinInfo(**p) for p in data["pins"]]
        return cls(**data)


# Backward-compatible type alias for older callers.
PhaseAResult = DutContext


def dut_context_path(run_dir: Path) -> Path:
    """Return the preferred DUT context path for a run directory."""
    return run_dir / "dut_context.json"


def load_dut_context(run_dir: Path) -> DutContext:
    """Load DUT context, accepting the legacy checkpoint name as fallback."""
    preferred = run_dir / "dut_context.json"
    if preferred.exists():
        return DutContext.load(preferred)
    legacy = run_dir / "phase_a_result.json"
    if legacy.exists():
        return DutContext.load(legacy)
    raise FileNotFoundError(
        f"DUT context not found in {run_dir} "
        "(expected dut_context.json; legacy phase_a_result.json also accepted)"
    )


# 鈹€鈹€ Step 1: Export Symbol 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def export_symbol(client: VirtuosoClient, lib: str, cell: str) -> bool:
    """Generate symbol view from schematic via TSG pipeline.

    Returns True if symbol was created (or already existed).
    """
    # Check if symbol already exists
    r = skill_exec(client, f'ddGetObj("{lib}" "{cell}")~>views~>name',
                   context="export_symbol_check", fail_ok=True)
    views = re.findall(r'"([^"]+)"', r.output)
    if "symbol" in views:
        print(f"[step1] Symbol already exists: {lib}/{cell}/symbol")
        return True

    # Set geometric pin sorting (preserves schematic spatial layout)
    skill_exec(client, 'schSetEnv("ssgSortPins" "geometric")',
               context="export_symbol_set_sorting")

    # TSG two-call pipeline
    r = skill_exec(
        client,
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))',
        context="export_symbol_tsg", fail_ok=True,
    )
    if not r.ok:
        print(f"[step1] ERROR: TSG failed: {r.errors}")
        return False

    # Verify
    r = skill_exec(client, f'ddGetObj("{lib}" "{cell}")~>views~>name',
                   context="export_symbol_verify", fail_ok=True)
    views = re.findall(r'"([^"]+)"', r.output)
    ok = "symbol" in views
    print(f"[step1] Symbol export: {'OK' if ok else 'FAILED'} 鈥?views: {views}")
    return ok


# 鈹€鈹€ Step 2: Redistribute Symbol Pins 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def redistribute_symbol(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    run_dir: Path,
    *,
    debug: bool = False,
) -> bool:
    """Redistribute symbol pins on 2 sides (left=outer, right=CORE/duplicate).

    Sub-steps:
      2a. Regenerate symbol via TSG (fresh start)
      2b. Extract symbol info (rects, lines, labels, terminals)
      2c. Calculate new layout (body + pin positions) in pure Python
      2d. Apply layout via generated SKILL script

    Returns True if redistribution succeeded.
    """
    print(f"[step2] Redistributing symbol pins for {lib}/{cell}")
    build_dir = run_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    # 2a: Fresh TSG 鈥?delete old symbol and regenerate
    skill_exec(client, f'ddDeleteCellView("{lib}" "{cell}" "symbol")',
               context="redistribute_delete_symbol", fail_ok=True)
    skill_exec(client, 'schSetEnv("ssgSortPins" "geometric")',
               context="redistribute_set_sorting")
    r = skill_exec(
        client,
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))',
        context="redistribute_tsg", fail_ok=True,
    )
    if not r.ok:
        print(f"[step2a] ERROR: TSG failed: {r.errors}")
        return False
    print(f"[step2a] Fresh TSG: OK")

    # 2b: Extract symbol info
    load_r = client.load_il(str(SKILL_DIR / "extract_symbol_info.il"))
    if not load_r.ok:
        print(f"[step2b] ERROR loading extractor: {load_r.errors}")
        return False
    r = skill_exec(client, f'extractSymbolInfo("{lib}" "{cell}")',
                   timeout=60, context="redistribute_extract", fail_ok=True)
    if not r.ok:
        print(f"[step2b] ERROR extracting: {r.errors}")
        return False

    info = parse_symbol_info(r.output)
    print(f"[step2b] Extracted: {len(info.rects)} rects, {len(info.lines)} lines, "
          f"{len(info.labels)} labels, {len(info.terminals)} terminals")

    # Save raw extraction data
    (build_dir / "extract_raw.txt").write_text(r.output or "", encoding="utf-8")

    # 2c: Calculate layout
    engine = LayoutEngine(LayoutConfig())
    result = engine.redesign(info)
    body = result.body
    for side_name in ["left", "right"]:
        count = sum(1 for p in result.pins if p.side.value == side_name)
        print(f"[step2c] {side_name}: {count} pins")

    # Save layout result (debug only)
    if debug:
        layout_data = {
            "lib": lib, "cell": cell,
            "body": {k: v for k, v in asdict(body).items()},
            "pins": [{k: (v.value if isinstance(v, Side) else v)
                      for k, v in asdict(p).items()} for p in result.pins],
        }
        (build_dir / "layout_result.json").write_text(
            json.dumps(layout_data, indent=2), encoding="utf-8"
        )

    # 2d: Apply layout
    skill_code = generate_apply_skill(lib, cell, result, engine.config)
    apply_il = build_dir / "apply_layout.il"
    apply_il.write_text(skill_code, encoding="utf-8")

    load_r = client.load_il(str(apply_il), timeout=120)
    if not load_r.ok:
        print(f"[step2d] ERROR: {load_r.errors}")
        return False
    print(f"[step2d] Layout applied: OK")

    # Verify
    sym = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" nil "r")'
    r = skill_exec(client, f'{sym}~>bBox', context="redistribute_verify_bbox", fail_ok=True)
    print(f"[step2] Verify bBox: {r.output}")
    r = skill_exec(client, f'length({sym}~>terminals)', context="redistribute_verify_terms", fail_ok=True)
    print(f"[step2] Verify terminals: {r.output}")

    return True


# 鈹€鈹€ Symbol Editing Ops 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def symbol_move_pin(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    pin_name: str,
    x: float,
    y: float,
    side: str = "left",
) -> bool:
    """Move one symbol pin (rect + wire + label) to a new (x, y) position.

    Must call after TSG has generated the symbol view.  The wire stub
    is re-oriented to extend outward from the body based on ``side``
    (one of "left"/"right"/"top"/"bottom").
    """
    client.load_il(str(SKILL_DIR / "symbol_move_pin.il"))
    r = skill_exec(
        client,
        f'symbolMovePin("{lib}" "{cell}" "{pin_name}" {x:g} {y:g} "{side}")',
        context=f"symbol_move_pin_{pin_name}", fail_ok=True,
    )
    if not r.ok:
        print(f"[symbol_move_pin] ERROR: {r.errors}")
        return False
    ok = "MOVE-PIN" in r.output
    if ok:
        print(f"[symbol_move_pin] {pin_name} -> ({x:.3f}, {y:.3f})")
    else:
        print(f"[symbol_move_pin] FAILED for {pin_name}")
    return ok


# 鈹€鈹€ Step 3: Create TB Cellview 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def create_tb_cellview(client: VirtuosoClient, lib: str, primary_cell: str) -> str:
    """Create a new schematic cellview named {primary_cell}_tb.

    Returns the tb cell name.
    """
    tb_cell = f"{primary_cell}_tb"

    # Create fresh (mode "w") 鈥?overwrites if exists
    r = skill_exec(
        client,
        f'dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "w")',
        context="create_tb_cellview", fail_ok=True,
    )
    if not r.output or r.output.strip().lower() == "nil":
        raise RuntimeError(
            f"Failed to create {lib}/{tb_cell}/schematic 鈥?"
            "cellview may be open in GUI (close it first)"
        )

    # Save the empty cellview
    skill_exec(
        client,
        f'dbSave(dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a"))',
        context="create_tb_cellview_save",
    )
    print(f"[step3] Created: {lib}/{tb_cell}/schematic")
    return tb_cell


# 鈹€鈹€ Step 4a: Place DUT Instance 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def place_dut(client: VirtuosoClient, lib: str, tb_cell: str, primary_cell: str) -> bool:
    """Place the primary cell's symbol as DUT instance in _tb schematic."""
    ops = [create_inst(lib, primary_cell, "symbol", "DUT", 2.5, 0.0, "R0")]
    batch_ops(client, lib, tb_cell, ops)
    print(f"[step4a] DUT placed: OK")
    return True


# 鈹€鈹€ Step 4b: Extract DUT Pin Info 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def extract_dut_pins(client: VirtuosoClient, lib: str, primary_cell: str) -> list[PinInfo]:
    """Extract all pin info from the symbol view of the primary cell.

    Queries terminal names, directions, and positions from the symbol cellview.
    Determines pin side by distance to bBox edges (same logic as layout engine).
    CORE pins get their side flipped so TB wires point inward (toward DUT center).
    """
    sym_cv = f'dbOpenCellViewByType("{lib}" "{primary_cell}" "symbol" nil "r")'

    # Get terminal names
    r_names = skill_exec(client, f'{sym_cv}~>terminals~>name',
                         context="extract_pins_names", fail_ok=True)
    names = re.findall(r'"([^"]+)"', r_names.output)
    if not names:
        print(f"[step4b] ERROR: No terminals found in {lib}/{primary_cell}/symbol")
        return []

    # Get terminal directions
    r_dirs = skill_exec(client, f'{sym_cv}~>terminals~>direction',
                        context="extract_pins_dirs", fail_ok=True)
    directions = re.findall(r'"([^"]+)"', r_dirs.output)

    # Get symbol bBox edges for side classification
    r_bbox = skill_exec(client, f'{sym_cv}~>bBox',
                        context="extract_pins_bbox", fail_ok=True)
    bbox_match = re.findall(r'[-\d.]+', r_bbox.output)
    if len(bbox_match) >= 4:
        body_L = float(bbox_match[0])
        body_B = float(bbox_match[1])
        body_R = float(bbox_match[2])
        body_T = float(bbox_match[3])
    else:
        body_L, body_B, body_R, body_T = -1.0, -1.0, 1.0, 1.0

    pins = []
    for i, name in enumerate(names):
        direction = directions[i] if i < len(directions) else "inputOutput"

        # Get first pin figure bBox (handles multi-pin terminals like VSS)
        r_pin = skill_exec(
            client,
            f'car(car(nth({i} {sym_cv}~>terminals)~>pins)~>figs)~>bBox',
            context=f"extract_pin_{name}", fail_ok=True,
        )
        pin_match = re.findall(r'[-\d.]+', r_pin.output)
        if len(pin_match) >= 4:
            px = (float(pin_match[0]) + float(pin_match[2])) / 2.0
            py = (float(pin_match[1]) + float(pin_match[3])) / 2.0
        else:
            px, py = 0.0, 0.0

        # After redistribution, all pins are on left/right only.
        # CORE pins always go right; others by x-position relative to body center.
        if name.endswith("_CORE"):
            side = "right"
        else:
            body_cx = (body_L + body_R) / 2.0
            side = "left" if px < body_cx else "right"

        pins.append(PinInfo(name=name, direction=direction, x=px, y=py, side=side))

    print(f"[step4b] Extracted {len(pins)} pins from {lib}/{primary_cell}/symbol")
    return pins


# 鈹€鈹€ Step 4c: Add Wire + Label (label-based wiring) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def add_wire_labels(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    *,
    result: ClassificationResult | None = None,
) -> list[str]:
    """Add labeled wire stubs on each DUT instance terminal.

    For digital_io_output _CORE pins, uses the shared net _DIG_OUT_CORE_NET
    so that one shared vpulse (placed later) connects to all of them.
    All other pins keep their original pin name as the net label.

    Returns list of net names that were labeled.
    """
    # Build the set of left-side digital_io_output pin names
    dig_out_names: set[str] = set()
    if result:
        for pc in result.pins:
            if pc.device_class == "digital_io_output":
                dig_out_names.add(pc.name)

    labeled_nets = []
    cfg = SIDE_CONFIGS
    ops = []

    for pin in pins:
        # Right-side _CORE counterpart of a digital output 鈫?shared net
        base = pin.name[:-5] if pin.name.endswith("_CORE") else None
        if pin.side == "right" and base in dig_out_names:
            net_name = _DIG_OUT_CORE_NET
        else:
            net_name = pin.name
        labeled_nets.append(net_name)
        ops.append(label_term_directed(
            "DUT", pin.name, net_name,
            stub_direction=pin.side,
            extension_length=abs(cfg[pin.side]["extend_x"]),
            rotation=cfg[pin.side]["label_rotation"],
            justification=cfg[pin.side]["label_align"],
        ))

    batch_ops(client, lib, tb_cell, ops)
    print(f"[step4c] Added {len(labeled_nets)} wire stubs on DUT pins: OK")
    return labeled_nets


# 鈹€鈹€ Step 4d: Place Sources & Loads 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _pin_position_in_tb(
    pin: PinInfo, dut_xy: tuple[float, float]
) -> tuple[float, float]:
    """Convert symbol-coordinate pin position to tb-schematic coordinate."""
    return (dut_xy[0] + pin.x, dut_xy[1] + pin.y)


def _source_load_position(
    px: float, py: float, side: str, offset: float = _SRC_LOAD_OFFSET,
) -> tuple[float, float]:
    """Return (x, y) for a source/load placed offset um outward from a pin.

    Only supports left/right sides (all pins are on left/right after redistribution).
    """
    if side == "left":
        return (px - offset, py)
    if side == "right":
        return (px + offset, py)
    raise ValueError(f"_source_load_position: unexpected side={side!r}, expected 'left' or 'right'")


def _resolve_param_value(value: str, vdd_value: float) -> str:
    """Replace VDD placeholder with the actual voltage value.

    Handles:
      "VDD" 鈫?str(vdd_value)
      "VDD/2" 鈫?computed division
    Other values are returned as-is.
    """
    if "VDD" not in value:
        return value
    result = value.replace("VDD", str(vdd_value))
    # Evaluate simple division like "0.9/2"
    if "/" in result:
        parts = result.split("/")
        if len(parts) == 2:
            try:
                val = float(parts[0]) / float(parts[1])
                return f"{val:g}"
            except (ValueError, ZeroDivisionError):
                pass
    return result


def _find_core_pin_name(pin_name: str, right_pins: dict[str, PinInfo]) -> str:
    """Find the corresponding right-side (CORE) pin name for a left-side pin.

    Search order:
      1. {pin_name}_CORE in right_pins
      2. Same name in right_pins (duplicate pin)
      3. Default to {pin_name}_CORE
    """
    core_name = f"{pin_name}_CORE"
    if core_name in right_pins:
        return core_name
    if pin_name in right_pins:
        return pin_name
    return core_name


def _set_cdf_params(
    lib: str,
    tb_cell: str,
    inst_name: str,
    params: dict,
    vdd_value: float,
    *,
    client: VirtuosoClient,
    resolve_vdd: bool = False,
) -> None:
    """Set CDF parameters on an instance via setInstParams SKILL function."""
    if not params:
        return
    pairs = []
    for key, val in params.items():
        pairs.append(f'"{key}"')
        resolved = _resolve_param_value(val, vdd_value) if resolve_vdd else str(val)
        pairs.append(f'"{resolved}"')
    skill = (
        f'setInstParams("{lib}" "{tb_cell}" "{inst_name}" '
        f"list({' '.join(pairs)}))"
    )
    r = client.execute_skill(skill, timeout=30)
    if r.errors or "error" in (r.output or "").lower():
        print(f"[step4d] WARNING: CDF params failed for {inst_name}: {r.errors or r.output}")



def _persist_cdf_overrides(
    lib: str,
    tb_cell: str,
    left_pins: list,
    classifications: dict,
    ground_pin_pvss: list[str],
    client: VirtuosoClient,
) -> None:
    """Persist instance-level CDF overrides to disk via cdfSaveForm."""
    inst_names: list[str] = []
    for pin_name in ground_pin_pvss:
        inst_names.append(f"PVSS_{pin_name}")
    for pin in left_pins:
        cls = classifications.get(pin.name)
        if cls:
            dc = cls.device_class
            if dc == "analog_ground" or (dc is None and cls.pin_type in ("ground", "no_connect")):
                continue
            if cls.stimulus and cls.stimulus_params:
                inst_names.append(f"SRC_{pin.name}")
            if cls.load and cls.load_params:
                inst_names.append(f"LOAD_{pin.name}")
            if cls.inner_stimulus and cls.inner_params:
                inst_names.append(f"INNER_{pin.name}")
            if cls.inner_load and cls.inner_load_params:
                inst_names.append(f"INNER_LOAD_{pin.name}")
    from sim_io.pin_types import PAD_RULES, classify_pin_heuristic
    for pin in left_pins:
        cls = classifications.get(pin.name)
        if not cls or cls.device_class:
            continue
        pad_type = classify_pin_heuristic(pin)
        rule = PAD_RULES.get(pad_type)
        if rule:
            for role in ("source", "load"):
                cfg = rule.get(role)
                if cfg:
                    inst_names.append(f"{"SRC" if role == "source" else "LOAD"}_{pin.name}")
    for name in inst_names:
        client.execute_skill(
            f'cdfSaveForm("{lib}" "{tb_cell}" "{name}")',
            timeout=10,
        )
    client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a") dbSave(cv) t)',
        timeout=30,
    )
    print(f"[step4d] Persisted CDF overrides for {len(inst_names)} instances")

def place_sources_and_loads(
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    *,
    classifications: dict[str, PinClassification] | None = None,
    result: ClassificationResult | None = None,
    dut_xy: tuple[float, float] = (2.5, 0.0),
    vdd_value: float = 1.8,
    client: Optional[VirtuosoClient] = None,
) -> list[str]:
    """Place sources, loads, PVSS devices, and inner devices based on pin classification.

    Dual-side topology:
      Phase 0: Collect ground pins 鈫?one PVSS per ground pin
      Phase 1: Place analogLib/gnd (defines GND net = gnd!) + PVSS devices
      Phase 2: Place outer devices (left side) using AI classification
      Phase 3: Place inner devices (right side) for CORE/duplicate pins
      Phase 4: Set CDF parameters for all instances

    Label convention:
      - DUT pin labels always use the original pin name (GIOL, GND_DAT, 鈥?
      - PVSS PLUS uses the ground pin name (matches DUT pin for connectivity)
      - PVSS MINUS and all fallback ground connections use "GND" (= gnd! via analogLib/gnd)
      - Source/load MINUS uses the primary ground pin name for the domain

    Falls back to PAD_RULES when no LLM classification is available.

    Returns list of instance names placed.
    """
    client.load_il(str(SKILL_DIR / "set_inst_params.il"))
    placed: list[str] = []
    ops: list[str] = []
    classifications = classifications or {}

    # Convenience lookups
    right_pins = {p.name: p for p in pins if p.side == "right"}
    left_pins  = [p for p in pins if p.side == "left"]
    left_by_name = {p.name: p for p in left_pins}

    # digital_low_gnd: MINUS for digital IO input inner caps and shared output vpulse
    dig_low_gnd = (result.digital_low_gnd if result else "") or _GND_NET

    # 鈹€鈹€ Phase 0: Collect analog ground pins for PVSS placement 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # Only analog_ground (device_class) or legacy pin_type=="ground" without
    # a digital device_class go through PVSS treatment in Phase 1.
    # Digital supply grounds (dig_consumer_ground, dig_provider_ground) get their own
    # outer vdc in Phase 2 and are NOT added here.
    ground_pin_pvss: list[str] = []
    for pin in left_pins:
        cls = classifications.get(pin.name)
        if cls:
            dc = cls.device_class
            is_analog_gnd = (
                dc == "analog_ground" or
                (dc is None and cls.pin_type == "ground")
            )
            if is_analog_gnd and pin.name not in ground_pin_pvss:
                ground_pin_pvss.append(pin.name)
        elif classify_pin_heuristic(pin) == "ground":
            if pin.name not in ground_pin_pvss:
                ground_pin_pvss.append(pin.name)

    # 鈹€鈹€ Phase 1: analogLib/gnd + one PVSS per analog ground pin 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # All source/load MINUS terminals use "gnd!" directly (Virtuoso global ground).
    # The analogLib/gnd symbol is placed for visual reference only 鈥?it connects
    # to gnd! internally; no terminal labeling needed.
    min_pin_y   = min((p.y for p in pins), default=-5.0)
    pvss_base_y = dut_xy[1] + min_pin_y - 4.0
    pvss_x_start = dut_xy[0] - 4.0
    pvss_spacing = 2.5

    gnd_sym_x = pvss_x_start - pvss_spacing
    ops.append(create_inst("analogLib", "gnd", "symbol", "GND_SYM",
                           gnd_sym_x, pvss_base_y, "R0"))
    placed.append("GND_SYM")

    for i, pin_name in enumerate(sorted(ground_pin_pvss)):
        px = pvss_x_start + i * pvss_spacing
        pvss_name = f"PVSS_{pin_name}"
        ops.append(create_inst("analogLib", "vdc", "symbol", pvss_name, px, pvss_base_y, "R0"))
        placed.append(pvss_name)
        ops.append(label_term(pvss_name, "PLUS", pin_name))
        ops.append(label_term(pvss_name, "MINUS", _GND_NET))

    # 鈹€鈹€ Phase 2: Outer devices (left side) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    for pin in left_pins:
        cls = classifications.get(pin.name)
        px, py = _pin_position_in_tb(pin, dut_xy)

        if cls:
            dc = cls.device_class

            # analog_ground handled by Phase 1; skip here
            if dc == "analog_ground" or (dc is None and cls.pin_type in ("ground", "no_connect")):
                continue
            # analog_input -> outer vdc (DC bias), supports AC via CDF acmag/phase
            # The vsource is placed with dc=VDD/2 by default; CDF params
            # from pin_classifications (e.g. {"dc": "0.9", "acmag": "1"})
            # override this after placement.
            if dc == "analog_input":
                inst_name = f"SRC_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                ops.append(create_inst("analogLib", "vdc", "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", _GND_NET))
                # Source params from classification; fall back to VDD/2 DC bias
                if not cls.stimulus_params:
                    cls.stimulus = "vdc"
                    cls.stimulus_params = {"dc": "VDD/2"}
                continue

            # analog_output -> outer cap load
            if dc == "analog_output":
                inst_name = f"LOAD_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                ops.append(create_inst("analogLib", "cap", "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", _GND_NET))
                # Load params from classification; fall back to 1pF
                if not cls.load_params:
                    cls.load = "cap"
                    cls.load_params = {"c": "1p"}
                continue

            # digital_io_output 鈫?outer cap, PLUS=pin, MINUS=GND
            if dc == "digital_io_output":
                inst_name = f"LOAD_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                ops.append(create_inst("analogLib", "cap", "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", _GND_NET))
                continue

            # Outer stimulus
            if cls.stimulus:
                inst_name = f"SRC_{pin.name}"
                sx, sy = _source_load_position(px, py, "left")
                ops.append(create_inst("analogLib", cls.stimulus, "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                # analog_current idc is INVERTED: PLUS=GND, MINUS=pin
                if dc == "analog_current":
                    ops.append(label_term(inst_name, "PLUS", _GND_NET))
                    ops.append(label_term(inst_name, "MINUS", pin.name))
                else:
                    ops.append(label_term(inst_name, "PLUS", pin.name))
                    ops.append(label_term(inst_name, "MINUS", _GND_NET))

            # Outer load (legacy bidirectional or explicit load field)
            if cls.load:
                inst_name = f"LOAD_{pin.name}"
                sx, sy = _source_load_position(px, py, "left", offset=_LOAD_OFFSET)
                ops.append(create_inst("analogLib", cls.load, "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pin.name))
                ops.append(label_term(inst_name, "MINUS", _GND_NET))

        else:
            # Fallback: PAD_RULES heuristic
            pad_type = classify_pin_heuristic(pin)
            if pad_type == "ground":
                continue
            rule = PAD_RULES.get(pad_type)
            if not rule:
                continue
            has_both = "source" in rule and "load" in rule
            for role in ("source", "load"):
                cfg = rule.get(role)
                if not cfg:
                    continue
                inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
                offset = _LOAD_OFFSET if (has_both and role == "load") else _SRC_LOAD_OFFSET
                sx, sy = _source_load_position(px, py, "left", offset=offset)
                ops.append(create_inst(cfg["lib"], cfg["cell"], "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, cfg["term"], pin.name))
                ops.append(label_term(inst_name, cfg["ref_term"], _GND_NET))

    # 鈹€鈹€ Phase 3: Inner devices 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # analog_power   -> inner idc, MINUS = PVSS inner pin
    # analog_current -> inner vdc, MINUS = PVSS inner pin
    # analog_input   -> no inner device (single-ended input; inner pin noConn via LLM)
    # analog_output  -> no inner device (single-ended output; inner pin load via LLM)
    # digital_io_input -> inner cap (10pF), MINUS = dig_low_gnd
    # digital_io_output -> skipped (shared vpulse in Phase 3b)
    # all others -> no inner device

    def _inner_pin_name(base: str) -> str:
        """Resolve the inner (right-side) pin name for a given base pin name.

        Priority:
          1. {base}_CORE exists in right_pins 鈫?"{base}_CORE"
          2. {base} exists in right_pins (duplicate) 鈫?"{base}"
          3. Neither exists 鈫?"{base}" (label matches the left-side net; duplicate
             semantics 鈥?both outer/inner on the same net, device still placed)
        """
        if f"{base}_CORE" in right_pins:
            return f"{base}_CORE"
        return base  # covers both right-side duplicate and no-right-side fallback

    for pin in left_pins:
        cls = classifications.get(pin.name)
        dc  = cls.device_class if cls else None

        core_pin_name = _inner_pin_name(pin.name)
        core_pin = right_pins.get(core_pin_name)
        if core_pin:
            cpx, cpy = _pin_position_in_tb(core_pin, dut_xy)
        else:
            # No right-side pin at all (true duplicate on left only) 鈥?place inner
            # device offset from the left-side pin, pointing rightward
            cpx = dut_xy[0] + abs(pin.x) + 1.5
            cpy = dut_xy[1] + pin.y
        sx, sy = _source_load_position(cpx, cpy, "right")

        if cls and dc == "digital_io_input":
            inst_name = f"INNER_{pin.name}"
            ops.append(create_inst("analogLib", "cap", "symbol",
                                   inst_name, sx, sy, "R90"))
            placed.append(inst_name)
            ops.append(label_term(inst_name, "PLUS", core_pin_name))
            ops.append(label_term(inst_name, "MINUS", dig_low_gnd))

        elif cls and dc in ("analog_power", "analog_current") and cls.inner_stimulus:
            # MINUS = inner pin of the local PVSS device
            pvss_inner = _inner_pin_name(cls.local_pvss) if cls.local_pvss else _GND_NET
            inst_name = f"INNER_{pin.name}"
            ops.append(create_inst("analogLib", cls.inner_stimulus, "symbol",
                                   inst_name, sx, sy, "R90"))
            placed.append(inst_name)
            ops.append(label_term(inst_name, "PLUS", core_pin_name))
            ops.append(label_term(inst_name, "MINUS", pvss_inner))

        elif cls and cls.inner_stimulus and dc not in (
            "digital_io_output", "analog_ground",
            "dig_consumer_power", "dig_consumer_ground", "dig_provider_power", "dig_provider_ground",
        ):
            # Legacy / other inner devices
            gnet = cls.local_pvss or cls.ground_net
            inner_minus = _inner_pin_name(gnet) if gnet else _GND_NET
            inst_name = f"INNER_{pin.name}"
            if cls.inner_stimulus == "noConn":
                ops.append(create_inst("analogLib", "noConn", "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", core_pin_name))
            else:
                ops.append(create_inst("analogLib", cls.inner_stimulus, "symbol",
                                       inst_name, sx, sy, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", core_pin_name))
                ops.append(label_term(inst_name, "MINUS", inner_minus))
            if cls.inner_load:
                load_inst = f"INNER_LOAD_{pin.name}"
                lx, ly = _source_load_position(cpx, cpy, "right", offset=_LOAD_OFFSET)
                ops.append(create_inst("analogLib", cls.inner_load, "symbol",
                                       load_inst, lx, ly, "R90"))
                placed.append(load_inst)
                ops.append(label_term(load_inst, "PLUS", core_pin_name))
                ops.append(label_term(load_inst, "MINUS", inner_minus))

    # 鈹€鈹€ Phase 3b: Shared digital output vpulse 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # One vpulse drives ALL digital output _CORE pins via the shared net.
    dig_out_core_pins = [
        right_pins[_find_core_pin_name(pin.name, right_pins)]
        for pin in left_pins
        if (classifications.get(pin.name) and
            classifications[pin.name].device_class == "digital_io_output" and
            _find_core_pin_name(pin.name, right_pins) in right_pins)
    ]
    if dig_out_core_pins and result and result.shared_output_vpulse:
        mid = dig_out_core_pins[len(dig_out_core_pins) // 2]
        cpx, cpy = _pin_position_in_tb(mid, dut_xy)
        sx, sy = _source_load_position(cpx, cpy, "right")
        ops.append(create_inst("analogLib", "vpulse", "symbol",
                               "INNER_DIG_OUT", sx, sy, "R90"))
        placed.append("INNER_DIG_OUT")
        ops.append(label_term("INNER_DIG_OUT", "PLUS", _DIG_OUT_CORE_NET))
        ops.append(label_term("INNER_DIG_OUT", "MINUS", dig_low_gnd))

    # 鈹€鈹€ Phase 4: Digital supply current sources 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # One idc between each (power_pin_net, ground_pin_net) pair.
    if result and result.digital_supply_pairs:
        for pair in result.digital_supply_pairs:
            pwr = pair.get("power", "")
            gnd = pair.get("ground", "")
            if not pwr or not gnd:
                continue
            pwr_pin = left_by_name.get(pwr)
            gnd_pin = left_by_name.get(gnd)
            if pwr_pin and gnd_pin:
                ppx, ppy = _pin_position_in_tb(pwr_pin, dut_xy)
                gpx, gpy = _pin_position_in_tb(gnd_pin, dut_xy)
                idc_x = ppx - _SRC_LOAD_OFFSET * 2.0
                idc_y = (ppy + gpy) / 2.0
                inst_name = f"ISUPPLY_{pwr}"
                ops.append(create_inst("analogLib", "idc", "symbol",
                                       inst_name, idc_x, idc_y, "R90"))
                placed.append(inst_name)
                ops.append(label_term(inst_name, "PLUS", pwr))
                ops.append(label_term(inst_name, "MINUS", gnd))

    batch_ops(client, lib, tb_cell, ops, timeout=120)

    # 鈹€鈹€ Phase 5: CDF parameters 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # PVSS devices (analog ground) 鈥?vdc ~0 but not exactly 0 to avoid
    # schematic warning (zero-voltage source shorted to ground).
    for pin_name in ground_pin_pvss:
        _set_cdf_params(lib, tb_cell, f"PVSS_{pin_name}", {"vdc": "0.013"}, vdd_value, client=client)

    for pin in left_pins:
        cls = classifications.get(pin.name)
        if cls:
            dc = cls.device_class
            if dc == "analog_ground" or (dc is None and cls.pin_type in ("ground", "no_connect")):
                continue
            if dc == "digital_io_output":
                lp = cls.load_params or {"c": "10p"}
                _set_cdf_params(lib, tb_cell, f"LOAD_{pin.name}", lp, vdd_value, client=client)
                continue
            if cls.stimulus and cls.stimulus_params:
                _set_cdf_params(lib, tb_cell, f"SRC_{pin.name}",
                                cls.stimulus_params, vdd_value, client=client, resolve_vdd=True)
            if cls.load and cls.load_params:
                _set_cdf_params(lib, tb_cell, f"LOAD_{pin.name}",
                                cls.load_params, vdd_value, client=client, resolve_vdd=True)
            if dc == "digital_io_input":
                _set_cdf_params(lib, tb_cell, f"INNER_{pin.name}",
                                {"c": "10p"}, vdd_value, client=client)
            elif cls.inner_stimulus and cls.inner_params and cls.inner_stimulus != "noConn":
                _set_cdf_params(lib, tb_cell, f"INNER_{pin.name}",
                                cls.inner_params, vdd_value, client=client, resolve_vdd=True)
            if cls.inner_load and cls.inner_load_params:
                _set_cdf_params(lib, tb_cell, f"INNER_LOAD_{pin.name}",
                                cls.inner_load_params, vdd_value, client=client, resolve_vdd=True)
        else:
            pad_type = classify_pin_heuristic(pin)
            if pad_type == "ground":
                continue
            rule = PAD_RULES.get(pad_type)
            if not rule:
                continue
            for role in ("source", "load"):
                cfg = rule.get(role)
                if cfg and cfg.get("params"):
                    inst_name = f"{'SRC' if role == 'source' else 'LOAD'}_{pin.name}"
                    _set_cdf_params(lib, tb_cell, inst_name,
                                    cfg["params"], vdd_value, client=client, resolve_vdd=True)

    if result and result.shared_output_vpulse and "INNER_DIG_OUT" in placed:
        _set_cdf_params(lib, tb_cell, "INNER_DIG_OUT",
                        result.shared_output_vpulse, vdd_value, client=client, resolve_vdd=True)

    if result and result.digital_supply_pairs:
        for pair in result.digital_supply_pairs:
            pwr = pair.get("power", "")
            inst_name = f"ISUPPLY_{pwr}"
            if pwr and inst_name in placed:
                _set_cdf_params(lib, tb_cell, inst_name,
                                {"idc": pair.get("idc", "5m")}, vdd_value, client=client)

    # Final check-and-save: setInstParams modifies CDF without calling schCheck,
    # which causes OSSHNL-109 "modified since last extraction" on netlist generation.
    client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a") '
        f'schCheck(cv) dbSave(cv) t)',
        timeout=30,
    )
    _persist_cdf_overrides(lib, tb_cell, left_pins, classifications, ground_pin_pvss, client)

    n_pvss  = len(ground_pin_pvss) + 1
    n_outer = sum(1 for p in placed if p.startswith(("SRC_", "LOAD_", "ISUPPLY_")))
    n_inner = sum(1 for p in placed if p.startswith("INNER_"))
    print(f"[step4d] Placed {n_pvss} PVSS + {n_outer} outer + {n_inner} inner: OK")
    return placed
