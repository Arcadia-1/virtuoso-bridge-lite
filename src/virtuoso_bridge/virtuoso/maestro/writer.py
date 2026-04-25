"""Write Maestro configuration: create tests, set analyses, outputs, corners, etc.

All functions take a session string and call mae* SKILL functions.
They return the raw SKILL output string.
"""

import logging

from virtuoso_bridge import VirtuosoClient


logger = logging.getLogger(__name__)


def _q(client: VirtuosoClient, expr: str, timeout: int | None = None) -> str:
    kwargs = {"timeout": timeout} if timeout is not None else {}
    r = client.execute_skill(expr, **kwargs)
    if r.errors:
        raise RuntimeError(f"SKILL error: {r.errors[0]}")
    return r.output or ""


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def create_test(client: VirtuosoClient, test: str, *,
                lib: str, cell: str, view: str = "schematic",
                simulator: str = "spectre", session: str = "") -> str:
    """maeCreateTest — create a new test."""
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeCreateTest("{test}" ?lib "{lib}" ?cell "{cell}" '
        f'?view "{view}" ?simulator "{simulator}"{s})')


def set_design(client: VirtuosoClient, test: str, *,
               lib: str, cell: str, view: str = "schematic",
               session: str = "") -> str:
    """maeSetDesign — change the DUT for an existing test."""
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeSetDesign("{test}" "{lib}" "{cell}" "{view}"{s})')


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def set_analysis(client: VirtuosoClient, test: str, analysis: str, *,
                 enable: bool = True, options: str = "", session: str = "") -> str:
    """maeSetAnalysis — enable/disable an analysis and set its options.

    options: SKILL alist string, e.g. '(("start" "1") ("stop" "10G") ("dec" "20"))'
    """
    s = f' ?session "{session}"' if session else ""
    en = "t" if enable else "nil"
    opts = f" ?options `{options}" if options else ""
    return _q(client,
        f'maeSetAnalysis("{test}" "{analysis}" ?enable {en}{opts}{s})')


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def add_output(client: VirtuosoClient, name: str, test: str, *,
               output_type: str = "", signal_name: str = "",
               expr: str = "", session: str = "") -> str:
    """maeAddOutput — add an output (waveform or expression)."""
    s = f' ?session "{session}"' if session else ""
    parts = f'maeAddOutput("{name}" "{test}"'
    if output_type:
        parts += f' ?outputType "{output_type}"'
    if signal_name:
        parts += f' ?signalName "{signal_name}"'
    if expr:
        parts += f' ?expr "{expr}"'
    parts += f'{s})'
    return _q(client, parts)


def set_spec(client: VirtuosoClient, name: str, test: str, *,
             lt: str = "", gt: str = "", session: str = "") -> str:
    """maeSetSpec — set pass/fail spec on an output."""
    s = f' ?session "{session}"' if session else ""
    parts = f'maeSetSpec("{name}" "{test}"'
    if lt:
        parts += f' ?lt "{lt}"'
    if gt:
        parts += f' ?gt "{gt}"'
    parts += f'{s})'
    return _q(client, parts)


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def set_var(client: VirtuosoClient, name: str, value: str, *,
            type_name: str = "", type_value: str = "",
            session: str = "") -> str:
    """maeSetVar — set a design variable.

    Global:    set_var(client, "vdd", "1.35")
    Test-level: set_var(client, "f", "100M,2G,4G,8G",
                        type_name="test", type_value='("IB_PSS")')
    Corner:    set_var(client, "vdd", "1.2 1.4",
                       type_name="corner", type_value='("myCorner")')

    Note: if a test has a local variable, it overrides the global one.
    Use type_name="test" to set test-level variables directly.
    Comma-separated values create a parametric sweep.
    """
    s = f' ?session "{session}"' if session else ""
    parts = f'maeSetVar("{name}" "{value}"'
    if type_name:
        parts += f' ?typeName "{type_name}"'
    if type_value:
        parts += f" ?typeValue '{type_value}"
    parts += f'{s})'
    return _q(client, parts)


def get_var(client: VirtuosoClient, name: str, *, session: str = "") -> str:
    """maeGetVar — get the value of a design variable."""
    s = f' ?session "{session}"' if session else ""
    return _q(client, f'maeGetVar("{name}"{s})')


