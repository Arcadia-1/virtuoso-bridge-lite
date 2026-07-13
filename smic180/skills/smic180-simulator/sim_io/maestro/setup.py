"""Maestro setup builder — SimDeckConfig → Maestro API calls.

Converts SIM-IO's SimDeckConfig into a sequence of Maestro SKILL calls
that create a fully configured simulation setup.  Uses background-mode
sessions (maeOpenSetup) for configuration — no GUI window required.

Typical usage::

    session = build_maestro_setup(client, lib, tb_cell, config)
    # ... run simulation ...
    close_session(client, session)

Or let build_maestro_setup manage the session lifecycle itself
(pass auto_close=True) and re-open for simulation later.
"""

from __future__ import annotations


from pathlib import Path
from typing import Optional

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    open_session,
    close_session,
    create_test,
    set_analysis,
    add_output,
    set_spec,
    set_var,
    set_env_option,
    set_sim_option,
    save_setup,
    set_current_run_mode,
    setup_corner,
)

# Default spec limits for IO ring validation
_DEFAULT_I_MAX = 0.1       # 100 mA max quiescent current per power pin

from sim_io.sim.config import (
    SimDeckConfig,
    ModelInclude,
    AnalysisSpec,
    SweepSpec,
    DesignVar,
    SimOptions,
    OutputExpression,
    PinMeasurement,
    SaveSignal,
)
from sim_io.pin_types import PinInfo, classify_pin_heuristic


# ── Ensure Maestro View ────────────────────────────────────────

def ensure_maestro_view(client: VirtuosoClient, lib: str, cell: str) -> str:
    """Bootstrap the ``maestro`` cellview if it doesn't exist on disk.

    Freshly created testbench cells have no ``maestro`` view.  Calling
    ``deOpenCellView`` on a missing view pops a blocking dialog, but
    ``maeOpenSetup`` creates it in memory.  We open a background session,
    save to disk — idempotent if the view already exists.

    Returns the session string so callers can reuse it instead of
    opening a second session (avoids ASSEMBLER-8127 lock conflicts).

    Must be called BEFORE ``open_gui_session``; not needed before
    ``open_session`` (background mode handles missing views).
    """
    # maeOpenSetup creates the session regardless — attaches to
    # existing view or creates a new one in memory.
    r = client.execute_skill(
        f'maeOpenSetup("{lib}" "{cell}" "maestro")', timeout=60
    )
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(
            f"maeOpenSetup failed for {lib}/{cell}: {r.errors}"
        )
    session = r.output.strip().strip('"')

    # Flush to disk — without this the view doesn't persist for
    # future open_gui_session calls.
    r = client.execute_skill(
        f'maeSaveSetup(?session "{session}")', timeout=30
    )
    if r.errors:
        # Don't fail — the save may fail if the view already exists
        # and is unchanged.  The session is still valid.
        print(f"[maestro] WARNING: maeSaveSetup in ensure_maestro_view: {r.errors}")

    return session


# ── Auto-Generate Outputs ───────────────────────────────────────

_DIGITAL_PIN_TYPES = frozenset({
    "digital_input", "digital_output", "digital_bidirectional",
    "clock", "reset",
})

_SKIP_PIN_TYPES = frozenset({"ground", "no_connect", "reference", "bias_current"})
_DIG_OUT_CORE_NET = "DIG_OUT_CORE"


def _measurement_net_name(
    pin_name: str,
    pins: list[PinInfo],
    classifications: dict[str, "PinClassification"] | None = None,
) -> str:
    pin_by_name = {p.name: p for p in pins or []}
    pin = pin_by_name.get(pin_name)
    if pin and pin.side == "right" and pin_name.endswith("_CORE"):
        base = pin_name[:-5]
        cls = classifications.get(base) if classifications else None
        if cls and cls.pin_type == "digital_output":
            return _DIG_OUT_CORE_NET
    return pin_name


