"""
Simulation Deck Template — deck builder for Spectre.

Takes an si-exported netlist (circuit only) and appends:
  - global declaration
  - Design variables (parameters)
  - Model includes (multiple with sections)
  - Simulator options
  - Info statements
  - Analyses (multiple, with sweep params)
  - Save signals (specific or allpub)
  - Output expressions

Produces a complete, ready-to-run Spectre deck.
"""

from __future__ import annotations

from pathlib import Path

from sim_io.sim.config import SimDeckConfig


_SEPARATOR = "// === si-generated circuit netlist (DO NOT EDIT above this line) ==="


def build_sim_deck(
    netlist_text: str,
    config: SimDeckConfig,
) -> str:
    """Build a complete Spectre deck from si netlist + SimDeckConfig."""
    return _build_deck_from_deck_config(netlist_text, config)


def _build_deck_from_deck_config(
    netlist_text: str,
    cfg: SimDeckConfig,
) -> str:
    """Assemble a complete Spectre deck from netlist + SimDeckConfig.

    Deck order:
      1. si netlist (circuit only)
      2. global 0
      3. parameters VDD=1.8
      4. include "path" section=X (multiple)
      5. simulatorOptions options ...
      6. info what=X where=Y
      7. Analysis blocks (dc, tran, ac, ...)
      8. save signals
      9. saveOptions fallback
    """
    lines: list[str] = []

    # 1. Original netlist
    lines.append(netlist_text.rstrip())
    lines.append("")
    lines.append(_SEPARATOR)
    lines.append("")

    # 2. Global declaration
    lines.append("simulator lang=spectre")
    if cfg.global_ground:
        lines.append(f"global {cfg.global_ground}")
    lines.append("")

    # 3. Design variables
    if cfg.design_vars:
        params = " ".join(f"{v.name}={v.expression}" for v in cfg.design_vars)
        lines.append(f"parameters {params}")
        lines.append("")

    # 4. Model includes
    for mi in cfg.model_includes:
        is_spice = mi.path.endswith(".spi") or mi.path.endswith(".sp")
        is_hspice_lib = mi.path.endswith(".l")
        section_str = f" section={mi.section}" if mi.section else ""

        if is_hspice_lib:
            # HSPICE library format: .lib "path" section
            lines.append("simulator lang=spice")
            section_part = f" {mi.section}" if mi.section else ""
            lines.append(f'.lib "{mi.path}"{section_part}')
            lines.append("simulator lang=spectre")
        elif is_spice:
            # SPICE format: .include "path" (no section support)
            lines.append("simulator lang=spice")
            lines.append(f'.include "{mi.path}"')
            lines.append("simulator lang=spectre")
        else:
            # Spectre format: include "path" section=X
            lines.append(f'include "{mi.path}"{section_str}')
    if cfg.model_includes:
        lines.append("")

    # 5. Simulator options
    opts = cfg.sim_options
    opt_parts = [
        f"reltol={opts.reltol}",
        f"vabstol={opts.vabstol}",
        f"iabstol={opts.iabstol}",
        f"gmin={opts.gmin}",
        f"temp={opts.temp}",
        f"tnom={opts.tnom}",
        f"pivrel={opts.pivrel}",
    ]
    for k, v in opts.extra.items():
        opt_parts.append(f"{k}={v}")
    lines.append(f"simulatorOptions options {' '.join(opt_parts)}")
    lines.append("")

    # 6. Info statements
    for info in cfg.info_statements:
        lines.append(f"{info.info_type} info what={info.what} where={info.where}")
    if cfg.info_statements:
        lines.append("")

    # 7. Analyses
    for analysis in cfg.analyses:
        if not analysis.enabled:
            continue
        lines.append(_format_analysis(analysis))
    if cfg.analyses:
        lines.append("")

    # 8. Save signals (specific)
    if cfg.save_signals:
        for sig in cfg.save_signals:
            name = sig.signal.lstrip("/")
            lines.append(f"save {name}")
        lines.append("")

    # 9. Save options (fallback)
    if cfg.save_default:
        lines.append(f"saveOptions options save={cfg.save_default}")

    return "\n".join(lines) + "\n"


def _format_analysis(a) -> str:
    """Format a single AnalysisSpec into a Spectre analysis line."""
    if a.name == "tran":
        parts = [f"tran tran stop={a.stop}"]
        if a.errpreset:
            parts.append(f"errpreset={a.errpreset}")
        for k, v in a.extra_options.items():
            parts.append(f"{k}={v}")
        return " ".join(parts)

    if a.name == "dc":
        parts = ["dc dc"]
        if a.sweep:
            sw = a.sweep
            if sw.param:
                parts.append(f"param={sw.param}")
            if sw.start and sw.stop:
                parts.append(f"start={sw.start} stop={sw.stop}")
            if sw.lin:
                parts.append(f"lin={sw.lin}")
            if sw.dec:
                parts.append(f"dec={sw.dec}")
        for k, v in a.extra_options.items():
            parts.append(f"{k}={v}")
        return " ".join(parts)

    if a.name == "ac":
        parts = ["ac ac"]
        if a.sweep:
            sw = a.sweep
            if sw.start and sw.stop:
                parts.append(f"start={sw.start} stop={sw.stop}")
            if sw.dec:
                parts.append(f"dec={sw.dec}")
            elif sw.lin:
                parts.append(f"lin={sw.lin}")
        for k, v in a.extra_options.items():
            parts.append(f"{k}={v}")
        return " ".join(parts)

    if a.name == "noise":
        parts = ["noise noise"]
        stop = a.stop or a.extra_options.get("stop", "10G")
        dec = (a.sweep.dec if a.sweep and a.sweep.dec else a.extra_options.get("dec", "10"))
        parts.append(f"stop={stop}")
        parts.append(f"dec={dec}")
        for k, v in a.extra_options.items():
            if k not in ("stop", "dec"):
                parts.append(f"{k}={v}")
        return " ".join(parts)

    # Generic fallback
    return f"{a.name} {a.name}"


def build_sim_deck_from_file(
    netlist_path: str | Path,
    config: SimDeckConfig,
) -> str:
    """Read an si netlist file and build a complete Spectre deck."""
    netlist_text = Path(netlist_path).read_text(encoding="utf-8")
    return build_sim_deck(netlist_text, config)
