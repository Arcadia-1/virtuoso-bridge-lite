from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import (
    add_output,
    create_netlist_for_corner,
    run_and_wait,
    run_simulation,
)
from virtuoso_bridge.virtuoso.maestro import MaestroOps
from virtuoso_bridge.virtuoso.maestro import writer


class _RecordingClient:
    def __init__(self) -> None:
        self.expressions: list[str] = []
        self.skill_kwargs: list[dict] = []
        self.ssh_runner = None

    def execute_skill(self, expression: str, **kwargs):
        self.expressions.append(expression)
        self.skill_kwargs.append(kwargs)
        return SimpleNamespace(errors=[], output="t")


def test_create_netlist_for_corner_uses_current_session_by_default() -> None:
    client = _RecordingClient()

    result = create_netlist_for_corner(
        client,
        "tran_test",
        "tt",
        "/tmp/tran_tt",
    )

    assert result == "t"
    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt")'
    ]


def test_add_output_escapes_all_skill_string_parameters() -> None:
    client = _RecordingClient()

    add_output(
        client,
        'Pin"\\HB',
        'hb"\\1tone',
        output_type='point"\\type',
        signal_name='VIN"\\PLUS',
        expr='harmonic(pvi(\'hb, "\\/IN_PORT", 0, "/VIN/PLUS", 0), 1)',
        session='session"\\3',
    )

    assert client.expressions == [
        'maeAddOutput("Pin\\"\\\\HB" "hb\\"\\\\1tone" '
        '?outputType "point\\"\\\\type" ?signalName "VIN\\"\\\\PLUS" '
        '?expr "harmonic(pvi(\'hb, \\"\\\\/IN_PORT\\", 0, \\"/VIN/PLUS\\", 0), 1)" '
        '?session "session\\"\\\\3")'
    ]


def test_run_simulation_forwards_timeout_to_skill_request() -> None:
    client = _RecordingClient()

    run_simulation(client, session='session"\\3', callback='done"\\callback', timeout=90)

    assert client.expressions == [
        'maeRunSimulation(?session "session\\"\\\\3" ?callback "done\\"\\\\callback")'
    ]
    assert client.skill_kwargs == [{"timeout": 90}]


def test_run_and_wait_uses_one_timeout_budget(monkeypatch) -> None:
    client = _RecordingClient()
    clock = [100.0]
    start_timeouts: list[float] = []
    wait_timeouts: list[float] = []

    def fake_run_simulation(*args, **kwargs):
        start_timeouts.append(kwargs["timeout"])
        clock[0] += 12
        return '"Interactive.1"'

    def fake_wait(*args, **kwargs):
        wait_timeouts.append(kwargs["timeout"])
        return "done"

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(writer, "_wait_until_done", fake_wait)
    monkeypatch.setattr(writer.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeef"))

    history, status = run_and_wait(client, timeout=60)

    assert (history, status) == ('"Interactive.1"', "done")
    assert start_timeouts == [60]
    assert wait_timeouts == [48]


def test_create_netlist_for_corner_passes_explicit_session() -> None:
    client = _RecordingClient()

    create_netlist_for_corner(
        client,
        "tran_test",
        "tt",
        "/tmp/tran_tt",
        session="session3",
    )

    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt" '
        '?session "session3")'
    ]


def test_create_netlist_for_corner_session_is_keyword_only() -> None:
    client = _RecordingClient()

    with pytest.raises(TypeError):
        create_netlist_for_corner(
            client,
            "tran_test",
            "tt",
            "/tmp/tran_tt",
            "session3",
        )


def test_maestro_ops_passes_explicit_session_to_corner_netlist_export() -> None:
    client = _RecordingClient()

    MaestroOps(client).create_netlist_for_corner(
        "tran_test",
        "tt",
        "/tmp/tran_tt",
        session="session3",
    )

    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt" '
        '?session "session3")'
    ]