def delete_var(client: VirtuosoClient, name: str, *,
               test: str = "", session: str = "") -> str:
    """Delete a design variable using axl* API.

    Global:     delete_var(client, "f")
    Test-level: delete_var(client, "f", test="IB_PSS")

    Note: to delete a global variable, you must first delete it
    from all tests that have a local copy.
    """
    sess = session or _q(client, 'car(maeGetSessions())')
    if test:
        expr = (f'axlRemoveElement(axlGetVar('
                f'axlGetTest(axlGetMainSetupDB("{sess}") "{test}") "{name}"))')
    else:
        expr = (f'axlRemoveElement(axlGetVar('
                f'axlGetMainSetupDB("{sess}") "{name}"))')
    return _q(client, expr)


# ---------------------------------------------------------------------------
# Parameters (parametric sweep)
# ---------------------------------------------------------------------------

def get_parameter(client: VirtuosoClient, name: str, *,
                  type_name: str = "", type_value: str = "",
                  session: str = "") -> str:
    """maeGetParameter — get value of a parameter for a test or corner."""
    s = f' ?session "{session}"' if session else ""
    parts = f'maeGetParameter("{name}"'
    if type_name:
        parts += f' ?typeName "{type_name}"'
    if type_value:
        parts += f' ?typeValue `{type_value}'
    parts += f'{s})'
    return _q(client, parts)


def set_parameter(client: VirtuosoClient, name: str, value: str, *,
                  type_name: str = "", type_value: str = "",
                  session: str = "") -> str:
    """maeSetParameter — add or update a parameter at global or corner level.

    For global:  set_parameter(client, "cload", "1p")
    For corner:  set_parameter(client, "cload", "1p 2p",
                               type_name="corner", type_value='("myCorner")')
    """
    s = f' ?session "{session}"' if session else ""
    parts = f'maeSetParameter("{name}" "{value}"'
    if type_name:
        parts += f' ?typeName "{type_name}"'
    if type_value:
        parts += f' ?typeValue `{type_value}'
    parts += f'{s})'
    return _q(client, parts)


# ---------------------------------------------------------------------------
# Environment & Simulator Options
# ---------------------------------------------------------------------------

def set_env_option(client: VirtuosoClient, test: str, options: str, *,
                   session: str = "") -> str:
    """maeSetEnvOption — set environment options (model files, view list, etc.).

    options: SKILL alist string, e.g.
      '(("modelFiles" (("/path/model.scs" "tt"))))'
    """
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeSetEnvOption("{test}" ?options `{options}{s})')


def set_sim_option(client: VirtuosoClient, test: str, options: str, *,
                   session: str = "") -> str:
    """maeSetSimOption — set simulator options (reltol, temp, etc.).

    options: SKILL alist string, e.g.
      '(("temp" "85") ("reltol" "1e-5"))'
    """
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeSetSimOption("{test}" ?options `{options}{s})')


# ---------------------------------------------------------------------------
# Corners
# ---------------------------------------------------------------------------

def set_corner(client: VirtuosoClient, name: str, *,
               disable_tests: str = "", session: str = "") -> str:
    """maeSetCorner — create or modify a corner.

    disable_tests: SKILL list string, e.g. '("AC" "TRAN")'
    """
    s = f' ?session "{session}"' if session else ""
    dt = f' ?disableTests `{disable_tests}' if disable_tests else ""
    return _q(client, f'maeSetCorner("{name}"{dt}{s})')


