"""Spectre log regressions and bounded execution of queue-option commands."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from virtuoso_bridge.spectre import runner


SUCCESS_LOG = """Licensing Information:
License checkout succeeded.
spectre completes with 0 errors, 5 warnings, and 15 notices.
"""
LICENSE_FATAL = (
    "FATAL (SPECTRE-209): Cannot run the simulation because the required license "
    "could not be checked out."
)
CRASH_FATAL = "FATAL (SPECTRE-18): Segmentation fault."


def _result(stdout, *, stderr="", returncode=0, output_dir=None):
    return runner._build_simulation_result(
        runner._SpectreRunResult(True, output_dir, returncode, stdout, stderr, None, {}),
        output_format=None,
    )


def test_successful_license_checkout_has_no_errors():
    result = _result(SUCCESS_LOG)
    assert result.ok
    assert result.errors == []


@pytest.mark.parametrize("returncode", [0, 134])
def test_native_fatal_codes_and_wrapped_explanation_survive(returncode):
    result = _result(
        "Licensing Information:\nFATAL (SPECTRE-209):\n"
        "    Cannot run the simulation because the required license could not be checked out.\n",
        stderr=CRASH_FATAL, returncode=returncode,
    )
    assert not result.ok
    assert result.metadata["returncode"] == returncode
    details = "\n".join(result.errors)
    assert "SPECTRE-209" in details and CRASH_FATAL in details
    assert "could not be checked out" in details


def test_license_banner_does_not_reclassify_netlist_failure():
    diagnostic = "ERROR (SFE-23): instance M0 refers to an undefined model."
    result = _result("Licensing Information:\n" + diagnostic, returncode=1)
    assert not result.ok
    assert "license error" not in result.errors
    assert diagnostic in result.errors


@pytest.mark.parametrize("header", [
    "FATAL: Failed to initialize license API!",
    "lmStatus: ERROR (LMC-01902): License call failed.",
])
def test_license_api_diagnostic_preserves_wrapped_detail_with_zero_exit(header):
    continuation = "    The requested license feature is unavailable."
    result = _result(header + "\n" + continuation)
    assert not result.ok
    assert "license error" in result.errors
    assert header + "\n" + continuation in result.errors


@pytest.mark.parametrize("message", [
    "License checkout failed: feature unavailable.",
    "Error: cannot check out the required license.",
    "License request denied.",
])
def test_plain_license_failure_retains_reason(message):
    result = _result(message, returncode=1)
    assert not result.ok
    assert "license error" in result.errors
    assert message in result.errors


def test_success_after_license_wait_is_not_an_error():
    result = _result("License checkout failed; waiting for available license.\n" + SUCCESS_LOG)
    assert result.ok
    assert result.errors == []


def test_fatal_output_with_partial_files_still_fails(tmp_path):
    (tmp_path / "incomplete.raw").write_text("partial fixture")
    result = _result(CRASH_FATAL, output_dir=tmp_path)
    assert not result.ok
    assert result.status == runner.ExecutionStatus.PARTIAL
    assert CRASH_FATAL in result.errors


@pytest.fixture
def command_fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "load_vb_env", lambda: None)
    monkeypatch.setattr(runner.CadenceEnvironment, "from_env", lambda *args: runner.CadenceEnvironment(""))
    monkeypatch.setenv("VB_SPECTRE_HOST", "localhost")
    script = tmp_path / "fixture tool.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "raw = pathlib.Path(args[args.index('-raw') + 1])\n"
        "raw.mkdir(parents=True, exist_ok=True)\n"
        "(raw / 'argv.json').write_text(json.dumps(args))\n"
        f"print({SUCCESS_LOG!r})\n"
    )
    netlist = tmp_path / "input deck.scs"
    netlist.write_text("simulator lang=spectre\n")
    return shlex.join([sys.executable, str(script)]), netlist


@pytest.mark.parametrize("factory", ["local", "from_env", "remote"])
@pytest.mark.parametrize("queue, explicit, expected", [
    ("default", [], ["900"]),
    (None, [], []),
    (0, [], ["0"]),
    (12, [], ["12"]),
    (None, ["+lqtimeout", "7"], ["7"]),
    (12, ["+lqtimeout", "7"], ["7"]),
])
def test_queue_option_reaches_real_process(command_fixture, tmp_path, factory, queue, explicit, expected):
    if factory == "remote" and (os.name == "nt" or not shutil.which("sh")):
        pytest.skip("generated remote command requires a POSIX shell")
    command, netlist = command_fixture
    options = dict(spectre_cmd=command, work_dir=tmp_path / "output", output_format=None, timeout=5)
    if queue != "default":
        options["license_queue_timeout"] = queue
    if factory == "remote":
        from tests.test_cadence_env import ShellRunner
        sim = runner.SpectreSimulator(
            remote_host="fixture-host", ssh_runner=ShellRunner(),
            remote_work_dir=str(tmp_path / "remote work"), **options,
        )
    else:
        sim = getattr(runner.SpectreSimulator, factory)(**options)
    result = sim.run_simulation(netlist, {"spectre_args": explicit})
    assert result.ok, result.errors
    assert result.errors == []
    args = json.loads((Path(result.metadata["output_dir"]) / "argv.json").read_text())
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "+lqtimeout"] == expected


@pytest.mark.parametrize("value", [-1, True, 1.5, "900"])
def test_invalid_queue_timeout_rejected_before_execution(value):
    with pytest.raises(ValueError, match="license_queue_timeout"):
        runner.SpectreSimulator.local(license_queue_timeout=value)


@pytest.mark.parametrize("location", ["command", "constructor", "per_run"])
@pytest.mark.parametrize("flag", ["+lqtimeout", "+lqt"])
def test_explicit_queue_arguments_win(command_fixture, tmp_path, location, flag):
    command, netlist = command_fixture
    explicit = [flag, "7"]
    if location == "command":
        command += " " + shlex.join(explicit)
    sim = runner.SpectreSimulator.local(
        spectre_cmd=command, work_dir=tmp_path / "output", output_format=None,
        spectre_args=explicit if location == "constructor" else [],
        license_queue_timeout=12,
    )
    result = sim.run_simulation(netlist, {"spectre_args": explicit if location == "per_run" else []})
    assert result.ok, result.errors
    args = json.loads((Path(result.metadata["output_dir"]) / "argv.json").read_text())
    assert [args[i:i + 2] for i, arg in enumerate(args) if arg in ("+lqtimeout", "+lqt")] == [explicit]
