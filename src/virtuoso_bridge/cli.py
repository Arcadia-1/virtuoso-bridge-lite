"""CLI entry points for virtuoso-bridge."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from virtuoso_bridge.transport.ssh import SSHRunner, remote_ssh_env_from_os


def _env_template_path() -> Path:
    return Path(__file__).with_name("resources") / ".env_template"


def _generate_env_template() -> str:
    import getpass
    from virtuoso_bridge.virtuoso.basic.bridge import _default_remote_port
    try:
        username = getpass.getuser()
    except Exception:
        username = ""
    remote_port = _default_remote_port(username)
    local_port = remote_port + 1
    template = _env_template_path().read_text(encoding="utf-8")
    return template.format(remote_port=remote_port, local_port=local_port)


def _is_virtuoso_bridge_project(pyproject: Path) -> bool:
    try:
        head = pyproject.read_text(encoding="utf-8")[:4000]
    except OSError:
        return False
    return 'name = "virtuoso-bridge"' in head


def _repo_root() -> Path:
    raw = os.environ.get("VIRTUOSO_BRIDGE_ROOT", "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if _is_virtuoso_bridge_project(p / "pyproject.toml"):
            return p
    here = Path(__file__).resolve()
    for parent in here.parents:
        pm = parent / "pyproject.toml"
        if pm.is_file() and _is_virtuoso_bridge_project(pm):
            return parent
    cwd = Path.cwd()
    nested = cwd / "virtuoso-bridge" / "pyproject.toml"
    if nested.is_file() and _is_virtuoso_bridge_project(nested):
        return nested.parent
    root_pm = cwd / "pyproject.toml"
    if root_pm.is_file() and _is_virtuoso_bridge_project(root_pm):
        return cwd
    raise RuntimeError(
        "Could not locate virtuoso-bridge project root. "
        "Run from the repo directory or set VIRTUOSO_BRIDGE_ROOT."
    )


def _load_repo_env() -> None:
    vb_env = _repo_root() / ".env"
    if vb_env.is_file():
        load_dotenv(vb_env, override=True)


def _fmt(seconds: float) -> str:
    return f"{seconds:.3f}s"


# -- init -------------------------------------------------------------------

def cli_init() -> int:
    env_path = _repo_root() / ".env"
    if env_path.exists():
        print(f".env already exists at {env_path}")
    else:
        env_path.write_text(_generate_env_template(), encoding="utf-8")
        print(f".env created at {env_path}")
    print("\nNext: edit .env, set VB_REMOTE_HOST, then run: virtuoso-bridge start")
    return 0


# -- start ------------------------------------------------------------------

def _ssh_precheck(profile: str | None = None) -> int | None:
    """Quick SSH connectivity check. Returns exit code on failure, None on success."""
    ssh_env = remote_ssh_env_from_os(profile)

    if ssh_env.jump_host:
        user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(host=ssh_env.jump_host, user=user, connect_timeout=5, persistent_shell=False)
        if not runner.test_connection():
            print(f"SSH to jump host {ssh_env.jump_host} failed. Fix SSH first.")
            return 1

    if ssh_env.remote_host:
        jump_user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(
            host=ssh_env.remote_host, user=ssh_env.remote_user,
            jump_host=ssh_env.jump_host, jump_user=jump_user,
            connect_timeout=5, persistent_shell=False,
        )
        if not runner.test_connection():
            print(f"SSH to {ssh_env.remote_host} failed. Fix SSH first.")
            return 1
    return None


def _start_one_profile(profile: str | None) -> int:
    """Start tunnel for a single profile (thread-safe, uses explicit profile)."""
    suffix = f"_{profile}" if profile else ""
    if not os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip():
        print(f"VB_REMOTE_HOST{suffix} is not set. Run: virtuoso-bridge init")
        return 1

    precheck = _ssh_precheck(profile)
    if precheck is not None:
        return precheck

    from virtuoso_bridge.transport.tunnel import SSHClient

    if SSHClient.is_running(profile):
        print("Tunnel already running.")
        return 0

    label = f" [{profile}]" if profile else ""
    print(f"Starting tunnel{label}...")
    ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
    started = time.monotonic()
    ssh.warm()
    elapsed = time.monotonic() - started
    print(f"tunnel.warm = {_fmt(elapsed)}")
    ssh.close()

    time.sleep(1.0)
    if not SSHClient.is_running(profile):
        print("[warning] Tunnel process exited shortly after start.")
        print("Try starting the tunnel manually:")
        ssh_env = remote_ssh_env_from_os(profile)
        port = ssh.port
        manual_cmd = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes -N -L {port}:127.0.0.1:{port}"
        if ssh_env.jump_host:
            jump = f"{ssh_env.jump_user or ssh_env.remote_user}@{ssh_env.jump_host}" if (ssh_env.jump_user or ssh_env.remote_user) else ssh_env.jump_host
            manual_cmd += f" -J {jump}"
        target = f"{ssh_env.remote_user}@{ssh_env.remote_host}" if ssh_env.remote_user else ssh_env.remote_host
        manual_cmd += f" {target}"
        print(f"  {manual_cmd}")
        return 1

    return 0


def _start_one() -> int:
    """Start tunnel for the current profile (read from _CLI_PROFILE)."""
    return _start_one_profile(_get_cli_profile())


def cli_start() -> int:
    _load_repo_env()
    profile = _get_cli_profile()
    if profile is None:
        profiles = _discover_profiles()
        if len(profiles) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(profiles)) as ex:
                list(ex.map(_start_one_profile, profiles))
            return cli_status()
    return _start_one_profile(profile)


# -- stop -------------------------------------------------------------------

def _stop_one() -> int:
    """Stop tunnel for the current profile."""
    profile = _get_cli_profile()
    from virtuoso_bridge.transport.tunnel import SSHClient

    label = f" [{profile}]" if profile else ""
    if not SSHClient.is_running(profile):
        print(f"No tunnel running{label}.")
        return 0

    ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
    ssh.stop()
    print(f"Tunnel stopped{label}.")
    return 0


def cli_stop() -> int:
    _load_repo_env()
    return _for_each_profile(_stop_one)


# -- restart ----------------------------------------------------------------

def _restart_one() -> int:
    """Restart tunnel for the current profile."""
    profile = _get_cli_profile()
    from virtuoso_bridge.transport.tunnel import SSHClient

    if SSHClient.is_running(profile):
        label = f" [{profile}]" if profile else ""
        print(f"Stopping tunnel{label}...")
        ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
        ssh.stop()
        time.sleep(0.5)

    return _start_one()


def cli_restart() -> int:
    _load_repo_env()
    return _for_each_profile(_restart_one)


# -- status -----------------------------------------------------------------

def _print_status() -> int:
    _load_repo_env()
    profile = _get_cli_profile()
    from virtuoso_bridge.transport.tunnel import SSHClient
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

    state = SSHClient.read_state(profile)
    running = SSHClient.is_running(profile)

    label = f" [{profile}]" if profile else ""
    print(f"  Virtuoso Bridge Status{label}")

    # Tunnel
    suffix = f"_{profile}" if profile else ""
    configured_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    configured_user = os.getenv(f"VB_REMOTE_USER{suffix}", "").strip()
    jump_host = os.getenv(f"VB_JUMP_HOST{suffix}", "").strip()

    print(f"\n[tunnel] {'running' if running else 'NOT running'}")
    print(f"  remote host : {configured_host or '(not set)'}")
    print(f"  remote user : {configured_user or '(not set)'}")
    if jump_host:
        print(f"  jump host   : {jump_host}")
    if state:
        print(f"  local port  : {state.get('port')}")
        setup_path = state.get("setup_path")
    else:
        setup_path = None

    # Daemon (Virtuoso CIW)
    if running and state:
        port = state["port"]
        try:
            vc = VirtuosoClient(host="127.0.0.1", port=port, timeout=5)
            ok = vc.test_connection(timeout=5)
            print(f"\n[daemon] {'OK — connected to Virtuoso CIW' if ok else 'NO RESPONSE'}")
            if ok:
                hostname_result = vc.execute_skill('getHostName()', timeout=5)
                remote_hostname = (hostname_result.output or "").strip().strip('"')
                if remote_hostname:
                    print(f"  hostname : {remote_hostname}")
                if remote_hostname and configured_host and remote_hostname != configured_host:
                    print(f"  [warning] hostname mismatch: VB_REMOTE_HOST{suffix}={configured_host}")

                # Say hello in Virtuoso CIW
                vc.execute_skill(
                    'printf("\\n  [virtuoso-bridge] Hello from Virtuoso Bridge! Connection OK.\\n\\n")',
                    timeout=5,
                )
            if not ok and setup_path:
                print(f"\n  Daemon not responding. Load in Virtuoso CIW:")
                print(f"    load(\"{setup_path}\")")
        except Exception as e:
            print(f"\n[daemon] error: {e}")
    elif not running:
        print(f"\n[daemon] cannot check (tunnel not running)")
        if setup_path:
            print(f"  After starting, load in Virtuoso CIW:")
            print(f"    load(\"{setup_path}\")")

    # Spectre
    if running:
        _print_spectre_status(profile, suffix)

    print("\n========================================================================")
    return 0 if running else 1


def _print_spectre_status(profile: str | None, suffix: str) -> None:
    """Check and print Spectre availability via SSH.

    Strategy: try ``which spectre`` directly first (works when the user's
    login shell already has Cadence on PATH).  If that fails and
    VB_CADENCE_CSHRC is set, source it in a csh sub-shell and retry.
    """
    from virtuoso_bridge.transport.tunnel import SSHClient

    try:
        ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
        ssh._ssh_runner._verbose = False

        # 1. Direct check — works if spectre is already on PATH
        result = ssh._ssh_runner.run_command(
            "which spectre 2>/dev/null && spectre -V 2>&1 | head -1",
            timeout=10,
        )
        stdout = result.stdout.strip()

        # 2. Fallback — source VB_CADENCE_CSHRC to set up PATH
        if not stdout:
            cadence_cshrc = (
                os.getenv(f"VB_CADENCE_CSHRC{suffix}", "").strip()
                or os.getenv("VB_CADENCE_CSHRC", "").strip()
            )
            if cadence_cshrc:
                check_cmd = (
                    "cat > /tmp/_vb_spectre_check.csh << 'EOFCSH'\n"
                    "#!/bin/csh -f\n"
                    'if (! $?HOSTNAME) setenv HOSTNAME `hostname`\n'
                    'if (! $?LD_LIBRARY_PATH) setenv LD_LIBRARY_PATH ""\n'
                    f"source {cadence_cshrc}\n"
                    "which spectre\n"
                    "spectre -V\n"
                    "EOFCSH\n"
                    "csh -f /tmp/_vb_spectre_check.csh 2>&1 | head -5; "
                    "rm -f /tmp/_vb_spectre_check.csh"
                )
                result = ssh._ssh_runner.run_command(check_cmd, timeout=15)
                stdout = result.stdout.strip()

        ssh.close()

        spectre_path = None
        version = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("@(#)$CDS:"):
                version = line
            elif "/" in line and "spectre" in line.lower():
                spectre_path = line

        if spectre_path:
            print(f"\n[spectre] OK")
            print(f"  path    : {spectre_path}")
            if version:
                print(f"  version : {version}")
        else:
            print(f"\n[spectre] NOT FOUND")
    except Exception as e:
        print(f"\n[spectre] error: {e}")


def _discover_profiles() -> list[str | None]:
    """Scan environment for all VB_REMOTE_HOST* variables and return profile list.

    Returns a list where None represents the default (unsuffixed) profile
    and strings represent named profiles.
    """
    profiles: list[str | None] = []
    pattern = re.compile(r"^VB_REMOTE_HOST(?:_(.+))?$")
    for key in sorted(os.environ):
        m = pattern.match(key)
        if m and os.environ[key].strip():
            profiles.append(m.group(1))  # None for default, name for suffixed
    return profiles


def _for_each_profile(fn: Callable[[], int]) -> int:
    """Run *fn* for each profile. If -p was given, run only that one.

    Returns 0 if any profile succeeded (returned 0), 1 otherwise.
    """
    profile = _get_cli_profile()
    if profile is not None:
        return fn()
    profiles = _discover_profiles()
    if not profiles:
        print("No profiles found. Set VB_REMOTE_HOST in .env first.")
        return 1
    any_ok = False
    for i, p in enumerate(profiles):
        _CLI_PROFILE[0] = p
        ret = fn()
        if ret == 0:
            any_ok = True
        if i < len(profiles) - 1:
            print()
    return 0 if any_ok else 1


def cli_status() -> int:
    _load_repo_env()
    return _for_each_profile(_print_status)


# -- license ----------------------------------------------------------------

def cli_license() -> int:
    _load_repo_env()
    profile = _get_cli_profile()
    suffix = f"_{profile}" if profile else ""
    cadence_cshrc = os.getenv(f"VB_CADENCE_CSHRC{suffix}", "").strip() or os.getenv("VB_CADENCE_CSHRC", "").strip()
    if not cadence_cshrc:
        print("VB_CADENCE_CSHRC is not set.")
        return 1

    from virtuoso_bridge.transport.tunnel import SSHClient
    if not SSHClient.is_running(profile):
        hint = f"Run `virtuoso-bridge start -p {profile}` first." if profile else "Run `virtuoso-bridge start` first."
        print(f"No tunnel running. {hint}")
        return 1

    # Create SSHRunner with verbose=False to suppress [cmd] output
    ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
    ssh._ssh_runner._verbose = False

    from virtuoso_bridge.spectre.runner import SpectreSimulator
    sim = SpectreSimulator.from_env(profile=profile, ssh_runner=ssh._ssh_runner)
    info = sim.check_license()

    print(f"[spectre] {info.get('spectre_path', 'NOT FOUND')}")
    if info.get("version"):
        print(f"  version: {info['version']}")
    licenses = info.get("licenses", [])
    if licenses:
        print(f"\n[licenses in use] ({len(licenses)} features)")
        for line in licenses:
            print(f"  {line}")

    ssh.close()
    return 0 if info.get("ok") else 1


# -- main -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virtuoso-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create a starter .env")
    for name, hlp in [
        ("start", "Start SSH tunnel + deploy daemon"),
        ("stop", "Stop the SSH tunnel"),
        ("restart", "Restart the SSH tunnel"),
        ("status", "Check tunnel + daemon status"),
        ("license", "Check Spectre license availability"),
    ]:
        sp = subparsers.add_parser(name, help=hlp)
        sp.add_argument("-p", "--profile", default=None,
                        help="Connection profile (reads VB_*_<profile> env vars)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "init": cli_init,
        "start": cli_start,
        "stop": cli_stop,
        "restart": cli_restart,
        "status": cli_status,
        "license": cli_license,
    }
    # Pass profile to commands that support it
    profile = getattr(args, "profile", None)
    if profile is not None:
        _CLI_PROFILE[0] = profile
    return dispatch[args.command]()


# Global profile for CLI commands (avoids changing all function signatures)
_CLI_PROFILE: list[str | None] = [None]


def _get_cli_profile() -> str | None:
    return _CLI_PROFILE[0]
