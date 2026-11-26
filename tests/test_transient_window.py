"""Synthetic offline tests: no OCEAN, SSH connection, simulator, or CIW."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from virtuoso_bridge.cadence_env import CadenceEnvironment
from virtuoso_bridge.spectre import waveform as wf
from virtuoso_bridge.transport.ssh import CommandResult, SSHRunner
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


PSF = "/synthetic/one-point/psf"
REMOTE = "/tmp/vb-waveform-Ab1234567890"
FULL = "C\t0\t3\t0\t1\nD\t0\t0\t2\nD\t0\t0.5\t3\nD\t0\t1\t4\nEND\n"
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("sh") or not shutil.which("timeout"),
    reason="requires native POSIX sh and GNU timeout; synthetic executable only",
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, request):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline waveform tests attempted real process/SSH/CIW access")

    if "native_posix" not in request.fixturenames:
        monkeypatch.setattr(wf.subprocess, "run", forbidden)
    monkeypatch.setattr(SSHRunner, "__init__", forbidden)
    monkeypatch.setattr(SSHClient, "from_env", forbidden)
    monkeypatch.setattr(VirtuosoClient, "__init__", forbidden)
    monkeypatch.setattr(VirtuosoClient, "execute_skill", forbidden)


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    # Keep production's actual temporary-directory lifecycle, inside the test root.
    factory = wf.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        wf, "tempfile",
        SimpleNamespace(TemporaryDirectory=lambda **kw: factory(dir=tmp_path, **kw)),
    )
    return tmp_path


class FakeRunner:
    def __init__(self, payload=FULL, *, failure=None, remote=REMOTE):
        self.payload = payload
        self.failure = failure
        self.remote = remote
        self.commands = []
        self.uploads = []
        self.downloads = []

    def run_command(self, command, timeout=None):
        self.commands.append((command, timeout))
        if "mktemp -d" in command:
            return CommandResult(0, self.remote + "\n", "")
        if command.startswith("rm -rf -- "):
            return CommandResult(0, "", "")
        if isinstance(self.failure, BaseException):
            raise self.failure
        return CommandResult(self.failure or 0, "", "synthetic reader diagnostic")

    def upload_text(self, text, remote_path, timeout=None):
        self.uploads.append((text, remote_path, timeout))
        return CommandResult(0, "", "")

    def download(self, remote_path, local_path, timeout=None):
        self.downloads.append((remote_path, Path(local_path), timeout))
        Path(local_path).write_text(self.payload, encoding="utf-8")
        return CommandResult(0, "", "")


def read(runner, **kwargs):
    return wf.read_transient_window(
        PSF, ["OUT"], 0, 1, 0.5, runner=runner,
        cadence_env=CadenceEnvironment(), **kwargs,
    )


@pytest.mark.parametrize("overrides", [
    {"results_dir": "relative/psf"},
    {"results_dir": "C:/results/psf"},
    {"results_dir": "/point/../other/psf"},
    {"results_dir": "/point/psf\n"},
    {"signals": []}, {"signals": "OUT"}, {"signals": ["OUT", "OUT"]},
    {"signals": [""]}, {"signals": ["OUT\tbad"]}, {"signals": [1]},
    {"start": True}, {"start": -1}, {"start": float("nan")},
    {"stop": float("inf")}, {"stop": 0}, {"stop": -1},
    {"step": 0}, {"step": -0.5}, {"step": False}, {"step": "0.5"},
    {"max_samples": True}, {"max_samples": 0}, {"max_samples": 1_000_001},
])
def test_request_rejects_invalid_input(overrides):
    request = dict(results_dir=PSF, signals=["OUT"], start=0, stop=1,
                   step=0.5, max_samples=100)
    request.update(overrides)
    with pytest.raises(ValueError):
        wf._request(**request)


def test_request_exact_keys_grid_and_aggregate_budget():
    with pytest.raises(ValueError, match="empty"):
        wf._request(PSF, iter(()), 0, 1, 0.5, 100)
    keys = [r"BUS\<0\>", "I1.XOUT", "V0:p"]
    assert wf._request(PSF, keys, 0, 1, 0.5, 9) == (keys, 3)
    assert wf._request(PSF, ["OUT"], 0, 1, 0.3, 4) == (["OUT"], 4)
    with pytest.raises(ValueError, match="Total samples"):
        wf._request(PSF, keys, 0, 1, 0.5, 8)
    with pytest.raises(ValueError, match="sampling grid"):
        wf._request(PSF, ["OUT"], 0, 1, 0.5, 2)
    with pytest.raises(ValueError):
        wf._request(PSF, ["OUT"], 0, 1e308, 1e-308, 100)


def test_invalid_request_budget_or_executable_never_reaches_transport():
    runner = FakeRunner()
    for kwargs in ({"max_samples": 2}, {"ocean_bin": "ocean\nrun"}):
        with pytest.raises(ValueError):
            read(runner, **kwargs)
    assert not runner.commands


def test_script_preserves_raw_keys_and_contains_only_bounded_reads():
    keys = [r"BUS\<0\>", 'a"b\\c', "I1.XOUT", "V0:p"]
    path = '/synthetic/a "quote"/psf'
    script = wf._script(path, keys, 0, 1, 0.5, 3)
    assert f"openResults({json.dumps(path)})" in script
    assert all(json.dumps(key) in script for key in keys)
    assert "getData(query ?result 'tran)" in script
    assert "at>=first && at<=last" in script
    assert "for(k 0 2" in script
    assert "errset(vbWindowSignal" in script
    assert 'fprintf(fp "END\\n")' in script
    for forbidden in ("execute_skill", "maeOpenResults", "asiGet", "run(", "v(query"):
        assert forbidden not in script


def test_script_coerces_fixnums_with_float_and_avoids_unicode_escapes():
    script = wf._script("/tmp/设计/psf", ["信号_OUT"], 0.0, 10.0, 1.0, 11)
    assert 'fprintf(fp "C\\t%d\\t%d\\t%.17g\\t%.17g\\n" idx n float(first) float(last))' in script
    assert "at=float(0)+k*float(1)" in script
    assert "when(at>=float(0) && at<=float(10) && at>=first && at<=last" in script
    assert 'fprintf(fp "D\\t%d\\t%.17g\\t%.17g\\n" idx float(at) float(val))' in script
    assert 'openResults("/tmp/设计/psf")' in script
    assert '"信号_OUT"' in script
    assert "\\u" not in script
    assert script.count("(") == script.count(")")


def test_parse_complete_window_does_not_claim_simulation_completion():
    result = wf._parse(FULL, ["OUT"], 0, 1, 0.5, 3)
    assert result["state"] == "ok"
    assert result["window_complete"] is True
    assert result["simulation_complete"] is None
    wave = result["signals"]["OUT"]
    assert wave["coverage"] == dict(native_points=3, first=0, last=1)
    assert wave["time"] == [0, 0.5, 1]
    assert wave["values"] == [2, 3, 4]
    assert wave["sample_count"] == 3
    assert wave["error"] is None


def test_parse_short_and_missing_signals_preserve_successful_siblings():
    payload = (FULL.removesuffix("END\n")
               + "C\t1\t2\t0.25\t0.75\nD\t1\t0.5\t8\n"
               + "E\t2\tunreadable_signal\nEND\n")
    result = wf._parse(payload, ["OUT", "SHORT", "MISSING"], 0, 1, 0.5, 3)
    assert result["state"] == "partial"
    assert result["window_complete"] is False
    assert result["signals"]["OUT"]["window_complete"] is True
    short = result["signals"]["SHORT"]
    assert short["time"] == [0.5]  # Do not re-anchor at the coverage start.
    assert short["values"] == [8]
    assert short["window_complete"] is False
    assert result["signals"]["MISSING"]["error"] == "unreadable_signal"


@pytest.mark.parametrize("payload", [
    "E\t0\tresults_unavailable\nEND\n",
    "E\t0\tunreadable_signal\nEND\n",
    "C\t0\t2\t2\t3\nEND\n",
])
def test_parse_unavailable_psf_signal_or_nonoverlapping_coverage(payload):
    result = wf._parse(payload, ["OUT"], 0, 1, 0.5, 3)
    assert result["state"] == "unavailable"
    assert result["signals"]["OUT"]["time"] == []
    assert result["window_complete"] is False
    assert result["simulation_complete"] is None


def test_parse_error_discards_only_failed_signals_partially_written_values():
    payload = "C\t0\t3\t0\t1\nD\t0\t0\t2\nE\t0\tunreadable_signal\nEND\n"
    wave = wf._parse(payload, ["OUT"], 0, 1, 0.5, 3)["signals"]["OUT"]
    assert wave["time"] == wave["values"] == []
    assert wave["sample_count"] == 0
    assert wave["error"] == "unreadable_signal"


def test_parse_single_readable_point_returns_only_exact_grid_hit():
    result = wf._parse("C\t0\t1\t0.5\t0.5\nD\t0\t0.5\t7\nEND\n",
                       ["OUT"], 0, 1, 0.5, 3)
    assert result["signals"]["OUT"]["time"] == [0.5]
    assert result["state"] == "partial"


@pytest.mark.parametrize("payload", [
    "", FULL.removesuffix("END\n"), FULL + "END\n", "END\n",
    "C\t1\t3\t0\t1\nEND\n", "C\t-1\t3\t0\t1\nEND\n",
    "C\t0\t0\t0\t1\nEND\n", "C\t0\t2\t1\t0\nEND\n",
    "C\t0\t3\tnan\t1\nEND\n", "C\t0\t3\t0\tinf\nEND\n",
    "D\t0\t0\t1\nEND\n", "C\t0\t3\t0\t1\nD\t0\t0\tnan\nEND\n",
    FULL.replace("D\t0\t0.5\t3", "D\t0\t0.25\t3"),
    FULL.replace("D\t0\t0.5\t3\n", ""),
    FULL.replace("D\t0\t0.5\t3", "D\t0\t0\t3"),
    FULL.replace("D\t0\t1\t4", "D\t0\t1.5\t4"),
    "E\t0\tunknown_error\nEND\n",
    "E\t0\tunreadable_signal\nC\t0\t3\t0\t1\nEND\n",
    FULL.replace("END\n", "C\t0\t3\t0\t1\nEND\n"),
])
def test_parse_rejects_malformed_incomplete_or_off_grid_exports(payload):
    with pytest.raises(RuntimeError):
        wf._parse(payload, ["OUT"], 0, 1, 0.5, 3)


def test_remote_reader_uses_only_explicit_point_and_owned_scratch(scratch):
    runner = FakeRunner()
    result = read(runner, ocean_bin="/opt/Cadence tools/ocean", timeout=7)
    assert result["state"] == "ok"
    assert result["results_dir"] == PSF
    assert result["requested_window"] == dict(start=0, stop=1, step=0.5)
    assert result["atomic_snapshot"] is False
    assert result["observed_at"] <= result["finished_at"]
    code, target, _ = runner.uploads[0]
    assert target == REMOTE + "/read.ocn"
    assert f"openResults({json.dumps(PSF)})" in code
    command, deadline = runner.commands[1]
    inner = shlex.split(command)[2]
    assert "cd " + REMOTE in inner
    assert "timeout -k 5s 7s" in inner
    assert "unset DISPLAY" in inner and "< /dev/null" in inner
    assert "/opt/Cadence tools/ocean" in shlex.split(inner)[-1]
    assert deadline == 22
    assert runner.commands[-1][0] == "rm -rf -- " + REMOTE
    assert runner.downloads[0][0] == REMOTE + "/window.tsv"
    assert not runner.downloads[0][1].exists()
    assert all(PSF not in command for command, _ in runner.commands)


@pytest.mark.parametrize("payload,state", [
    ("E\t0\tresults_unavailable\nEND\n", "unavailable"),
    ("E\t0\tunreadable_signal\nEND\n", "unavailable"),
    ("C\t0\t2\t0\t0.5\nD\t0\t0\t2\nD\t0\t0.5\t3\nEND\n", "partial"),
])
def test_remote_reader_returns_structured_invalid_missing_or_short_result(scratch, payload, state):
    assert read(FakeRunner(payload))["state"] == state


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("synthetic ssh", 16),
    OSError("synthetic disconnected transport"),
    124,
])
def test_remote_failure_reports_retained_scratch_without_retry(scratch, failure):
    runner = FakeRunner(failure=failure)
    with pytest.raises(RuntimeError, match="scratch: " + REMOTE) as caught:
        read(runner, timeout=1)
    assert caught.value.__cause__ is not None
    assert len(runner.commands) == 2
    assert not runner.downloads
    assert not any("rm -rf" in command for command, _ in runner.commands)


@pytest.mark.parametrize("remote", ["/", "/tmp/other", REMOTE + "/../victim", REMOTE + ";id"])
def test_remote_bad_scratch_acknowledgment_never_uploads_or_cleans(scratch, remote):
    runner = FakeRunner(remote=remote)
    with pytest.raises(RuntimeError, match="isolated reader directory"):
        read(runner)
    assert len(runner.commands) == 1
    assert not runner.uploads


def test_remote_malformed_export_retains_scratch_for_diagnosis(scratch):
    runner = FakeRunner("C\t0\t3\t0\t1\n")
    with pytest.raises(RuntimeError, match="Incomplete OCEAN export"):
        read(runner)
    assert len(runner.commands) == 2
    assert not any("rm -rf" in command for command, _ in runner.commands)


@pytest.mark.parametrize("stage", ["upload_text", "download"])
def test_remote_transfer_failure_retains_scratch(scratch, monkeypatch, stage):
    runner = FakeRunner()
    monkeypatch.setattr(runner, stage, lambda *a, **kw: CommandResult(1, "", "synthetic transfer failure"))
    with pytest.raises(RuntimeError, match="scratch: " + REMOTE):
        read(runner)
    assert not any("rm -rf" in command for command, _ in runner.commands)


@pytest.mark.parametrize("failure", [1, OSError("synthetic cleanup failure")])
def test_remote_cleanup_failure_does_not_discard_successful_read(scratch, failure):
    class CleanupFailureRunner(FakeRunner):
        def run_command(self, command, timeout=None):
            if command.startswith("rm -rf -- "):
                self.commands.append((command, timeout))
                if isinstance(failure, BaseException):
                    raise failure
                return CommandResult(failure, "", "synthetic cleanup failure")
            return super().run_command(command, timeout)

    result = read(CleanupFailureRunner())
    assert result["state"] == "ok"
    assert REMOTE in result["diagnostics"]


def test_remote_successive_reads_allocate_distinct_reader_directories(scratch):
    first = FakeRunner(remote=REMOTE + "A")
    second = FakeRunner(remote=REMOTE + "B")
    assert read(first)["state"] == read(second)["state"] == "ok"
    assert first.uploads[0][1] != second.uploads[0][1]
    assert second.remote not in first.commands[-1][0]
    assert first.remote not in second.commands[-1][0]


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "60"])
def test_invalid_timeout_fails_before_transport(timeout):
    runner = FakeRunner()
    with pytest.raises(ValueError, match="timeout"):
        read(runner, timeout=timeout)
    assert runner.commands == []


def test_local_reader_stub_uses_isolated_directory_and_selected_environment(monkeypatch, scratch):
    monkeypatch.setattr(wf, "os", SimpleNamespace(name="posix"))
    calls = []

    def local_stub(command, **kwargs):
        root = Path(kwargs["cwd"])
        calls.append((command, kwargs, root))
        assert f"openResults({json.dumps(PSF)})" in (root / "read.ocn").read_text()
        (root / "window.tsv").write_text(FULL, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "synthetic startup banner", "")

    monkeypatch.setattr(wf.subprocess, "run", local_stub)
    env = CadenceEnvironment("/synthetic/env selector.sh", "sh")
    result = wf.read_transient_window(PSF, ["OUT"], 0, 1, 0.5, cadence_env=env, timeout=2)
    assert result["state"] == "ok"
    command, kwargs, root = calls[0]
    assert "env selector.sh" in command
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 17
    assert not root.exists()


def test_local_stub_timeout_cleans_local_scratch_without_retry(monkeypatch, scratch):
    monkeypatch.setattr(wf, "os", SimpleNamespace(name="posix"))
    roots = []

    def timeout_stub(command, **kwargs):
        roots.append(Path(kwargs["cwd"]))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(wf.subprocess, "run", timeout_stub)
    with pytest.raises(subprocess.TimeoutExpired):
        read(None, timeout=1)
    assert len(roots) == 1
    assert not roots[0].exists()


def test_local_nonzero_exit_never_parses_existing_output(monkeypatch, scratch):
    monkeypatch.setattr(wf, "os", SimpleNamespace(name="posix"))

    def failed_stub(command, **kwargs):
        (Path(kwargs["cwd"]) / "window.tsv").write_text(FULL, encoding="utf-8")
        return subprocess.CompletedProcess(command, 124, "", "synthetic timeout")

    monkeypatch.setattr(wf.subprocess, "run", failed_stub)
    with pytest.raises(RuntimeError, match=r"OCEAN reader failed \(124\)"):
        read(None)


def test_native_windows_requires_explicit_runner(monkeypatch):
    monkeypatch.setattr(wf, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="explicit SSH runner"):
        read(None)


@pytest.fixture
def native_posix(scratch):
    executable = scratch / "synthetic ocean"
    source = (
        "import os, pathlib, sys, time\n"
        "assert os.environ['VB_FAKE_SELECTOR'] == 'literal value $ with spaces'\n"
        "assert 'DISPLAY' not in os.environ\n"
        "assert sys.stdin.read() == ''\n"
        "assert '-nograph' in sys.argv and '-restore' in sys.argv\n"
        "assert 'openResults(' in pathlib.Path('read.ocn').read_text()\n"
        "mode = os.environ.get('VB_FAKE_MODE', 'ok')\n"
        "if mode == 'fail':\n"
        "    sys.stderr.write('synthetic native failure\\n')\n"
        "    sys.exit(23)\n"
        "if mode == 'hang':\n"
        "    pathlib.Path(os.environ['VB_FAKE_PID_FILE']).write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        f"pathlib.Path('window.tsv').write_text({FULL!r})\n"
    )
    executable.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable)
        + " -c " + shlex.quote(source) + ' "$@"\n', encoding="utf-8",
    )
    executable.chmod(0o700)

    def environment(mode="ok", pid_file=None):
        selector = scratch / "selector with spaces.sh"
        body = (
            "export VB_FAKE_SELECTOR='literal value $ with spaces'\n"
            f"export VB_FAKE_MODE={shlex.quote(mode)}\n"
            "export DISPLAY='synthetic-display-must-be-unset'\n"
            "printf 'synthetic selector banner\\n'\n"
        )
        if pid_file is not None:
            body += "export VB_FAKE_PID_FILE=" + shlex.quote(str(pid_file)) + "\n"
        selector.write_text(body, encoding="utf-8")
        return CadenceEnvironment(str(selector), "sh")

    return executable, environment


@POSIX_ONLY
def test_native_posix_selector_inheritance_and_fake_ocean(native_posix):
    executable, environment = native_posix
    result = wf.read_transient_window(
        PSF, ["OUT"], 0, 1, 0.5, ocean_bin=str(executable),
        cadence_env=environment(), timeout=3,
    )
    assert result["state"] == "ok"
    assert result["signals"]["OUT"]["values"] == [2, 3, 4]
    assert result["simulation_complete"] is None
    assert "synthetic selector banner" in result["diagnostics"]


@POSIX_ONLY
def test_native_posix_fake_ocean_nonzero_exit(native_posix):
    executable, environment = native_posix
    with pytest.raises(RuntimeError, match=r"(?s)failed \(23\).*synthetic native failure"):
        wf.read_transient_window(
            PSF, ["OUT"], 0, 1, 0.5, ocean_bin=str(executable),
            cadence_env=environment("fail"), timeout=3,
        )


@POSIX_ONLY
def test_native_posix_timeout_kills_owned_fake_not_unrelated_sentinel(native_posix, scratch):
    executable, environment = native_posix
    pid_file = scratch / "owned-reader.pid"
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=r"failed \(124\)"):
            wf.read_transient_window(
                PSF, ["OUT"], 0, 1, 0.5, ocean_bin=str(executable),
                cadence_env=environment("hang", pid_file), timeout=1,
            )
        assert time.monotonic() - started < 10
        assert pid_file.is_file(), "The owned fake must have started before timing out"
        owned_pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(owned_pid, 0)
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        try:
            sentinel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sentinel.kill()
            sentinel.wait(timeout=5)
