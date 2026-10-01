"""Write Maestro configuration: create tests, set analyses, outputs, corners, etc.

All functions take a session string and call mae* SKILL functions.
They return the raw SKILL output string.
"""

from __future__ import annotations

import logging
import time
import uuid

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError
from virtuoso_bridge.virtuoso.requests import RequestRecoveryError
from virtuoso_bridge.virtuoso.ops import escape_skill_string


logger = logging.getLogger(__name__)


def _q(client: VirtuosoClient, expr: str, timeout: float | None = None) -> str:
    kwargs = {"timeout": timeout} if timeout is not None else {}
    r = client.execute_skill(expr, **kwargs)
    if r.errors:
        if getattr(r, "metadata", {}).get("request_handle"):
            raise RequestRecoveryError(r)
        if getattr(r, "metadata", {}).get("dialog_guard"):
            raise DialogBlockedError(r)
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
    s = f' ?session "{escape_skill_string(session)}"' if session else ""
    parts = (
        f'maeAddOutput("{escape_skill_string(name)}" '
        f'"{escape_skill_string(test)}"'
    )
    if output_type:
        parts += f' ?outputType "{escape_skill_string(output_type)}"'
    if signal_name:
        parts += f' ?signalName "{escape_skill_string(signal_name)}"'
    if expr:
        parts += f' ?expr "{escape_skill_string(expr)}"'
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
                   callback: str = "", run_mode: str = "",
                   timeout: float | None = None) -> str:
    """maeRunSimulation — run simulation (async, returns immediately).

    Returns the history name (e.g. "Interactive.1").

    Args:
        session: session name (default: current session)
        callback: SKILL procedure name to call when run finishes
        run_mode: explicit Maestro run mode; empty preserves the Cadence default
        timeout: socket timeout for Maestro to accept the run request
    """
    parts = "maeRunSimulation("
    if session:
        parts += f'?session "{escape_skill_string(session)}" '
    if callback:
        parts += f'?callback "{escape_skill_string(callback)}" '
    if run_mode:
        parts += f'?runMode "{escape_skill_string(run_mode)}" '
    parts = parts.rstrip() + ")"
    return _q(client, parts, timeout=timeout)


def _remove_marker(runner, marker: str) -> None:
    """Delete the marker file in either local or remote mode."""
    if runner is None:
        from pathlib import Path as _Path
        try:
            _Path(marker).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    else:
        runner.run_command(f"rm -f {marker}", timeout=10)


def _wait_until_done(client: VirtuosoClient, marker: str,
                      timeout: float = 600) -> str:
    """Internal: poll the marker file written by run_and_wait's SKILL callback.

    Cadence-side ``maeRunSimulation(?callback ...)`` registers a SKILL
    callback that ``echo``s ``done`` to ``marker`` when the run finishes.
    Local mode reads the file directly via stdlib; remote mode ``ssh
    cat``s it every 2 s on the SSH channel, keeping the SKILL channel
    free for other work (read-only dialog inspection, ...). Shared mode checks
    blockers out-of-band at most once every ten seconds, without cancelling
    a simulation or waiting for the full completion timeout.

    Not public — call :func:`run_and_wait` instead, which sets up the
    callback + marker and calls this helper.
    """
    import time as _time
    from pathlib import Path as _Path

    runner = client.ssh_runner

    deadline = _time.monotonic() + timeout
    next_dialog_check = 0.0
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break

        if runner is None:
            mp = _Path(marker)
            if mp.exists():
                content = mp.read_text().strip()
                if content:
                    _remove_marker(runner, marker)
                    return content
        else:
            r = runner.run_command(
                f"cat {marker} 2>/dev/null",
                timeout=min(10.0, remaining),
            )
            if r.returncode == 0 and r.stdout.strip():
                _remove_marker(runner, marker)
                return r.stdout.strip()

        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break
        dialogs = getattr(client, "dialogs", None)
        if dialogs is not None and dialogs.enabled and _time.monotonic() >= next_dialog_check:
            blocked = dialogs.preflight(timeout=min(5.0, remaining))
            next_dialog_check = _time.monotonic() + 10.0
            if blocked is not None:
                blocked.errors = [
                    "Simulation completion wait stopped: shared CIW has a blocker "
                    "or cannot be inspected. Bridge did not request simulation cancellation."
                ]
                blocked.metadata.pop("request_sent", None)
                blocked.metadata.update(outcome="unknown", phase="completion_wait",
                                        completion_marker=marker)
                raise DialogBlockedError(blocked)
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
        _time.sleep(min(2.0, remaining))

    raise TimeoutError(f"Simulation did not finish within {timeout}s")


