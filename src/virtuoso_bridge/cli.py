"""CLI entry points for virtuoso-bridge."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from virtuoso_bridge.env import default_user_env_path, load_vb_env, set_runtime_env_file
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


def _load_cli_env() -> Path | None:
    return load_vb_env()


def _fmt(seconds: float) -> str:
    return f"{seconds:.3f}s"


# -- init -------------------------------------------------------------------

def cli_init() -> int:
    env_path = default_user_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
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

    # When a remote target is configured, prefer a single end-to-end probe.
    # On some Windows/OpenSSH + remote-shell combinations, probing the jump
    # host alone via ``ssh host -T exit 0`` can false-negative even though the
    # actual proxied connection to the remote host succeeds.
    if ssh_env.jump_host and not ssh_env.remote_host:
        user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(host=ssh_env.jump_host, user=user, connect_timeout=5, persistent_shell=False)
        if not runner.test_connection():
            print(f"SSH to jump host {ssh_env.jump_host} failed.")
            print(f"  Check VB_JUMP_HOST in your .env file.")
            print(f"  Verify: ssh {user}@{ssh_env.jump_host}")
            return 1

    if ssh_env.remote_host:
        jump_user = ssh_env.jump_user or ssh_env.remote_user
        runner = SSHRunner(
            host=ssh_env.remote_host, user=ssh_env.remote_user,
            jump_host=ssh_env.jump_host, jump_user=jump_user,
            connect_timeout=5, persistent_shell=False,
        )
        if not runner.test_connection():
            print(f"SSH to {ssh_env.remote_host} failed.")
            print(f"  Check VB_REMOTE_HOST and VB_REMOTE_USER in your .env file.")
            if ssh_env.jump_host:
                print(f"  Verify: ssh -J {jump_user}@{ssh_env.jump_host} {ssh_env.remote_user}@{ssh_env.remote_host}")
            else:
                print(f"  Verify: ssh {ssh_env.remote_user}@{ssh_env.remote_host}")
            print(f"  For a local VM, use the VM's IP (run `ip addr` inside the VM).")
            return 1
    return None


def _start_one_profile(profile: str | None) -> int:
    """Start tunnel for a single profile (thread-safe, uses explicit profile)."""
    suffix = f"_{profile}" if profile else ""
    remote_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    if not remote_host:
        print(
            f"VB_REMOTE_HOST{suffix} is not set. "
            "Use --env FILE, create ./.env, or run `virtuoso-bridge init` to create ~/.virtuoso-bridge/.env."
        )
        return 1

    from virtuoso_bridge.transport.tunnel import SSHClient, _is_localhost

    is_local = _is_localhost(remote_host)

    if not is_local:
        precheck = _ssh_precheck(profile)
        if precheck is not None:
            return precheck

    if SSHClient.is_running(profile):
        msg = "Bridge already running." if is_local else "Tunnel already running."
        print(msg)
        return 0

    label = f" [{profile}]" if profile else ""
    if is_local:
        print(f"Setting up local bridge{label}...")
    else:
        print(f"Starting tunnel{label}...")
    ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
    try:
        started = time.monotonic()
        ssh.warm()
        elapsed = time.monotonic() - started
        print(f"tunnel.warm = {_fmt(elapsed)}")

        if is_local:
            # For local mode, print setup_path for user to load in CIW
            state = SSHClient.read_state(profile)
            if state:
                setup_path = state.get("setup_path")
                if setup_path:
                    print(f"  Load in Virtuoso CIW: load(\"{setup_path}\")")
            return 0

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
    finally:
        ssh.close()


def _start_one() -> int:
    """Start tunnel for the current profile (read from _CLI_PROFILE)."""
    return _start_one_profile(_get_cli_profile())


def cli_start() -> int:
    _load_cli_env()
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
    _load_cli_env()
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
    _load_cli_env()
    return _for_each_profile(_restart_one)


# -- status -----------------------------------------------------------------

def _print_load_hint(setup_path: str) -> None:
    """Print CIW load command and .cdsinit auto-load suggestion."""
    print(f"\n  Load in Virtuoso CIW:")
    print(f"    load(\"{setup_path}\")")
    print(f"\n  To auto-load on every Virtuoso startup, add to your .cdsinit:")
    print(f"    load(\"{setup_path}\")")


def _print_status() -> int:
    _load_cli_env()
    profile = _get_cli_profile()
    from virtuoso_bridge.transport.tunnel import SSHClient, _is_localhost
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

    state = SSHClient.read_state(profile)
    running = SSHClient.is_running(profile)

    from virtuoso_bridge import __version__
    label = f" [{profile}]" if profile else ""
    print(f"  Virtuoso Bridge v{__version__}{label}")

    suffix = f"_{profile}" if profile else ""
    configured_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    configured_user = os.getenv(f"VB_REMOTE_USER{suffix}", "").strip()
    jump_host = os.getenv(f"VB_JUMP_HOST{suffix}", "").strip()

    is_local = _is_localhost(configured_host) if configured_host else False

    # Infer setup_path from user config when state is unavailable
    def _infer_setup_path() -> str | None:
        user = configured_user
        if not user:
            import getpass
            try:
                user = getpass.getuser()
            except Exception:
                return None
        return f"/tmp/virtuoso_bridge_{user}/virtuoso_bridge/virtuoso_setup.il"

    if is_local:
        print(f"\n[mode] local (no SSH tunnel)")
        if state:
            print(f"  port : {state.get('port')}")
            setup_path = state.get("setup_path")
        else:
            setup_path = None
    else:
        # Remote tunnel mode
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

    if not setup_path:
        setup_path = _infer_setup_path()

    # Daemon (Virtuoso CIW)
    # For local mode, check daemon if we have state (don't require 'running')
    can_check_daemon = (is_local and state) or (running and state)
    if can_check_daemon:
        if state is None:
            print("\n[daemon] cannot check (state missing)")
            return 1
        port = state["port"]
        try:
            vc = VirtuosoClient(host="127.0.0.1", port=port, timeout=5)
            ok = vc.test_connection(timeout=5)
            print(f"\n[daemon] {'OK - connected to Virtuoso CIW' if ok else 'NO RESPONSE'}")
            if ok:
                # Query Virtuoso environment info
                for skill_expr, label in [
                    ('getHostName()', 'hostname'),
                    ('getCurrentTime()', 'time'),
                    ('getVersion()', 'version'),
                    ('getWorkingDir()', 'workdir'),
                ]:
                    try:
                        r = vc.execute_skill(skill_expr, timeout=5)
                        val = (r.output or "").strip().strip('"')
                        if val:
                            print(f"  {label:<10s}: {val}")
                    except Exception:
                        pass

                # Say hello in Virtuoso CIW with timestamp
                vc.execute_skill(
                    r'printf("\n  [virtuoso-bridge] Status check at %s - connection OK.\n\n" getCurrentTime())',
                    timeout=5,
                )
            if not ok and setup_path:
                _print_load_hint(setup_path)
        except Exception as e:
            print(f"\n[daemon] error: {e}")
    elif not is_local and not running:
        print(f"\n[daemon] cannot check (tunnel not running)")
        if setup_path:
            _print_load_hint(setup_path)

    # Spectre
    if is_local or running:
        _print_spectre_status(profile, suffix)

    print("\n========================================================================")
    if is_local:
        return 0  # local mode: no tunnel to check
    return 0 if running else 1


def _print_spectre_status(profile: str | None, suffix: str) -> None:
    """Check and print Spectre availability.

    For local mode: uses shutil.which and subprocess locally.
    For remote mode: SSH-based check via SSHClient.

    Strategy (remote): try ``which spectre`` directly first (works when the
    user's login shell already has Cadence on PATH).  If that fails and
    VB_CADENCE_CSHRC is set, source it in a csh sub-shell and retry.
    """
    import shutil
    import subprocess

    from virtuoso_bridge.transport.tunnel import SSHClient, _is_localhost

    configured_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    is_local = _is_localhost(configured_host) if configured_host else False

    if is_local:
        try:
            spectre_path = shutil.which("spectre")
            version = None
            if spectre_path:
                try:
                    result = subprocess.run(
                        ["spectre", "-V"],
                        capture_output=True, text=True, timeout=10,
                    )
                    for line in (result.stdout + result.stderr).splitlines():
                        if line.strip().startswith("@(#)$CDS:"):
                            version = line.strip()
                            break
                except Exception:
                    pass
            if spectre_path:
                print(f"\n[spectre] OK")
                print(f"  path    : {spectre_path}")
                if version:
                    print(f"  version : {version}")
            else:
                print(f"\n[spectre] NOT FOUND")
        except Exception as e:
            print(f"\n[spectre] error: {e}")
        return

    # Remote mode — SSH-based check
    ssh = None
    try:
        ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
        runner = ssh.ssh_runner
        if runner is None:
            print("\n[spectre] local mode (no SSH runner)")
            return
        runner._verbose = False

        # 1. Direct check — works if spectre is already on PATH
        result = runner.run_command(
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
                result = runner.run_command(check_cmd, timeout=15)
                stdout = result.stdout.strip()

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
    finally:
        if ssh is not None:
            ssh.close()


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
    _load_cli_env()
    return _for_each_profile(_print_status)


# -- license ----------------------------------------------------------------

def cli_license() -> int:
    _load_cli_env()
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

    from virtuoso_bridge.transport.tunnel import _is_localhost
    from virtuoso_bridge.spectre.runner import SpectreSimulator

    suffix = f"_{profile}" if profile else ""
    configured_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()

    ssh = None
    try:
        if _is_localhost(configured_host):
            sim = SpectreSimulator.from_env(profile=profile)
        else:
            # Create SSHRunner with verbose=False to suppress [cmd] output
            ssh = SSHClient.from_env(keep_remote_files=True, profile=profile)
            runner = ssh.ssh_runner
            if runner is None:
                print("No SSH runner available for remote license check.")
                return 1
            runner._verbose = False
            sim = SpectreSimulator.from_env(profile=profile, ssh_runner=runner)

        info = sim.check_license()

        print(f"[spectre] {info.get('spectre_path', 'NOT FOUND')}")
        if info.get("version"):
            print(f"  version: {info['version']}")
        licenses = info.get("licenses", [])
        if licenses:
            print(f"\n[licenses in use] ({len(licenses)} features)")
            for line in licenses:
                print(f"  {line}")

        return 0 if info.get("ok") else 1
    finally:
        if ssh is not None:
            ssh.close()


# -- main -------------------------------------------------------------------

def _make_ssh_runner() -> tuple["SSHRunner", str]:
    """Create an SSHRunner from .env config (for X11 commands)."""
    from virtuoso_bridge.transport.ssh import SSHRunner
    profile = _get_cli_profile()
    suffix = f"_{profile}" if profile else ""
    remote_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    remote_user = os.getenv(f"VB_REMOTE_USER{suffix}", "").strip()
    jump_host = os.getenv(f"VB_JUMP_HOST{suffix}", "").strip() or None
    jump_user = os.getenv(f"VB_JUMP_USER{suffix}", remote_user).strip() or None
    if not remote_host:
        raise SystemExit("Error: VB_REMOTE_HOST not set")
    return SSHRunner(host=remote_host, user=remote_user,
                     jump_host=jump_host, jump_user=jump_user), remote_user


def cli_dismiss_dialog() -> int:
    """Find and dismiss blocking Virtuoso GUI dialogs via X11."""
    _load_cli_env()
    from virtuoso_bridge.virtuoso import x11
    runner, user = _make_ssh_runner()

    dialogs = x11.dismiss_dialogs(runner, user)
    if not dialogs:
        print("No dialog windows found.")
        return 0

    for d in dialogs:
        if "error" in d:
            print(f"  Error: {d['error']}")
        elif "dismissed" in d:
            print(f"  Dismissed: {d['dismissed']}")
        elif "title" in d:
            print(f'  Found: "{d["title"]}" at ({d.get("x",0)},{d.get("y",0)})')
    return 0



_SCREENSHOT_TARGET: list[str] = ["ciw"]

# Mutable bag for cli_snapshot — set from argparse, read inside the handler.
# `output_root=None` is a sentinel for "user didn't pass -o" — that's what
# selects brief stdout mode.
_SNAPSHOT_OPTS: dict = {
    "output_root": None,
    "json":        False,
}


def cli_windows() -> int:
    """List all open Virtuoso windows.

    Annotates the focused line with its bound maestro session (when
    the focused window is an ADE Assembler) and lists all open
    sessions in a footer.  All info comes from a single SKILL round-
    trip — no scp.
    """
    _load_cli_env()
    import sys
    from virtuoso_bridge import VirtuosoClient
    from virtuoso_bridge.virtuoso.maestro.reader._parse_skill import (
        _parse_skill_str_list,
    )

    client = VirtuosoClient.from_env()
    windows = client.list_windows()
    if not windows:
        print("No windows found.")
        return 1

    # One SKILL call → focused window number + focused window's bound
    # maestro session id (via davSession attribute) + all open sessions.
    focused_num = ""
    focused_session = ""
    sessions: list[str] = []
    try:
        r = client.execute_skill(
            "let((w) w = hiGetCurrentWindow() list("
            "if(w sprintf(nil \"%d\" w~>windowNum) \"\")"
            " if(w w->davSession \"\")"
            " maeGetSessions()))"
        )
        out = (r.output or "").strip()
        if out.startswith("(") and out.endswith(")"):
            inner = out[1:-1].strip()
            # First two tokens are quoted strings; the rest is the
            # ``maeGetSessions()`` list literal.
            m = re.match(r'\s*"([^"]*)"\s*"([^"]*)"\s*(.*)', inner, re.DOTALL)
            if m:
                focused_num = m.group(1).strip()
                focused_session = m.group(2).strip()
                sessions = _parse_skill_str_list(m.group(3).strip())
    except Exception:
        pass

    use_color = sys.stdout.isatty()
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    focused_name = next(
        (w["name"] for w in windows if w["num"] == focused_num), "")
    if focused_num:
        label = f"{focused_num}  {focused_name}" if focused_name else focused_num
        suffix = f"  [{focused_session}]" if focused_session else ""
        print(f"Focused: {BOLD}{label}{RESET}{suffix}\n")

    for w in windows:
        is_focused = w["num"] == focused_num
        marker = "*" if is_focused else " "
        name = f"{BOLD}{w['name']}{RESET}" if is_focused else w["name"]
        print(f"{marker} {w['num']:>4}  {name}")

    if sessions:
        print()
        print(f"Maestro sessions ({len(sessions)}): {', '.join(sessions)}")
    return 0


def cli_snapshot() -> int:
    """Snapshot the currently-focused Virtuoso window.

    Three modes:
      default     : brief one-screen summary to stdout (fast —
                     brief_bundle only, ~150ms).
      ``-o ROOT`` : full ``snapshot(output_root=ROOT)`` — pure SKILL +
                     5 scp's; writes maestro.sdb + active.state (raw) +
                     state_from_sdb.xml + state_from_active_state.xml
                     (filtered) + state_from_skill.json + histories.json
                     + latest_history.json + <history>/ run artifacts.
      ``--json``  : full in-memory snapshot dict as JSON to stdout.
    """
    _load_cli_env()
    import json
    import re
    import sys
    from virtuoso_bridge import VirtuosoClient
    from virtuoso_bridge.virtuoso import snapshot as poly_snapshot
    from virtuoso_bridge.virtuoso.snapshot import classify_window
    from virtuoso_bridge.virtuoso.maestro import snapshot as _maestro_snapshot

    client = VirtuosoClient.from_env()
    opts = _SNAPSHOT_OPTS

    # Focused window title — decode SKILL octal escapes (e.g. \256 -> ®).
    title = (client.execute_skill(
        'let((cw) cw = hiGetCurrentWindow() if(cw hiGetWindowName(cw) ""))'
    ).output or "").strip().strip('"')
    title = re.sub(r'\\(\d{3})', lambda m: chr(int(m.group(1), 8)), title)
    kind = classify_window(title)

    # Mode 1: -o ROOT — full disk snapshot (maestro only for now).
    if opts["output_root"] is not None:
        if kind != "maestro":
            print(f"[{kind}] {title}", file=sys.stderr)
            print(f"-o ROOT only supports maestro for now.", file=sys.stderr)
            return 1
        result = _maestro_snapshot(client, output_root=opts["output_root"])
        print(result.get("output_dir", ""))
        return 0

    # Mode 2: --json — full in-memory dict to stdout.
    if opts["json"]:
        result = poly_snapshot(client) if kind != "maestro" else poly_snapshot(client)
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
        return 0

    # Mode 3 (default): brief stdout summary.
    if kind == "unknown":
        print(f"no Virtuoso window in focus  ({title or '(no title)'})", file=sys.stderr)
        return 1
    if kind != "maestro":
        # Other kinds: just identify, no commentary.
        print(f"[{kind}] {title}")
        return 0

    # Maestro brief: just call snapshot() (no output_root) and render
    # its sparse dict.  2 SKILL round-trips total, no scp.  ~150ms.
    snap = _maestro_snapshot(client)
    _print_maestro_brief(snap)
    return 0


_BRIEF_INCLUDE_PREFIXES = (
    "ddGetObj(",                              # lib readPath
    "maeGetSetup(",                           # test name(s)
    "maeGetEnabledAnalysis(",                 # analysis names
    "maeGetAnalysis(",                        # per-analysis settings
)


def _print_maestro_brief(d: dict) -> None:
    """Dump high-signal SKILL sections to stdout, ``state_from_skill.txt``
    format (``[label]`` + verbatim value).  Whitelist of probe prefixes
    above — new probes default to disk-dump-only.  ``snapshot -o ROOT``
    keeps the full set.  No alist→dict parsing; no path lines (paths
    can't be verified without scp)."""
    from virtuoso_bridge.virtuoso.maestro.reader.snapshot import format_skill_sections
    sections = [(label, raw) for label, raw in (d.get("raw_sections") or [])
                if any(label.startswith(p) for p in _BRIEF_INCLUDE_PREFIXES)]
    text = format_skill_sections(sections)
    if text:
        print(text, end="")


def cli_screenshot() -> int:
    """Take a screenshot of a Virtuoso window."""
    _load_cli_env()
    from virtuoso_bridge import VirtuosoClient

    client = VirtuosoClient.from_env()
    raw_target = _SCREENSHOT_TARGET[0]

    # Resolve target
    target: str | int
    if raw_target.isdigit():
        target = int(raw_target)
    else:
        target = raw_target

    from pathlib import Path
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    result = client.screenshot(output=output_dir, target=target)
    if result.status.value != "success":
        print(f"Error: {result.errors[0] if result.errors else 'screenshot failed'}")
        return 1
    print(result.output)
    return 0


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
        sp.add_argument("--env", default=None,
                        help="Explicit .env file path (highest priority)")
    sp_dismiss = subparsers.add_parser(
        "dismiss-dialog", help="Find and dismiss blocking Virtuoso GUI dialogs")
    sp_dismiss.add_argument("-p", "--profile", default=None,
                            help="Connection profile")
    sp_dismiss.add_argument("--env", default=None,
                            help="Explicit .env file path (highest priority)")

    sp_screenshot = subparsers.add_parser(
        "screenshot", help="Take a screenshot of a Virtuoso window")
    sp_screenshot.add_argument(
        "target", nargs="?", default="ciw",
        help="ciw (default), current, a view name (schematic/layout/maestro), or window number")
    sp_screenshot.add_argument("-p", "--profile", default=None,
                               help="Connection profile")
    sp_screenshot.add_argument("--env", default=None,
                               help="Explicit .env file path (highest priority)")

    sp_windows = subparsers.add_parser("windows", help="List all open Virtuoso windows")
    sp_windows.add_argument("-p", "--profile", default=None,
                            help="Connection profile")
    sp_windows.add_argument("--env", default=None,
                            help="Explicit .env file path (highest priority)")

    sp_snap = subparsers.add_parser(
        "snapshot",
        help="Brief summary of the focused Virtuoso window "
             "(maestro/schematic/...).  -o ROOT for full disk dump; "
             "--json for full in-memory JSON.")
    sp_snap.add_argument("-o", "--output-root", default=None,
                         help="Full snapshot to disk under this dir "
                              "(slow: includes latest history log + spectre.out tail). "
                              "Without -o, prints a brief summary to stdout.")
    sp_snap.add_argument("--json", action="store_true",
                         help="Print full snapshot dict as JSON to stdout (overrides default brief)")
    sp_snap.add_argument("-p", "--profile", default=None,
                         help="Connection profile")
    sp_snap.add_argument("--env", default=None,
                         help="Explicit .env file path (highest priority)")

    return parser


def _make_stdio_safe() -> None:
    # Window/cell names may contain non-ASCII chars (e.g. '®' in Cadence
    # titles). On hosts whose locale is GBK / cp1252 / etc., the default
    # stdout encoding cannot represent them and print() raises
    # UnicodeEncodeError. Force UTF-8 (every modern terminal renders it
    # regardless of LANG) and keep errors='replace' as a last-resort
    # safety net.
    import sys
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _make_stdio_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "init": cli_init,
        "start": cli_start,
        "stop": cli_stop,
        "restart": cli_restart,
        "status": cli_status,
        "license": cli_license,
        "dismiss-dialog": cli_dismiss_dialog,
        "screenshot": cli_screenshot,
        "windows": cli_windows,
        "snapshot": cli_snapshot,
    }
    # Pass profile to commands that support it
    profile = getattr(args, "profile", None)
    if profile is not None:
        _CLI_PROFILE[0] = profile
    set_runtime_env_file(getattr(args, "env", None))
    screenshot_target = getattr(args, "target", None)
    if screenshot_target is not None:
        _SCREENSHOT_TARGET[0] = screenshot_target
    if args.command == "snapshot":
        for k in _SNAPSHOT_OPTS:
            v = getattr(args, k, None)
            if v is not None:
                _SNAPSHOT_OPTS[k] = v
    return dispatch[args.command]()


# Global profile for CLI commands (avoids changing all function signatures)
_CLI_PROFILE: list[str | None] = [None]


def _get_cli_profile() -> str | None:
    return _CLI_PROFILE[0]
