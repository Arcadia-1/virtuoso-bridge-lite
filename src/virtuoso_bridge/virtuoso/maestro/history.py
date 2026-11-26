"""Maestro simulation history discovery and lock management."""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


_HISTORY_TAG = "VB_MAESTRO_HISTORY_V1"
_LOCK_TAG = "VB_MAESTRO_HISTORY_LOCK_V1"
_UNKNOWN_TRANSPORT_MARKERS = (
    "socket timeout",
    "socket error",
    "connection refused",
)


class MaestroHistoryError(RuntimeError):
    """A Maestro history probe or lock operation failed."""


class MaestroHistoryOutcomeUnknown(MaestroHistoryError):
    """A lock mutation may have executed, but its final state is unconfirmed."""


class MaestroHistory(BaseModel):
    """One history entry observed in a named Maestro session."""

    model_config = ConfigDict(frozen=True)

    name: str
    locked: bool
    current: bool = False


class MaestroHistoryLockResult(BaseModel):
    """Verified result of requesting one history lock state."""

    model_config = ConfigDict(frozen=True)

    session: str
    history: str
    requested_locked: bool
    before: MaestroHistory
    after: MaestroHistory
    changed: bool
    outcome: Literal["applied", "already_satisfied", "confirmed_after_unknown"]


