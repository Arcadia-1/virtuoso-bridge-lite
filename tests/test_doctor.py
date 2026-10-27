"""``virtuoso-bridge doctor``: read-only checks with fake transports.

Every remote command and SKILL query the doctor issues is recorded and
checked: nothing may write on the remote host, start a tunnel or daemon, or
call a mutating SKILL function. The scratch-directory shell runs for real
with the local POSIX sh against a temporary directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from virtuoso_bridge import cli, daemon_auth, doctor
from virtuoso_bridge.env import set_runtime_env_file
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult, SSHRunner
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso import x11
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

posix_sh = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="requires a POSIX shell")

TOKEN = "ab" * 32
STALE = "cd" * 32
TARGET = "designer@eda-host:65123"
WORK = "/tmp/virtuoso_bridge_designer/client1/virtuoso_bridge"
STATE = {
    "mode": "remote", "port": 65123, "remote_port": 65123, "target": TARGET,
    "remote_user": "designer", "daemon_host": "eda-host",
    "setup_path": f"{WORK}/virtuoso_setup.il", "identity_path": f"{WORK}/daemon_identity.txt",
}
SPECTRE_OUT = "SPECTRE_PATH=/cad/ic/bin/spectre\n@(#)$CDS: spectre version 23.1.0 64bit\n"
ALWAYS = "(" + " ".join(f'"{f}_UseNextLicense=always"' for f in doctor.LICENSE_FEATURES) + ")"
HEALTHY_SKILL = {
    "RBLastBind": '"127.0.0.1:65123"',
    "hiIsFormDisplayed": "nil",
    "UseNextLicense": ALWAYS,
    "cdsGetInstPath": '"/cad/ic"',
    'getShellEnvVar("USER")': '"designer"',
}

# Redirections that only discard or reroute output; any other ``>`` writes.
_HARMLESS_REDIRECT = re.compile(r"\d*>&\d|\d*>\s*/dev/(?:null|stderr)\b")
_REMOTE_WRITE = re.compile(
    r"\b(?:mkdir|chmod|chown|rm|rmdir|mv|cp|ln|touch|tee|truncate|scp|rsync|vb_ensure_dir)\b|>"
)


def _writes(command: str) -> bool:
    shell = _HARMLESS_REDIRECT.sub("", command)
    # Paths are data: a temp directory may well be named "rm" or "cp".
    shell = re.sub(r"(?<![\w./@-])/[\w./@-]*", "/PATH", shell)
    return bool(_REMOTE_WRITE.search(shell))
_MUTATING_SKILL = re.compile(
    r"\b(?:envSetVal|setShellEnvVar|hiFormDone|hiFormCancel|hiFormApply|hiDisplayForm|"
    r"hiCloseWindow|dbSave|dbClose|dbPurge|ddDeleteObj|load|techGetTechFile|RBStart|RBStop)\s*\("
)


def _ok(output: str) -> VirtuosoResult:
    return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)


class FakeRunner:
    """Records commands; answers by substring; runs the scratch probe with sh."""

    host = "eda-host"

    def __init__(self, routes: dict | None = None, *, env: dict | None = None) -> None:
        self.commands: list[tuple[str, float | None]] = []
        self.writes: list[tuple] = []
        self._verbose = True
        self.env = env
        self.routes = {
            "bridge_token": TOKEN + "\n",
            "stat -c %U /proc/": "designer\n",
            "daemon_identity.txt": "host=eda-host\nip=192.0.2.10\npid=4242\nbind=127.0.0.1:65123\n",
            "SPECTRE_PATH": SPECTRE_OUT,
            "which virtuoso": "/cad/ic/tools/bin/virtuoso\n",
            "doc/finder/SKILL": "/cad/ic/doc/finder/SKILL\n",
            "CADENCE_DOC_ROOT": "",
            "VB_DOC_ROOT_OK": "VB_DOC_ROOT_OK\n",
            "whoami": "designer\n",
        }
        self.routes.update(routes or {})

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append((command, timeout))
        if "for vb_path in" in command and "for vb_path in" not in self.routes:
            proc = subprocess.run(["sh", "-s"], input=command, capture_output=True, text=True,
                                  timeout=30, env=self.env)
            return CommandResult(proc.returncode, proc.stdout, proc.stderr)
        for key, value in self.routes.items():
            if key in command:
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, CommandResult):
                    return value
                return CommandResult(0, value, "")
        return CommandResult(0, "", "")

    def upload(self, *args, **kwargs):
        self.writes.append(("upload", args))
        return CommandResult(0, "", "")

    def upload_text(self, *args, **kwargs):
        self.writes.append(("upload_text", args))
        return CommandResult(0, "", "")

    def download(self, *args, **kwargs):
        self.writes.append(("download", args))
        return CommandResult(0, "", "")

    def close(self) -> None:
        pass


class FakeDaemonClient:
    """Answers the handshake and read-only SKILL; records every query."""

    def __init__(self, caps: dict | None = None, *, error: Exception | None = None,
                 skill: dict | None = None, valid_token: str | None = None) -> None:
        self.caps = caps if caps is not None else {
            "proto": 1, "auth": "on", "virtuoso_pid": 4242, "lane": {"state": "idle"},
        }
        self.error = error
        self.skill = dict(HEALTHY_SKILL if skill is None else skill)
        self.valid_token = valid_token
        self.sent: list[str] = []
        self.daemon_token: str | None = None
        self.ssh_runner = None

    def _ensure_daemon_capabilities(self, deadline, refresh=False):
        if self.valid_token is not None and self.daemon_token != self.valid_token:
            raise daemon_auth.DaemonAuthError("AuthError: request MAC mismatch")
        if self.error is not None:
            raise self.error
        return self.caps

    def handshake_virtuoso_pid(self, timeout=5):
        caps = self._ensure_daemon_capabilities(None)
        return caps.get("virtuoso_pid") if caps.get("auth") == "on" else None

    def execute_skill(self, expr, timeout=None, *, retry_connect=True):
        self.sent.append(expr)
        for key, value in self.skill.items():
            if key in expr:
                return value if isinstance(value, VirtuosoResult) else _ok(value)
        return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[f"unexpected SKILL {expr}"])


def _payload(status: str = "clear", pid: int = 4242, dialogs: list | None = None) -> dict:
    return {
        "status": status, "target": {"pid": pid, "display": ":7", "ciw_window": "0x10"},
        "dialogs": dialogs or [], "diagnostics": [] if status != "indeterminate" else ["no CIW window"],
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real .env, venv binding, home directory or state leaks into a test."""
    original = set(os.environ)
    for key in list(os.environ):
        if key.startswith(("VB_", "RB_")):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home, "USERPROFILE": home, "VIRTUAL_ENV": tmp_path / "venv",
        "VB_STATE_DIR": tmp_path / "state", "VB_LOG_DIR": tmp_path / "logs",
        "VB_CACHE_DIR": tmp_path / "cache",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.chdir(tmp_path)
    set_runtime_env_file(None)
    # Building an SSH runner installs the DEBUG command log; keep it per test.
    pkg_logger = logging.getLogger("virtuoso_bridge")
    handlers, level = list(pkg_logger.handlers), pkg_logger.level
    yield
    for handler in list(pkg_logger.handlers):
        if handler not in handlers:
            pkg_logger.removeHandler(handler)
            handler.close()
    pkg_logger.setLevel(level)
    set_runtime_env_file(None)
    # A loaded .env writes os.environ directly; monkeypatch restores the rest.
    for key in list(os.environ):
        if key.startswith(("VB_", "RB_")) and key not in original:
            del os.environ[key]


