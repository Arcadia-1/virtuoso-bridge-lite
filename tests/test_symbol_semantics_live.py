from __future__ import annotations

import os
import uuid

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.env import set_runtime_env_file
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.symbol import (
    symbol_create_instance_label,
    symbol_create_logical_label,
    symbol_create_pin,
    symbol_create_rect,
    symbol_create_selection_box,
    symbol_set_term_order,
)


pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_LIVE_TESTS") != "1",
    reason="set VB_RUN_LIVE_TESTS=1 to run against a live Virtuoso session",
)


def _semantic_labels(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(label["text"]): label
        for label in labels
        if label.get("text") in {"IN", "OUT", "[@instanceName]", "[@partName]"}
    }


def test_manual_symbol_semantics_round_trip_in_virtuoso() -> None:
    env_file = os.getenv("VB_TEST_ENV_FILE")
    if env_file:
        set_runtime_env_file(env_file)
    lib = os.getenv("VB_TEST_LIB")
    if not lib:
        pytest.fail("VB_TEST_LIB is required for live symbol tests")

    cell = f"VB_SYMBOL_SEMANTICS_{uuid.uuid4().hex[:10]}"
    client = VirtuosoClient.from_env(timeout=60)

    try:
        with client.symbol.edit(lib, cell, mode="w", timeout=60) as symbol:
            symbol.add(symbol_create_rect("device", "drawing", -0.5, -0.5, 0.5, 0.5))
            symbol.add(
                symbol_create_pin(
                    "IN",
                    -1.0,
                    0.0,
                    direction="input",
                    label_x=-0.45,
                    label_y=0.0,
                )
            )
            symbol.add(
                symbol_create_pin(
                    "OUT",
                    1.0,
                    0.0,
                    direction="output",
                    label_x=0.45,
                    label_y=0.0,
                    label_justification="centerRight",
                )
            )
            symbol.add(symbol_create_instance_label(0.0, 0.75))
            symbol.add(symbol_create_logical_label(0.0, -0.75))
            symbol.add(symbol_create_selection_box(-1.0, -0.5, 1.0, 0.5))
            symbol.add(symbol_set_term_order(["IN", "OUT"]))

        ports = client.symbol.read_ports(lib, cell, timeout=60)
        labels = _semantic_labels(ports["labels"])

        assert set(labels) == {"IN", "OUT", "[@instanceName]", "[@partName]"}
        assert labels["IN"]["labelType"] == "normalLabel"
        assert labels["IN"]["layerName"] == "pin"
        assert labels["IN"]["purpose"] == "label"
        assert labels["OUT"]["labelType"] == "normalLabel"
        assert labels["OUT"]["layerName"] == "pin"
        assert labels["OUT"]["purpose"] == "label"
        assert labels["[@instanceName]"]["labelType"] == "NLPLabel"
        assert labels["[@instanceName]"]["layerName"] == "instance"
        assert labels["[@instanceName]"]["purpose"] == "label"
        assert labels["[@partName]"]["labelType"] == "NLPLabel"
        assert labels["[@partName]"]["layerName"] == "device"
        assert labels["[@partName]"]["purpose"] == "label"
        assert ports["selectionBoxes"] == [[[-1.0, -0.5], [1.0, 0.5]]]
        assert ports["termOrder"] == ["IN", "OUT"]
        assert {term["name"] for term in ports["terms"]} == {"IN", "OUT"}
    finally:
        cleanup = client.execute_skill(
            f'let((openCv obj) '
            f'openCv = dbFindOpenCellViewByName("{lib}" "{cell}" "symbol") '
            'when(openCv unless(dbClose(openCv) error("live test close failed"))) '
            f'obj = ddGetObj("{lib}" "{cell}" "symbol") '
            'if(obj then ddDeleteObj(obj) else t))',
            timeout=60,
        )
        assert cleanup.status == ExecutionStatus.SUCCESS
        assert (cleanup.output or "").strip() == "t"