def _strip_skill_atom(raw: str) -> str:
    return (raw or "").strip().strip('"')


def run_and_wait(client: VirtuosoClient, *, session: str = "",
                 run_mode: str = "", timeout: int = 600) -> tuple[str, str]:
    """Run simulation and wait for completion without blocking SKILL.

    Uses maeRunSimulation(?callback ...) to register a completion callback
    atomically with the simulation start — no race condition possible.
    The callback writes a marker file; Python polls it via SSH.

    The SKILL channel remains free during the wait — you can still
    execute_skill, inspect dialogs, take screenshots, etc.

    ``timeout`` is an end-to-end budget covering the simulation-start request
    and completion polling. Returns (history, status) — e.g.
    ('"Interactive.3"', 'done').
    """
    deadline = time.monotonic() + timeout

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Simulation did not finish within {timeout}s")
        return remaining

    # runner is None in local mode (Virtuoso on the same host); _remove_marker
    # and _wait_until_done both handle that case via local fs operations.
    runner = client.ssh_runner

    nonce = uuid.uuid4().hex[:8]
    marker = f"/tmp/vb_sim_done_{nonce}"
    _remove_marker(runner, marker)

    # Define callback that writes marker file when simulation finishes.
    # Use system("echo ... > file") instead of outfile/fprintf to avoid
    # SKILL I/O buffering issues in callback context.
    try:
        _q(client, f'''
procedure(_vb_sim_done_{nonce}(session runID)
  system(sprintf(nil "echo done > {marker}"))
  printf("[%s sim done] run %L\\n" nth(2 parseString(getCurrentTime())) runID))
''', timeout=remaining_timeout())
    except RequestRecoveryError as exc:
        exc.result.metadata.update(maestro_phase="callback_setup", session=session,
                                   simulation_start_sent=False)
        raise

    try:
        # Start once: missing acknowledgement must not trigger a second run.
        history = run_simulation(client, session=session,
                                 callback=f"_vb_sim_done_{nonce}",
                                 run_mode=run_mode,
                                 timeout=remaining_timeout())
    except RequestRecoveryError as exc:
        exc.result.metadata.update(completion_marker=marker, session=session,
                                   maestro_phase="simulation_start",
                                   simulation_start_sent=exc.result.metadata.get("request_sent"))
        raise

    history_name = _strip_skill_atom(history)
    if not history_name or history_name == "nil":
        # Do not query the SKILL channel again: a user dialog may now block it.
        # Retain the callback marker so a late completion remains observable.
        message = (
            "maeRunSimulation returned nil (no history acknowledged). "
            "No form was dismissed and the simulation was not retried. "
            "Use read-only dialogs.inspect(pid=...) to diagnose blockers; "
            "verify run state before retrying. "
            f"session={session or '<current>'}, completion_marker={marker}"
        )
        dialogs = getattr(client, "dialogs", None)
        if dialogs is not None and dialogs.enabled:
            failure = VirtuosoResult(
                status=ExecutionStatus.ERROR, errors=[message],
                metadata={"completion_marker": marker, "session": session},
            )
            failure = dialogs.annotate_failure(
                failure, timeout=max(0, deadline - time.monotonic()),
            )
            raise DialogBlockedError(failure)
        raise RuntimeError(message)

    # Poll marker via SSH (SKILL channel stays free)
    try:
        status = _wait_until_done(client, marker, timeout=remaining_timeout())
    except DialogBlockedError as exc:
        exc.result.metadata.update(history=history_name, session=session,
                                   phase="completion_wait")
        raise
    return history, status


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def create_netlist_for_corner(client: VirtuosoClient, test: str,
                              corner: str, output_dir: str, *,
                              session: str = "") -> str:
    """maeCreateNetlistForCorner — export standalone netlist for a corner.

    If *session* is omitted, Cadence uses the current Maestro session.
    """
    s = f' ?session "{session}"' if session else ""
    return _q(client,
        f'maeCreateNetlistForCorner("{test}" "{corner}" "{output_dir}"{s})')


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