@pytest.fixture(autouse=True)
def forbidden(monkeypatch):
    """Fail the test if anything starts, deploys, provisions or reloads."""
    called: list[str] = []

    def refuse(name):
        def _refused(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"doctor called {name}")
        return _refused

    for name in ("warm", "ensure_tunnel", "ensure_remote_setup", "ensure_local_setup",
                 "save_state", "stop", "ensure_daemon_token", "refresh_daemon_token",
                 "upload_file", "upload_text"):
        monkeypatch.setattr(SSHClient, name, refuse(f"SSHClient.{name}"))
    for name in ("start_port_forward", "stop_port_forward"):
        monkeypatch.setattr(SSHRunner, name, refuse(f"SSHRunner.{name}"))
    for name in ("ensure_ready", "warm_remote_session", "load_il", "dismiss_dialog"):
        monkeypatch.setattr(VirtuosoClient, name, refuse(f"VirtuosoClient.{name}"))
    monkeypatch.setattr(x11, "dismiss_window", refuse("x11.dismiss_window"))
    monkeypatch.setattr(x11, "dismiss_dialogs", refuse("x11.dismiss_dialogs"))
    yield called
    assert called == []


def _context(monkeypatch, tmp_path, *, runner=None, client=None, running=True, state=None,
             inspection=None, profile=None, deployed=True):
    monkeypatch.setenv(f"VB_REMOTE_HOST{'_' + profile if profile else ''}", "eda-host")
    monkeypatch.setenv(f"VB_REMOTE_USER{'_' + profile if profile else ''}", "designer")
    monkeypatch.setenv(f"VB_REMOTE_PORT{'_' + profile if profile else ''}", "65123")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("VB_CLIENT_ID", "client1")
    if deployed:
        path = scratch
        for part in ("daemon",):
            path = path / part
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
    runner = runner or FakeRunner()
    client = client or FakeDaemonClient()
    ssh = SSHClient(remote_host="eda-host", remote_user="designer", port=65123, profile=profile)
    ssh._ssh_runner = runner
    ssh._deployment_runner = runner
    ssh._role_runners = {"daemon": runner}
    if state is None and running:
        state = dict(STATE)
    monkeypatch.setattr(SSHClient, "is_running", staticmethod(lambda profile=None: running))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None, **kw: state))
    monkeypatch.setattr(SSHClient, "running_targets", classmethod(lambda cls: []))
    inspections: list[dict] = []

    def inspect(runner_arg, user, **kwargs):
        inspections.append(kwargs)
        return inspection if inspection is not None else _payload(pid=kwargs["pid"])

    monkeypatch.setattr(x11, "inspect_dialogs", inspect)
    ctx = doctor.DoctorContext(profile)
    ctx.make_ssh = lambda: ssh
    created: list[tuple[int, str | None]] = []

    def make_client(port, token):
        created.append((port, token))
        client.daemon_token = token
        return client

    ctx.make_daemon_client = make_client
    ctx.inspections = inspections
    ctx.created = created
    return ctx, runner, client


def _run(ctx) -> dict[str, doctor.CheckResult]:
    try:
        results = doctor.run_checks(ctx)
    finally:
        ctx.close()
    assert [r.name for r in results] == [name for name, _ in doctor.CHECKS]
    return {r.name: r for r in results}


def _assert_read_only(runner: FakeRunner, client: FakeDaemonClient, ctx=None) -> None:
    assert runner.writes == []
    for command, timeout in runner.commands:
        assert not _writes(command), command
        assert timeout is not None and 0 < timeout <= doctor.ENV_TIMEOUT, (command, timeout)
    for expr in client.sent + (ctx.skill_sent if ctx is not None else []):
        assert not _MUTATING_SKILL.search(expr), expr
        doctor.assert_read_only_skill(expr)


# -- whole runs -----------------------------------------------------------------

