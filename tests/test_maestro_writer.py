from __future__ import annotations

from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import create_netlist_for_corner
from virtuoso_bridge.virtuoso.maestro import MaestroOps


class _RecordingClient:
    def __init__(self) -> None:
        self.expressions: list[str] = []

    def execute_skill(self, expression: str, **_kwargs):
        self.expressions.append(expression)
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