def _auto_generate_outputs(
    client: VirtuosoClient,
    tname: str,
    pins: list[PinInfo],
    vdd_value: float,
    session: str,
    classifications: dict[str, "PinClassification"] | None = None,
) -> list[str]:
    """Add voltage + current + power outputs with spec boundaries.

    Per-pin outputs based on classification:
      - power: voltage net + current expression + power expression + specs
      - digital/clock/reset: voltage net + spec boundaries
      - analog: voltage net (no spec — context-dependent)
      - ground/no_connect/reference/bias_current: skipped

    Returns list of source instance names whose branch currents need
    to be saved (e.g. ["SRC_VDD"]) so the caller can add save signals.
    """
    n_voltage = 0
    n_current = 0
    n_power = 0
    current_sources: list[str] = []

    for pin in pins:
        # Use LLM classification if available, else heuristic
        if classifications and pin.name in classifications:
            cls = classifications[pin.name]
            pad_type = cls.pin_type
            ground_net = cls.ground_net or "--GND"
        else:
            pad_type = classify_pin_heuristic(pin)
            ground_net = "--GND"

        if pad_type in _SKIP_PIN_TYPES:
            continue

        # Use actual net name, not pin name
        # For ground pins already skipped, remaining pins use pin.name as net
        sig_path = f"/{pin.name}"

        # 1. Voltage output — all non-ground pins
        add_output(client, pin.name, tname,
                   output_type="net", signal_name=sig_path,
                   session=session)
        n_voltage += 1

        # 2. Voltage spec boundaries for digital pins
        #    set_spec on a waveform (net) checks ALL time points — wrong for
        #    digital signals that transition.  Instead, add scalar OCEAN
        #    expressions (ymax/ymin) and set spec on those.
        if pad_type in _DIGITAL_PIN_TYPES:
            # Use ?result "tran" so ymax/ymin only evaluate on transient
            # waveform data.  Without this, DC sweep returns a scalar and
            # ymax(nil) causes "_ymaxMethod: can't handle" eval errors.
            vmax_name = f"vmax_{pin.name}"
            vmax_expr = _escape_ocean_expr(f'ymax(v("{sig_path}" ?result "tran"))')
            add_output(client, vmax_name, tname,
                       output_type="point", expr=vmax_expr,
                       session=session)
            set_spec(client, vmax_name, tname,
                     gt=str(round(0.9 * vdd_value, 4)),
                     session=session)

            vmin_name = f"vmin_{pin.name}"
            vmin_expr = _escape_ocean_expr(f'ymin(v("{sig_path}" ?result "tran"))')
            add_output(client, vmin_name, tname,
                       output_type="point", expr=vmin_expr,
                       session=session)
            set_spec(client, vmin_name, tname,
                     lt=str(round(0.1 * vdd_value, 4)),
                     session=session)

        # 3. Power pin: current + power expressions with specs.
        # Only for left-side (outer) pins that have a SRC_ device placed.
        # Right-side (CORE) power pins have no SRC_ device in the netlist.
        if pad_type == "power" and getattr(pin, "side", "left") == "left":
            src_name = f"SRC_{pin.name}"
            current_sources.append(src_name)

            # Current through VDD source
            i_name = f"I_{pin.name}"
            # average(abs()) reduces the tran waveform to a scalar — required
            # for output_type="point". Terminal :p = PLUS (Spectre PSF lowercase).
            i_expr = _escape_ocean_expr(
                f'average(abs(i("{src_name}:p" ?result "tran")))'
            )
            add_output(client, i_name, tname,
                       output_type="point", expr=i_expr,
                       session=session)
            set_spec(client, i_name, tname,
                     gt="0",
                     session=session)
            set_spec(client, i_name, tname,
                     lt=str(_DEFAULT_I_MAX),
                     session=session)
            n_current += 1

            # Average power = mean(|V × I|) over tran.
            # Both v() and i() need ?result "tran" to avoid multi-analysis mismatch.
            p_name = f"P_{pin.name}"
            p_expr = _escape_ocean_expr(
                f'average(abs(v("{sig_path}" ?result "tran") * i("{src_name}:p" ?result "tran")))'
            )
            add_output(client, p_name, tname,
                       output_type="point", expr=p_expr,
                       session=session)
            set_spec(client, p_name, tname,
                     gt="0",
                     session=session)
            n_power += 1

    print(f"[maestro] Auto-generated outputs: {n_voltage} voltage, "
          f"{n_current} current, {n_power} power")
    return current_sources