@posix_sh
def test_healthy_remote_setup_is_all_ok_and_read_only(monkeypatch, tmp_path):
    ctx, runner, client = _context(monkeypatch, tmp_path)

    results = _run(ctx)

    assert {name: r.status for name, r in results.items()} == {
        name: doctor.OK for name, _ in doctor.CHECKS
    }, results
    assert doctor.exit_code(list(results.values())) == 0
    assert ctx.created == [(65123, TOKEN)]
    assert "matches SSH user designer" in results["tunnel/daemon"].detail
    assert "127.0.0.1:65123 (daemon banner)" in results["listen address"].detail
    assert "23.1.0" in results["spectre"].detail
    assert "/cad/ic/doc/finder/SKILL" in results["documentation"].detail
    assert ctx.inspections and all(k["deploy_helper"] is False for k in ctx.inspections)
    assert ctx.inspections[0]["pid"] == 4242
    _assert_read_only(runner, client, ctx)
    # Unlike start's token path, the doctor reads the token and writes no cache.
    assert not (tmp_path / "home" / ".virtuoso-bridge").exists()


@posix_sh
def test_without_a_tunnel_nothing_is_started_and_daemon_checks_skip(monkeypatch, tmp_path):
    ctx, runner, client = _context(monkeypatch, tmp_path, running=False, deployed=False)

    results = _run(ctx)

    assert results["tunnel/daemon"].status == doctor.WARN
    assert f"no tunnel running for {TARGET}" in results["tunnel/daemon"].detail
    assert "virtuoso-bridge start" in results["tunnel/daemon"].fix
    assert results["listen address"].status == doctor.OK
    assert results["remote scratch directory"].status == doctor.SKIP
    assert "not deployed yet" in results["remote scratch directory"].detail
    for name in ("dialogs", "license checkout"):
        assert results[name].status == doctor.SKIP
        assert "no tunnel is running" in results[name].detail
    assert results["spectre"].status == doctor.OK
    assert results["documentation"].status == doctor.OK
    assert ctx.created == [] and client.sent == [] and ctx.inspections == []
    assert not any("bridge_token" in command for command, _ in runner.commands)
    assert not (tmp_path / "state").exists()
    assert doctor.exit_code(list(results.values())) == 0
    _assert_read_only(runner, client, ctx)


def test_local_daemon_uses_the_local_token_and_no_ssh(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("VB_BRIDGE_TOKEN", str(token_file))
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    state = {"mode": "local", "port": 65432, "identity_path": str(tmp_path / "none.txt")}
    monkeypatch.setattr(SSHClient, "is_running", staticmethod(lambda profile=None: True))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None, **kw: state))
    monkeypatch.setattr(x11, "inspect_dialogs", lambda runner, user, **kw: _payload(pid=kw["pid"]))
    monkeypatch.setattr("virtuoso_bridge.spectre.runner.probe_spectre_locally",
                        lambda spectre_bin="": ("/cad/ic/bin/spectre", "@(#)$CDS: spectre version 23.1"))
    client = FakeDaemonClient()
    ctx = doctor.DoctorContext(None)
    ctx.make_daemon_client = lambda port, token: (setattr(client, "daemon_token", token), client)[1]

    results = _run(ctx)

    assert results["tunnel/daemon"].status == doctor.OK, results["tunnel/daemon"]
    assert client.daemon_token == TOKEN
    assert "local daemon port 65432" in results["tunnel/daemon"].detail
    assert results["listen address"].detail == "the daemon listens on 127.0.0.1:65123 (RBLastBind in the CIW)"
    assert results["remote scratch directory"].status == doctor.SKIP
    assert results["dialogs"].status == results["license checkout"].status == doctor.OK
    assert results["spectre"].status == doctor.OK
    _assert_read_only(FakeRunner(), client, ctx)


def test_unconfigured_host_fails_configuration_and_skips_the_rest(monkeypatch):
    ctx = doctor.DoctorContext(None)
    ctx.make_ssh = lambda: pytest.fail("must not build a transport")

    results = _run(ctx)

    assert results["configuration"].status == doctor.FAIL
    assert "VB_REMOTE_HOST" in results["configuration"].fix
    assert "-p " not in results["configuration"].fix
    assert all(r.status == doctor.SKIP for name, r in results.items() if name != "configuration")
    assert doctor.exit_code(list(results.values())) == 1


def test_unconfigured_default_names_the_configured_profiles(monkeypatch):
    monkeypatch.setenv("VB_REMOTE_HOST_worker", "eda-host")
    result = doctor.check_configuration(doctor.DoctorContext(None))
    assert result.status == doctor.FAIL
    assert "select a configured profile (-p worker)" in result.fix


def test_missing_env_file_fails_configuration(monkeypatch):
    ctx = doctor.DoctorContext(None, env_error=".env file not found: /cad/missing.env")
    results = _run(ctx)
    assert results["configuration"].status == doctor.FAIL
    assert "could not be loaded" in results["spectre"].detail


def test_a_crashing_check_does_not_stop_the_others(monkeypatch):
    def boom(ctx):
        raise RuntimeError("kaput")

    results = doctor.run_checks(
        doctor.DoctorContext(None),
        checks=(("first", boom), ("configuration", doctor.check_configuration)),
    )
    assert [(r.name, r.status) for r in results] == [("first", doctor.WARN), ("configuration", doctor.FAIL)]
    assert "kaput" in results[0].detail


# -- 2. tunnel/daemon -----------------------------------------------------------------

@pytest.mark.parametrize("error", [
    ConnectionRefusedError("Connection refused"),
    daemon_auth.DaemonAuthError(
        "the service behind the port did not answer the bridge capability handshake — it is not a bridge daemon"),
])
def test_tunnel_without_a_loaded_daemon_warns_with_the_load_line(monkeypatch, tmp_path, error):
    ctx, runner, client = _context(monkeypatch, tmp_path, client=FakeDaemonClient(error=error))

    result = _run(ctx)

    daemon = result["tunnel/daemon"]
    assert daemon.status == doctor.WARN
    assert f'load("{WORK}/virtuoso_setup.il")' in daemon.fix
    assert result["license checkout"].status == doctor.SKIP
    assert client.sent == []
    _assert_read_only(runner, client, ctx)