def setup_corner(client: VirtuosoClient, name: str, *,
                 model_file: str = "", model_section: str = "",
                 variables: dict[str, str] | None = None,
                 session: str = "") -> str:
    """Convenience wrapper: create a corner + set its vars + attach a model file.

    Internally does three things any caller can also do manually:

      1. :func:`set_corner` — create the corner (``maeSetCorner``).
      2. For each entry in ``variables``: ``maeSetVar(?typeName "corner" ?typeValue …)``.
      3. ``axlGetCorner`` → ``axlPutModel`` → ``axlSetModelFile`` /
         ``axlSetModelSection`` — attach the model.

    Intentionally heavier than its peers — use it when you want a fully
    configured corner in one call.  For "just create an empty corner",
    call :func:`set_corner` directly.

    Args:
        name: Corner name, e.g. "tt_25"
        model_file: Path to model file, e.g. "/path/to/mypdk.scs"
        model_section: Model section name, e.g. "tt"
        variables: Corner-specific variables, e.g. {"temperature": "25", "vdd": "1.2"}
        session: Maestro session ID
    """
    s = f' ?session "{session}"' if session else ""

    # Create the corner
    set_corner(client, name, session=session)

    # Set corner-specific variables
    if variables:
        for var_name, var_value in variables.items():
            _q(client,
               f'maeSetVar("{var_name}" "{var_value}" '
               f'?typeName "corner" ?typeValue \'("{name}"){s})')

    # Set model file + section via axl* setup-DB API
    if model_file:
        sess_id = session or _q(client, "car(maeGetSessions())")
        model_name = model_file.rsplit("/", 1)[-1] if "/" in model_file else model_file
        expr = (
            f'let((sdb corn model) '
            f'sdb = axlGetMainSetupDB("{sess_id}") '
            f'corn = axlGetCorner(sdb "{name}") '
            f'model = axlPutModel(corn "{model_name}") '
            f'axlSetModelFile(model "{model_file}") '
            f'{f"""axlSetModelSection(model "{model_section}") """ if model_section else ""}'
            f'model)'
        )
        _q(client, expr)

    return name


def load_corners(client: VirtuosoClient, filepath: str, *,
                 sections: str = "corners",
                 operation: str = "overwrite") -> str:
    """maeLoadCorners — load corners from a CSV file."""
    return _q(client,
        f'maeLoadCorners("{filepath}" ?sections "{sections}" '
        f'?operation "{operation}")')


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def set_current_run_mode(client: VirtuosoClient, run_mode: str, *,
                         session: str = "") -> str:
    """maeSetCurrentRunMode — switch run mode.

    run_mode: e.g. "Single Run, Sweeps and Corners"
    """
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeSetCurrentRunMode(?runMode "{run_mode}"{s})')


def set_job_control_mode(client: VirtuosoClient, mode: str, *,
                         session: str = "") -> str:
    """maeSetJobControlMode — set job control mode (e.g. "Local", "LSCS")."""
    s = f' ?session "{session}"' if session else ""
    return _q(client, f'maeSetJobControlMode("{mode}"{s})')


def set_job_policy(client: VirtuosoClient, policy, *,
                   test_name: str = "", job_type: str = "",
                   session: str = "") -> str:
    """maeSetJobPolicy — set job policy for a test."""
    s = f' ?session "{session}"' if session else ""
    parts = f"maeSetJobPolicy({policy}"
    if test_name:
        parts += f' ?testName "{test_name}"'
    if job_type:
        parts += f' ?jobType "{job_type}"'
    parts += f'{s})'
    return _q(client, parts)


def run_simulation(client: VirtuosoClient, *, session: str = "",
                   callback: str = "") -> str:
    """maeRunSimulation — run simulation (async, returns immediately).

    Returns the history name (e.g. "Interactive.1").

    Args:
        session: session name (default: current session)
        callback: SKILL procedure name to call when run finishes
    """
    parts = "maeRunSimulation("
    if session:
        parts += f'?session "{session}" '
    if callback:
        parts += f'?callback "{callback}" '
    parts = parts.rstrip() + ")"
    return _q(client, parts)


