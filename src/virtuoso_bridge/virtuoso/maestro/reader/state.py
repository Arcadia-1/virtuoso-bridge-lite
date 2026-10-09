"""Structured, fail-closed Maestro window and session state probes."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import VirtuosoResult
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr


Context = Literal[
    "gui", "headless", "no_window", "non_maestro_window", "not_found", "unknown",
]
Access = Literal["editing", "reading", "unknown"]

_PROBE_TAG = "VB_MAESTRO_STATE_V1"
_MAE_TITLE_RE = re.compile(
    r"ADE\s+(Assembler|Explorer)\s+(Editing|Reading):\s+"
    r"(\S+)\s+(\S+)\s+([^\s*]+)(\*?)"
    r"(?:\s+Version:\s*\S+(?:\s*-\s*\S+)?)?\s*$"
)


class MaestroStateProbeError(RuntimeError):
    """The CIW state probe failed or returned an ambiguous payload."""

    def __init__(self, message: str, *, result: VirtuosoResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class MaestroSessionState(BaseModel):
    """Observed state of one Maestro session or the current GUI context."""

    model_config = ConfigDict(frozen=True)

    context: Context
    access: Access = "unknown"
    unsaved: bool | None = None
    session: str | None = None
    window_num: int | None = None
    application: Literal["assembler", "explorer"] | None = None
    lib: str | None = None
    cell: str | None = None
    view: str | None = None
    title: str | None = None
    current: bool = False
    source: Literal["window_title", "session_inventory", "window_inventory"]
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)


class _WindowObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    current: bool
    window_num: int
    title: str
    axl_session: str | None
    dav_session: str | None


class _Inventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    windows: tuple[_WindowObservation, ...]
    sessions: tuple[str, ...]
    current_window_num: int | None
    current_title: str | None


def parse_maestro_title(title: str) -> dict[str, object] | None:
    """Parse a known ADE title; return ``None`` instead of guessing."""
    match = _MAE_TITLE_RE.search(title or "")
    if match is None:
        return None
    application, mode, lib, cell, view, star = match.groups()
    return {
        "application": application.lower(),
        "access": mode.lower(),
        "unsaved": star == "*",
        "lib": lib,
        "cell": cell,
        "view": view,
    }


def _probe_skill() -> str:
    return f'''prog((vbRows vbSessions vbCurrent vbCurrentNum vbCurrentTitle
                     vbW vbTitle vbAxl vbDav vbNum)
  vbCurrent=hiGetCurrentWindow()
  vbCurrentNum=when(vbCurrent car(errset(vbCurrent~>windowNum)))
  vbCurrentTitle=when(vbCurrent car(errset(hiGetWindowName(vbCurrent))))
  vbRows=nil
  foreach(vbW hiGetWindowList()
    vbNum=car(errset(vbW~>windowNum))
    vbTitle=car(errset(hiGetWindowName(vbW)))
    vbAxl=when(isCallable('axlGetWindowSession)
      car(errset(axlGetWindowSession(vbW))))
    vbDav=car(errset(vbW~>davSession))
    when(numberp(vbNum)
      vbRows=cons(list(if(vbCurrent && vbW==vbCurrent then t else nil)
                       vbNum vbTitle vbAxl vbDav) vbRows)))
  vbSessions=if(isCallable('maeGetSessions) then maeGetSessions() else nil)
  return(list("{_PROBE_TAG}" reverse(vbRows) vbSessions
              vbCurrentNum vbCurrentTitle))
)'''


def _string_or_none(value, label: str, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or (not value and not allow_empty):
        raise MaestroStateProbeError(f"Malformed Maestro state {label}.")
    return value


def _parse_inventory(raw: str) -> _Inventory:
    value = parse_sexpr(raw)
    if not isinstance(value, list) or len(value) != 5 or value[0] != _PROBE_TAG:
        raise MaestroStateProbeError("Malformed Maestro state probe envelope.")
    raw_windows, raw_sessions, raw_current_num, raw_current_title = value[1:]
    if raw_windows is None:
        raw_windows = []
    if raw_sessions is None:
        raw_sessions = []
    if not isinstance(raw_windows, list) or not isinstance(raw_sessions, list):
        raise MaestroStateProbeError("Malformed Maestro state inventory.")

    windows: list[_WindowObservation] = []
    seen_window_nums: set[int] = set()
    for row in raw_windows:
        if not isinstance(row, list) or len(row) != 5 or row[0] not in {True, None}:
            raise MaestroStateProbeError("Malformed Maestro window row.")
        try:
            window_num = int(row[1])
        except (TypeError, ValueError) as exc:
            raise MaestroStateProbeError("Malformed Maestro window number.") from exc
        if window_num <= 0 or window_num in seen_window_nums:
            raise MaestroStateProbeError("Duplicate or invalid Maestro window number.")
        seen_window_nums.add(window_num)
        title = _string_or_none(row[2], "window title", allow_empty=True) or ""
        windows.append(_WindowObservation(
            current=row[0] is True,
            window_num=window_num,
            title=title,
            axl_session=_string_or_none(row[3], "axl session"),
            dav_session=_string_or_none(row[4], "dav session"),
        ))

    sessions: list[str] = []
    for item in raw_sessions:
        session = _string_or_none(item, "session inventory entry")
        if session is None or session in sessions:
            raise MaestroStateProbeError("Duplicate or empty Maestro session inventory entry.")
        sessions.append(session)

    current_window_num = None
    if raw_current_num is not None:
        try:
            current_window_num = int(raw_current_num)
        except (TypeError, ValueError) as exc:
            raise MaestroStateProbeError("Malformed current window number.") from exc
        if current_window_num <= 0:
            raise MaestroStateProbeError("Malformed current window number.")

    return _Inventory(
        windows=tuple(windows),
        sessions=tuple(sessions),
        current_window_num=current_window_num,
        current_title=_string_or_none(
            raw_current_title, "current window title", allow_empty=True,
        ),
    )


def _probe_inventory(client: VirtuosoClient, *, timeout: float = 30) -> _Inventory:
    result = client.execute_skill(_probe_skill(), timeout=timeout)
    if not getattr(result, "ok", not getattr(result, "errors", [])):
        detail = "; ".join(getattr(result, "errors", []) or [])
        raise MaestroStateProbeError(
            "Maestro state probe failed." + (f" {detail}" if detail else ""),
            result=result,
        )
    if getattr(result, "errors", []):
        raise MaestroStateProbeError(
            "Maestro state probe reported errors: " + "; ".join(result.errors),
            result=result,
        )
    try:
        return _parse_inventory((getattr(result, "output", "") or "").strip())
    except MaestroStateProbeError as exc:
        raise MaestroStateProbeError(str(exc), result=result) from exc


def _state_from_window(window: _WindowObservation) -> MaestroSessionState | None:
    diagnostics: list[str] = []
    session = window.axl_session or window.dav_session
    if window.axl_session and window.dav_session and window.axl_session != window.dav_session:
        diagnostics.append(
            "axlGetWindowSession and davSession disagree; the target session is ambiguous."
        )
        session = None
    parsed = parse_maestro_title(window.title)
    if session is None and parsed is None:
        return None
    if parsed is None:
        diagnostics.append("The ADE window title format is not recognized.")
        return MaestroSessionState(
            context="gui",
            session=session,
            window_num=window.window_num,
            title=window.title,
            current=window.current,
            source="window_inventory",
            diagnostics=tuple(diagnostics),
        )
    return MaestroSessionState(
        context="gui",
        access=parsed["access"],
        unsaved=parsed["unsaved"],
        session=session,
        window_num=window.window_num,
        application=parsed["application"],
        lib=parsed["lib"],
        cell=parsed["cell"],
        view=parsed["view"],
        title=window.title,
        current=window.current,
        source="window_title",
        diagnostics=tuple(diagnostics),
    )


def _states_from_inventory(inventory: _Inventory) -> list[MaestroSessionState]:
    states: list[MaestroSessionState] = []
    window_sessions: set[str] = set()
    for window in inventory.windows:
        bindings = {
            session for session in (window.axl_session, window.dav_session) if session
        }
        window_sessions.update(bindings)
        if len(bindings) > 1:
            diagnostics = (
                "axlGetWindowSession and davSession disagree; "
                "the target session is ambiguous.",
            )
            for session in sorted(bindings):
                states.append(MaestroSessionState(
                    context="unknown",
                    session=session,
                    window_num=window.window_num,
                    title=window.title,
                    current=window.current,
                    source="window_inventory",
                    diagnostics=diagnostics,
                ))
            continue
        state = _state_from_window(window)
        if state is not None:
            states.append(state)
    for session in inventory.sessions:
        if session not in window_sessions:
            states.append(MaestroSessionState(
                context="headless",
                session=session,
                source="session_inventory",
                diagnostics=(
                    "The session has no observed GUI window; background and stale GUI sessions "
                    "cannot be distinguished from this inventory alone.",
                ),
            ))
    return states


def list_session_states(
    client: VirtuosoClient, *, timeout: float = 30,
) -> list[MaestroSessionState]:
    """Return one state per observed GUI or headless Maestro session."""
    return _states_from_inventory(_probe_inventory(client, timeout=timeout))


def get_session_state(
    client: VirtuosoClient, session: str | None = None, *, timeout: float = 30,
) -> MaestroSessionState:
    """Return an exact session state, or the current window context."""
    inventory = _probe_inventory(client, timeout=timeout)
    states = _states_from_inventory(inventory)
    if session is not None:
        matches = [state for state in states if state.session == session]
        if not matches:
            return MaestroSessionState(
                context="not_found", session=session, source="session_inventory",
                diagnostics=("The requested session is not present in the current inventory.",),
            )
        if len(matches) == 1:
            return matches[0]
        return MaestroSessionState(
            context="unknown", session=session, source="session_inventory",
            diagnostics=("More than one GUI window is bound to the requested session.",),
        )

    if inventory.current_window_num is None:
        return MaestroSessionState(
            context="no_window", source="window_inventory",
            diagnostics=("Virtuoso has no current window.",),
        )
    matches = [state for state in states if state.window_num == inventory.current_window_num]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        diagnostics = tuple(dict.fromkeys(
            diagnostic
            for state in matches
            for diagnostic in state.diagnostics
        )) + ("The current window maps to more than one Maestro state.",)
        return MaestroSessionState(
            context="unknown",
            window_num=inventory.current_window_num,
            title=inventory.current_title,
            current=True,
            source="window_inventory",
            diagnostics=diagnostics,
        )
    return MaestroSessionState(
        context="non_maestro_window",
        window_num=inventory.current_window_num,
        title=inventory.current_title,
        current=True,
        source="window_inventory",
        diagnostics=("The current window is not bound to a Maestro session.",),
    )


__all__ = [
    "MaestroSessionState",
    "MaestroStateProbeError",
    "get_session_state",
    "list_session_states",
    "parse_maestro_title",
]