@pytest.mark.parametrize("message,needle", [
    ("AuthError: request MAC mismatch", "rejected this client's token"),
    ("bridge protocol version mismatch: daemon speaks v0, client speaks v1", "handshake failed"),
])
def test_handshake_refusals_fail(monkeypatch, tmp_path, message, needle):
    client = FakeDaemonClient(error=daemon_auth.DaemonAuthError(message))
    ctx, runner, _ = _context(monkeypatch, tmp_path, client=client)
    daemon = _run(ctx)["tunnel/daemon"]
    assert daemon.status == doctor.FAIL
    assert needle in daemon.detail
    assert "restart" in daemon.fix
    assert client.sent == []


def test_a_stale_cached_token_is_replaced_by_reading_the_daemon_host(monkeypatch, tmp_path):
    client = FakeDaemonClient(valid_token=TOKEN)
    ctx, runner, _ = _context(monkeypatch, tmp_path, client=client)
    cache = ctx.ssh.remote_cache_path()
    daemon_auth.update_remote_cache(cache, token=STALE)

    daemon = _run(ctx)["tunnel/daemon"]

    assert daemon.status == doctor.OK, daemon
    assert ctx.created == [(65123, STALE)]
    assert client.daemon_token == TOKEN
    assert sum("bridge_token" in command for command, _ in runner.commands) == 1
    assert daemon_auth.read_remote_cache(cache)["token"] == STALE  # read, never rewritten


def test_missing_token_fails_without_provisioning(monkeypatch, tmp_path):
    runner = FakeRunner({"bridge_token": CommandResult(1, "", "")})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner)
    daemon = _run(ctx)["tunnel/daemon"]
    assert daemon.status == doctor.FAIL
    assert "no valid bridge token" in daemon.detail
    assert ctx.created == []
    _assert_read_only(runner, client, ctx)


def test_a_foreign_daemon_fails_and_blocks_skill(monkeypatch, tmp_path):
    runner = FakeRunner({"stat -c %U /proc/": "intruder\n"})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner)

    results = _run(ctx)

    assert results["tunnel/daemon"].status == doctor.FAIL
    assert "'intruder'" in results["tunnel/daemon"].detail
    assert "VB_ALLOW_CROSS_USER_DAEMON=1 only if" in results["tunnel/daemon"].fix
    assert results["license checkout"].status == doctor.SKIP
    assert not any("UseNextLicense" in expr for expr in client.sent)


def test_a_poisoned_lane_warns_and_no_skill_is_sent(monkeypatch, tmp_path):
    caps = {"proto": 1, "auth": "on", "virtuoso_pid": 4242, "server_time_ms": 90_000,
            "lane": {"state": "poisoned", "detail": "legacy timeout", "since_ms": 30_000}}
    ctx, runner, client = _context(monkeypatch, tmp_path, client=FakeDaemonClient(caps))

    results = _run(ctx)

    assert results["tunnel/daemon"].status == doctor.WARN
    assert "IPC lane poisoned for 60 s (legacy timeout)" in results["tunnel/daemon"].detail
    assert results["license checkout"].status == doctor.SKIP
    assert "lane is poisoned" in results["license checkout"].detail
    assert results["dialogs"].status == doctor.OK  # X11 needs no SKILL
    assert client.sent == []


def test_an_unauthenticated_daemon_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_ALLOW_UNAUTHENTICATED_DAEMON", "1")
    caps = {"proto": 1, "auth": "off", "virtuoso_pid": None, "lane": {"state": "idle"}}
    ctx, runner, client = _context(monkeypatch, tmp_path, client=FakeDaemonClient(caps))
    results = _run(ctx)
    assert results["tunnel/daemon"].status == doctor.WARN
    assert "WITHOUT token authentication" in results["tunnel/daemon"].detail
    assert "X11 not inspected" in results["dialogs"].detail


# -- 3. listen address -------------------------------------------------------------------

def test_a_daemon_listening_on_all_interfaces_warns(monkeypatch, tmp_path):
    runner = FakeRunner({"daemon_identity.txt": "bind=0.0.0.0:65123\n"})
    ctx, _, _ = _context(monkeypatch, tmp_path, runner=runner)
    result = _run(ctx)["listen address"]
    assert result.status == doctor.WARN
    assert "0.0.0.0:65123" in result.detail
    assert "restart" in result.fix and "older setup" in result.fix


def test_opting_into_all_interfaces_warns_even_on_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_DAEMON_LISTEN_ALL_t28", "1")
    ctx, _, _ = _context(monkeypatch, tmp_path, profile="t28")
    result = _run(ctx)["listen address"]
    assert result.status == doctor.WARN
    assert "VB_DAEMON_LISTEN_ALL_t28=1" in result.detail
    assert "Remove VB_DAEMON_LISTEN_ALL_t28=1" in result.fix
    assert "-p t28" in result.fix


def test_listen_all_without_a_daemon_reports_the_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_DAEMON_LISTEN_ALL", "1")
    ctx, _, _ = _context(monkeypatch, tmp_path, running=False)
    result = _run(ctx)["listen address"]
    assert result.status == doctor.WARN
    assert "0.0.0.0" in result.detail


def test_bind_falls_back_to_the_ciw_variable(monkeypatch, tmp_path):
    runner = FakeRunner({"daemon_identity.txt": CommandResult(1, "", "")})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner)
    result = _run(ctx)["listen address"]
    assert result.status == doctor.OK
    assert "RBLastBind in the CIW" in result.detail
    assert doctor.BIND_QUERY in client.sent


# -- 4. remote scratch directory ------------------------------------------------------

def _chain(tmp_path):
    top = tmp_path / "scratch"
    return [top, top / "daemon"]


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@posix_sh
def test_scratch_group_access_warns_and_is_left_unchanged(monkeypatch, tmp_path):
    ctx, runner, client = _context(monkeypatch, tmp_path)
    _chain(tmp_path)[1].chmod(0o755)

    result = doctor.check_scratch_dir(_configured(ctx))

    assert result.status == doctor.WARN
    assert "daemon (mode 755)" in result.detail
    assert result.fix.startswith("Run `chmod 700 ")
    assert _mode(_chain(tmp_path)[1]) == 0o755  # observed, never tightened
    _assert_read_only(runner, client)