def _wait_until_done(client: VirtuosoClient, marker: str,
                      timeout: int = 600) -> str:
    """Internal: poll the marker file written by run_and_wait's SKILL callback.

    Cadence-side ``maeRunSimulation(?callback ...)`` registers a SKILL
    callback that ``echo``s ``done`` to ``marker`` when the run finishes.
    This function ``ssh cat``s that marker every 2 s on the SSH channel,
    keeping the SKILL channel free for other work (dialog dismissal,
    screenshots, ...).

    Not public — call :func:`run_and_wait` instead, which sets up the
    callback + marker and calls this helper.
    """
    import time as _time

    runner = client.ssh_runner
    if runner is None:
        raise RuntimeError("No SSH connection (tunnel not started?)")

    runner = client.ssh_runner
    if runner is None:
        raise RuntimeError("No SSH connection (tunnel not started?)")

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        r = runner.run_command(f"cat {marker} 2>/dev/null", timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            runner.run_command(f"rm -f {marker}", timeout=10)
            return r.stdout.strip()
        _time.sleep(2)

    raise TimeoutError(f"Simulation did not finish within {timeout}s")


def _strip_skill_atom(raw: str) -> str:
    return (raw or "").strip().strip('"')


def _diagnose_run_not_started(client: VirtuosoClient, session: str) -> dict[str, str]:
    """Collect quick diagnostics when maeRunSimulation returns nil."""
    info: dict[str, str] = {
        "session": session or "",
        "test": "",
        "enabled_analyses": "",
        "is_explorer_window": "unknown",
        "current_form": "",
    }

    try:
        test = _strip_skill_atom(_q(client, f'car(maeGetSetup(?session "{session}"))'))
        if test and test != "nil":
            info["test"] = test
    except Exception:  # noqa: BLE001
        pass

    if info["test"]:
        try:
            # Best-effort probe: some older Virtuoso/Maestro environments may not
            # expose maeGetEnabledAnalysis, so keep diagnostics partial on failure.
            enabled = _q(
                client,
                f'maeGetEnabledAnalysis("{info["test"]}" ?session "{session}")',
            )
            info["enabled_analyses"] = (enabled or "").strip()
        except Exception:  # noqa: BLE001
            pass

    try:
        is_explorer = _q(
            client,
            'let((s) s = car(errset(sevSession(hiGetCurrentWindow()))) if(s then "t" else "nil"))',
        )
        info["is_explorer_window"] = _strip_skill_atom(is_explorer)
    except Exception:  # noqa: BLE001
        pass

    try:
        form = _q(client, 'let((f) f = hiGetCurrentForm() when(f f~>name))')
        info["current_form"] = _strip_skill_atom(form)
    except Exception:  # noqa: BLE001
        pass

    return info


def _try_recover_blocking_form(client: VirtuosoClient, info: dict[str, str]) -> bool:
    """Best-effort unblock if a modal form is active. Returns True if attempted."""
    form_name = info.get("current_form", "")
    if not form_name or form_name == "nil":
        return False

    try:
        # First try SKILL-side dismissal for current modal form.
        _q(client, 'let((f) f = hiGetCurrentForm() when(f hiFormDone(f)) t)')
    except Exception:  # noqa: BLE001
        pass

    try:
        # If SKILL channel is partially blocked, use X11 fallback.
        client.dismiss_dialog()
    except Exception:  # noqa: BLE001
        pass
    return True


def run_and_wait(client: VirtuosoClient, *, session: str = "",
                 timeout: int = 600) -> tuple[str, str]:
    """Run simulation and wait for completion without blocking SKILL.

    Uses maeRunSimulation(?callback ...) to register a completion callback
    atomically with the simulation start — no race condition possible.
    The callback writes a marker file; Python polls it via SSH.

    The SKILL channel remains free during the wait — you can still
    execute_skill, dismiss dialogs, take screenshots, etc.

    Returns (history, status) — e.g. ('"Interactive.3"', 'done').
    """
    import uuid

    runner = client.ssh_runner
    if runner is None:
        raise RuntimeError("No SSH connection (tunnel not started?)")

    nonce = uuid.uuid4().hex[:8]
    marker = f"/tmp/vb_sim_done_{nonce}"
    runner.run_command(f"rm -f {marker}", timeout=10)

    # Define callback that writes marker file when simulation finishes.
    # Use system("echo ... > file") instead of outfile/fprintf to avoid
    # SKILL I/O buffering issues in callback context.
    client.execute_skill(f'''
procedure(_vb_sim_done_{nonce}(session runID)
  system(sprintf(nil "echo done > {marker}"))
  printf("[%s sim done] run %L\\n" nth(2 parseString(getCurrentTime())) runID))
''')

    # Start simulation with callback — atomic, no race condition.
    # If Virtuoso returns nil here, no run was started and the callback
    # can never fire, so fail fast instead of entering endless marker polling.
    history = run_simulation(client, session=session,
                             callback=f"_vb_sim_done_{nonce}")
    history_name = _strip_skill_atom(history)
    if not history_name or history_name == "nil":
        info = _diagnose_run_not_started(client, session)
        recovered = _try_recover_blocking_form(client, info)

        # One retry after recovery if we had an active modal form.
        if recovered:
            history = run_simulation(client, session=session,
                                     callback=f"_vb_sim_done_{nonce}")
            history_name = _strip_skill_atom(history)

        if not history_name or history_name == "nil":
            runner.run_command(f"rm -f {marker}", timeout=10)
            test = info.get("test", "") or "<unknown>"
            analyses = info.get("enabled_analyses", "") or "<unknown>"
            explorer = info.get("is_explorer_window", "unknown")
            form = info.get("current_form", "") or "<none>"
            extra = (
                f"session={session}, test={test}, enabled_analyses={analyses}, "
                f"explorer_window={explorer}, current_form={form}."
            )
            logger.warning(
                "Simulation did not start after diagnostics/recovery attempt: %s",
                extra,
            )
            raise RuntimeError(
                "maeRunSimulation returned nil (simulation not started). "
                "If this is ADE Explorer, use Explorer run path "
                "(sevRun(sevSession(window))) instead of maeRunSimulation. "
                "Also verify at least one analysis is enabled and no modal dialog "
                "is blocking the GUI. " + extra
            )

    # Poll marker via SSH (SKILL channel stays free)
    status = _wait_until_done(client, marker, timeout=timeout)
    return history, status


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def create_netlist_for_corner(client: VirtuosoClient, test: str,
                              corner: str, output_dir: str) -> str:
    """maeCreateNetlistForCorner — export standalone netlist for a corner."""
    return _q(client,
        f'maeCreateNetlistForCorner("{test}" "{corner}" "{output_dir}")')


def export_output_view(client: VirtuosoClient, filepath: str, *,
                       view: str = "Detail") -> str:
    """maeExportOutputView — export results to CSV."""
    return _q(client,
        f'maeExportOutputView(?fileName "{filepath}" ?view "{view}")')


def write_script(client: VirtuosoClient, filepath: str) -> str:
    """maeWriteScript — export entire setup as reproducible SKILL script."""
    return _q(client, f'maeWriteScript("{filepath}")')


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_adel_to_maestro(client: VirtuosoClient, lib: str, cell: str,
                            state: str) -> str:
    """maeMigrateADELStateToMaestro — convert ADE L state to maestro view."""
    return _q(client,
        f'maeMigrateADELStateToMaestro("{lib}" "{cell}" "{state}")')


def migrate_adexl_to_maestro(client: VirtuosoClient, lib: str, cell: str,
                             view: str = "adexl", *,
                             maestro_view: str = "maestro") -> str:
    """maeMigrateADEXLToMaestro — convert ADE XL view to maestro view."""
    return _q(client,
        f'maeMigrateADEXLToMaestro("{lib}" "{cell}" "{view}" '
        f'?maestroView "{maestro_view}")')


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_setup(client: VirtuosoClient, lib: str, cell: str, *,
               session: str = "") -> str:
    """maeSaveSetup — save the maestro setup to disk."""
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro"{s})')


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def open_maestro_gui_with_history(client: VirtuosoClient, lib: str, cell: str,
                                  *, history: str = "") -> str:
    """Open Maestro GUI window and display a simulation history.

    If history is not given, auto-detects the latest from asiGetResultsDir.

    Steps:
        1. asiGetResultsDir → extract history name
        2. deOpenCellView → open GUI window (read mode)
        3. maeMakeEditable → switch to edit mode
        4. maeRestoreHistory → load history results into GUI
        5. maeSaveSetup → persist

    Returns the history name.
    """
    import re

    # Auto-detect history name
    if not history:
        r = client.execute_skill('asiGetResultsDir(asiGetCurrentSession())')
        rd = (r.output or "").strip('"')
        m = re.search(r'/maestro/results/maestro/([^/]+)/', rd)
        if not m:
            raise RuntimeError("No simulation history found")
        history = m.group(1)

    _q(client, f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "r")')
    _q(client, 'maeMakeEditable()')
    _q(client, f'maeRestoreHistory("{history}")')
    _q(client, f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro")')

    return history
