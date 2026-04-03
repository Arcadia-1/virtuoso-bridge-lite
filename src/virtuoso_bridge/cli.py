"""CLI entry points for virtuoso-bridge."""

from __future__ import annotations

import argparse
import os
import time
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

def _ssh_precheck() -> int | None:
    """Quick SSH connectivity check. Returns exit code on failure, None on success."""
    ssh_env = remote_ssh_env_from_os()

    if ssh_env.jump_host:
        user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(host=ssh_env.jump_host, user=user, connect_timeout=5, persistent_shell=False)
        if not runner.test_connection(timeout=2):
            print(f"SSH to jump host {ssh_env.jump_host} failed. Fix SSH first.")
            return 1

    if ssh_env.remote_host:
        jump_user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(
            host=ssh_env.remote_host, user=ssh_env.remote_user,
            jump_host=ssh_env.jump_host, jump_user=jump_user,
            connect_timeout=5, persistent_shell=False,
        )
        if not runner.test_connection(timeout=2):
            print(f"SSH to {ssh_env.remote_host} failed. Fix SSH first.")
            return 1
    return None


def cli_start() -> int:
    _load_repo_env()
    if not os.getenv("VB_REMOTE_HOST", "").strip():
        print("VB_REMOTE_HOST is not set. Run: virtuoso-bridge init")
        return 1

    precheck = _ssh_precheck()
    if precheck is not None:
        return precheck

    from virtuoso_bridge.transport.tunnel import SSHClient

    if SSHClient.is_running():
        print("Tunnel already running.")
        return _print_status()

    print("Starting tunnel...")
    ssh = SSHClient.from_env(keep_remote_files=True)
    started = time.monotonic()
    ssh.warm()
    elapsed = time.monotonic() - started
    print(f"tunnel.warm = {_fmt(elapsed)}")
    ssh.close()  # close SSH runner, tunnel process stays alive (detached)

    # Post-start health check: verify tunnel is still alive after a brief delay
    time.sleep(1.0)
    if not SSHClient.is_running():
        print("[warning] Tunnel process exited shortly after start.")
        print("Try starting the tunnel manually:")
        ssh_env = remote_ssh_env_from_os()
        port = ssh.port
        manual_cmd = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes -N -L {port}:127.0.0.1:{port}"
        if ssh_env.jump_host:
            jump = f"{ssh_env.jump_user or ssh_env.remote_user}@{ssh_env.jump_host}" if (ssh_env.jump_user or ssh_env.remote_user) else ssh_env.jump_host
            manual_cmd += f" -J {jump}"
        target = f"{ssh_env.remote_user}@{ssh_env.remote_host}" if ssh_env.remote_user else ssh_env.remote_host
        manual_cmd += f" {target}"
        print(f"  {manual_cmd}")
        return 1

    return _print_status()


# -- stop -------------------------------------------------------------------

def cli_stop() -> int:
    _load_repo_env()
    from virtuoso_bridge.transport.tunnel import SSHClient

    if not SSHClient.is_running():
        print("No tunnel running.")
        return 0

    ssh = SSHClient.from_env(keep_remote_files=True)
    ssh.stop()
    print("Tunnel stopped.")
    return 0


# -- restart ----------------------------------------------------------------

def cli_restart() -> int:
    _load_repo_env()
    from virtuoso_bridge.transport.tunnel import SSHClient

    if SSHClient.is_running():
        print("Stopping tunnel...")
        ssh = SSHClient.from_env(keep_remote_files=True)
        ssh.stop()
        time.sleep(0.5)

    return cli_start()


# -- status -----------------------------------------------------------------

def _print_status() -> int:
    _load_repo_env()
    from virtuoso_bridge.transport.tunnel import SSHClient
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

    state = SSHClient.read_state()
    running = SSHClient.is_running()

    print("========================================================================")
    print(f"[tunnel] {'running' if running else 'NOT running'}")
    if state:
        print(f"  port: {state.get('port')}")
        print(f"  tunnel_pid: {state.get('tunnel_pid')}")
        print(f"  remote: {state.get('remote_host')}")
        setup_path = state.get("setup_path")
        if setup_path:
            print(f"  setup: load(\"{setup_path}\")")

    if running and state:
        port = state["port"]
        try:
            vc = VirtuosoClient(host="127.0.0.1", port=port, timeout=5)
            ok = vc.test_connection(timeout=5)
            print(f"[daemon] {'OK' if ok else 'NO RESPONSE'}")
            if ok:
                # Hostname verification: check remote hostname matches VB_REMOTE_HOST
                hostname_result = vc.execute_skill('getHostName()', timeout=5)
                remote_hostname = (hostname_result.output or "").strip().strip('"')
                configured_host = os.getenv("VB_REMOTE_HOST", "").strip()
                if remote_hostname and configured_host and remote_hostname != configured_host:
                    print(f"[warning] remote hostname is '{remote_hostname}' but VB_REMOTE_HOST is '{configured_host}'")
                    print(f"  Make sure VB_REMOTE_HOST points to the machine running Virtuoso, not the jump host.")
            if not ok and setup_path:
                print(f"\n  Please execute in Virtuoso CIW: load(\"{setup_path}\")")
        except Exception as e:
            print(f"[daemon] error: {e}")
    elif not running:
        print("[daemon] cannot check (tunnel not running)")

    _print_spectre_license()
    print("========================================================================")
    return 0 if running else 1


def _print_spectre_license() -> None:
    cadence_cshrc = os.getenv("VB_CADENCE_CSHRC", "").strip()
    if not cadence_cshrc:
        return

    try:
        from virtuoso_bridge.spectre.runner import SpectreSimulator
        sim = SpectreSimulator.from_env()
        info = sim.check_license()
    except Exception:
        return

    print(f"[spectre] {info.get('spectre_path', 'NOT FOUND')}")
    if info.get("version"):
        print(f"  version: {info['version']}")
    for line in info.get("licenses", []):
        print(f"  {line}")


def cli_status() -> int:
    _load_repo_env()
    return _print_status()


# -- main -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virtuoso-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create a starter .env")
    subparsers.add_parser("start", help="Start SSH tunnel + deploy daemon")
    subparsers.add_parser("stop", help="Stop the SSH tunnel")
    subparsers.add_parser("restart", help="Restart the SSH tunnel")
    subparsers.add_parser("status", help="Check tunnel + daemon + license")
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
    }
    return dispatch[args.command]()