@posix_sh
def test_scratch_symlink_fails(monkeypatch, tmp_path):
    ctx, runner, _ = _context(monkeypatch, tmp_path, deployed=False)
    planted = tmp_path / "planted"
    planted.mkdir()
    (tmp_path / "scratch" / "daemon").symlink_to(planted)

    result = doctor.check_scratch_dir(_configured(ctx))

    assert result.status == doctor.FAIL
    assert "daemon is a symlink" in result.detail
    assert "VB_REMOTE_SCRATCH_ROOT" in result.fix
    assert list(planted.iterdir()) == []


@posix_sh
def test_scratch_owned_by_someone_else_fails(monkeypatch, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text(f"#!/bin/sh\necho {os.getuid() + 1}\n", encoding="utf-8")
    (fake_bin / "id").chmod(0o755)
    runner = FakeRunner(env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"})
    ctx, _, _ = _context(monkeypatch, tmp_path, runner=runner)

    result = doctor.check_scratch_dir(_configured(ctx))

    assert result.status == doctor.FAIL
    assert f"not you (uid {os.getuid() + 1})" in result.detail


@posix_sh
def test_scratch_not_deployed_is_skip_and_creates_nothing(monkeypatch, tmp_path):
    ctx, _, _ = _context(monkeypatch, tmp_path, deployed=False)
    result = doctor.check_scratch_dir(_configured(ctx))
    assert result.status == doctor.SKIP
    assert "not deployed yet" in result.detail
    assert list((tmp_path / "scratch").iterdir()) == []


@posix_sh
def test_scratch_script_reports_each_state_for_real(tmp_path):
    own = tmp_path / "own"
    own.mkdir(mode=0o700)
    loose = own / "loose"
    loose.mkdir(mode=0o750)
    loose.chmod(0o750)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(own)
    paths = [str(own), str(loose), str(tmp_path / "file"), str(tmp_path / "link"),
             str(tmp_path / "no such dir")]

    proc = subprocess.run(["sh", "-s"], input=doctor.scratch_inspection_script(paths),
                          capture_output=True, text=True, timeout=30)

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == f"UID {os.getuid()}"
    assert lines[1:] == [
        f"DIR {os.getuid()} 700 {own}", f"DIR {os.getuid()} 750 {loose}",
        f"NOTDIR {tmp_path / 'file'}", f"SYMLINK {tmp_path / 'link'}",
        f"MISSING {tmp_path / 'no such dir'}",
    ]


def test_scratch_unexpected_output_warns():
    status, detail, _ = doctor.classify_scratch("garbage\n", ["/tmp/a"], "/tmp")
    assert status == doctor.WARN and "could not inspect" in detail


def test_scratch_ssh_failure_fails(monkeypatch, tmp_path):
    runner = FakeRunner({"for vb_path in": CommandResult(255, "", "ssh: connect to host eda-host: Connection refused")})
    ctx, _, _ = _context(monkeypatch, tmp_path, runner=runner)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/fixture/scratch")
    result = doctor.check_scratch_dir(_configured(ctx))
    assert result.status == doctor.FAIL
    assert "SSH to eda-host failed: ssh: connect to host eda-host: Connection refused" == result.detail
    assert "ssh designer@eda-host true" in result.fix


def test_an_unreachable_host_costs_one_timeout(monkeypatch, tmp_path):
    hang = subprocess.TimeoutExpired("ssh", 10)
    runner = FakeRunner({"for vb_path in": hang, "": hang})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner, running=False)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/fixture/scratch")

    results = _run(ctx)

    assert results["remote scratch directory"].status == doctor.FAIL
    assert "SSH to eda-host failed: no answer within 10 s" in results["remote scratch directory"].detail
    for name in ("spectre", "documentation"):
        assert results[name].status == doctor.SKIP
        assert "SSH to eda-host already failed" in results[name].detail
    assert len(runner.commands) == 1
    assert doctor.exit_code(list(results.values())) == 1


def test_ssh_failure_while_reading_the_token_is_reported_as_ssh(monkeypatch, tmp_path):
    down = CommandResult(255, "", "ssh: connect to host eda-host port 22: Connection timed out")
    ctx, runner, client = _context(monkeypatch, tmp_path, runner=FakeRunner({"bridge_token": down}))

    results = _run(ctx)

    assert results["tunnel/daemon"].status == doctor.FAIL
    assert "SSH to eda-host failed: ssh: connect to host eda-host port 22" in results["tunnel/daemon"].detail
    assert results["remote scratch directory"].status == doctor.SKIP
    assert ctx.created == [] and client.sent == []


def test_local_mode_has_no_remote_scratch(monkeypatch):
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    ctx = _configured(doctor.DoctorContext(None))
    assert doctor.check_scratch_dir(ctx).status == doctor.SKIP


# -- 5. dialogs ---------------------------------------------------------------------------

def test_a_blocking_dialog_fails_lists_titles_and_stops_skill(monkeypatch, tmp_path):
    blocked = _payload("blocked", dialogs=[{"window_id": "0x20", "title": "PDK setting information"}])
    ctx, runner, client = _context(monkeypatch, tmp_path, inspection=blocked)

    results = _run(ctx)

    dialogs = results["dialogs"]
    assert dialogs.status == doctor.FAIL
    assert "'PDK setting information'" in dialogs.detail
    assert "never dismisses" in dialogs.fix
    assert 'techGetTechFile(ddGetObj("<pdkLib>"))' in dialogs.fix
    assert results["license checkout"].status == doctor.SKIP
    assert "a dialog blocks the CIW" in results["license checkout"].detail
    assert not any("hiIsFormDisplayed" in e or "UseNextLicense" in e for e in client.sent)
    assert doctor.exit_code(list(results.values())) == 1


def test_indeterminate_inspection_warns(monkeypatch, tmp_path):
    ctx, _, _ = _context(monkeypatch, tmp_path, inspection=_payload("indeterminate"))
    dialogs = _run(ctx)["dialogs"]
    assert dialogs.status == doctor.WARN
    assert "indeterminate (no CIW window)" in dialogs.detail
    assert "inspect-dialogs --pid 4242" in dialogs.fix


def test_a_displayed_pdk_form_warns_with_the_headless_recipe(monkeypatch, tmp_path):
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "hiIsFormDisplayed": '("tsmc_PDK_info_table")'})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)

    dialogs = _run(ctx)["dialogs"]

    assert dialogs.status == doctor.WARN
    assert "tsmc_PDK_info_table" in dialogs.detail
    assert "techGetTechFile" in dialogs.fix and ".cdsinit" in dialogs.fix
    query = next(e for e in client.sent if "hiIsFormDisplayed" in e)
    assert "boundp(vbSym)" in query and '"tsmc_PDK_info_table"' in query


