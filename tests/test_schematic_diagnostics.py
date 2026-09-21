from __future__ import annotations

from pathlib import Path

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.schematic import (
    check_and_save_schematic,
    schematic_check_save_diagnostics_skill,
)
from virtuoso_bridge.virtuoso.schematic import diagnostics as diagnostics_module


class _Client:
    def __init__(self, response: VirtuosoResult) -> None:
        self.response = response
        self.skill = ""
        self.screenshot_calls: list[dict[str, object]] = []

    def execute_skill(self, skill: str, timeout: int = 60) -> VirtuosoResult:
        self.skill = skill
        return self.response

    def screenshot(self, **kwargs) -> VirtuosoResult:
        self.screenshot_calls.append(kwargs)
        return VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output="/tmp/check-save.png",
        )


def _success(output: str) -> VirtuosoResult:
    return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)


def test_check_save_skill_uses_explicit_cellview_and_incremental_log_slices() -> None:
    skill = schematic_check_save_diagnostics_skill(
        'my"lib',
        "amp",
        view="schematic",
    )

    assert 'ddGetObj("my\\\"lib" "amp" "schematic")' in skill
    assert skill.index("ddGetObj(") < skill.index("dbOpenCellViewByType(")
    assert 'dbOpenCellViewByType("my\\\"lib" "amp" "schematic"' in skill
    assert "hiGetLogFileName()" in skill
    assert "hiFlushLogFile()" in skill
    assert "vbCheckStart = fileLength(vbLogPath)" in skill
    assert "fileSeek(vbCheckPort vbCheckStart 0)" in skill
    assert skill.index("schCheck(vbCv)") < skill.index("dbSave(vbCv)")
    assert "unwindProtect" in skill
    assert "dbClose(vbCv)" in skill


def test_check_save_returns_codes_counts_and_current_operation_lines() -> None:
    client = _Client(
        _success(
            '("vbCheckSave" 1 1 t t '
            '("ERROR (SCH-1001): dangling wire\\n" "ordinary chatter\\n") '
            '("WARNING (DB-270000): save note\\n"))'
        )
    )

    result = check_and_save_schematic(client, "LIB", "AMP")

    assert result.status == "check_failed"
    assert result.checked
    assert result.saved
    assert result.check_error_count == 1
    assert result.check_warning_count == 1
    assert result.log_capture_available
    assert result.check_log_lines == (
        "ERROR (SCH-1001): dangling wire",
        "ordinary chatter",
    )
    assert [(item.severity, item.source, item.code) for item in result.diagnostics] == [
        ("error", "schCheck", "SCH-1001"),
        ("warning", "dbSave", "DB-270000"),
        ("warning", "schCheck", None),
    ]


def test_check_save_keeps_authoritative_count_when_log_text_is_missing() -> None:
    client = _Client(_success('("vbCheckSave" 2 0 t nil nil nil)'))

    result = check_and_save_schematic(client, "LIB", "AMP")

    assert result.status == "check_failed"
    assert result.check_error_count == 2
    assert not result.log_capture_available
    assert any("reported 2 error" in item.message for item in result.errors)
    assert any("log capture is unavailable" in item.message for item in result.warnings)


def test_check_save_reports_db_save_nil() -> None:
    client = _Client(_success('("vbCheckSave" 0 0 nil t nil nil)'))

    result = check_and_save_schematic(client, "LIB", "AMP")

    assert result.status == "save_failed"
    assert result.checked
    assert not result.saved
    assert any(item.source == "dbSave" for item in result.errors)


def test_check_save_reports_blocking_modal_after_timeout(monkeypatch) -> None:
    client = _Client(
        VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["Socket timeout after 5s"],
        )
    )
    monkeypatch.setattr(
        diagnostics_module,
        "_discover_x11_windows",
        lambda _client: [
            {
                "kind": "known_modal",
                "window_id": "0x42",
                "title": "Schematic Check Error",
            }
        ],
    )

    result = check_and_save_schematic(
        client,
        "LIB",
        "AMP",
        timeout=5,
        capture_screenshot=True,
    )

    assert result.status == "blocked"
    assert result.modal_windows[0]["window_id"] == "0x42"
    assert any("Schematic Check Error" in item.message for item in result.errors)
    assert any("not attempted" in item.message for item in result.warnings)
    assert client.screenshot_calls == []


def test_check_save_recognizes_timed_out_transport_wording(monkeypatch) -> None:
    client = _Client(
        VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["Operation timed out after 5s"],
        )
    )
    monkeypatch.setattr(
        diagnostics_module,
        "_discover_x11_windows",
        lambda _client: [],
    )

    result = check_and_save_schematic(client, "LIB", "AMP", timeout=5)

    assert result.status == "timeout"


def test_check_save_can_capture_ciw_screenshot_for_nonblocking_diagnostics() -> None:
    client = _Client(
        _success(
            '("vbCheckSave" 0 1 t t '
            '("WARNING (SCH-42): inherited warning\\n") nil)'
        )
    )

    result = check_and_save_schematic(
        client,
        "LIB",
        "AMP",
        capture_screenshot=True,
        screenshot_output=Path("evidence.png"),
    )

    assert result.status == "saved"
    assert result.screenshot == Path("/tmp/check-save.png")
    assert client.screenshot_calls == [
        {"output": Path("evidence.png"), "target": "ciw", "timeout": 30}
    ]


def test_check_save_rejects_malformed_protocol_output() -> None:
    client = _Client(_success("t"))

    result = check_and_save_schematic(client, "LIB", "AMP")

    assert result.status == "protocol_error"
    assert not result.checked
    assert "complete SKILL list" in result.errors[0].message
