"""SOS session health, controlled recovery, and exact cellview lock queries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
import posixpath
import re
import shlex
from typing import TYPE_CHECKING

from .cellview import (
    SOSCellViewState,
    SOSCellViewTarget,
    _NOBJSTATUS_ATTRIBUTES,
    _OA_TYPES,
    _STATUS_FORMAT,
    _STATUS_NOTICES,
    _ONLINE_NOTICE,
    _maestro_active,
    _parse_nobjstatus,
    _parse_nobjstatus_record,
    _resolve_ciw_target,
    _text,
)
from .environment import resolve_soscmd, sos_runner
from .workarea import SOSWorkarea

if TYPE_CHECKING:
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


_LOCK_ATTRIBUTES = (
    "-gaRevision", "-gaCurrentVer", "-gaModified", "-gaCiModified",
    "-gaOutOfDate", "-gaReference", "-gaCheckedOutBy", "-gaCheckOutTime",
    "-gachkout_path", "-gaWaRoot",
)
_OFFLINE_DETAILS = {
    0: "SOS server is reachable and the session is online.",
    1: "SOS was started offline and must be restarted to reconnect.",
    2: "SOS server is unavailable.",
    3: "SOS server protocol or clock compatibility check failed.",
}
_NOWIN_DETAILS = {
    0: "SOS session is running with its GUI.",
    1: "SOS session is running without GUI and can switch modes.",
    2: "SOS session is running without GUI and cannot switch modes.",
}


@dataclass(frozen=True)
class SOSLockInfo:
    target: SOSCellViewTarget
    state: SOSCellViewState
    scope: str
    owner: str = ""
    checkout_time: str = ""
    checkout_path: str = ""
    lock_workarea: str = ""
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "action": "lock_info",
            "outcome": "success",
            "target": asdict(self.target),
            "before": asdict(self.state),
            "lock": {
                "scope": self.scope,
                "owner": self.owner,
                "checkout_time": self.checkout_time,
                "checkout_path": self.checkout_path,
                "workarea": self.lock_workarea,
            },
            "diagnostics": list(self.diagnostics),
        }


def _target(lib: str, cell: str, view: str, timeout: float) -> SOSCellViewTarget:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive.")
    return SOSCellViewTarget(*(
        _text(value, label)
        for value, label in ((lib, "lib"), (cell, "cell"), (view, "view"))
    ))


def _run_in(owner: VirtuosoClient, directory: str, executable: str, *args: str,
            timeout: float):
    runner = sos_runner(owner)
    if runner is None:
        raise RuntimeError(
            "SOS requires a GUI-host SSH runner or an explicitly local POSIX client."
        )
    command = "cd {directory} && {argv}".format(
        directory=shlex.quote(directory),
        argv=shlex.join([executable, *args]),
    )
    return runner.run_command(command, timeout=timeout)


def _query(owner: VirtuosoClient, directory: str, executable: str, name: str,
           timeout: float) -> str:
    result = _run_in(owner, directory, executable, "query", name, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip()
            or f"SOS query {name} failed with exit {result.returncode}."
        )
    value = result.stdout.strip()
    if not value or any(char in value for char in "\x00\r\n\t"):
        raise RuntimeError(f"SOS query {name} returned a malformed value.")
    return value


def _query_code(owner: VirtuosoClient, directory: str, executable: str, name: str,
                allowed: set[int], timeout: float) -> int:
    value = _query(owner, directory, executable, name, timeout)
    if not re.fullmatch(r"[0-9]+", value) or int(value) not in allowed:
        raise RuntimeError(f"SOS query {name} returned unsupported code {value!r}.")
    return int(value)


def _session_identity(owner: VirtuosoClient, target: SOSCellViewTarget, executable: str,
                      timeout: float) -> tuple[SOSCellViewTarget, dict[str, str | int]]:
    offline = _query_code(owner, target.directory, executable, "is_offline", {0, 1, 2, 3}, timeout)
    nowin = _query_code(owner, target.directory, executable, "is_nowin", {0, 1, 2}, timeout)
    workarea = _query(owner, target.directory, executable, "wa_root", timeout)
    if not posixpath.isabs(workarea) or workarea.startswith("//"):
        raise RuntimeError("SOS query wa_root did not return an absolute POSIX path.")
    workarea = posixpath.normpath(workarea)
    if target.directory == workarea or posixpath.commonpath([target.directory, workarea]) != workarea:
        raise RuntimeError("Resolved cellview is outside the SOS query workarea.")
    target = replace(
        target, workarea=workarea, path=posixpath.relpath(target.directory, workarea),
    )
    session = {
        "running": 1,
        "offline_code": offline,
        "offline_detail": _OFFLINE_DETAILS[offline],
        "nowin_code": nowin,
        "nowin_detail": _NOWIN_DETAILS[nowin],
        "project": _query(owner, target.directory, executable, "project", timeout),
        "server": _query(owner, target.directory, executable, "server", timeout),
        "rso": _query(owner, target.directory, executable, "rso", timeout),
        "last_update_time": _query(
            owner, target.directory, executable, "last_update_time", timeout,
        ),
    }
    return target, session


def _native_status(area: SOSWorkarea, target: SOSCellViewTarget, timeout: float) -> tuple[bool, str]:
    result = area._run(
        "status", "-Nhdr", "-f" + _STATUS_FORMAT, "./" + target.path, timeout=timeout,
    )
    if result.returncode:
        return False, result.stderr.strip() or result.stdout.strip() or "SOS status failed."
    lines = [
        line for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in _STATUS_NOTICES
        and not _ONLINE_NOTICE.fullmatch(line.strip())
    ]
    if len(lines) != 1:
        return False, f"Expected one native status row, got {len(lines)}."
    fields = lines[0].split("\t", 7)
    if len(fields) != 8 or fields[7] not in {target.path, "./" + target.path}:
        return False, "SOS native status returned a different or malformed object row."
    return True, "Native SOS status returned the exact target."


def _lock_record(area: SOSWorkarea, target: SOSCellViewTarget, timeout: float):
    result = area._run("nobjstatus", *_LOCK_ATTRIBUTES, "./" + target.path, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "SOS nobjstatus failed."
        )
    path, status_code, _type_code, attributes = _parse_nobjstatus_record(result.stdout)
    if path not in {target.path, "./" + target.path}:
        raise RuntimeError("SOS nobjstatus returned a different object path.")
    state = _parse_nobjstatus(result.stdout, target)
    if status_code == "3":
        scope = "current_workarea"
    elif status_code == "4":
        scope = "other_workarea"
    elif status_code == "6":
        scope = "unlocked_checkout"
    else:
        scope = "none"
    for name in ("CheckedOutBy", "CheckOutTime", "chkout_path", "WaRoot"):
        value = attributes.get(name, "")
        if any(char in value for char in "\x00\r\n\t"):
            raise RuntimeError(f"SOS nobjstatus returned an invalid {name} attribute.")
        attributes.setdefault(name, "")
    for name in ("chkout_path", "WaRoot"):
        value = attributes[name]
        if value and (not posixpath.isabs(value) or value.startswith("//")):
            raise RuntimeError(f"SOS nobjstatus returned a non-absolute {name} attribute.")
    if scope == "other_workarea" and not (
        attributes["CheckedOutBy"] and attributes["chkout_path"]
    ):
        raise RuntimeError("SOS reported another-workarea lock without owner and checkout path.")
    return state, scope, attributes


def lock_info_cellview(owner: VirtuosoClient, lib: str, cell: str, view: str, *,
                       timeout: float = 60, soscmd: str | None = None) -> SOSLockInfo:
    target = _target(lib, cell, view, timeout)
    if sos_runner(owner) is None:
        raise RuntimeError(
            "SOS requires a GUI-host SSH runner or an explicitly local POSIX client."
        )
    target = _resolve_ciw_target(owner, target, timeout)
    executable = resolve_soscmd(owner, soscmd, timeout=timeout)
    running = _query_code(owner, target.directory, executable, "is_running", {0, 1}, timeout)
    if running != 1:
        raise RuntimeError("SOS workarea session is not running; use session-doctor first.")
    target, _session = _session_identity(owner, target, executable, timeout)
    state, scope, attributes = _lock_record(
        SOSWorkarea(owner, target.workarea, soscmd=executable), target, timeout,
    )
    return SOSLockInfo(
        target=target,
        state=state,
        scope=scope,
        owner=attributes.get("CheckedOutBy", ""),
        checkout_time=attributes.get("CheckOutTime", ""),
        checkout_path=attributes.get("chkout_path", ""),
        lock_workarea=attributes.get("WaRoot", ""),
        diagnostics=("Server-queried exact-object status; no local cache was requested.",),
    )


def diagnose_session_cellview(owner: VirtuosoClient, lib: str, cell: str, view: str, *,
                              timeout: float = 60, soscmd: str | None = None) -> dict:
    target = _target(lib, cell, view, timeout)
    checks: list[dict] = []
    session: dict[str, str | int] = {"running": 0}
    state = None
    executable = None
    try:
        if sos_runner(owner) is None:
            raise RuntimeError(
                "SOS requires a GUI-host SSH runner or an explicitly local POSIX client."
            )
        target = _resolve_ciw_target(owner, target, timeout)
        checks.append({"name": "ciw_target", "ok": True,
                       "detail": "Cellview identity resolved without an SOS status refresh."})
        executable = resolve_soscmd(owner, soscmd, timeout=timeout)
        checks.append({"name": "sos_executable", "ok": True, "detail": executable})
        running = _query_code(owner, target.directory, executable, "is_running", {0, 1}, timeout)
        session["running"] = running
        checks.append({"name": "session_running", "ok": running == 1,
                       "detail": "SOS workarea session is running." if running == 1
                       else "SOS workarea session is stopped; no command was used to start it."})
        if running == 1:
            target, session = _session_identity(owner, target, executable, timeout)
            checks.append({"name": "server_connectivity", "ok": session["offline_code"] == 0,
                           "detail": session["offline_detail"]})
            checks.append({"name": "session_mode", "ok": True,
                           "detail": session["nowin_detail"]})
            area = SOSWorkarea(owner, target.workarea, soscmd=executable)
            state, _scope, _attributes = _lock_record(area, target, timeout)
            checks.append({"name": "server_object_status", "ok": True,
                           "detail": "Server returned one exact target record."})
            native_ok, native_detail = _native_status(area, target, timeout)
            checks.append({"name": "native_status", "ok": native_ok,
                           "detail": native_detail})
    except Exception as exc:
        checks.append({"name": "session_probe", "ok": False, "detail": str(exc)})
    ok = bool(checks) and all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "action": "session_doctor",
        "outcome": "success" if ok else "blocked",
        "target": asdict(target),
        "session": session,
        "before": asdict(state) if state else None,
        "checks": checks,
        "diagnostics": [
            "Read-only session checks; is_running is queried before commands that may start SOS."
        ],
    }


def restart_session_cellview(owner: VirtuosoClient, lib: str, cell: str, view: str, *,
                             dry_run: bool = False,
                             force_cadence_disconnect: bool = False,
                             timeout: float = 60, soscmd: str | None = None) -> dict:
    before = diagnose_session_cellview(
        owner, lib, cell, view, timeout=timeout, soscmd=soscmd,
    )
    target = SOSCellViewTarget(**before["target"])
    session = before.get("session") or {}
    result = {
        "ok": False,
        "action": "session_restart",
        "outcome": "blocked",
        "target": before["target"],
        "session_before": session,
        "session_after": None,
        "checks": before.get("checks", []),
        "diagnostics": [],
    }
    if session.get("running") != 1 or not target.workarea or not target.path:
        result["diagnostics"].append(
            "No running, positively identified SOS session is available to restart."
        )
        return result
    if target.unsaved:
        result["diagnostics"].append(
            "Unsaved changes in the connected CIW; save or close them before session recovery."
        )
        return result
    if before["ok"]:
        result["outcome"] = "noop"
        result["ok"] = True
        result["diagnostics"].append("SOS session is healthy; no restart was performed.")
        return result
    try:
        if _maestro_active(owner, timeout):
            result["diagnostics"].append(
                "A Maestro session is active in the connected CIW; session restart is prohibited."
            )
            return result
    except Exception as exc:
        result["diagnostics"].append(f"Maestro activity could not be confirmed: {exc}")
        return result
    if dry_run:
        result["outcome"] = "dry_run"
        result["ok"] = True
        result["diagnostics"].append(
            "Recovery preconditions passed; SOS was not stopped or restarted."
        )
        if not force_cadence_disconnect:
            result["diagnostics"].append(
                "A normal exitsos refusal will remain blocked unless force Cadence disconnect is explicit."
            )
        return result

    executable = resolve_soscmd(owner, soscmd, timeout=timeout)
    exit_reply = None
    try:
        exit_reply = _run_in(owner, target.workarea, executable, "exitsos", timeout=timeout)
    except Exception as exc:
        result["outcome"] = "unknown"
        result["diagnostics"].extend((
            f"SOS exitsos result unavailable: {exc}",
            "No force exit or restart was attempted; query session status before retrying.",
        ))
        return result

    if exit_reply.returncode:
        try:
            still_running = _query_code(
                owner, target.workarea, executable, "is_running", {0, 1}, timeout,
            )
        except Exception as exc:
            result["outcome"] = "unknown"
            result["diagnostics"].extend((
                f"SOS exit failed and follow-up session state is unavailable: {exc}",
                "No force exit or restart was attempted; do not retry automatically.",
            ))
            return result
        detail = exit_reply.stderr.strip() or exit_reply.stdout.strip()
        if still_running != 1:
            result["outcome"] = "unknown"
            result["diagnostics"].extend((
                detail or "SOS exitsos returned nonzero after the session stopped.",
                "The session state changed despite an unsuccessful reply; no command was repeated.",
            ))
            return result
        if not force_cadence_disconnect:
            result["diagnostics"].extend((
                detail or "SOS refused normal session exit.",
                "Use force Cadence disconnect only on a dedicated idle CIW.",
            ))
            return result
        try:
            exit_reply = _run_in(
                owner, target.workarea, executable, "exitsos", "-F", timeout=timeout,
            )
        except Exception as exc:
            result["outcome"] = "unknown"
            result["diagnostics"].extend((
                f"Forced SOS exit result unavailable: {exc}",
                "Do not repeat the forced exit; query session status first.",
            ))
            return result
        if exit_reply.returncode:
            detail = exit_reply.stderr.strip() or exit_reply.stdout.strip()
            try:
                still_running = _query_code(
                    owner, target.workarea, executable, "is_running", {0, 1}, timeout,
                )
            except Exception as exc:
                result["outcome"] = "unknown"
                if detail:
                    result["diagnostics"].append(detail)
                result["diagnostics"].extend((
                    f"Forced SOS exit failed and follow-up session state is unavailable: {exc}",
                    "Do not repeat the forced exit; query session status first.",
                ))
                return result
            if still_running != 1:
                result["outcome"] = "unknown"
                result["diagnostics"].extend((
                    detail or "Forced SOS exit returned nonzero after the session stopped.",
                    "The session state changed despite an unsuccessful reply; no command was repeated.",
                ))
                return result
            result["diagnostics"].append(detail or "Forced SOS exit failed.")
            return result

    try:
        stopped = _query_code(
            owner, target.workarea, executable, "is_running", {0, 1}, timeout,
        )
    except Exception as exc:
        result["outcome"] = "unknown"
        result["diagnostics"].append(
            f"SOS exit completed, but stopped state is unavailable: {exc}"
        )
        return result
    if stopped != 0:
        result["outcome"] = "unknown"
        result["diagnostics"].append(
            "SOS exit returned success, but the workarea session still appears to be running."
        )
        return result

    area = SOSWorkarea(owner, target.workarea, soscmd=executable)
    try:
        start_reply = area._run(
            "nobjstatus", *_NOBJSTATUS_ATTRIBUTES, "./" + target.path, timeout=timeout,
        )
    except Exception as exc:
        result["outcome"] = "unknown"
        result["diagnostics"].extend((
            f"SOS restart query result unavailable: {exc}",
            "Do not repeat the restart automatically; inspect the session first.",
        ))
        return result
    if start_reply.returncode:
        result["outcome"] = "failed"
        result["diagnostics"].append(
            start_reply.stderr.strip() or start_reply.stdout.strip()
            or "SOS session did not restart through the read-only object query."
        )
        return result
    try:
        _parse_nobjstatus(start_reply.stdout, target)
    except Exception as exc:
        result["outcome"] = "unknown"
        result["diagnostics"].append(
            f"SOS restarted, but exact target verification failed: {exc}"
        )
        return result
    after = diagnose_session_cellview(
        owner, lib, cell, view, timeout=timeout, soscmd=executable,
    )
    result["session_after"] = after.get("session")
    result["checks"] = after.get("checks", [])
    result["target"] = after.get("target", result["target"])
    if after.get("ok"):
        result["ok"] = True
        result["outcome"] = "success"
        result["diagnostics"].append(
            "SOS session restarted once and passed server-object plus native-status verification."
        )
    else:
        result["outcome"] = "unknown"
        result["diagnostics"].extend(after.get("diagnostics") or [])
        result["diagnostics"].append(
            "Session restart was dispatched, but full health could not be confirmed; do not retry automatically."
        )
    return result