def test_split_gui_host_skips_x11_but_still_queries_forms(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_GUI_HOST", "gui-host")
    ctx, _, client = _context(monkeypatch, tmp_path)
    dialogs = doctor.check_dialogs(_after_daemon(ctx))
    assert dialogs.status == doctor.OK
    assert "GUI host gui-host is not daemon host eda-host" in dialogs.detail
    assert ctx.inspections == []


def test_a_timed_out_query_stops_further_skill(monkeypatch, tmp_path):
    timeout = VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Socket timeout after 10s"])
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "hiIsFormDisplayed": timeout})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)

    results = _run(ctx)

    assert results["license checkout"].status == doctor.SKIP
    assert "did not answer" in results["license checkout"].detail
    assert not any("UseNextLicense" in e or "cdsGetInstPath" in e for e in client.sent)


# -- 6. license -----------------------------------------------------------------------------

def test_license_prompting_tiers_warn_with_cdsinit_lines(monkeypatch, tmp_path):
    answer = ('("VSEL_UseNextLicense=ask" "VLSL_UseNextLicense=always" "VLSXL_UseNextLicense=never" '
              '"ADEL_UseNextLicense=<unset>" "ADEXL_UseNextLicense=always" "ADE_UseNextLicense=always" '
              '"Explorer_UseNextLicense=<unset>")')
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "UseNextLicense": answer})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)

    result = _run(ctx)["license checkout"]

    assert result.status == doctor.WARN
    assert result.detail.startswith('UseNextLicense is "ask" for VSEL and "never" for VLSXL: ')
    assert result.fix.splitlines() == [
        "Add to .cdsinit:",
        'envSetVal("license" "VSEL_UseNextLicense" \'string "always")',
        'envSetVal("license" "VLSXL_UseNextLicense" \'string "always")',
    ]
    # The fix is text for the user; the doctor itself never sets anything.
    assert not any("envSetVal" in e for e in client.sent)


def test_license_all_always_is_ok_and_undefined_ones_are_named(monkeypatch, tmp_path):
    answer = '("VSEL_UseNextLicense=always" "ADE_UseNextLicense=<unset>")'
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "UseNextLicense": answer})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)
    result = _run(ctx)["license checkout"]
    assert result.status == doctor.OK
    assert result.detail == 'UseNextLicense is "always" for VSEL (ADE not defined)'


@pytest.mark.parametrize("value,needle", [
    ("<unknown>", "no envIsVal()"), ("<unset>", "no UseNextLicense variable is defined"),
])
def test_license_variables_unknown_to_this_virtuoso_skip(monkeypatch, tmp_path, value, needle):
    answer = f'("VSEL_UseNextLicense={value}" "ADE_UseNextLicense={value}")'
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "UseNextLicense": answer})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)
    result = _run(ctx)["license checkout"]
    assert result.status == doctor.SKIP
    assert needle in result.detail


# The answer of a fresh IC 6.1.8 session (no .cdsinit) to license_query(), recorded live:
# its tools/dfII/etc/tools/license/.cdsenv defines these nine, all "prompt" by default.
IC618_DEFAULTS = ("VLSL", "VLSXL", "VSEL", "ADEL", "ADEXL", "VLSAdvOpt", "VIVA", "ADE", "Explorer")


def test_license_defaults_of_a_fresh_session_warn_for_every_tier(monkeypatch, tmp_path):
    answer = "(" + " ".join(f'"{f}_UseNextLicense=prompt"' for f in IC618_DEFAULTS) + ")"
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "UseNextLicense": answer})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)

    result = _run(ctx)["license checkout"]

    assert result.status == doctor.WARN
    assert result.detail.startswith(
        'UseNextLicense is "prompt" for VLSL, VLSXL, VSEL, ADEL, ADEXL, VLSAdvOpt, VIVA, ADE, Explorer: ')
    assert result.fix.splitlines()[1:] == [
        f'envSetVal("license" "{f}_UseNextLicense" \'string "always")' for f in IC618_DEFAULTS]


def test_license_variables_named_only_by_the_installation_are_checked(monkeypatch, tmp_path):
    # A release can define a variable the built-in list does not know.
    answer = '("VSEL_UseNextLicense=always" "NewTool_UseNextLicense=prompt")'
    client = FakeDaemonClient(skill={**HEALTHY_SKILL, "UseNextLicense": answer})
    ctx, _, _ = _context(monkeypatch, tmp_path, client=client)

    result = _run(ctx)["license checkout"]

    assert result.status == doctor.WARN
    assert result.detail.startswith('UseNextLicense is "prompt" for NewTool: ')
    assert result.fix.splitlines()[1:] == [
        'envSetVal("license" "NewTool_UseNextLicense" \'string "always")']


def test_license_fallback_names_cover_the_editors():
    for feature in ("VSEL", "VLSL", "VLSXL", "VLSAdvOpt", "VIVA"):
        assert feature in doctor.LICENSE_FEATURES


