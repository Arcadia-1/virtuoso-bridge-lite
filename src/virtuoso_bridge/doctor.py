"""Read-only setup diagnosis behind ``virtuoso-bridge doctor``.

Each check observes and reports ``OK``/``WARN``/``FAIL``/``SKIP`` with a
one-line reason and, for WARN/FAIL, a concrete fix. Nothing here starts,
stops or restarts a tunnel or daemon, deploys files, writes on the remote
host, changes a Virtuoso setting or dismisses a dialog: remote commands are
bounded reads, and every SKILL query is checked against a list of mutating
functions before it is sent. Checks that need the daemon are skipped when no
tunnel or daemon answers; the doctor never starts one.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
_SEVERITY = {SKIP: 0, OK: 1, WARN: 2, FAIL: 3}

# Bounds for every remote call: SSH commands, the handshake, SKILL queries.
REMOTE_TIMEOUT = 10.0
# Commands that source the site's Cadence environment first can be slower.
ENV_TIMEOUT = 30.0
X11_TIMEOUT = 15.0

# ``<X>_UseNextLicense`` variables of the "license" tool. Anything but
# "always" lets a missing lower-tier license pop a modal "Next License"
# dialog on the first edit, which blocks the CIW. The query reads the names a
# version defines from its installed defaults (LICENSE_CDSENV); this list is
# the fallback when that file cannot be read (IC 6.1.8 defines exactly these).
LICENSE_FEATURES = ("VSEL", "VLSL", "VLSXL", "VLSAdvOpt", "VIVA", "ADEL", "ADEXL", "ADE", "Explorer")
LICENSE_CDSENV = "tools/dfII/etc/tools/license/.cdsenv"

# SKILL form symbols of PDK first-use popups that block SKILL until closed.
PDK_POPUP_FORMS = ("tsmc_PDK_info_table",)

PDK_HEADLESS_HINT = (
    "For a headless session, initialise the PDK tech before the bridge is used, "
    'e.g. techGetTechFile(ddGetObj("<pdkLib>")) in .cdsinit or the -restore script.'
)
CADENCE_ENV_REFERENCE = (
    "skills/virtuoso/references/connection.md#cadence-environment-on-the-remote-host"
)

# Functions a diagnosis must never call. A safety net: the doctor builds only
# read-only queries, and this refuses to send anything else.
_MUTATING_SKILL = re.compile(
    r"\b(?:envSetVal|envLoadFile|envLoadVals|setShellEnvVar|"
    r"hiForm(?:Done|Cancel|Apply|Close|Defaults)|hiDisplayForm|hiCloseWindow|"
    r"hiDeleteWindow|hiEnqueueCmd|dbSave|dbClose|dbPurge|dbCreate\w*|dbDelete\w*|"
    r"ddDeleteObj|ddCreate\w*|techSetTechFile|techGetTechFile|load|loadi|"
    r"evalstring|sh|system|ipcBeginProcess|ipcKillProcess|ipcWriteProcess|"
    r"RBStart|RBStop|RBStopAll|deleteFile|createDir|maeRunSimulation)\s*\("
)


def assert_read_only_skill(skill: str) -> None:
    """Refuse a SKILL expression that calls a known mutating function."""
    match = _MUTATING_SKILL.search(skill)
    if match:
        raise ValueError(f"doctor refuses to send mutating SKILL ({match.group(0).rstrip('( ')})")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


class ReadOnlyRunner:
    """Bounded remote commands only; no upload, download or file transfer.

    Records an SSH transport failure (an exception, or OpenSSH's exit status
    255) so a check can tell an unreachable host from a negative answer, and
    later checks need not wait for the same host again.
    """

    def __init__(self, runner: Any, timeout: float, failures: Optional[dict[str, str]] = None) -> None:
        self._runner = runner
        self._timeout = timeout
        self._failures = failures if failures is not None else {}
        self.transport_error = ""
        if hasattr(runner, "_verbose"):
            runner._verbose = False

    @property
    def host(self) -> str:
        return str(getattr(self._runner, "host", "") or "")

    def _failed(self, error: str) -> None:
        self.transport_error = error
        self._failures.setdefault(_host_key(self.host), error)

    def run_command(self, command: str, timeout: Optional[float] = None) -> Any:
        bound = self._timeout if timeout is None else min(float(timeout), self._timeout)
        try:
            result = self._runner.run_command(command, timeout=bound)
        except subprocess.TimeoutExpired:
            self._failed(f"no answer within {bound:g} s")
            raise
        except Exception as exc:
            self._failed(str(exc).strip() or type(exc).__name__)
            raise
        if getattr(result, "returncode", 0) == 255 and not self.transport_error:
            lines = (getattr(result, "stderr", "") or "").strip().splitlines()
            self._failed(lines[-1] if lines else "ssh exited with status 255")
        return result


class ReadOnlyVirtuosoClient(VirtuosoClient):
    """Daemon client that sends only read-only SKILL, each call bounded.

    Used for the doctor's own queries and for the shared helpers it calls
    (owner check, install-root fallback), so all of them pass the same guard.
    """

    def __init__(self, *args: Any, max_timeout: float = REMOTE_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_timeout = max_timeout
        self.sent: list[str] = []

    def execute_skill(self, skill_code: str, timeout: Optional[float] = None, *,
                      retry_connect: bool = True) -> Any:
        assert_read_only_skill(skill_code)
        self.sent.append(skill_code)
        bound = self._max_timeout if timeout is None else min(timeout, self._max_timeout)
        # Never resend: a request may have reached a CIW that is merely slow.
        return super().execute_skill(skill_code, timeout=bound, retry_connect=False)


def _is_localhost(host: Optional[str]) -> bool:
    from virtuoso_bridge.transport.tunnel import _is_localhost as is_local

    return is_local(host)


def _host_key(host: Optional[str]) -> str:
    return (host or "").strip().rstrip(".").lower()


def _same_host(left: Optional[str], right: Optional[str]) -> bool:
    return _host_key(left) == _host_key(right)


class DoctorContext:
    """Configuration and observations shared by the checks of one run."""

    def __init__(
        self,
        profile: Optional[str] = None,
        *,
        env_path: Optional[Path] = None,
        env_error: str = "",
        timeout: float = REMOTE_TIMEOUT,
    ) -> None:
        self.profile = profile
        self.suffix = f"_{profile}" if profile else ""
        self.env_path = env_path
        self.env_error = env_error
        self.timeout = timeout
        self.config_ok = False
        self.roles: Any = None
        self.target: Optional[str] = None
        self.state: Optional[dict[str, Any]] = None
        self.running = False
        self.daemon_client: Any = None
        self.caps: Optional[dict[str, Any]] = None
        # Why SKILL must not be sent now; empty once an idle, verified daemon answered.
        self.skill_blocker = "the daemon was not checked"
        self.skill_sent: list[str] = []
        # SSH transport failures by host, so a dead host costs one timeout.
        self.ssh_failures: dict[str, str] = {}
        self._ssh: Any = None
        self._keep: list[Any] = []

    # -- seams (tests replace these) ---------------------------------------

    def make_ssh(self) -> Any:
        """Transport configuration only; constructing it connects nowhere."""
        from virtuoso_bridge.transport.tunnel import SSHClient

        return SSHClient.from_env(keep_remote_files=True, profile=self.profile)

    def make_daemon_client(self, port: int, token: Optional[str]) -> Any:
        client = ReadOnlyVirtuosoClient(
            host="127.0.0.1", port=port, timeout=self.timeout, log_to_ciw=False,
            daemon_token=token, max_timeout=self.timeout,
        )
        if getattr(self.ssh, "_discover_instances", False):
            client._tunnel = self.ssh
            client.use_current_selection()
        return client

    def make_docs_client(self) -> Any:
        client = VirtuosoClient(host="127.0.0.1", port=self.ssh.port, tunnel=self.ssh, log_to_ciw=False)
        client._install_root_probe = self.running_install_root
        self._keep.append(client)
        return client

    # -- shared helpers ----------------------------------------------------

    @property
    def ssh(self) -> Any:
        if self._ssh is None:
            self._ssh = self.make_ssh()
            for runner in getattr(self._ssh, "_role_runners", {}).values():
                runner._verbose = False
        return self._ssh

    def runner(self, role: str, timeout: Optional[float] = None) -> Optional[ReadOnlyRunner]:
        ssh = self.ssh
        raw = {
            "daemon": lambda: ssh.ssh_runner,
            "deploy": lambda: ssh.deployment_runner,
            "gui": lambda: ssh.gui_runner,
            "spectre": lambda: ssh.spectre_runner,
        }[role]()
        return None if raw is None else ReadOnlyRunner(raw, timeout or self.timeout, self.ssh_failures)

    def ssh_failed(self, host: Optional[str]) -> str:
        """The SSH transport error an earlier command to *host* hit, or ""."""
        return self.ssh_failures.get(_host_key(host), "")

    def command_hint(self, command: str) -> str:
        return f"virtuoso-bridge {command}" + (f" -p {self.profile}" if self.profile else "")

    def read_token(self) -> tuple[Optional[str], bool]:
        """The daemon token and whether it came from the local cache.

        Read-only, unlike the start path: a missing token is reported, never
        provisioned.
        """
        ssh = self.ssh
        if getattr(ssh, "_discover_instances", False):
            return ssh.read_daemon_token(), False
        if getattr(ssh, "ssh_runner", None) is None:
            return daemon_auth.read_local_token(), False
        cache = ssh.remote_cache_path()
        cached = daemon_auth.read_remote_cache(cache).get("token") if cache else None
        if isinstance(cached, str) and daemon_auth.is_valid_token(cached):
            return cached.lower(), True
        return self.fetch_remote_token(), False

    def fetch_remote_token(self) -> Optional[str]:
        if getattr(self.ssh, "_discover_instances", False):
            return self.ssh.read_daemon_token()
        runner = self.runner("daemon")
        if runner is None:
            return None
        try:
            result = runner.run_command("cat ~/.virtuoso-bridge/bridge_token 2>/dev/null")
        except Exception:  # noqa: BLE001 - recorded as an SSH failure of the host
            return None
        text = (result.stdout or "").strip()
        return text.lower() if result.returncode == 0 and daemon_auth.is_valid_token(text) else None

    def skill(self, expr: str) -> tuple[Optional[str], str]:
        """Run one read-only SKILL query: ``(output, "")`` or ``(None, reason)``."""
        if self.skill_blocker:
            return None, self.skill_blocker
        assert_read_only_skill(expr)
        self.skill_sent.append(expr)
        result = self.daemon_client.execute_skill(expr, timeout=self.timeout, retry_connect=False)
        if result.status == ExecutionStatus.SUCCESS:
            return (result.output or "").strip(), ""
        error = "; ".join(result.errors) or "query failed"
        if "timeout" in error.lower():
            # Do not queue more requests behind a CIW that stopped answering.
            self.skill_blocker = (
                f"the CIW did not answer a query within {self.timeout:g} s "
                "(a dialog may block it)"
            )
        return None, error

    def running_install_root(self) -> Optional[str]:
        """Install root of the connected Virtuoso, for documentation discovery."""
        from virtuoso_bridge.virtuoso.basic.bridge import _install_root_from_skill_output

        output, _reason = self.skill("cdsGetInstPath()")
        return _install_root_from_skill_output(output) if output else None

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:  # noqa: BLE001 - closing must not mask results
                pass


# -- helpers for results ----------------------------------------------------

def _skip_unconfigured(name: str, ctx: DoctorContext) -> CheckResult:
    reason = "the configuration could not be loaded" if ctx.env_error else "no daemon host is configured"
    return CheckResult(name, SKIP, f"not checked: {reason}")


def _ssh_failure(name: str, ctx: DoctorContext, host: str) -> CheckResult:
    user = ctx.roles.remote_user if ctx.roles is not None else None
    target = f"{user}@{host}" if user else host
    return CheckResult(
        name, FAIL, f"SSH to {host} failed: {ctx.ssh_failed(host) or 'no answer'}",
        f"Check VB_REMOTE_HOST{ctx.suffix}/VB_REMOTE_USER{ctx.suffix} (and VB_JUMP_HOST{ctx.suffix}); "
        f"`ssh {target} true` must work without a password prompt.",
    )


def _ssh_already_failed(name: str, ctx: DoctorContext, host: str) -> Optional[CheckResult]:
    error = ctx.ssh_failed(host)
    if not error:
        return None
    return CheckResult(name, SKIP, f"not checked: SSH to {host} already failed ({error})")


def _combine(name: str, parts: list[tuple[str, str, str]]) -> CheckResult:
    """One result from (status, detail, fix) parts: the most severe status wins."""
    status = max((part[0] for part in parts), key=_SEVERITY.__getitem__)
    fixes: list[str] = []
    for part_status, _detail, fix in parts:
        if fix and part_status in (WARN, FAIL) and fix not in fixes:
            fixes.append(fix)
    return CheckResult(name, status, "; ".join(part[1] for part in parts), " ".join(fixes))


# -- 1. configuration ---------------------------------------------------------

def check_configuration(ctx: DoctorContext) -> CheckResult:
    name = "configuration"
    if ctx.env_error:
        return CheckResult(
            name, FAIL, ctx.env_error,
            "Pass an existing file to --env, or omit --env to use ./.env or ~/.virtuoso-bridge/.env.",
        )
    from virtuoso_bridge.profile import resolve_profile_info
    from virtuoso_bridge.transport.remote_roles import remote_host_roles_from_os
    from virtuoso_bridge.transport.tunnel import _configured_target

    ctx.roles = remote_host_roles_from_os(ctx.profile, load=False)
    ctx.target = _configured_target(ctx.profile)
    info = resolve_profile_info()
    if ctx.profile:
        source = info.source if info.profile == ctx.profile else "-p/--profile"
        profile_text = f"{ctx.profile} (from {source})"
    else:
        profile_text = "default"
    parts = [
        f".env {ctx.env_path}" if ctx.env_path else ".env none found (process environment only)",
        f"profile {profile_text}",
    ]
    if ctx.target is None:
        from virtuoso_bridge.cli import _discover_profiles

        parts.append("no daemon host is configured")
        profiles = [profile for profile in _discover_profiles() if profile]
        others = f", or select a configured profile (-p {' | '.join(profiles)})" if profiles else ""
        return CheckResult(
            name, FAIL, "; ".join(parts),
            f"Set VB_REMOTE_HOST{ctx.suffix} (or VB_DAEMON_HOST{ctx.suffix}) in the .env, pass "
            "--env FILE, or run `virtuoso-bridge init designer@eda-host` to create "
            f"~/.virtuoso-bridge/.env{others}.",
        )
    ctx.config_ok = True
    roles = ctx.roles
    mode = "local, no SSH" if _is_localhost(roles.daemon_host) else "remote"
    parts.append(f"daemon target {ctx.target} ({mode})")
    for label, host in (("GUI", roles.gui_host), ("deploy", roles.deploy_host),
                        ("Spectre", roles.spectre_host)):
        if host and not _same_host(host, roles.daemon_host):
            parts.append(f"{label} host {host}")
    return CheckResult(name, OK, "; ".join(parts))


# -- 2. tunnel and daemon ---------------------------------------------------

def _handshake(client: Any, timeout: float) -> tuple[Optional[dict[str, Any]], Optional[Exception]]:
    """Signed capability handshake; answered by the daemon, not the CIW."""
    try:
        return client._ensure_daemon_capabilities(time.monotonic() + timeout), None
    except (daemon_auth.DaemonAuthError, OSError) as exc:
        return None, exc


def _handshake_failure(name: str, ctx: DoctorContext, where: str, error: Exception) -> CheckResult:
    text = str(error).strip() or type(error).__name__
    setup = str((ctx.state or {}).get("setup_path") or "")
    if isinstance(error, OSError) or "did not answer the bridge capability handshake" in text:
        # Through an SSH tunnel, a port nobody listens on looks like a closed reply.
        fix = (f'Load the bridge in the CIW: load("{setup}")' if setup
               else "Load the generated virtuoso_setup.il in the CIW.")
        return CheckResult(
            name, WARN, f"{where} running, but no bridge daemon answered ({text})",
            fix + " Doctor never starts or reloads the daemon.",
        )
    if "AuthError" in text:
        return CheckResult(
            name, FAIL, f"{where}: the daemon rejected this client's token ({text})",
            "Another user's daemon may hold this port, or the token changed: check "
            f"`{ctx.command_hint('status')}`, then reload your own daemon with "
            f"`{ctx.command_hint('restart')}`.",
        )
    return CheckResult(
        name, FAIL, f"{where}: handshake failed ({text})",
        f"Run `{ctx.command_hint('restart')}` (or re-load virtuoso_setup.il in the CIW) "
        "to run the current daemon.",
    )


def check_daemon(ctx: DoctorContext) -> CheckResult:
    name = "tunnel/daemon"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.cli import _describe_lane
    from virtuoso_bridge.daemon_guard import OVERRIDE_ENV, check_daemon_user
    from virtuoso_bridge.transport.tunnel import SSHClient

    ctx.state = SSHClient.read_state(ctx.profile)
    ctx.running = SSHClient.is_running(ctx.profile)
    local = _is_localhost(ctx.roles.daemon_host)
    start_fix = (f"Run `{ctx.command_hint('start')}`, then load the printed setup file in the "
                 "CIW. Doctor never starts a tunnel.")
    if not ctx.running:
        if local:
            ctx.skill_blocker = "the local bridge is not set up"
            return CheckResult(name, WARN, "local bridge not set up; no daemon contacted", start_fix)
        ctx.skill_blocker = "no tunnel is running"
        detail = f"no tunnel running for {ctx.target}; no daemon contacted"
        others = [f"{target} [{other}]" if other else target
                  for other, target in SSHClient.running_targets()]
        if others:
            detail += f" (other tunnels running: {', '.join(others)})"
        return CheckResult(name, WARN, detail, start_fix)

    try:
        port = int((ctx.state or {}).get("port"))
    except (TypeError, ValueError):
        ctx.skill_blocker = "the saved tunnel state names no port"
        return CheckResult(name, WARN, "the saved tunnel state names no port", start_fix)
    where = f"local daemon port {port}" if local else f"tunnel to {ctx.target} (local port {port})"

    automatic = getattr(ctx.ssh, "_discover_instances", False)
    if automatic:
        from virtuoso_bridge.instance_discovery import validate_record
        try:
            record = validate_record(ctx.ssh.instance_record)
            if record not in ctx.ssh.list_instances():
                raise ValueError("saved instance is no longer live")
            if record["port"] != (ctx.state or {}).get("remote_port"):
                raise ValueError("saved endpoint differs from selected instance")
        except (ValueError, RuntimeError) as exc:
            ctx.skill_blocker = "selected instance is not verified"
            return CheckResult(name, FAIL, str(exc),
                               "Use sessions and start to select explicitly. Doctor does not retarget.")

    token, from_cache = ctx.read_token()
    if token is None and not local and ctx.ssh_failed(ctx.roles.daemon_host):
        ctx.skill_blocker = "SSH to the daemon host failed"
        return _ssh_failure(name, ctx, ctx.roles.daemon_host)
    if token is None and (automatic or not daemon_auth.allow_unauthenticated()):
        ctx.skill_blocker = "no bridge token was found"
        token_home = "this machine" if local else ctx.roles.daemon_host
        return CheckResult(
            name, FAIL, f"{where} running, but no valid bridge token in "
            f"~/.virtuoso-bridge/bridge_token on {token_home}",
            f"Run `{ctx.command_hint('restart')}`; start provisions the token, doctor only reads it.",
        )
    client = ctx.make_daemon_client(port, token)
    ctx.daemon_client = client
    caps, error = _handshake(client, ctx.timeout)
    if isinstance(error, daemon_auth.DaemonAuthError) and from_cache:
        fresh = ctx.fetch_remote_token()  # the cached copy may be stale
        if fresh and fresh != token:
            client.daemon_token = fresh
            caps, error = _handshake(client, ctx.timeout)
    if error is not None or caps is None:
        ctx.skill_blocker = "the daemon did not complete the handshake"
        return _handshake_failure(name, ctx, where, error or RuntimeError("no capabilities"))
    ctx.caps = caps
    ctx.skill_blocker = ""

    facts = [f"{where} running"]
    fixes: list[str] = []
    status = OK
    if caps.get("auth") == "on":
        facts.append(f"signed handshake from Virtuoso pid {caps.get('virtuoso_pid')}")
    else:
        status = WARN
        facts.append("the daemon runs WITHOUT token authentication")
        fixes.append(
            f"Unset {daemon_auth.UNAUTH_OPTIN_ENV} and {daemon_auth.DAEMON_UNAUTH_OPTIN_ENV}, "
            f"then `{ctx.command_hint('restart')}`."
        )
    lane = _describe_lane(caps)
    if lane:
        status = WARN
        facts.append(f"IPC lane {lane}")
        ctx.skill_blocker = f"the daemon's IPC lane is {lane}"
        fixes.append(
            "Let the CIW finish or close what occupies it; a poisoned lane resynchronizes "
            "once the CIW answers. Doctor sends no SKILL meanwhile."
        )

    check = check_daemon_user(client, profile=ctx.profile, timeout=int(ctx.timeout),
                              runner=ctx.runner("daemon"))
    if check.busy:
        status = WARN
        facts.append(f"daemon user not verified: {check.error}")
    elif not check.ok:
        ctx.skill_blocker = "the daemon's owner was not verified"
        return CheckResult(
            name, FAIL, "; ".join(facts + [check.error]),
            f"Make the tunnel reach your own daemon (check VB_REMOTE_PORT{ctx.suffix}, then "
            f"`{ctx.command_hint('restart')}`); set {OVERRIDE_ENV}=1 only if this cross-user "
            "connection is intentional.",
        )
    elif check.skipped:
        status = WARN
        facts.append("daemon user not checked: no daemon answered the query")
    elif check.daemon_user and check.expected_user and check.daemon_user != check.expected_user:
        status = WARN
        facts.append(f"daemon user {check.daemon_user} is not SSH user {check.expected_user} "
                     f"({OVERRIDE_ENV} is set)")
        fixes.append(f"Unset {OVERRIDE_ENV} unless this cross-user connection is intentional.")
    elif check.daemon_user:
        facts.append(f"daemon user {check.daemon_user}"
                     + (f" matches SSH user {check.expected_user}" if check.expected_user else ""))
    return CheckResult(name, status, "; ".join(facts), " ".join(fixes))


# -- 3. listen address ------------------------------------------------------

BIND_QUERY = "if(boundp('RBLastBind) RBLastBind \"\")"


def check_listen_address(ctx: DoctorContext) -> CheckResult:
    name = "listen address"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.daemon_guard import clean_skill_output

    key = f"VB_DAEMON_LISTEN_ALL{ctx.suffix}"
    listen_all = os.getenv(key, "").strip() == "1"
    bind, source = "", ""
    if ctx.state:
        try:
            # The banner the CIW-side loader saved next to the setup file.
            bind = str(ctx.ssh.read_daemon_identity().get("bind") or "")
            source = "daemon banner"
        except Exception:  # noqa: BLE001 - fall back to the CIW or the configuration
            bind = ""
    if not bind and not ctx.skill_blocker:
        output, _reason = ctx.skill(BIND_QUERY)
        bind = clean_skill_output(output)
        source = "RBLastBind in the CIW"
    opt_out = (f"Remove {key}=1 unless clients connect without an SSH tunnel, then run "
               f"`{ctx.command_hint('restart')}`.")
    if bind:
        host = bind.rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            fix = opt_out if listen_all else (
                f"The daemon was started by an older setup; `{ctx.command_hint('restart')}` "
                "reloads it on 127.0.0.1.")
            return CheckResult(name, WARN, f"the daemon listens on {bind} ({source}), not only "
                               "on loopback", fix)
        if listen_all:
            return CheckResult(name, WARN, f"the daemon listens on {bind} ({source}), but {key}=1 "
                               "makes the next start listen on all interfaces", opt_out)
        return CheckResult(name, OK, f"the daemon listens on {bind} ({source})")
    if listen_all:
        return CheckResult(name, WARN, f"{key}=1 asks the daemon to listen on all interfaces "
                           "(0.0.0.0)", opt_out)
    return CheckResult(name, OK, "the configuration keeps the daemon on 127.0.0.1 "
                       "(no bind address reported yet)")


# -- 4. remote scratch directory ----------------------------------------------

def scratch_inspection_script(paths: list[str]) -> str:
    """POSIX sh that reports, without changing anything, each directory's state.

    One line per path: ``SYMLINK``/``MISSING``/``NOTDIR``/``UNKNOWN`` path, or
    ``DIR uid mode path``. The first line is ``UID <uid>``.
    """
    quoted = " ".join(shlex.quote(path) for path in paths)
    return (
        "printf 'UID %s\\n' \"$(id -u 2>/dev/null)\"\n"
        f"for vb_path in {quoted}; do\n"
        "    if [ -L \"$vb_path\" ]; then printf 'SYMLINK %s\\n' \"$vb_path\"\n"
        "    elif [ ! -e \"$vb_path\" ]; then printf 'MISSING %s\\n' \"$vb_path\"\n"
        "    elif [ ! -d \"$vb_path\" ]; then printf 'NOTDIR %s\\n' \"$vb_path\"\n"
        "    elif vb_meta=$(stat -c '%u %a' \"$vb_path\" 2>/dev/null) ||\n"
        "        vb_meta=$(stat -f '%u %Lp' \"$vb_path\" 2>/dev/null); then\n"
        "        printf 'DIR %s %s\\n' \"$vb_meta\" \"$vb_path\"\n"
        "    else printf 'UNKNOWN %s\\n' \"$vb_path\"\n"
        "    fi\n"
        "done\n"
    )


def classify_scratch(stdout: str, paths: list[str], root: str) -> tuple[str, str, str]:
    """(status, detail, fix) for the output of :func:`scratch_inspection_script`."""
    from virtuoso_bridge.transport.remote_paths import REMOTE_SCRATCH_ROOT_ENV

    uid = ""
    seen: dict[str, tuple[str, str, str]] = {}
    for line in (stdout or "").splitlines():
        if line.startswith("UID "):
            uid = line[4:].strip()
            continue
        kind, _, rest = line.partition(" ")
        if kind == "DIR":
            owner, mode, path = (rest.split(" ", 2) + ["", ""])[:3]
            seen[path] = (kind, owner, mode)
        elif kind in ("SYMLINK", "MISSING", "NOTDIR", "UNKNOWN"):
            seen[rest] = (kind, "", "")
    work_dir = paths[-1]
    if not uid or any(path not in seen for path in paths):
        return WARN, f"could not inspect {work_dir} (unexpected output)", (
            f"Inspect it yourself: ls -ld {' '.join(shlex.quote(p) for p in paths)}")
    failures, loose, unknown = [], [], []
    for path in paths:
        kind, owner, mode = seen[path]
        if kind == "SYMLINK":
            failures.append(f"{path} is a symlink")
        elif kind == "NOTDIR":
            failures.append(f"{path} is not a directory")
        elif kind == "UNKNOWN":
            unknown.append(path)
        elif kind == "DIR":
            if owner != uid:
                failures.append(f"{path} is owned by uid {owner}, not you (uid {uid})")
            else:
                try:
                    if int(mode, 8) & 0o077:
                        loose.append((path, mode))
                except ValueError:
                    unknown.append(path)
    if failures:
        return FAIL, "; ".join(failures), (
            f"Remove or rename the listed path (start refuses to deploy into it), or set "
            f"{REMOTE_SCRATCH_ROOT_ENV} to a directory only you can write; then run "
            "`virtuoso-bridge start`.")
    if loose:
        listed = ", ".join(f"{path} (mode {mode})" for path, mode in loose)
        return WARN, f"group/other can access {listed}", (
            f"Run `chmod 700 {' '.join(shlex.quote(path) for path, _ in loose)}` on the "
            "deploy host; the next `virtuoso-bridge start` also tightens them.")
    if unknown:
        return WARN, f"could not read the owner/mode of {', '.join(unknown)}", (
            f"Inspect it yourself: ls -ld {' '.join(shlex.quote(p) for p in unknown)}")
    if seen[work_dir][0] == "MISSING":
        existing = [path for path in paths if seen[path][0] == "DIR"]
        note = f" ({', '.join(existing)} private)" if existing else ""
        return SKIP, f"not deployed yet: {work_dir} does not exist{note}", ""
    return OK, f"{work_dir} and each directory below {root} are yours, mode 0700, no symlinks", ""


def check_scratch_dir(ctx: DoctorContext) -> CheckResult:
    name = "remote scratch directory"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.transport.remote_paths import (
        default_virtuoso_bridge_dir,
        private_dirs_below,
        remote_scratch_root,
    )

    host = ctx.roles.deploy_host
    if _is_localhost(host):
        return CheckResult(name, SKIP, "local mode: the bridge deploys into this machine's "
                           "state directory, not a remote scratch directory")
    runner = ctx.runner("deploy")
    if runner is None:
        return CheckResult(name, SKIP, "no SSH transport to the deploy host")
    skipped = _ssh_already_failed(name, ctx, host)
    if skipped is not None:
        return skipped
    result = None
    try:
        from virtuoso_bridge.transport.tunnel import _profiled_bridge_leaf
        from virtuoso_bridge.transport.remote_paths import resolve_client_id
        automatic = getattr(ctx.ssh, "_discover_instances", False)
        root = remote_scratch_root(runner)
        work_dir = default_virtuoso_bridge_dir(
            "", "daemon" if automatic else _profiled_bridge_leaf(ctx.profile),
            None if automatic else resolve_client_id(ctx.profile), runner=runner)
        paths = [root] + private_dirs_below(root, work_dir)
        if not runner.transport_error:
            result = runner.run_command(scratch_inspection_script(paths))
    except Exception:  # noqa: BLE001 - an SSH failure is reported below
        if not runner.transport_error:
            raise
    if result is None or runner.transport_error:
        return _ssh_failure(name, ctx, host)
    status, detail, fix = classify_scratch(result.stdout, paths, root)
    return CheckResult(name, status, detail, fix)


# -- 5. dialogs and PDK popups ----------------------------------------------

def pdk_forms_query(forms: tuple[str, ...] = PDK_POPUP_FORMS) -> str:
    """SKILL listing which of *forms* exist and are displayed (read-only)."""
    names = " ".join(f'"{form}"' for form in forms)
    return (
        "let((vbShown vbSym) "
        f"foreach(vbName list({names}) "
        "vbSym = stringToSymbol(vbName) "
        "when(boundp(vbSym) && isCallable('hiIsFormDisplayed) "
        "&& car(errset(hiIsFormDisplayed(symeval(vbSym)))) "
        "vbShown = cons(vbName vbShown))) "
        "vbShown)"
    )


def _x11_dialogs(ctx: DoctorContext, pid: Any) -> tuple[str, str, str]:
    from virtuoso_bridge.virtuoso import x11
    from virtuoso_bridge.virtuoso.dialogs import DialogInspection, DialogTarget, parse_inspection

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return SKIP, "X11 not inspected: the daemon reported no signed Virtuoso PID", ""
    roles = ctx.roles
    if not _same_host(roles.gui_host, roles.daemon_host):
        return SKIP, (f"X11 not inspected: GUI host {roles.gui_host} is not daemon host "
                      f"{roles.daemon_host}; run `virtuoso-bridge inspect-dialogs --pid <PID>` "
                      "with the PID on the GUI host"), ""
    target = DialogTarget(pid=pid)
    try:
        runner = None if _is_localhost(roles.gui_host) else ctx.runner("gui", timeout=X11_TIMEOUT)
        payload = x11.inspect_dialogs(
            runner, roles.remote_user or "", pid=pid, profile=ctx.profile,
            timeout=X11_TIMEOUT, deploy_helper=False,
        )
        report = parse_inspection(payload, target)
    except Exception as exc:  # noqa: BLE001 - unknown is indeterminate, never clear
        report = DialogInspection(status="indeterminate", target=target, diagnostics=[str(exc)])
    if report.status == "blocked":
        titles = ", ".join(repr(str(item.get("title") or "(untitled)")) for item in report.dialogs)
        return FAIL, f"X11: Virtuoso pid {pid} shows candidate blocking windows: {titles}", (
            "Resolve them in Virtuoso yourself: their origin is unknown and doctor never "
            "dismisses dialogs (see skills/virtuoso/references/shared-ciw-dialogs.md). "
            "If one is a PDK first-use form: " + PDK_HEADLESS_HINT)
    if report.status == "indeterminate":
        why = report.diagnostics[0] if report.diagnostics else "no ownership evidence"
        return WARN, f"X11: inspection of Virtuoso pid {pid} is indeterminate ({why})", (
            f"Run `virtuoso-bridge inspect-dialogs --pid {pid} --json` for details; "
            "indeterminate does not mean the CIW is idle.")
    return OK, f"X11: no candidate dialogs for Virtuoso pid {pid}", ""


def _pdk_forms(ctx: DoctorContext) -> tuple[str, str, str]:
    from virtuoso_bridge.virtuoso.skill_output import parse_skill_str_list

    output, reason = ctx.skill(pdk_forms_query())
    if output is None:
        return SKIP, f"PDK forms not queried ({reason})", ""
    try:
        shown = parse_skill_str_list(output)
    except ValueError:
        return SKIP, f"PDK forms: unexpected answer {output!r}", ""
    if shown:
        return WARN, f"PDK popup form displayed: {', '.join(shown)}", (
            "Close it in Virtuoso (doctor never dismisses it); SKILL waits until it is closed. "
            + PDK_HEADLESS_HINT)
    return OK, f"no PDK popup form displayed ({', '.join(PDK_POPUP_FORMS)})", ""


def check_dialogs(ctx: DoctorContext) -> CheckResult:
    name = "dialogs"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    if ctx.caps is None:
        return CheckResult(name, SKIP, f"not checked: needs the daemon ({ctx.skill_blocker})")
    pid = ctx.caps.get("virtuoso_pid") if ctx.caps.get("auth") == "on" else None
    x11_part = _x11_dialogs(ctx, pid)
    if x11_part[0] == FAIL:
        ctx.skill_blocker = "a dialog blocks the CIW"
    return _combine(name, [x11_part, _pdk_forms(ctx)])


# -- 6. license checkout policy -----------------------------------------------

def license_query(features: tuple[str, ...] = LICENSE_FEATURES) -> str:
    """SKILL reading ``<X>_UseNextLicense`` of the "license" tool (read-only).

    The names are the ``license <X>_UseNextLicense`` lines of the running
    installation's ``LICENSE_CDSENV``, so a release that adds or drops a
    variable is covered without a code change; ``features`` is used only when
    that file cannot be read. ``envIsVal(... t)`` faults the tool in if nothing
    has loaded it yet: that reads its ``.cdsenv`` files the way the first
    license checkout would, and sets nothing. A variable this Virtuoso does not
    define reads as ``<unset>``; without ``envIsVal`` every value is
    ``<unknown>``. Nothing is read unguarded.
    """
    names = " ".join(f'"{feature}_UseNextLicense"' for feature in features)
    return (
        "let((vbOut vbVal vbNames vbFile vbPort vbLine vbTok) "
        f"vbFile = and(isCallable('cdsGetInstPath) cdsGetInstPath(\"{LICENSE_CDSENV}\")) "
        "when(and(stringp(vbFile) isFile(vbFile) isReadable(vbFile) vbPort = infile(vbFile)) "
        "while(gets(vbLine vbPort) "
        "vbTok = parseString(vbLine \" \\t\\n\") "
        "when(and(length(vbTok) > 1 equal(car(vbTok) \"license\") "
        "equal(index(cadr(vbTok) \"_UseNextLicense\") \"_UseNextLicense\")) "
        "vbNames = append1(vbNames cadr(vbTok)))) "
        "close(vbPort)) "
        f"foreach(vbVar or(vbNames list({names})) "
        "vbVal = cond((!isCallable('envIsVal) \"<unknown>\") "
        "(envIsVal(\"license\" vbVar t) envGetVal(\"license\" vbVar)) "
        "(t \"<unset>\")) "
        "vbOut = cons(sprintf(nil \"%s=%s\" vbVar "
        "if(stringp(vbVal) vbVal sprintf(nil \"%L\" vbVal))) vbOut)) "
        "reverse(vbOut))"
    )


def check_license(ctx: DoctorContext) -> CheckResult:
    name = "license checkout"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.virtuoso.skill_output import parse_skill_str_list

    output, reason = ctx.skill(license_query())
    if output is None:
        return CheckResult(name, SKIP, f"not checked: {reason}")
    try:
        pairs = [item.partition("=") for item in parse_skill_str_list(output)]
    except ValueError:
        return CheckResult(name, SKIP, f"not checked: unexpected answer {output!r}")
    values = {var: value for var, sep, value in pairs if sep}
    defined = {var: value for var, value in values.items() if value not in ("<unset>", "<unknown>")}
    if values and all(value == "<unknown>" for value in values.values()):
        return CheckResult(name, SKIP, "not checked: this Virtuoso has no envIsVal() to read "
                           "the variables without an error")
    if not defined:
        return CheckResult(name, SKIP, "no UseNextLicense variable is defined in this Virtuoso")
    prompting = {var: value for var, value in defined.items() if value.strip('"').lower() != "always"}
    if prompting:
        by_value: dict[str, list[str]] = {}
        for var, value in prompting.items():
            by_value.setdefault(value.strip('"'), []).append(_feature(var))
        listed = " and ".join(f'"{value}" for {", ".join(features)}'
                              for value, features in by_value.items())
        fix = "Add to .cdsinit:\n" + "\n".join(
            f'envSetVal("license" "{var}" \'string "always")' for var in prompting)
        return CheckResult(
            name, WARN, f"UseNextLicense is {listed}: a missing license tier can pop a modal "
            "'Next License' dialog on the first edit, which blocks the CIW", fix)
    missing = [_feature(var) for var in values if var not in defined]
    note = f" ({', '.join(missing)} not defined)" if missing else ""
    return CheckResult(name, OK, "UseNextLicense is \"always\" for "
                       f"{', '.join(_feature(var) for var in defined)}{note}")


def _feature(var: str) -> str:
    suffix = "_UseNextLicense"
    return var[: -len(suffix)] if var.endswith(suffix) else var


# -- 7. Spectre -----------------------------------------------------------------

def check_spectre(ctx: DoctorContext) -> CheckResult:
    name = "spectre"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.cadence_env import CadenceEnvironment
    from virtuoso_bridge.spectre.runner import (
        parse_spectre_probe,
        probe_spectre_locally,
        spectre_probe_script,
    )

    suffix = ctx.suffix
    try:
        environment = CadenceEnvironment.from_env(ctx.profile)
    except ValueError as exc:
        return CheckResult(name, FAIL, f"configuration error: {exc}",
                           f"Set VB_CADENCE_ENV_SHELL{suffix} to csh or sh.")
    spectre_bin = (os.getenv(f"VB_SPECTRE_BIN{suffix}", "").strip()
                   or os.getenv("VB_SPECTRE_BIN", "").strip())
    hint = (
        f"Set VB_SPECTRE_BIN{suffix} to the spectre command (a site wrapper with arguments "
        f"works), or VB_CADENCE_CSHRC{suffix} to a script that sets up the Cadence environment "
        f"(with VB_CADENCE_ENV_SHELL{suffix}=sh for a POSIX sh script); see {CADENCE_ENV_REFERENCE}."
    )
    probe = spectre_probe_script(spectre_bin)
    host = ctx.roles.spectre_host
    returncode, stdout, stderr = 0, "", ""
    path = version = None
    if _is_localhost(host):
        where = "this machine"
        if environment.script:
            try:
                proc = subprocess.run(["sh", "-c", environment.wrap(probe)],
                                      capture_output=True, text=True, timeout=ENV_TIMEOUT)
            except subprocess.TimeoutExpired:
                return CheckResult(name, FAIL, f"the probe did not finish within {ENV_TIMEOUT:g} s "
                                   f"on {where}", hint)
            returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            path, version = parse_spectre_probe(stdout)
        else:
            path, version = probe_spectre_locally(spectre_bin)
    else:
        where = str(host)
        runner = ctx.runner("spectre", timeout=ENV_TIMEOUT)
        if runner is None:
            return CheckResult(name, SKIP, "no SSH transport to the Spectre host")
        skipped = _ssh_already_failed(name, ctx, where)
        if skipped is not None:
            return skipped
        try:
            result = runner.run_command(environment.wrap(probe))
        except Exception:  # noqa: BLE001 - the runner recorded it as an SSH failure
            result = None
        if result is None or runner.transport_error:
            return _ssh_failure(name, ctx, where)
        returncode, stdout, stderr = result.returncode, result.stdout or "", result.stderr or ""
        path, version = parse_spectre_probe(stdout)
    if returncode != 0 and stderr.strip():
        first = stderr.strip().splitlines()[-1]
        return CheckResult(name, FAIL, f"environment/probe error on {where}: {first}", hint)
    if path:
        # Like `status`: a site wrapper may print its version in another form.
        return CheckResult(name, OK, f"{path} on {where}: {version or 'version not reported by -V'}")
    if spectre_bin:
        return CheckResult(name, FAIL, f"VB_SPECTRE_BIN={spectre_bin} does not name an "
                           f"executable on {where}", hint)
    if environment.script:
        return CheckResult(name, FAIL, f"spectre not found on {where} after sourcing "
                           f"{environment.script}", hint)
    return CheckResult(name, WARN, f"spectre not found on PATH on {where} "
                       "(non-interactive shell)", hint)


# -- 8. documentation -----------------------------------------------------------

def check_docs(ctx: DoctorContext) -> CheckResult:
    name = "documentation"
    if not ctx.config_ok:
        return _skip_unconfigured(name, ctx)
    from virtuoso_bridge.virtuoso.docs_search import resolve_doc_roots
    from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder

    suffix = ctx.suffix
    host = ctx.roles.gui_host
    via = ""
    if _is_localhost(host):
        where = "this machine"
        finder = SKILLFinder().discover(remote_runner=None)
        roots = [str(root) for root in resolve_doc_roots()]
        if not roots and finder is not None:
            roots = [str(finder.parent.parent)]
    else:
        where = str(host)
        runner = ctx.runner("gui", timeout=ENV_TIMEOUT)
        if runner is None:
            return CheckResult(name, SKIP, "no SSH transport to the documentation host")
        skipped = _ssh_already_failed(name, ctx, where)
        if skipped is not None:
            return skipped
        docs = ctx.make_docs_client()
        try:
            # The lookups skill-find and doc-search run, including their
            # fallback to the install root a running Virtuoso reports.
            finder = docs._discover_remote_finder_root(runner)
            roots = list(docs._discover_remote_doc_roots(runner))
        except Exception:  # noqa: BLE001 - an SSH failure is reported below
            if not runner.transport_error:
                raise
            finder, roots = None, []
        if runner.transport_error:
            return _ssh_failure(name, ctx, where)
        reported = getattr(docs, "_fallback_install_root_value", None)
        if isinstance(reported, str) and reported:
            via = f" (via install root {reported} reported by the running Virtuoso)"
    hint = (
        f"Set VB_CADENCE_CSHRC{suffix} to a script that exports the Cadence environment "
        f"(with VB_CADENCE_ENV_SHELL{suffix}=sh for a POSIX sh script), or start the bridge and "
        f"load it in Virtuoso so the running CIW reports its install root; see "
        f"{CADENCE_ENV_REFERENCE}."
    )
    if finder is not None and roots:
        return CheckResult(name, OK, f"skill-find uses {finder}; doc-search uses "
                           f"{', '.join(roots)} on {where}{via}")
    if roots:
        return CheckResult(name, WARN, f"doc-search uses {', '.join(roots)} on {where}{via}, but "
                           "skill-find finds no SKILL Finder data (doc/finder/SKILL)", hint)
    return CheckResult(name, WARN, f"no Cadence documentation found on {where}: `virtuoso` is "
                       "not on PATH in a non-interactive shell and no running Virtuoso reported "
                       "its install root", hint)


# -- running and reporting --------------------------------------------------------

# Dialogs run before the license query: a blocked CIW would not answer it.
CHECKS: tuple[tuple[str, Callable[[DoctorContext], CheckResult]], ...] = (
    ("configuration", check_configuration),
    ("tunnel/daemon", check_daemon),
    ("listen address", check_listen_address),
    ("remote scratch directory", check_scratch_dir),
    ("dialogs", check_dialogs),
    ("license checkout", check_license),
    ("spectre", check_spectre),
    ("documentation", check_docs),
)


def run_checks(
    ctx: DoctorContext,
    *,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    checks: tuple[tuple[str, Callable[[DoctorContext], CheckResult]], ...] = CHECKS,
) -> list[CheckResult]:
    """Run every check in order; one that raises does not stop the others."""
    results: list[CheckResult] = []
    for name, check in checks:
        try:
            result = check(ctx)
        except Exception as exc:  # noqa: BLE001 - report it, keep checking
            result = CheckResult(
                name, WARN, f"check did not complete: {str(exc).strip() or type(exc).__name__}",
                "Re-run `virtuoso-bridge doctor`; if it persists, report it with the command log.",
            )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def format_result(result: CheckResult) -> str:
    lines = [f"[{result.status:<4}] {result.name}: {result.detail}"]
    if result.fix and result.status in (WARN, FAIL):
        first, *rest = result.fix.splitlines()
        lines.append(f"       fix: {first}")
        lines.extend(f"            {line}" for line in rest)
    return "\n".join(lines)


def format_summary(results: list[CheckResult]) -> str:
    counts = {status: sum(r.status == status for r in results) for status in (OK, WARN, FAIL, SKIP)}
    return ", ".join(f"{counts[status]} {status}" for status in (OK, WARN, FAIL, SKIP))


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(result.status == FAIL for result in results) else 0
