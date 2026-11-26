"""Open ViVA/AWV waveform windows for Maestro histories."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from virtuoso_bridge.virtuoso.ops import escape_skill_string


def _skill_string_list(values: Iterable[str]) -> str:
    return "list(" + " ".join(f'"{escape_skill_string(value)}"' for value in values) + ")"


def _skill_window_ref(window: int | str) -> str:
    if isinstance(window, bool):
        raise ValueError("window must be a window number or window:<number>")
    if isinstance(window, int):
        if window <= 0:
            raise ValueError("window must be a positive window number")
        return f"window({window})"
    text = str(window).strip()
    if text.isdigit():
        if int(text) <= 0:
            raise ValueError("window must be a positive window number")
        return f"window({text})"
    match = re.fullmatch(r"(?:window:(\d+)|window\((\d+)\))", text)
    if match:
        window_num = match.group(1) or match.group(2)
        if int(window_num) <= 0:
            raise ValueError("window must be a positive window number")
        return f"window({window_num})"
    raise ValueError("window must be a window number or window:<number>")


def maestro_open_waveform_viewer_skill(
    lib: str,
    cell: str,
    history: str,
    *,
    signals: list[str] | tuple[str, ...],
    view: str = "maestro",
    application: str = "Assembler",
    test: str | None = None,
    result: str = "tran",
    results_dir: str | Path | None = None,
) -> str:
    """Build SKILL to open a ViVA/AWV plot window for explicit signals.

    The Maestro results session is intentionally left open because AWV plot
    windows keep references to the opened result database.

    Args:
        test: Maestro test name used for maeGetOutputValue fallback.
        result: Spectre result name, e.g. "tran".
        results_dir: Optional raw PSF directory. When provided, openResults
            must succeed; otherwise the generated SKILL errors instead of
            falling back to another active results context.

    TODO: measurement waveform setup is intentionally not implemented here.
    TODO: template plot restore/apply support is intentionally not implemented here.
    """
    if not signals:
        raise ValueError("signals must not be empty")

    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_history = escape_skill_string(history)
    escaped_view = escape_skill_string(view)
    escaped_application = escape_skill_string(application)
    escaped_test = escape_skill_string(test or "")
    escaped_result = escape_skill_string(result)
    signal_expr = _skill_string_list(signals)
    if results_dir is None:
        results_dir_expr = "nil"
        raw_open_expr = "nil"
    else:
        escaped_results_dir = escape_skill_string(str(results_dir))
        results_dir_expr = f'"{escaped_results_dir}"'
        raw_open_expr = f'car(errset(openResults("{escaped_results_dir}") nil))'

    signal_blocks: list[str] = []
    for signal in signals:
        escaped_signal = escape_skill_string(signal)
        output_value_expr = (
            f'errset(maeGetOutputValue("{escaped_signal}" "{escaped_test}") nil)'
            if test
            else f'errset(maeGetOutputValue("{escaped_signal}" vbTestName) nil)'
        )
        signal_blocks.append(
            "vbWaveform = nil "
            "vbWaveResult = if(vbRawResultsOpen "
            f'then errset(v("{escaped_signal}" ?result "{escaped_result}" ?resultsDir vbResultsDir) nil) '
            f'else errset(v("{escaped_signal}" ?result "{escaped_result}") nil)) '
            "vbWaveform = if(vbWaveResult then car(vbWaveResult) else nil) "
            "unless(vbWaveform "
            "when(vbTestName == \"\" "
            "vbTestNamesResult = errset(maeGetResultTests() nil) "
            "vbTestNames = if(vbTestNamesResult then car(vbTestNamesResult) else nil) "
            "when(vbTestNames vbTestName = car(vbTestNames))) "
            "when(vbTestName != \"\" "
            f"vbOutputResult = {output_value_expr} "
            "vbWaveform = if(vbOutputResult then car(vbOutputResult) else nil))) "
            f'unless(vbWaveform error("missing waveform: {escaped_signal}")) '
            "vbWaveforms = append(vbWaveforms list(vbWaveform)) "
        )

    return (
        "let((vbSession vbResultsOpenResult vbResultsOpen vbResultsDir vbRawResultsOpen "
        "vbWaveforms vbWaveform vbWaveResult vbTestName vbTestNamesResult vbTestNames "
        "vbOutputResult vbWindowResult vbWindowId vbPlotResult vbOpenResult vbOpenOk) "
        "vbOpenOk = nil "
        "vbOpenResult = errset(progn("
        "unless(and(isCallable('maeOpenSetup) isCallable('maeOpenResults) "
        "isCallable('maeGetResultTests) isCallable('maeGetOutputValue) "
        "isCallable('openResults) isCallable('awvCreatePlotWindow) "
        "isCallable('awvPlotWaveform) isCallable('v) "
        "isCallable('hiCloseWindow) isCallable('maeCloseSession)) "
        'error("waveform viewer API unavailable")) '
        f'vbSession = maeOpenSetup("{escaped_lib}" "{escaped_cell}" "{escaped_view}" '
        f'?application "{escaped_application}" ?mode "r") '
        'unless(vbSession error("open maestro failed")) '
        f'vbResultsOpenResult = errset(maeOpenResults(?session vbSession ?history "{escaped_history}") nil) '
        "vbResultsOpen = if(vbResultsOpenResult then car(vbResultsOpenResult) else nil) "
        'unless(vbResultsOpen error("open results failed")) '
        f"vbResultsDir = {results_dir_expr} "
        f"vbRawResultsOpen = {raw_open_expr} "
        'when(vbResultsDir && !vbRawResultsOpen error("open raw results failed")) '
        f'vbTestName = "{escaped_test}" '
        "vbWaveforms = nil "
        f"{''.join(signal_blocks)}"
        "vbWindowResult = errset(awvCreatePlotWindow() nil) "
        "vbWindowId = if(vbWindowResult then car(vbWindowResult) else nil) "
        'unless(vbWindowId error("create waveform window failed")) '
        f"vbPlotResult = errset(awvPlotWaveform(vbWindowId vbWaveforms ?expr {signal_expr}) nil) "
        'unless(vbPlotResult && car(vbPlotResult) error("plot waveform failed")) '
        "vbOpenOk = t "
        f'list("opened" "{escaped_lib}" "{escaped_cell}" "{escaped_view}" "{escaped_history}" vbSession vbWindowId)) nil) '
        "unless(vbOpenOk "
        "when(vbWindowId errset(hiCloseWindow(vbWindowId) nil)) "
        "when(vbSession errset(maeCloseSession(?session vbSession ?forceClose t) nil))) "
        'unless(vbOpenResult error("open waveform viewer failed")) '
        "car(vbOpenResult))"
    )


def maestro_close_waveform_viewer_skill(
    *,
    window: int | str | None = None,
    session: str | None = None,
) -> str:
    """Build SKILL to close a waveform window and its retained Maestro session."""
    if window is None and session is None:
        raise ValueError("window or session must be provided")
    if session is not None:
        session = session.strip()
        if not session:
            raise ValueError("session must not be blank")

    window_expr = _skill_window_ref(window) if window is not None else "nil"
    session_expr = f'"{escape_skill_string(session)}"' if session else "nil"
    return (
        "let((vbWindow vbSession vbWindowCloseResult vbSessionCloseResult "
        "vbWindowsResult vbWindowsAfter vbSessionsResult vbSessionsAfter) "
        f"vbWindow = {window_expr} "
        f"vbSession = {session_expr} "
        "when(vbWindow "
        "vbWindowCloseResult = errset(hiCloseWindow(vbWindow) nil) "
        'unless(vbWindowCloseResult error("close waveform window failed")) '
        "vbWindowsResult = errset(hiGetWindowList() nil) "
        'unless(vbWindowsResult error("check waveform window close failed")) '
        "vbWindowsAfter = car(vbWindowsResult) "
        'when(member(vbWindow vbWindowsAfter) error("close waveform window failed"))) '
        "when(vbSession "
        "vbSessionCloseResult = errset(maeCloseSession(?session vbSession ?forceClose t) nil) "
        'unless(vbSessionCloseResult error("close waveform session failed")) '
        "vbSessionsResult = errset(maeGetSessions() nil) "
        'unless(vbSessionsResult error("check waveform session close failed")) '
        "vbSessionsAfter = car(vbSessionsResult) "
        'when(member(vbSession vbSessionsAfter) error("close waveform session failed"))) '
        'list("closed" vbSession vbWindow))'
    )


def open_waveform_viewer(
    client: Any,
    lib: str,
    cell: str,
    history: str,
    *,
    signals: list[str] | tuple[str, ...],
    view: str = "maestro",
    application: str = "Assembler",
    test: str | None = None,
    result: str = "tran",
    results_dir: str | Path | None = None,
    timeout: int = 60,
) -> Any:
    """Open a ViVA/AWV plot window by executing generated SKILL.

    Returns the raw VirtuosoResult from client.execute_skill(). The retained
    Maestro session and waveform window handles are encoded in result.output
    as a SKILL list and should be passed to close_waveform_viewer() by callers
    that need deterministic cleanup.
    """
    skill = maestro_open_waveform_viewer_skill(
        lib,
        cell,
        history,
        signals=signals,
        view=view,
        application=application,
        test=test,
        result=result,
        results_dir=results_dir,
    )
    return client.execute_skill(skill, timeout=timeout)


def close_waveform_viewer(
    client: Any,
    *,
    window: int | str | None = None,
    session: str | None = None,
    timeout: int = 30,
) -> Any:
    """Close a waveform window and/or the Maestro session retained for it."""
    skill = maestro_close_waveform_viewer_skill(window=window, session=session)
    return client.execute_skill(skill, timeout=timeout)