def test_license_query_is_guarded_and_reads_only():
    query = doctor.license_query()
    # Names come from the running installation's defaults file, read line by line...
    assert f'cdsGetInstPath("{doctor.LICENSE_CDSENV}")' in query
    assert doctor.LICENSE_CDSENV == "tools/dfII/etc/tools/license/.cdsenv"
    assert "infile(vbFile)" in query and "close(vbPort)" in query
    # ...and the built-in list is only the fallback when that file yields nothing.
    fallback = " ".join(f'"{feature}_UseNextLicense"' for feature in doctor.LICENSE_FEATURES)
    assert f"or(vbNames list({fallback}))" in query
    assert "isCallable('envIsVal)" in query and 'envIsVal("license" vbVar t)' in query
    assert "envSetVal" not in query


# -- 7. spectre -----------------------------------------------------------------------------

@pytest.mark.parametrize("routes,env,status,needle", [
    ({"SPECTRE_PATH": "SPECTRE_PATH=NOTFOUND\n"}, {}, doctor.WARN, "not found on PATH"),
    ({"SPECTRE_PATH": "SPECTRE_PATH=NOTFOUND\n"}, {"VB_SPECTRE_BIN": "site-wrapper spectre"},
     doctor.FAIL, "VB_SPECTRE_BIN=site-wrapper spectre does not name an executable"),
    ({"SPECTRE_PATH": CommandResult(1, "", "/cad/env/cadence.sh: not found")},
     {"VB_CADENCE_CSHRC": "/cad/env/cadence.sh", "VB_CADENCE_ENV_SHELL": "sh"},
     doctor.FAIL, "environment/probe error on eda-host: /cad/env/cadence.sh: not found"),
    ({"SPECTRE_PATH": "SPECTRE_PATH=/cad/ic/bin/spectre\nsite banner\n"}, {}, doctor.OK,
     "/cad/ic/bin/spectre on eda-host: version not reported by -V"),
    ({"SPECTRE_PATH": CommandResult(255, "", "ssh: Could not resolve hostname eda-host")}, {},
     doctor.FAIL, "SSH to eda-host failed"),
])
def test_spectre_outcomes(monkeypatch, tmp_path, routes, env, status, needle):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ctx, runner, _ = _context(monkeypatch, tmp_path, runner=FakeRunner(routes), running=False)
    result = doctor.check_spectre(_configured(ctx))
    assert result.status == status, result
    assert needle in result.detail
    if status != doctor.OK and "SSH" not in result.detail and "version" not in result.detail:
        assert "VB_CADENCE_CSHRC" in result.fix and "VB_SPECTRE_BIN" in result.fix
    probe = next(command for command, _ in runner.commands if "SPECTRE_PATH" in command)
    assert "-V" in probe


def test_spectre_on_this_machine_uses_the_local_probe(monkeypatch):
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setattr("virtuoso_bridge.spectre.runner.probe_spectre_locally",
                        lambda spectre_bin="": ("/cad/ic/bin/spectre", "@(#)$CDS: spectre version 23.1"))
    result = doctor.check_spectre(_configured(doctor.DoctorContext(None)))
    assert result.status == doctor.OK
    assert "this machine" in result.detail


# -- 8. documentation ---------------------------------------------------------------------

def test_docs_found_through_the_running_virtuoso(monkeypatch, tmp_path):
    runner = FakeRunner({"which virtuoso": "NOTFOUND\n"})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner)

    result = _run(ctx)["documentation"]

    assert result.status == doctor.OK, result
    assert "via install root /cad/ic reported by the running Virtuoso" in result.detail
    assert client.sent.count("cdsGetInstPath()") == 1


def test_no_docs_warns_with_the_cadence_environment_hint(monkeypatch, tmp_path):
    runner = FakeRunner({"which virtuoso": "NOTFOUND\n"})
    ctx, _, client = _context(monkeypatch, tmp_path, runner=runner, running=False)
    result = _run(ctx)["documentation"]
    assert result.status == doctor.WARN
    assert "no Cadence documentation found on eda-host" in result.detail
    assert "VB_CADENCE_CSHRC" in result.fix and "VB_CADENCE_ENV_SHELL=sh" in result.fix
    assert client.sent == []


def test_docs_without_skill_finder_warn(monkeypatch, tmp_path):
    runner = FakeRunner({"which virtuoso": "NOTFOUND\n", "CADENCE_DOC_ROOT": "DOC\t/cad/docs\n"})
    ctx, _, _ = _context(monkeypatch, tmp_path, runner=runner, running=False)
    result = _run(ctx)["documentation"]
    assert result.status == doctor.WARN
    assert "doc-search uses /cad/docs" in result.detail and "skill-find finds no" in result.detail


def test_docs_ssh_failure_fails(monkeypatch, tmp_path):
    runner = FakeRunner({"which virtuoso": CommandResult(255, "", "ssh: connect to host eda-host port 22: timed out")})
    ctx, _, _ = _context(monkeypatch, tmp_path, runner=runner, running=False)
    result = _run(ctx)["documentation"]
    assert result.status == doctor.FAIL
    assert "SSH to eda-host failed" in result.detail


# -- read-only guards ------------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    'envSetVal("license" "VSEL_UseNextLicense" \'string "always")',
    "hiFormDone(tsmc_PDK_info_table)", "hiFormCancel(form)", "dbSave(cv)",
    'load("/tmp/x.il")', "RBStop()", 'techGetTechFile(ddGetObj("pdkLib"))',
])
def test_mutating_skill_is_refused(expr):
    with pytest.raises(ValueError, match="mutating SKILL"):
        doctor.assert_read_only_skill(expr)


def test_every_doctor_query_passes_the_guard_and_is_balanced():
    for expr in (doctor.license_query(), doctor.pdk_forms_query(), doctor.BIND_QUERY,
                 "cdsGetInstPath()", 'getShellEnvVar("USER")', "ipcGetPid()"):
        doctor.assert_read_only_skill(expr)
        depth = 0
        for char in re.sub(r'"(?:\\.|[^"\\])*"', '""', expr):
            depth += {"(": 1, ")": -1}.get(char, 0)
            assert depth >= 0, expr
        assert depth == 0, expr


