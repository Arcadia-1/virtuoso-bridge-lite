from __future__ import annotations

from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import (
    add_output,
    create_netlist_for_corner,
    run_simulation,
)
from virtuoso_bridge.virtuoso.maestro import MaestroOps


class _RecordingClient:
    def __init__(self) -> None:
        self.expressions: list[str] = []
        self.skill_kwargs: list[dict] = []

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


def test_add_output_escapes_calculator_expression_quotes() -> None:
    client = _RecordingClient()

    add_output(
        client,
        "Pin_HB_W",
        "hb_1tone",
        output_type="point",
        expr='harmonic(pvi(\'hb, "/IN_PORT", 0, "/VIN/PLUS", 0), 1)',
        session="session3",
    )

    assert client.expressions == [
        'maeAddOutput("Pin_HB_W" "hb_1tone" ?outputType "point" '
        '?expr "harmonic(pvi(\'hb, \\"/IN_PORT\\", 0, \\"/VIN/PLUS\\", 0), 1)" '
        '?session "session3")'
    ]


def test_run_simulation_forwards_timeout_to_skill_request() -> None:
    client = _RecordingClient()

    run_simulation(client, session="session3", callback="done", timeout=90)

    assert client.expressions == [
        'maeRunSimulation(?session "session3" ?callback "done")'
    ]
    assert client.skill_kwargs == [{"timeout": 90}]


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
