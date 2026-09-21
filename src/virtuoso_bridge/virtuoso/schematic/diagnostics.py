"""Operation-scoped schematic check/save diagnostics.

``schCheck`` exposes authoritative error/warning counts but not the messages
behind those counts.  Cadence writes the message text to the session log.  The
helpers here bracket each operation with the public ``hiGetLogFileName`` /
``hiFlushLogFile`` APIs and return only the bytes appended during that
operation, so old CIW history is never mistaken for a current diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Literal

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.response import response_fields
from virtuoso_bridge.virtuoso.skill_output import (
    is_single_complete_skill_list,
    parse_sexpr,
)


DiagnosticSeverity = Literal["error", "warning", "info"]
CheckSaveStatus = Literal[
    "saved",
    "check_failed",
    "save_failed",
    "blocked",
    "timeout",
    "operation_error",
    "protocol_error",
]

_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)\b", re.IGNORECASE)
_SEVERITY_RE = re.compile(
    r"^\s*(?:\*+\s*)?(ERROR|WARNING|WARN|INFO)(?:\s*\*+)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SchematicDiagnostic:
    """One structured diagnostic emitted by the current operation."""

    severity: DiagnosticSeverity
    source: str
    message: str
    code: str | None = None


@dataclass(frozen=True)
class SchematicCheckSaveResult:
    """Result of checking and saving one explicit schematic cellview."""

    status: CheckSaveStatus
    lib: str
    cell: str
    view: str
    checked: bool
    saved: bool
    check_error_count: int
    check_warning_count: int
    log_capture_available: bool
    diagnostics: tuple[SchematicDiagnostic, ...] = ()
    check_log_lines: tuple[str, ...] = ()
    save_log_lines: tuple[str, ...] = ()
    modal_windows: tuple[dict[str, Any], ...] = ()
    screenshot: Path | None = None
    raw_output: str = ""

    @property
    def ok(self) -> bool:
        """Whether the schematic was checked without errors and saved."""

        return self.status == "saved"

    @property
    def errors(self) -> tuple[SchematicDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[SchematicDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")


def schematic_check_save_diagnostics_skill(
    lib: str,
    cell: str,
    *,
    view: str = "schematic",
) -> str:
    """Build SKILL for an explicit check/save plus incremental CDS.log read."""

    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    return (
        "let((vbCv vbViewObj vbCounts vbErrorCount vbWarningCount vbSaved vbResult "
        "vbLogAvailable vbLogPath vbCheckStart vbCheckEnd vbCheckPort "
        "vbCheckLine vbCheckLines vbSaveStart vbSaveEnd vbSavePort "
        "vbSaveLine vbSaveLines) "
        f'vbViewObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_view}") '
        'unless(vbViewObj error("schematic cellview not found")) '
        f'vbCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_view}" "schematic" "a") '
        'unless(vbCv error("schematic cellview not found")) '
        "vbResult = unwindProtect(progn("
        "vbLogAvailable = isCallable('hiGetLogFileName) && "
        "isCallable('hiFlushLogFile) "
        "when(vbLogAvailable "
        "hiFlushLogFile() "
        "vbLogPath = hiGetLogFileName() "
        "vbLogAvailable = vbLogPath && isFile(vbLogPath) "
        "when(vbLogAvailable vbCheckStart = fileLength(vbLogPath))) "
        "vbCounts = schCheck(vbCv) "
        "vbErrorCount = if(vbCounts car(vbCounts) 0) "
        "vbWarningCount = if(vbCounts cadr(vbCounts) 0) "
        "when(vbLogAvailable "
        "hiFlushLogFile() "
        "vbCheckEnd = fileLength(vbLogPath) "
        "when(vbCheckEnd >= vbCheckStart "
        "vbCheckPort = infile(vbLogPath) "
        "when(vbCheckPort "
        "fileSeek(vbCheckPort vbCheckStart 0) "
        "while(fileTell(vbCheckPort) < vbCheckEnd && "
        "gets(vbCheckLine vbCheckPort) "
        "vbCheckLines = cons(vbCheckLine vbCheckLines)) "
        "close(vbCheckPort) "
        "vbCheckLines = reverse(vbCheckLines))) "
        "hiFlushLogFile() "
        "vbSaveStart = fileLength(vbLogPath)) "
        "vbSaved = dbSave(vbCv) "
        "when(vbLogAvailable "
        "hiFlushLogFile() "
        "vbSaveEnd = fileLength(vbLogPath) "
        "when(vbSaveEnd >= vbSaveStart "
        "vbSavePort = infile(vbLogPath) "
        "when(vbSavePort "
        "fileSeek(vbSavePort vbSaveStart 0) "
        "while(fileTell(vbSavePort) < vbSaveEnd && gets(vbSaveLine vbSavePort) "
        "vbSaveLines = cons(vbSaveLine vbSaveLines)) "
        "close(vbSavePort) "
        "vbSaveLines = reverse(vbSaveLines)))) "
        'list("vbCheckSave" vbErrorCount vbWarningCount '
        "if(vbSaved t nil) if(vbLogAvailable t nil) "
        "vbCheckLines vbSaveLines)) "
        "when(vbCv dbClose(vbCv))) "
        "vbResult)"
    )


def check_and_save_schematic(
    client: Any,
    lib: str,
    cell: str,
    *,
    view: str = "schematic",
    timeout: int = 60,
    capture_screenshot: bool = False,
    screenshot_output: str | Path | None = None,
) -> SchematicCheckSaveResult:
    """Check and save one schematic, returning only current-run diagnostics.

    The error/warning counts returned by ``schCheck`` remain authoritative.
    Message text is classified from the incremental session-log slices.  If a
    modal blocks the CIW, X11 inspection is read-only and reports the blocking
    window without dismissing it.
    """

    response = client.execute_skill(
        schematic_check_save_diagnostics_skill(lib, cell, view=view),
        timeout=timeout,
    )
    errors, response_status, output = response_fields(response)
    status_value = getattr(response_status, "value", response_status)
    failed = bool(errors) or (
        status_value is not None
        and str(status_value).lower() not in {"success", "ok"}
    )
    if failed:
        return _failed_transport_result(
            client,
            lib,
            cell,
            view,
            errors or [output or f"status={status_value}"],
            output=output,
            timeout=timeout,
            capture_screenshot=capture_screenshot,
            screenshot_output=screenshot_output,
        )

    try:
        error_count, warning_count, saved, log_available, check_lines, save_lines = (
            _parse_check_save_output(output)
        )
    except (TypeError, ValueError) as exc:
        return SchematicCheckSaveResult(
            status="protocol_error",
            lib=lib,
            cell=cell,
            view=view,
            checked=False,
            saved=False,
            check_error_count=0,
            check_warning_count=0,
            log_capture_available=False,
            diagnostics=(
                SchematicDiagnostic(
                    severity="error",
                    source="bridge",
                    message=f"invalid check/save response: {exc}",
                ),
            ),
            raw_output=output,
        )

    diagnostics = list(_classify_log_lines(check_lines, source="schCheck"))
    diagnostics.extend(_classify_log_lines(save_lines, source="dbSave"))
    if error_count and not any(
        item.severity == "error" and item.source == "schCheck"
        for item in diagnostics
    ):
        diagnostics.append(
            SchematicDiagnostic(
                severity="error",
                source="schCheck",
                message=(
                    f"schCheck reported {error_count} error(s); per-message text "
                    "was not present in the operation log slice"
                ),
            )
        )
    if warning_count and not any(
        item.severity == "warning" and item.source == "schCheck"
        for item in diagnostics
    ):
        diagnostics.append(
            SchematicDiagnostic(
                severity="warning",
                source="schCheck",
                message=(
                    f"schCheck reported {warning_count} warning(s); per-message text "
                    "was not present in the operation log slice"
                ),
            )
        )
    if not saved and not any(item.source == "dbSave" for item in diagnostics):
        diagnostics.append(
            SchematicDiagnostic(
                severity="error",
                source="dbSave",
                message="dbSave returned nil",
            )
        )
    if not log_available:
        diagnostics.append(
            SchematicDiagnostic(
                severity="warning",
                source="bridge",
                message=(
                    "operation-scoped CIW log capture is unavailable on this "
                    "Virtuoso release; schCheck counts remain authoritative"
                ),
            )
        )

    if not saved:
        result_status: CheckSaveStatus = "save_failed"
    elif error_count:
        result_status = "check_failed"
    else:
        result_status = "saved"

    screenshot = None
    if capture_screenshot and (error_count or warning_count or not saved):
        screenshot, screenshot_diagnostic = _capture_ciw_screenshot(
            client,
            timeout=timeout,
            output=screenshot_output,
        )
        if screenshot_diagnostic is not None:
            diagnostics.append(screenshot_diagnostic)

    return SchematicCheckSaveResult(
        status=result_status,
        lib=lib,
        cell=cell,
        view=view,
        checked=True,
        saved=saved,
        check_error_count=error_count,
        check_warning_count=warning_count,
        log_capture_available=log_available,
        diagnostics=tuple(diagnostics),
        check_log_lines=check_lines,
        save_log_lines=save_lines,
        screenshot=screenshot,
        raw_output=output,
    )


def _parse_check_save_output(
    output: str,
) -> tuple[int, int, bool, bool, tuple[str, ...], tuple[str, ...]]:
    text = (output or "").strip()
    if not is_single_complete_skill_list(text):
        raise ValueError("expected one complete SKILL list")
    parsed = parse_sexpr(text)
    if not isinstance(parsed, list) or len(parsed) != 7 or parsed[0] != "vbCheckSave":
        raise ValueError(f"unexpected payload: {parsed!r}")
    try:
        error_count = int(parsed[1])
        warning_count = int(parsed[2])
    except (TypeError, ValueError) as exc:
        raise ValueError("schCheck counts are not integers") from exc
    if error_count < 0 or warning_count < 0:
        raise ValueError("schCheck counts must be non-negative")
    saved = parsed[3] is True
    log_available = parsed[4] is True
    check_lines = _string_tuple(parsed[5], label="check log")
    save_lines = _string_tuple(parsed[6], label="save log")
    return (
        error_count,
        warning_count,
        saved,
        log_available,
        check_lines,
        save_lines,
    )


def _string_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return tuple(item.rstrip("\r\n") for item in value)


def _classify_log_lines(
    lines: tuple[str, ...],
    *,
    source: str,
) -> tuple[SchematicDiagnostic, ...]:
    diagnostics: list[SchematicDiagnostic] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        severity_match = _SEVERITY_RE.search(line)
        code_match = _CODE_RE.search(line)
        if severity_match is None and code_match is None:
            continue
        severity_text = severity_match.group(1).lower() if severity_match else "info"
        severity: DiagnosticSeverity
        if severity_text == "error":
            severity = "error"
        elif severity_text in {"warning", "warn"}:
            severity = "warning"
        else:
            severity = "info"
        diagnostics.append(
            SchematicDiagnostic(
                severity=severity,
                source=source,
                message=line,
                code=code_match.group(1).upper() if code_match else None,
            )
        )
    return tuple(diagnostics)


def _failed_transport_result(
    client: Any,
    lib: str,
    cell: str,
    view: str,
    errors: list[str],
    *,
    output: str,
    timeout: int,
    capture_screenshot: bool,
    screenshot_output: str | Path | None,
) -> SchematicCheckSaveResult:
    timed_out = any(
        "timeout" in message.lower() or "timed out" in message.lower()
        for message in errors
    )
    windows: tuple[dict[str, Any], ...] = ()
    diagnostics = [
        SchematicDiagnostic(severity="error", source="transport", message=message)
        for message in errors
    ]
    result_status: CheckSaveStatus = "operation_error"
    if timed_out:
        discovered = _discover_x11_windows(client)
        windows = tuple(
            item
            for item in discovered
            if item.get("kind") in {"known_modal", "dialog_candidate"}
        )
        discovery_errors = [item.get("error") for item in discovered if item.get("error")]
        for message in discovery_errors:
            diagnostics.append(
                SchematicDiagnostic(
                    severity="warning",
                    source="x11",
                    message=str(message),
                )
            )
        if windows:
            result_status = "blocked"
            for window in windows:
                title = str(window.get("title") or "untitled modal window")
                diagnostics.append(
                    SchematicDiagnostic(
                        severity="error",
                        source="x11",
                        message=f"blocking Virtuoso window: {title}",
                    )
                )
        else:
            result_status = "timeout"

    screenshot = None
    if capture_screenshot:
        if timed_out:
            diagnostics.append(
                SchematicDiagnostic(
                    severity="warning",
                    source="screenshot",
                    message=(
                        "CIW screenshot was not attempted because the SKILL "
                        "channel is unresponsive; modal window metadata is "
                        "reported through X11 instead"
                    ),
                )
            )
        else:
            screenshot, screenshot_diagnostic = _capture_ciw_screenshot(
                client,
                timeout=timeout,
                output=screenshot_output,
            )
            if screenshot_diagnostic is not None:
                diagnostics.append(screenshot_diagnostic)

    return SchematicCheckSaveResult(
        status=result_status,
        lib=lib,
        cell=cell,
        view=view,
        checked=False,
        saved=False,
        check_error_count=0,
        check_warning_count=0,
        log_capture_available=False,
        diagnostics=tuple(diagnostics),
        modal_windows=windows,
        screenshot=screenshot,
        raw_output=output,
    )


def _discover_x11_windows(client: Any) -> list[dict[str, Any]]:
    try:
        from virtuoso_bridge.virtuoso import x11

        tunnel = getattr(client, "_tunnel", None)
        runner = getattr(tunnel, "gui_runner", None) if tunnel is not None else None
        if runner is None:
            runner = getattr(client, "ssh_runner", None)
        user = (
            getattr(runner, "user", None)
            or os.getenv("VB_GUI_USER")
            or os.getenv("VB_REMOTE_USER")
            or os.getenv("USER")
            or os.getenv("USERNAME")
            or "local"
        )
        profile = getattr(tunnel, "_profile", None) if tunnel is not None else None
        return x11.list_windows(
            runner,
            user,
            profile=profile,
            top_level=True,
        )
    except Exception as exc:  # best-effort out-of-band diagnostics
        return [{"error": f"X11 modal discovery failed: {exc}"}]


def _capture_ciw_screenshot(
    client: Any,
    *,
    timeout: int,
    output: str | Path | None,
) -> tuple[Path | None, SchematicDiagnostic | None]:
    try:
        response = client.screenshot(
            output=output,
            target="ciw",
            timeout=min(timeout, 30),
        )
    except Exception as exc:
        return None, SchematicDiagnostic(
            severity="warning",
            source="screenshot",
            message=f"CIW screenshot failed: {exc}",
        )
    errors, response_status, response_output = response_fields(response)
    status_value = getattr(response_status, "value", response_status)
    if errors or (
        status_value is not None
        and str(status_value).lower() not in {"success", "ok"}
    ):
        detail = errors[0] if errors else response_output or f"status={status_value}"
        return None, SchematicDiagnostic(
            severity="warning",
            source="screenshot",
            message=f"CIW screenshot failed: {detail}",
        )
    if not response_output.strip():
        return None, SchematicDiagnostic(
            severity="warning",
            source="screenshot",
            message="CIW screenshot returned no local path",
        )
    return Path(response_output), None


__all__ = [
    "CheckSaveStatus",
    "DiagnosticSeverity",
    "SchematicCheckSaveResult",
    "SchematicDiagnostic",
    "check_and_save_schematic",
    "schematic_check_save_diagnostics_skill",
]