# ── Build Outputs from LLM Measurement Intent ───────────────────

def _build_outputs_from_measurements(
    client: VirtuosoClient,
    tname: str,
    config: SimDeckConfig,
    pins: list[PinInfo],
    vdd_value: float,
    session: str,
    classifications: dict[str, "PinClassification"] | None = None,
) -> list[str]:
    """Translate LLM pin_measurements into Maestro output declarations.

    The LLM specifies WHAT to measure (voltage, current, power, custom)
    and spec constraints.  This function generates the correct OCEAN
    expressions, eval_types, and save signals — the LLM never writes
    OCEAN syntax directly.

    Returns list of source instance names whose branch currents need
    to be saved (e.g. ["SRC_VDD"]).
    """
    n_voltage = 0
    n_current = 0
    n_power = 0
    n_custom = 0
    current_sources: list[str] = []
    measured_pins = set(config.pin_measurements.keys())

    for pin_name, pm in config.pin_measurements.items():
        if not pm.measures:
            continue

        net_name = _measurement_net_name(pin_name, pins, classifications)
        sig_path = f"/{net_name}"

        # 1. Voltage net — always added when any measurement is requested
        add_output(client, pin_name, tname,
                   output_type="net", signal_name=sig_path,
                   session=session)
        n_voltage += 1

        # 2. Voltage scalar outputs (vmax/vmin) for any pin with "voltage"
        #    measurement.  Spec boundaries are optional — without them the
        #    scalar is still useful for measurements.json and verify.
        if "voltage" in pm.measures:
            if "vmax_above" in pm.spec:
                vmax_name = f"vmax_{pin_name}"
                vmax_expr = _escape_ocean_expr(f'ymax(v("{sig_path}" ?result "tran"))')
                add_output(client, vmax_name, tname,
                           output_type="point", expr=vmax_expr,
                           session=session)
                threshold = pm.spec["vmax_above"].replace("VDD", str(vdd_value))
                set_spec(client, vmax_name, tname,
                         gt=threshold, session=session)

            if "vmin_below" in pm.spec:
                vmin_name = f"vmin_{pin_name}"
                vmin_expr = _escape_ocean_expr(f'ymin(v("{sig_path}" ?result "tran"))')
                add_output(client, vmin_name, tname,
                           output_type="point", expr=vmin_expr,
                           session=session)
                threshold = pm.spec["vmin_below"].replace("VDD", str(vdd_value))
                set_spec(client, vmin_name, tname,
                         lt=threshold, session=session)

        # 3. Current through source
        if "current" in pm.measures:
            src_name = f"SRC_{pin_name}"
            current_sources.append(src_name)
            i_name = f"I_{pin_name}"
            i_expr = _escape_ocean_expr(
                f'average(abs(i("{src_name}:p" ?result "tran")))'
            )
            add_output(client, i_name, tname,
                       output_type="point", expr=i_expr,
                       session=session)
            set_spec(client, i_name, tname, gt="0", session=session)
            if "i_max" in pm.spec:
                set_spec(client, i_name, tname,
                         lt=pm.spec["i_max"], session=session)
            n_current += 1

        # 4. Power expression
        if "power" in pm.measures:
            src_name = f"SRC_{pin_name}"
            if src_name not in current_sources:
                current_sources.append(src_name)
            p_name = f"P_{pin_name}"
            p_expr = _escape_ocean_expr(
                f'average(abs(v("{sig_path}" ?result "tran") '
                f'* i("{src_name}:p" ?result "tran")))'
            )
            add_output(client, p_name, tname,
                       output_type="point", expr=p_expr,
                       session=session)
            set_spec(client, p_name, tname, gt="0", session=session)
            if "p_max" in pm.spec:
                set_spec(client, p_name, tname,
                         lt=pm.spec["p_max"], session=session)
            n_power += 1

        # 5. Custom expression
        if "custom" in pm.measures and pm.custom_expr:
            out_name = pm.custom_name or f"custom_{pin_name}"
            escaped = _escape_ocean_expr(pm.custom_expr)
            add_output(client, out_name, tname,
                       output_type="point", expr=escaped,
                       session=session)
            n_custom += 1

    # Add voltage net for unlisted non-ground pins (debug baseline)
    for pin in (pins or []):
        if pin.name in measured_pins:
            continue
        if classifications and pin.name in classifications:
            pad_type = classifications[pin.name].pin_type
        else:
            pad_type = classify_pin_heuristic(pin)
        if pad_type in _SKIP_PIN_TYPES:
            continue

        sig_path = f"/{pin.name}"
        add_output(client, pin.name, tname,
                   output_type="net", signal_name=sig_path,
                   session=session)
        n_voltage += 1

    print(f"[maestro] LLM-measurement outputs: {n_voltage} voltage, "
          f"{n_current} current, {n_power} power, {n_custom} custom")
    return current_sources