def test_read_only_client_refuses_before_sending_and_bounds_time(monkeypatch):
    client = doctor.ReadOnlyVirtuosoClient(daemon_token=TOKEN, max_timeout=3)
    sent = []
    monkeypatch.setattr(client, "_execute_skill_unguarded",
                        lambda code, timeout=None, retry_connect=True: sent.append((code, timeout, retry_connect))
                        or _ok("2"))
    with pytest.raises(ValueError):
        client.execute_skill('envSetVal("license" "ADE_UseNextLicense" \'string "always")')
    assert sent == []
    client.execute_skill("1+1", timeout=60)
    assert sent == [("1+1", 3, False)]


def test_read_only_runner_bounds_time_and_has_no_transfers():
    runner = FakeRunner()
    bounded = doctor.ReadOnlyRunner(runner, 5)
    for name in ("upload", "upload_text", "download", "upload_batch"):
        assert not hasattr(bounded, name)
    bounded.run_command("true")
    bounded.run_command("true", timeout=600)
    bounded.run_command("true", timeout=2)
    assert [timeout for _, timeout in runner.commands] == [5, 5, 2]
    assert runner._verbose is False


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh")
                    or not (shutil.which("python3") or shutil.which("python")),
                    reason="requires a POSIX shell and Python on PATH")
def test_dialog_inspection_can_stream_its_helper_without_remote_files(tmp_path):
    """deploy_helper=False feeds the helper to ``python -``: no mkdir, no upload."""
    runner = FakeRunner()
    remote = tmp_path / "remote"
    remote.mkdir()

    def run(command, timeout=None):
        runner.commands.append((command, timeout))
        proc = subprocess.run(["sh", "-s"], input=command, capture_output=True, text=True,
                              timeout=30, cwd=remote)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    runner.run_command = run
    pid = os.getpid()

    payload = x11.inspect_dialogs(runner, "designer", pid=pid, timeout=20, deploy_helper=False)

    assert payload["target"]["pid"] == pid
    assert payload["status"] in ("clear", "blocked", "indeterminate")
    assert not any("invalid inspection" in d for d in payload.get("diagnostics", []))
    assert runner.writes == [] and list(remote.iterdir()) == []
    helper_run = runner.commands[-1][0]
    assert " - --inspect-dialogs --pid " in helper_run
    assert "<<'VB_DIALOG_HELPER_SOURCE_END'" in helper_run
    for command, _ in runner.commands:
        shell_part = command.split("<<'VB_DIALOG_HELPER_SOURCE_END'")[0]
        assert not _writes(shell_part), shell_part


# -- CLI --------------------------------------------------------------------------------------

def test_cli_help_says_it_changes_nothing(capsys):
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Read-only" in text and "never starts, stops or restarts" in text
    assert "dismisses no dialog" in text and "--json" in text


def test_cli_json_output_and_exit_code(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "local.env"
    env_file.write_text("VB_REMOTE_HOST=localhost\n", encoding="utf-8")
    monkeypatch.setattr("virtuoso_bridge.spectre.runner.probe_spectre_locally",
                        lambda spectre_bin="": (None, None))
    monkeypatch.setattr("virtuoso_bridge.virtuoso.skill_finder.SKILLFinder.discover",
                        lambda self, remote_runner=None, profile=None: None)

    rc = cli.main(["doctor", "--json", "--env", str(env_file)])

    rows = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in rows] == [name for name, _ in doctor.CHECKS]
    assert all(set(row) == {"name", "status", "detail", "fix"} for row in rows)
    by_name = {row["name"]: row for row in rows}
    assert by_name["configuration"]["status"] == "OK"
    assert by_name["tunnel/daemon"]["status"] == "WARN"
    assert by_name["spectre"]["status"] == "WARN" and by_name["spectre"]["fix"]
    assert rc == 0
    assert not (tmp_path / "state").exists()


def test_cli_text_output_fails_without_a_host(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "empty.env"
    env_file.write_text("VB_REMOTE_HOST=\n", encoding="utf-8")

    rc = cli.main(["doctor", "--env", str(env_file)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "read-only, changes nothing" in out
    assert "[FAIL] configuration:" in out and "fix: Set VB_REMOTE_HOST" in out
    assert "[SKIP] tunnel/daemon: not checked" in out
    assert out.rstrip().endswith("0 OK, 0 WARN, 1 FAIL, 7 SKIP")


def test_registered_entrypoint_runs_doctor_offline(tmp_path):
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the virtuoso-bridge console script is not installed in this environment")
    env_file = tmp_path / "local.env"
    env_file.write_text("VB_REMOTE_HOST=localhost\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("VB_") and key not in {"HOME", "USERPROFILE", "VIRTUAL_ENV"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), VB_LOG_DIR=str(tmp_path / "logs"),
               VB_STATE_DIR=str(tmp_path / "state"), VB_CACHE_DIR=str(tmp_path / "cache"))

    result = subprocess.run([script, "doctor", "--json", "--env", str(env_file)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=120)

    rows = json.loads(result.stdout)
    assert [row["name"] for row in rows] == [name for name, _ in doctor.CHECKS], result.stderr
    statuses = {row["name"]: row["status"] for row in rows}
    assert statuses["configuration"] == "OK"
    assert statuses["tunnel/daemon"] == "WARN"
    assert statuses["dialogs"] == statuses["license checkout"] == "SKIP"
    assert result.returncode == (1 if "FAIL" in statuses.values() else 0)
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "state").exists()


# -- helpers ----------------------------------------------------------------------------------

def _configured(ctx):
    """Run the configuration check first, as run_checks does."""
    assert doctor.check_configuration(ctx).status == doctor.OK
    return ctx


def _after_daemon(ctx):
    _configured(ctx)
    doctor.check_daemon(ctx)
    return ctx
