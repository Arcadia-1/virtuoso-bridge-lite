"""Compact Maestro results remove duplication, not measurements."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jsonschema
import pytest

from tests.test_maestro_ic23_results import IC23_CSV
from tests.test_maestro_read_results import NO_VALUE_CSV, SAMPLE_CSV, SINGLE_POINT_CSV
from virtuoso_bridge.virtuoso.maestro.reader.runs import NoResults, _parse_detail_csv, read_results

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compact_mcp", ROOT / "mcp_server.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


class Client:
    def __init__(self, text):
        self.text = text
        self.calls = []
        self.responses = iter(['("TRAN")', "t", "t", '"passed"', "nil"])

    def execute_skill(self, command, timeout=None):
        self.calls.append(command)
        return SimpleNamespace(output=next(self.responses), errors=[])

    def download_file(self, remote, local):
        Path(local).write_text(self.text, encoding="utf-8")


def assert_same_measurements(legacy, compact):
    assert compact == {k: v for k, v in legacy.items() if k != "outputs"}
    reconstructed = [
        {"point": point["point"], "name": name, "value": info["value"],
         "spec_status": info["pass_fail"]}
        for point in compact["points"] for name, info in point["outputs"].items()
    ]
    assert reconstructed == legacy["outputs"]


@pytest.mark.parametrize("text", [SAMPLE_CSV, SINGLE_POINT_CSV, IC23_CSV, NO_VALUE_CSV, ""])
def test_compact_parser_is_lossless_except_for_redundant_flat_list(text):
    legacy = _parse_detail_csv(text, history="Interactive.7")
    compact = _parse_detail_csv(text, history="Interactive.7", compact=True)
    assert_same_measurements(legacy, compact)
    assert _parse_detail_csv(text, history="Interactive.7", compact=False) == legacy


def test_compact_missing_value_column_does_not_turn_specs_into_values():
    result = _parse_detail_csv(NO_VALUE_CSV, history="H", compact=True)
    output = result["points"][0]["outputs"]["t_conv"]
    assert output["value"] == ""
    assert output["spec"] == "< 914p"


def test_compact_uses_upstream_dynamic_column_mapping():
    text = (
        "Point,Pass/Fail,Weight,Output,Test,Spec,Nominal\n"
        "1,failed,0.5,delay,TRAN,< 1n,2.123456789e-9\n"
    )
    compact = _parse_detail_csv(text, history="H", compact=True)
    assert_same_measurements(_parse_detail_csv(text, history="H"), compact)
    assert compact["points"][0]["outputs"]["delay"] == {
        "value": "2.123456789e-9", "spec": "< 1n", "weight": "0.5", "pass_fail": "failed",
    }


@pytest.mark.parametrize("text", [SAMPLE_CSV, SINGLE_POINT_CSV, IC23_CSV])
@pytest.mark.parametrize("include_raw", [False, True])
def test_public_read_results_keeps_all_values_and_does_not_add_calls(text, include_raw):
    before, after = Client(text), Client(text)
    args = dict(lib="L", cell="C", history="Interactive.7", include_raw=include_raw)
    legacy = read_results(before, "s1", **args)
    compact = read_results(after, "s1", compact=True, **args)
    assert_same_measurements(legacy, compact)
    assert len(before.calls) == len(after.calls) == 5
    assert compact["overall_spec"] == "passed"
    if include_raw:
        assert compact["raw_csv"] == text


def test_compact_read_retains_no_results_reason():
    client = SimpleNamespace(execute_skill=lambda *a, **kw: SimpleNamespace(output="nil"))
    result = read_results(client, "s1", lib="L", cell="C", history="H", compact=True)
    assert isinstance(result, NoResults)
    assert "no test" in result.reason


def test_compact_read_sends_nothing_to_a_blocked_ciw():
    client = SimpleNamespace(
        dialogs=SimpleNamespace(probe=Mock(return_value=SimpleNamespace(
            status="blocked", dialogs=[{"title": "ADE blocked"}],
        ))), execute_skill=Mock(),
    )
    result = read_results(client, "s1", lib="L", cell="C", history="H", compact=True)
    assert isinstance(result, NoResults) and "ADE blocked" in result.reason
    client.execute_skill.assert_not_called()


@pytest.mark.parametrize("message", ["Socket timeout", "Socket error", "Connection refused"])
def test_compact_channel_loss_does_not_queue_cleanup_or_followup_reads(message):
    client = SimpleNamespace(
        execute_skill=Mock(side_effect=[
            SimpleNamespace(output='("TRAN")', errors=[]),
            SimpleNamespace(output="", errors=[message]),
        ]), download_file=Mock(),
    )
    result = read_results(client, "s1", lib="L", cell="C", history="H", compact=True)
    assert isinstance(result, NoResults) and message in result.reason
    assert client.execute_skill.call_count == 2
    client.download_file.assert_not_called()
    assert all("deleteFile" not in call.args[0] for call in client.execute_skill.call_args_list)


def test_compact_keeps_tiny_values_unicode_and_failed_specs():
    text = (
        "Point,Test,Output,Nominal,Spec,Weight,Pass/Fail\n"
        '1,TRAN,\u7535\u538b,-9.87654321012345e-15,> 0,0.125,failed\n'
        "1,TRAN,pending,,,,\n"
    )
    legacy = _parse_detail_csv(text, history="H")
    compact = _parse_detail_csv(text, history="H", compact=True)
    assert_same_measurements(legacy, compact)
    assert compact["points"][0]["outputs"]["\u7535\u538b"] == {
        "value": "-9.87654321012345e-15", "spec": "> 0", "weight": "0.125", "pass_fail": "failed",
    }
    assert compact["points"][0]["outputs"]["pending"]["value"] == ""


def test_mcp_compact_keeps_full_structured_data_and_short_text(monkeypatch):
    data = _parse_detail_csv(SAMPLE_CSV, history="H", compact=True)
    data["metadata"] = {"request_id": "keep-me"}
    unchanged = copy.deepcopy(data)
    read = Mock(return_value=data)
    monkeypatch.setattr(mcp, "_client", lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    result = mcp._impl_maestro_read({"session": "s1", "history": "H", "compact": True})
    read.assert_called_once_with("s1", history="H", compact=True)
    assert result["structuredContent"] == {"session": "s1", **unchanged}
    assert data == unchanged
    assert "21.63" not in result["content"][0]["text"]
    assert len(result["content"][0]["text"]) < 300
    assert not result.get("isError")


@pytest.mark.parametrize("compact", [None, False])
def test_mcp_default_result_contract_is_unchanged(monkeypatch, compact):
    data = _parse_detail_csv(SAMPLE_CSV, history="H")
    read = Mock(return_value=data)
    monkeypatch.setattr(mcp, "_client", lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    args = {"session": "s1", "history": "H"}
    if compact is not None:
        args["compact"] = compact
    result = mcp._impl_maestro_read(args)
    read.assert_called_once_with("s1", history="H")
    assert result["structuredContent"] == data
    assert json.loads(result["content"][0]["text"]) == data


def test_mcp_compact_refuses_oversize_instead_of_silently_truncating(monkeypatch):
    data = {"history": "H", "points": [{"point": 1, "outputs": {
        "value": {"value": "x" * mcp.STRUCT_LIMIT}}}]}
    read = Mock(return_value=data)
    monkeypatch.setattr(mcp, "_client", lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    result = mcp._impl_maestro_read({"session": "s1", "history": "H", "compact": True})
    assert result["isError"]
    assert result["structuredContent"]["session"] == "s1"
    assert result["structuredContent"]["history"] == "H"
    assert result["structuredContent"]["reason"] == "result_too_large"
    assert "read_waveform" not in result["content"][0]["text"]
    read.assert_called_once()


def test_mcp_compact_does_not_cut_measurements_at_text_limit(monkeypatch):
    value = "x" * (mcp.TEXT_LIMIT + 1)
    data = {"history": "H", "points": [{"point": 1, "outputs": {"value": {"value": value}}}]}
    read = Mock(return_value=data)
    monkeypatch.setattr(mcp, "_client", lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    result = mcp._impl_maestro_read({"session": "s1", "history": "H", "compact": True})
    assert not result.get("isError")
    assert result["structuredContent"]["points"][0]["outputs"]["value"]["value"] == value


def test_mcp_compact_retains_no_results_reason(monkeypatch):
    read = Mock(return_value=NoResults("CSV export unavailable"))
    monkeypatch.setattr(mcp, "_client", lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    result = mcp._impl_maestro_read({"session": "s1", "history": "H", "compact": True})
    assert result["structuredContent"]["no_results_reason"] == "CSV export unavailable"
    assert result["structuredContent"]["history"] == "H"


def test_mcp_compact_preserves_actual_blocked_ciw_reason(monkeypatch):
    client = SimpleNamespace(
        dialogs=SimpleNamespace(probe=Mock(return_value=SimpleNamespace(
            status="blocked", dialogs=[{"title": "ADE blocked"}],
        ))), execute_skill=Mock(),
    )
    client.maestro = SimpleNamespace(read_results=lambda session, **kwargs: read_results(
        client, session, lib="L", cell="C", **kwargs,
    ))
    monkeypatch.setattr(mcp, "_client", lambda: client)
    result = mcp._impl_maestro_read({"session": "s1", "history": "H", "compact": True})
    assert "ADE blocked" in result["structuredContent"]["no_results_reason"]
    assert result["structuredContent"]["session"] == "s1"
    assert result["structuredContent"]["history"] == "H"
    client.execute_skill.assert_not_called()


def test_compact_option_is_discoverable_and_boolean():
    tool = next(t for t in mcp._tool_defs() if t["name"] == "test_results")
    schema = tool["inputSchema"]
    jsonschema.validate({"session": "s1", "history": "H", "compact": True}, schema)
    assert schema["properties"]["compact"]["default"] is False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"session": "s1", "history": "H", "compact": "yes"}, schema)