# ── Analysis Options Builder ───────────────────────────────────

def _build_tran_options(stop: str, errpreset: str = "",
                        extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for tran analysis options."""
    parts = [f'("stop" "{stop}")']
    if errpreset:
        parts.append(f'("errpreset" "{errpreset}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")"


def _build_dc_options(sweep: Optional[SweepSpec] = None,
                      extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for dc analysis options."""
    parts: list[str] = []
    if sweep and sweep.param:
        parts.append(f'("param" "{sweep.param}")')
    if sweep and sweep.start:
        parts.append(f'("start" "{sweep.start}")')
    if sweep and sweep.stop:
        parts.append(f'("stop" "{sweep.stop}")')
    if sweep and sweep.lin:
        parts.append(f'("lin" "{sweep.lin}")')
    if sweep and sweep.dec:
        parts.append(f'("dec" "{sweep.dec}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")" if parts else ""


def _build_ac_options(sweep: Optional[SweepSpec] = None,
                      extra: dict[str, str] | None = None) -> str:
    """Build SKILL alist string for ac analysis options.

    AC sweeps require incrType and stepTypeLog to specify logarithmic
    sweep correctly — these are not in SimDeckConfig and must be inferred.
    """
    parts: list[str] = []
    if sweep:
        if sweep.start:
            parts.append(f'("start" "{sweep.start}")')
        if sweep.stop:
            parts.append(f'("stop" "{sweep.stop}")')
        if sweep.dec:
            parts.append(f'("incrType" "Logarithmic")')
            parts.append(f'("stepTypeLog" "Points Per Decade")')
            parts.append(f'("dec" "{sweep.dec}")')
        elif sweep.lin:
            parts.append(f'("incrType" "Linear")')
            parts.append(f'("lin" "{sweep.lin}")')
    if extra:
        for k, v in extra.items():
            parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")" if parts else ""


def _build_analysis_options(a: AnalysisSpec) -> str:
    """Convert an AnalysisSpec into a Maestro options alist string."""
    if a.name == "tran":
        return _build_tran_options(
            stop=a.stop or "10u",
            errpreset=a.errpreset,
            extra=a.extra_options,
        )
    elif a.name == "dc":
        return _build_dc_options(sweep=a.sweep, extra=a.extra_options)
    elif a.name == "ac":
        return _build_ac_options(sweep=a.sweep, extra=a.extra_options)
    else:
        # Generic: dump extra_options as alist
        if a.extra_options:
            parts = [f'("{k}" "{v}")' for k, v in a.extra_options.items()]
            return "(" + " ".join(parts) + ")"
        return ""


# ── Simulator Options Alist ────────────────────────────────────

def _build_sim_option_alist(opts: SimOptions) -> str:
    """Convert SimOptions to Maestro simulator options alist string.

    All values MUST be strings — integer/float values silently fail.
    """
    parts = [
        f'("temp" "{opts.temp}")',
        f'("reltol" "{opts.reltol}")',
        f'("vabstol" "{opts.vabstol}")',
        f'("iabstol" "{opts.iabstol}")',
        f'("gmin" "{opts.gmin}")',
        f'("tnom" "{opts.tnom}")',
        f'("pivrel" "{opts.pivrel}")',
        # Force ASCII PSF output so openResults() can access waveforms
        # from the PSF directory after the simulation completes.
        '("format" "psfascii")',
    ]
    for k, v in opts.extra.items():
        parts.append(f'("{k}" "{v}")')
    return "(" + " ".join(parts) + ")"


# ── OCEAN Expression Escaping ──────────────────────────────────

def _escape_ocean_expr(expr: str) -> str:
    """Escape an OCEAN expression for embedding in a SKILL string.

    Maestro add_output(?expr "...") sends the expression as a SKILL
    string.  OCEAN expressions like V("/VOUT") contain double quotes
    that must be backslash-escaped inside the SKILL string, otherwise
    SKILL sees unmatched quotes and silently ignores the expression.

    Example::
        V("/VOUT")          →  V(\\"/VOUT\\")
        dB20(mag(VF("/OUT"))) → dB20(mag(VF(\\"/OUT\\")))

    The 06a_rc_create example uses this pattern:
        expr=r'bandwidth(mag(VF(\\"/OUT\\")) 3 \\"low\\")'
    """
    return expr.replace('"', '\\"')


# ── Signal Path Convention ─────────────────────────────────────

def _to_maestro_signal_path(signal: str) -> str:
    """Convert a signal name to Maestro's net path convention.

    SIM-IO's testbench uses label-based wiring: each DUT pin gets a
    net label with the pin name (e.g., "VDD", "D0").  These are
    top-level nets in the testbench, so Maestro references them as
    "/VDD", "/D0" — NOT "/DUT/VDD" (which would be the instance
    terminal path used by Spectre save statements).

    Handles input formats:
        "/DUT/VOUT"  → "/VOUT"   (strip DUT hierarchy — top-level net)
        "DUT.VOUT"   → "/VOUT"
        "/VOUT"      → "/VOUT"   (already correct)
        "VOUT"       → "/VOUT"   (bare name → top-level)
    """
    sig = signal.strip()
    # Strip leading /
    if sig.startswith("/"):
        sig = sig[1:]
    # Handle DUT.VOUT or DUT/VOUT → VOUT
    if sig.startswith("DUT.") or sig.startswith("DUT/"):
        sig = sig[4:]  # strip "DUT." or "DUT/"
    # Ensure leading /
    if not sig.startswith("/"):
        sig = "/" + sig
    return sig


# ── Main Builder ───────────────────────────────────────────────

def build_maestro_setup(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    config: SimDeckConfig,
    *,
    pins: list[PinInfo] | None = None,
    test_name: str = "",
    auto_close: bool = True,
    classifications: dict[str, "PinClassification"] | None = None,
) -> str:
    """Build a complete Maestro test setup from SimDeckConfig.

    Opens a background session, creates a test, configures analyses,
    variables, model files, simulator options, outputs, and saves.
    Returns the session string (or empty string if auto_close=True).

    If ``save_signals`` and ``outputs`` in config are both empty,
    auto-generates Maestro outputs from the ``pins`` list so that
    ``read_results`` returns meaningful data.

    Parameters
    ----------
    client : VirtuosoClient
    lib, tb_cell : Library and testbench cell names
    config : SimDeckConfig — the simulation configuration
    pins : Pin info list from Step 4b (for auto-generating outputs)
    test_name : Maestro test name (default: tb_cell + "_test")
    auto_close : if True, close session after setup (re-open later for sim)
    """
    tname = test_name or f"{tb_cell}_test"

    print(f"\n{'='*60}")
    print(f" Maestro Setup: {lib}/{tb_cell}")
    print(f" Test: {tname}")
    print(f"{'='*60}\n")

    # Delete existing maestro view so the new setup starts completely fresh.
    # Without this, re-runs accumulate stale outputs from prior configurations.
    # Use ddGetObj (disk lookup, no open required) + dbDeleteCellView.
    r_del = client.execute_skill(
        f'let((obj path) '
        f'obj = ddGetObj("{lib}" "{tb_cell}" "maestro") '
        f'when(obj '
        f'  path = obj~>readPath '
        f'  sh(strcat("rm -rf " path)) '
        f'  ddDeleteRep(obj)) t)',
        timeout=60,
    )
    if r_del.errors:
        print(f"[maestro] NOTE: maestro view delete: {r_del.errors} (ok if first run)")

    # Step 1: Ensure maestro view exists on disk + get session
    # Reuse the session from ensure_maestro_view to avoid opening
    # a second session (which can cause ASSEMBLER-8127 lock conflicts).
    session = ensure_maestro_view(client, lib, tb_cell)
    print(f"[maestro] Session: {session} (background, reused from ensure)")

    try:
        # Step 3: Create test — points to the testbench schematic
        create_test(client, tname, lib=lib, cell=tb_cell,
                    view="schematic", simulator="spectre", session=session)
        print(f"[maestro] Created test: {tname} → {lib}/{tb_cell}/schematic")

        # Step 4: Disable default analyses (Maestro creates tran by default)
        for default_a in ("tran", "dc", "ac"):
            set_analysis(client, tname, default_a, enable=False, session=session)

        # Step 5: Enable + configure requested analyses
        for a in config.analyses:
            if not a.enabled:
                continue
            options = _build_analysis_options(a)
            set_analysis(client, tname, a.name, enable=True,
                         options=options, session=session)
            sweep_info = ""
            if a.sweep and a.sweep.param:
                sweep_info = f" sweep={a.sweep.param}"
            print(f"[maestro] Analysis: {a.name}{sweep_info} "
                  f"stop={a.stop or '(sweep)'}")

        # Step 6: Design variables — CRITICAL: must set VDD for IO ring
        for v in config.design_vars:
            set_var(client, v.name, v.expression, session=session)
            print(f"[maestro] Variable: {v.name} = {v.expression}")

        # Step 7: Model files + save signals config
        # CRITICAL: simulation fails without model files.
        # Also set saveSignals — "allpub" saves public node voltages;
        # "all" also saves branch currents needed for i("SRC_xxx/PLUS")
        # current/power OCEAN expressions.
        env_parts: list[str] = []
        if config.model_includes:
            model_entries = []
            for mi in config.model_includes:
                section_str = f' "{mi.section}"' if mi.section else ' ""'
                model_entries.append(f'("{mi.path}"{section_str})')
            model_entries_str = " ".join(model_entries)
            env_parts.append(f'("modelFiles" ({model_entries_str}))')

        # Determine save mode: if any power pin exists (needs current data),
        # use "all" to include branch currents; otherwise "allpub" (voltages).
        has_power = any(
            (classifications and pin.name in classifications
             and classifications[pin.name].pin_type == "power")
            or (not classifications and classify_pin_heuristic(pin) == "power")
            for pin in (pins or [])
        )
        save_val = config.save_default or "allpub"
        if has_power and save_val == "allpub":
            save_val = "all"
        # Also upgrade if pin_measurements needs current/power
        needs_current = any(
            "current" in pm.measures or "power" in pm.measures
            for pm in config.pin_measurements.values()
        )
        if needs_current and save_val == "allpub":
            save_val = "all"
        env_parts.append(f'("saveSignals" "{save_val}")')

        # switchViewList: hierarchy traversal order for the netlister.
        # SMIC180 IO pad cells may have empty spectre views but may have
        # valid hspiceD views. Including hspiceD before spectre lets
        # the netlister use SPICE-format subcircuit definitions for
        # IO pads. Spectre can parse SPICE format via lang=spice.
        # Order: hspiceD (IO pad fallback) → spectre (standard) →
        # cmos_sch → schematic → veriloga
        env_parts.append(
            '("switchViewList" "hspiceD spectre cmos_sch schematic veriloga")'
        )

        # Force ASCII PSF output so openResults() can read waveforms after the run.
        # Without this Maestro stores results only in .rdb (binary) which is
        # inaccessible from standalone OCEAN functions like v() / getData().
        env_parts.append('("psfFormat" "psfascii")')

        env_alist = "(" + " ".join(env_parts) + ")"
        set_env_option(client, tname, env_alist, session=session)
        print(f"[maestro] Env options: {len(config.model_includes)} model includes, "
              f"save={save_val}")

        # Step 8: Simulator options
        sim_alist = _build_sim_option_alist(config.sim_options)
        set_sim_option(client, tname, sim_alist, session=session)
        print(f"[maestro] Sim options: temp={config.sim_options.temp}, "
              f"reltol={config.sim_options.reltol}")

        # Step 9: Add outputs
        # Priority: pin_measurements (LLM intent) > explicit outputs > auto-generate
        # Maestro only computes what's declared as outputs — unlike Spectre's
        # "save allpub", Maestro needs explicit output declarations.
        has_pin_measurements = bool(config.pin_measurements)
        has_explicit_outputs = bool(config.save_signals) or bool(config.outputs)

        if has_pin_measurements:
            # Priority 1: LLM measurement intent → code translates to Maestro outputs
            vdd = 1.8
            for v in config.design_vars:
                if v.name.upper() == "VDD":
                    try:
                        vdd = float(v.expression)
                    except ValueError:
                        pass
                    break
            current_sources = _build_outputs_from_measurements(
                client, tname, config, pins or [], vdd, session,
                classifications=classifications,
            )
            if current_sources:
                print(f"[maestro] Current save signals needed for: {current_sources}")
        elif has_explicit_outputs:
            # Priority 2: Explicit outputs from SimDeckConfig (legacy)
            for sig in config.save_signals:
                sig_path = _to_maestro_signal_path(sig.signal)
                out_name = sig_path.lstrip("/").replace("/", "_")
                add_output(client, out_name, tname,
                           output_type="net", signal_name=sig_path,
                           session=session)
                print(f"[maestro] Output (net): {out_name} → {sig_path}")

            for out in config.outputs:
                escaped_expr = _escape_ocean_expr(out.expression)
                add_output(client, out.name, tname,
                           output_type="point", expr=escaped_expr,
                           session=session)
                print(f"[maestro] Output (expr): {out.name}")
        else:
            # Priority 3: Auto-generate from pins + classification
            vdd = 1.8
            for v in config.design_vars:
                if v.name.upper() == "VDD":
                    try:
                        vdd = float(v.expression)
                    except ValueError:
                        pass
                    break
            current_sources = _auto_generate_outputs(
                client, tname, pins or [], vdd, session,
                classifications=classifications,
            )
            if current_sources:
                print(f"[maestro] Current save signals needed for: {current_sources}")

        # Step 10b: Configure corners (if requested via SimDeckConfig metadata)
        _setup_corners_if_requested(client, tname, config, session=session)

        # Step 11: Set run mode
        set_current_run_mode(
            client, "Single Run, Sweeps and Corners", session=session
        )

        # Step 12: Save to disk
        save_setup(client, lib, tb_cell, session=session)
        print(f"[maestro] Setup saved: {lib}/{tb_cell}/maestro")

        print(f"\n[maestro] Setup complete: {tname}")
        _print_setup_summary(config)

    except Exception:
        # Always try to save what we have before raising
        try:
            save_setup(client, lib, tb_cell, session=session)
        except Exception:
            pass
        if auto_close:
            try:
                close_session(client, session)
            except Exception:
                pass
        raise

    if auto_close:
        close_session(client, session)
        print(f"[maestro] Session closed (auto_close)")
        return ""

    return session



def _setup_corners_if_requested(
    client: VirtuosoClient,
    test_name: str,
    config: SimDeckConfig,
    *,
    session: str = "",
) -> list[str]:
    """Configure Maestro corners from SimDeckConfig metadata.

    Looks for a ``_corners`` key in config (set by the corner sweep script).
    For each non-tt corner, calls bridge-lite's ``setup_corner()`` which:
      1. Creates the corner (maeSetCorner)
      2. Attaches the model file + section
    Returns list of configured corner names.
    """
    corners = getattr(config, "_corners", None)
    if not corners:
        return []

    # Find the core model file (first non-empty-section ModelInclude)
    model_file = ""
    for mi in config.model_includes:
        if mi.path and mi.section:
            model_file = mi.path
            break

    configured = []
    for corner in corners:
        if corner == "tt":
            continue  # tt is the default, no explicit corner needed
        section = corner
        try:
            setup_corner(
                client, corner,
                model_file=model_file,
                model_section=section,
                session=session,
            )
            configured.append(corner)
            print(f"[maestro] Corner configured: {corner} (section={section})")
        except Exception as exc:
            print(f"[maestro] WARNING: Failed to configure corner {corner}: {exc}")

    if configured:
        print(f"[maestro] Configured {len(configured)} corners: {", ".join(configured)}")
    return configured


def teardown_maestro_setup(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    *,
    save: bool = True,
) -> None:
    """Close a Maestro background session, optionally saving first."""
    if save:
        try:
            save_setup(client, lib, cell, session=session)
        except Exception:
            pass
    close_session(client, session)


# ── IO Pad Model Discovery ─────────────────────────────────────

def discover_io_model_file(client: VirtuosoClient, io_lib: str = "tpd018bcdnv5") -> str:
    """Search the PDK for IO pad model include files.

    SMIC180 IO pad cells (tpd018bcdnv5) need their subcircuit
    definitions included separately. This function searches common
    PDK paths for the IO model file.

    Returns the first found model file path, or empty string.
    """
    # Get the PDK root from the library's physical path
    r = client.execute_skill(f'ddGetObj("{io_lib}")~>readPath', timeout=15)
    lib_path = (r.output or "").strip().strip('"')
    if not lib_path or lib_path == "nil":
        print(f"[maestro] Cannot find library path for {io_lib}")
        return ""

    # The IO library path typically looks like:
    # /home/IC/Tech/smic180/... (when IO pad library is installed)
    # We need to find the models directory relative to it.
    # Common patterns:
    #   {lib_path}/models/spectre/*.scs
    #   {lib_path}/../models/spectre/*.scs
    #   {lib_path}/../../models/spectre/io*.scs

    # Search candidate paths for spectre/spice model files
    search_patterns = [
        f'fileSearch("{lib_path}/models/spectre" "*.scs")',
        f'fileSearch("{lib_path}/../models/spectre" "*.scs")',
        f'fileSearch("{lib_path}/../../models/spectre" "*io*.scs")',
        f'fileSearch("{lib_path}" "*.scs")',
        f'fileSearch("{lib_path}" "*.spi")',
        f'fileSearch("{lib_path}/models/spice" "*.spi")',
        f'fileSearch("{lib_path}/../models/spice" "*.spi")',
    ]

    for pattern in search_patterns:
        try:
            r = client.execute_skill(pattern, timeout=15)
            output = (r.output or "").strip()
            if output and output != "nil":
                # Parse the file list and look for IO-related model files
                files = re.findall(r'"([^"]*\.(?:scs|spi|sp))"', output)
                for f in files:
                    lower = f.lower()
                    if any(kw in lower for kw in ("io", "pad", "gpio", "iopad")):
                        print(f"[maestro] Found IO model file: {f}")
                        return f
                # Return first file if no IO-specific match
                if files:
                    print(f"[maestro] Found candidate model file: {files[0]}")
                    return files[0]
        except Exception:
            continue

    print(f"[maestro] No IO model files found for {io_lib}")
    return ""


# ── Summary Printer ────────────────────────────────────────────

def _print_setup_summary(cfg: SimDeckConfig) -> None:
    print(f"\n{'='*60}")
    print(f" Maestro Setup Summary")
    print(f"{'='*60}")
    print(f"  Analyses:       {len([a for a in cfg.analyses if a.enabled])}")
    for a in cfg.analyses:
        if a.enabled:
            sw = f" sweep({a.sweep.param})" if a.sweep and a.sweep.param else ""
            print(f"    {a.name}: stop={a.stop or '?'}{sw}")
    print(f"  Design vars:    {len(cfg.design_vars)}")
    for v in cfg.design_vars:
        print(f"    {v.name} = {v.expression}")
    print(f"  Model includes: {len(cfg.model_includes)}")
    print(f"  Sim options:    temp={cfg.sim_options.temp}, "
          f"reltol={cfg.sim_options.reltol}")
    print(f"  Save signals:   {len(cfg.save_signals)}")
    print(f"  Output exprs:   {len(cfg.outputs)}")
    print(f"  Pin measurements: {len(cfg.pin_measurements)}")
    print(f"  Source:         {cfg.source or 'unknown'}")
    print(f"{'='*60}\n")
