"""Tests for ``virtuoso_bridge.virtuoso.snapshot`` (issue #345)."""

from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.snapshot import (
    _focused_window_title,
    classify_window,
    snapshot,
)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Virtuoso® 6.1.8-64b - Log: /home/u/CDS.log", "ciw"),
        ("Virtuoso 6.1.8-64b - Log: /tmp/virtuoso.log", "ciw"),
        ("Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log", "ciw"),
        ("Virtuoso® Studio IC23.1-64b - Log: /home/u/CDS.log", "ciw"),
        ("Virtuoso® Studio IC23.1 ISR10 - Log: /tmp/CDS.log", "ciw"),
        ("Virtuoso® ICADVM20.1-64b - Log: /tmp/CDS.log", "ciw"),
        ("Virtuoso® Studio ICADVM20.1-64b - Log: /tmp/CDS.log", "ciw"),
        ("Virtuoso® ADE Assembler Editing: SAR_ADC _tb maestro", "maestro"),
        ("Virtuoso® ADE Explorer Reading: LIB cell schematic", "schematic"),
        ("Virtuoso® Schematic Editor L Editing: LIB cell schematic", "schematic"),
        ("Virtuoso® Layout Suite XL Editing: LIB cell layout", "layout"),
        ("Virtuoso® Visualization & Analysis XL", "waveform"),
        ("Cadence Hierarchy Editor: LIB cell config", "hierarchy"),
        ("", "unknown"),
        ("Unrelated Desktop Window", "unknown"),
    ],
)
def test_classify_window_handles_ic6_ic23_studio_and_icadvm_titles(title, expected) -> None:
    assert classify_window(title) == expected


def test_focused_window_title_decodes_octal_escapes_and_handles_nil() -> None:
    class _Client:
        def __init__(self, result: VirtuosoResult) -> None:
            self._result = result

        def execute_skill(self, expr: str):
            return self._result

    # SKILL %L encodes ® as octal \256.
    ciw_client = _Client(
        VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=r'"Virtuoso\256 Studio IC23.1 - Log: /tmp/CDS.log"',
        )
    )
    title = _focused_window_title(ciw_client)
    assert title == "Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log"
    assert classify_window(title) == "ciw"

    nil_client = _Client(VirtuosoResult(status=ExecutionStatus.SUCCESS, output="nil"))
    assert _focused_window_title(nil_client) == ""

    empty_client = _Client(VirtuosoResult(status=ExecutionStatus.SUCCESS, output='""'))
    assert _focused_window_title(empty_client) == ""


@pytest.mark.parametrize(
    "result, match",
    [
        (
            VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Socket timeout after 30s"]),
            "Socket timeout after 30s",
        ),
        (
            VirtuosoResult(status=ExecutionStatus.FAILURE, errors=[]),
            "status=failure",
        ),
        (
            VirtuosoResult(status=ExecutionStatus.SUCCESS, output='""', errors=["bridge error"]),
            "bridge error",
        ),
    ],
)
def test_focused_window_title_and_snapshot_raise_on_bridge_error(result, match) -> None:
    class _FailingClient:
        def execute_skill(self, expr: str):
            return result

    with pytest.raises(RuntimeError, match=match):
        _focused_window_title(_FailingClient())

    with pytest.raises(RuntimeError, match=match):
        snapshot(_FailingClient())


def test_snapshot_classifies_ic23_ciw_window() -> None:
    class _CiwClient:
        def execute_skill(self, expr: str):
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='"Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log"',
            )

    result = snapshot(_CiwClient())
    assert result == {
        "kind": "ciw",
        "window_title": "Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log",
        "supported": False,
        "data": None,
    }