def _require_name(value: str, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must be a non-empty string.")
    return normalized


def _history_probe_skill(session: str) -> str:
    escaped_session = escape_skill_string(session)
    return f'''prog((vbProbe vbSdb vbHistory vbNames vbCurrent vbRows
                      vbName vbEntry vbLocked)
  unless(isCallable('axlGetMainSetupDB)
    return(list("{_HISTORY_TAG}" "unsupported" "axlGetMainSetupDB")))
  unless(isCallable('axlGetHistory)
    return(list("{_HISTORY_TAG}" "unsupported" "axlGetHistory")))
  unless(isCallable('axlGetHistoryEntry)
    return(list("{_HISTORY_TAG}" "unsupported" "axlGetHistoryEntry")))
  unless(isCallable('axlGetHistoryLock)
    return(list("{_HISTORY_TAG}" "unsupported" "axlGetHistoryLock")))
  unless(isCallable('axlGetCurrentHistory)
    return(list("{_HISTORY_TAG}" "unsupported" "axlGetCurrentHistory")))

  vbProbe=errset(axlGetMainSetupDB("{escaped_session}"))
  unless(vbProbe
    return(list("{_HISTORY_TAG}" "probe_error" "axlGetMainSetupDB")))
  vbSdb=car(vbProbe)
  unless(vbSdb
    return(list("{_HISTORY_TAG}" "session_not_found" "{escaped_session}")))

  vbProbe=errset(axlGetHistory(vbSdb))
  unless(vbProbe
    return(list("{_HISTORY_TAG}" "probe_error" "axlGetHistory")))
  vbHistory=car(vbProbe)
  unless(vbHistory && listp(vbHistory) && length(vbHistory) >= 2
    return(list("{_HISTORY_TAG}" "malformed" "axlGetHistory")))
  vbNames=cadr(vbHistory)
  unless(listp(vbNames)
    return(list("{_HISTORY_TAG}" "malformed" "history_names")))

  vbProbe=errset(axlGetCurrentHistory("{escaped_session}"))
  unless(vbProbe
    return(list("{_HISTORY_TAG}" "probe_error" "axlGetCurrentHistory")))
  vbCurrent=car(vbProbe)
  vbRows=nil
  foreach(vbName vbNames
    vbProbe=errset(axlGetHistoryEntry(vbSdb vbName))
    unless(vbProbe && car(vbProbe)
      return(list("{_HISTORY_TAG}" "probe_error" vbName)))
    vbEntry=car(vbProbe)
    vbProbe=errset(axlGetHistoryLock(vbEntry))
    unless(vbProbe
      return(list("{_HISTORY_TAG}" "probe_error" vbName)))
    vbLocked=car(vbProbe)
    vbRows=cons(list(vbName vbLocked
                     if(vbCurrent == vbEntry then t else nil)) vbRows))
  return(list("{_HISTORY_TAG}" "ok" reverse(vbRows)))
)'''


def _set_history_lock_skill(session: str, history: str, locked: bool) -> str:
    escaped_session = escape_skill_string(session)
    escaped_history = escape_skill_string(history)
    lock_value = "t" if locked else "nil"
    return f'''prog((vbProbe)
  unless(isCallable('maeSetHistoryLock)
    return(list("{_LOCK_TAG}" "unsupported" "maeSetHistoryLock")))
  vbProbe=errset(maeSetHistoryLock("{escaped_history}" {lock_value}
                                  ?session "{escaped_session}"))
  unless(vbProbe
    return(list("{_LOCK_TAG}" "probe_error" "maeSetHistoryLock")))
  return(list("{_LOCK_TAG}" "ok" car(vbProbe)))
)'''


def _result_errors(result) -> str:
    return "; ".join(getattr(result, "errors", []) or [])


def _parse_history_payload(raw: str) -> list[MaestroHistory]:
    value = parse_sexpr(raw)
    if not isinstance(value, list) or len(value) != 3 or value[0] != _HISTORY_TAG:
        raise MaestroHistoryError("Malformed Maestro history probe envelope.")
    status, payload = value[1], value[2]
    if status != "ok":
        detail = payload if isinstance(payload, str) else repr(payload)
        raise MaestroHistoryError(f"Maestro history probe failed: {status}: {detail}")
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise MaestroHistoryError("Malformed Maestro history row list.")

    histories: list[MaestroHistory] = []
    seen: set[str] = set()
    for row in payload:
        if not isinstance(row, list) or len(row) != 3:
            raise MaestroHistoryError("Malformed Maestro history row.")
        name, locked, current = row
        if not isinstance(name, str) or not name or name in seen:
            raise MaestroHistoryError("Duplicate or invalid Maestro history name.")
        if ((locked is not True and locked is not None)
                or (current is not True and current is not None)):
            raise MaestroHistoryError("Malformed Maestro history lock/current flag.")
        seen.add(name)
        histories.append(MaestroHistory(
            name=name,
            locked=locked is True,
            current=current is True,
        ))
    return histories


def _parse_lock_payload(raw: str) -> bool:
    value = parse_sexpr(raw)
    if not isinstance(value, list) or len(value) != 3 or value[0] != _LOCK_TAG:
        raise MaestroHistoryError("Malformed Maestro history lock response.")
    status, payload = value[1], value[2]
    if status != "ok":
        detail = payload if isinstance(payload, str) else repr(payload)
        raise MaestroHistoryError(f"Maestro history lock failed: {status}: {detail}")
    if payload is not True and payload is not None:
        raise MaestroHistoryError("Malformed Maestro history lock return value.")
    return payload is True


def _transport_outcome_unknown(result) -> bool:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        if (
            metadata.get("outcome") == "not_started"
            or metadata.get("request_sent") is False
        ):
            return False
        if metadata.get("outcome") == "unknown":
            return True
        if metadata.get("request_handle") and not getattr(result, "ok", False):
            return True
    detail = _result_errors(result).lower()
    return not getattr(result, "ok", False) and any(
        marker in detail for marker in _UNKNOWN_TRANSPORT_MARKERS
    )


def list_histories(
    client: "VirtuosoClient", session: str, *, timeout: float = 30,
) -> list[MaestroHistory]:
    """List histories and lock state for one explicit Maestro session."""
    session = _require_name(session, "session")
    result = client.execute_skill(_history_probe_skill(session), timeout=timeout)
    if not getattr(result, "ok", False):
        raise MaestroHistoryError(
            "Maestro history probe execution failed"
            + (f": {_result_errors(result)}" if _result_errors(result) else ".")
        )
    return _parse_history_payload((getattr(result, "output", "") or "").strip())


def get_history(
    client: "VirtuosoClient", history: str, *, session: str, timeout: float = 30,
) -> MaestroHistory:
    """Return one exact history from one explicit Maestro session."""
    session = _require_name(session, "session")
    history = _require_name(history, "history")
    matches = [item for item in list_histories(client, session, timeout=timeout)
               if item.name == history]
    if not matches:
        raise MaestroHistoryError(
            f"History {history!r} was not found in Maestro session {session!r}."
        )
    if len(matches) != 1:
        raise MaestroHistoryError(
            f"History {history!r} is ambiguous in Maestro session {session!r}."
        )
    return matches[0]


def set_history_lock(
    client: "VirtuosoClient",
    history: str,
    locked: bool,
    *,
    session: str,
    timeout: float = 30,
) -> MaestroHistoryLockResult:
    """Set and verify one history lock without retrying an uncertain mutation."""
    session = _require_name(session, "session")
    history = _require_name(history, "history")
    if not isinstance(locked, bool):
        raise TypeError("locked must be a bool.")

    before = get_history(client, history, session=session, timeout=timeout)
    if before.locked == locked:
        return MaestroHistoryLockResult(
            session=session,
            history=history,
            requested_locked=locked,
            before=before,
            after=before,
            changed=False,
            outcome="already_satisfied",
        )

    mutation = None
    mutation_exception: Exception | None = None
    try:
        mutation = client.execute_skill(
            _set_history_lock_skill(session, history, locked),
            timeout=timeout,
            retry_connect=False,
        )
    except Exception as exc:
        mutation_exception = exc

    uncertain_transport = mutation_exception is not None or (
        mutation is not None and _transport_outcome_unknown(mutation)
    )
    if mutation is not None and not uncertain_transport:
        if not getattr(mutation, "ok", False):
            raise MaestroHistoryError(
                "maeSetHistoryLock execution failed"
                + (f": {_result_errors(mutation)}" if _result_errors(mutation) else ".")
            )
        if not _parse_lock_payload((getattr(mutation, "output", "") or "").strip()):
            raise MaestroHistoryError(
                f"maeSetHistoryLock returned nil for history {history!r}."
            )

    try:
        after = get_history(client, history, session=session, timeout=timeout)
    except Exception as exc:
        raise MaestroHistoryOutcomeUnknown(
            f"History {history!r} lock request was sent, but post-state could not be "
            f"verified: {exc}"
        ) from exc

    if uncertain_transport:
        if after.locked == locked:
            return MaestroHistoryLockResult(
                session=session,
                history=history,
                requested_locked=locked,
                before=before,
                after=after,
                changed=True,
                outcome="confirmed_after_unknown",
            )
        raise MaestroHistoryOutcomeUnknown(
            f"History {history!r} lock outcome is unknown after transport failure; "
            f"observed locked={after.locked}, requested locked={locked}."
        )

    if after.locked != locked:
        raise MaestroHistoryError(
            f"History {history!r} lock post-state mismatch: "
            f"observed locked={after.locked}, requested locked={locked}."
        )

    return MaestroHistoryLockResult(
        session=session,
        history=history,
        requested_locked=locked,
        before=before,
        after=after,
        changed=True,
        outcome="applied",
    )


def lock_history(
    client: "VirtuosoClient", history: str, *, session: str, timeout: float = 30,
) -> MaestroHistoryLockResult:
    """Lock one history and verify the resulting state."""
    return set_history_lock(
        client, history, True, session=session, timeout=timeout,
    )


def unlock_history(
    client: "VirtuosoClient", history: str, *, session: str, timeout: float = 30,
) -> MaestroHistoryLockResult:
    """Unlock one history and verify the resulting state."""
    return set_history_lock(
        client, history, False, session=session, timeout=timeout,
    )


__all__ = [
    "MaestroHistory",
    "MaestroHistoryError",
    "MaestroHistoryLockResult",
    "MaestroHistoryOutcomeUnknown",
    "get_history",
    "list_histories",
    "lock_history",
    "set_history_lock",
    "unlock_history",
]


_DELETE_TAG = "VB_MAESTRO_HISTORY_DELETE_V1"
_HISTORY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


def _delete_history_skill(session: str, history: str) -> str:
    s, h = escape_skill_string(session), escape_skill_string(history)
    return f'''prog((vbProbe vbSdb vbRun vbEntry)
  foreach(vbFn '(axlGetMainSetupDB axlGetRunStatus axlGetHistoryEntry axlGetHistoryLock axlRemoveElement)
    unless(isCallable(vbFn) return(list("{_DELETE_TAG}" "unsupported" symbolToString(vbFn)))))
  vbSdb=car(errset(axlGetMainSetupDB("{s}")))
  unless(vbSdb return(list("{_DELETE_TAG}" "session_not_found" "{s}")))
  vbRun=car(errset(axlGetRunStatus("{s}")))
  unless(listp(vbRun) && length(vbRun)==2 && numberp(car(vbRun)) && numberp(cadr(vbRun))
    return(list("{_DELETE_TAG}" "run_status_unknown" sprintf(nil "%L" vbRun))))
  when(car(vbRun) < cadr(vbRun)
    return(list("{_DELETE_TAG}" "running" sprintf(nil "%d of %d points done" car(vbRun) cadr(vbRun)))))
  vbEntry=car(errset(axlGetHistoryEntry(vbSdb "{h}")))
  unless(vbEntry return(list("{_DELETE_TAG}" "not_found" "{h}")))
  vbProbe=errset(axlGetHistoryLock(vbEntry))
  unless(vbProbe
    return(list("{_DELETE_TAG}" "lock_check_failed" "{h}")))
  when(car(vbProbe)
    return(list("{_DELETE_TAG}" "locked" "{h}")))
  vbProbe=errset(axlRemoveElement(vbEntry))
  unless(vbProbe && car(vbProbe)
    return(list("{_DELETE_TAG}" "remove_failed" "{h}")))
  when(isCallable('maeSaveSetup) errset(maeSaveSetup(?session "{s}")))
  return(list("{_DELETE_TAG}" "ok" "{h}"))
)'''


def delete_history(
    client: "VirtuosoClient",
    history: str,
    *,
    session: str,
    lib: str = "",
    cell: str = "",
    remove_files: bool = True,
    timeout: float = 60,
) -> list[str]:
    """Delete one history from a Maestro session, refusing while it runs.

    Removing a history entry while the session has a run in progress left
    IC23.1 unable to start runs ("Cannot find a setup database entry for
    handle N"), and closing that Maestro window then crashed Virtuoso. This
    refuses while ``axlGetRunStatus`` reports unfinished points, and for a
    locked history. IC23.1 removes the history's result files itself once
    the entry goes; with ``remove_files`` any left behind are removed too
    (``<history>`` and its ``.rdb``/``.log``/``.msg.db`` under the library,
    the run directory under the ADE project directory). Returns the paths
    this call removed, often none.
    """
    from virtuoso_bridge.virtuoso.maestro.reader.runs import _candidate_result_roots

    session = _require_name(session, "session")
    history = _require_name(history, "history")
    if not _HISTORY_NAME.fullmatch(history):
        raise ValueError(f"history {history!r} is not a plain history name")

    def skill(expr: str) -> str:
        r = client.execute_skill(expr, timeout=timeout)
        if not getattr(r, "ok", False):
            raise MaestroHistoryError(f"{expr[:60]}... failed: {_result_errors(r)}")
        return (r.output or "").strip()

    # Refuse a running session before touching anything; the delete below
    # checks again in the same SKILL call that removes the entry.
    raw = skill(f'car(errset(axlGetRunStatus("{escape_skill_string(session)}")))')
    done_total = re.fullmatch(r"\(\s*(\d+)\s+(\d+)\s*\)", raw)
    if done_total and int(done_total[1]) < int(done_total[2]):
        raise MaestroHistoryError(
            f"History {history!r} not deleted: running: "
            f"{done_total[1]} of {done_total[2]} points done")

    roots: list[str] = []
    if remove_files:
        if not lib or not cell:
            from virtuoso_bridge.virtuoso.maestro.reader._skill import _session_lib_cell
            found_lib, found_cell = _session_lib_cell(client, session)
            lib, cell = lib or found_lib, cell or found_cell
        lib_path = skill(f'ddGetObj("{escape_skill_string(lib)}")~>readPath').strip('"')
        if not lib_path or lib_path == "nil":
            raise MaestroHistoryError(f"library {lib!r} not found")
        tail = f"{lib}/{cell}/maestro/results/maestro/{history}"
        base = f"{lib_path}/{cell}/maestro/results/maestro/{history}"
        # Read the run directories before the entry (and its .rdb) goes.
        roots = [base + suffix for suffix in ("", ".rdb", ".log", ".msg.db")]
        for root in _candidate_result_roots(client, [], lib_path, cell, history, tail):
            if root not in roots:
                roots.append(root)

    result = client.execute_skill(
        _delete_history_skill(session, history),
        timeout=timeout,
        retry_connect=False,
    )
    if not getattr(result, "ok", False):
        if _transport_outcome_unknown(result):
            raise MaestroHistoryOutcomeUnknown(
                f"Deleting history {history!r} has an unknown outcome: {_result_errors(result)}")
        raise MaestroHistoryError(f"Deleting history {history!r} failed: {_result_errors(result)}")
    value = parse_sexpr((result.output or "").strip())
    if not isinstance(value, list) or len(value) != 3 or value[0] != _DELETE_TAG:
        raise MaestroHistoryError("Malformed Maestro history delete envelope.")
    if value[1] != "ok":
        raise MaestroHistoryError(f"History {history!r} not deleted: {value[1]}: {value[2]}")

    removed: list[str] = []
    suffix = re.compile(r"/maestro/results/maestro/" + re.escape(history) + r"(\.rdb|\.log|\.msg\.db)?$")
    runner = client.ssh_runner
    for path in roots:
        if not suffix.search(path):
            continue
        if runner is None:
            target = Path(path)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink(missing_ok=True)
            else:
                continue
            removed.append(path)
        else:
            q = shlex.quote(path)
            out = runner.run_command(f"sh -c {shlex.quote(f'test -e {q} && rm -rf {q} && echo removed')}")
            if "removed" in (out.stdout or ""):
                removed.append(path)
    return removed
