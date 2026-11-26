"""Tests for :func:`virtuoso_bridge.virtuoso.maestro.read_results`.

Pinned by issue #81 -- ``read_results`` silently returned ``{}`` on
real-world ADE Explorer / Assembler runs because the
``maeExportOutputView`` return-value check (``"/tmp/" not in r``)
was brittle: some Cadence versions return ``t`` on success rather
than echoing the filename, which made the check spuriously fail
even when the CSV had been written correctly.

Covers:

* ``_parse_detail_csv`` pure-function shape on a hand-crafted Cadence
  Detail CSV (single point + multi-point sweep).
* ``read_results`` end-to-end success path with a fake client whose
  ``execute_skill`` and ``download_file`` are wired to produce a CSV --
  proves we no longer gate on the SKILL return string.
* ``read_results`` failure path when the remote CSV never materialises;
  asserts the diagnostic warning *and* the empty-dict return.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from virtuoso_bridge.virtuoso.maestro.reader.runs import (
    _parse_detail_csv,
    export_waveform,
    read_results,
)


SAMPLE_CSV = (
    ",,Parameter,Nominal,,,\n"
    "\n"
    "Point,Test,Output,Nominal,Spec,Weight,Pass/Fail\n"
    "Parameters: VDD=0.9,,,,,,\n"
    "1,inv_test,Gain_dB,21.63,,,\n"
    "1,inv_test,Delay_ps,12.4,< 15p,1,passed\n"
    "Parameters: VDD=1.1,,,,,,\n"
    "2,inv_test,Gain_dB,22.81,,,\n"
    "2,inv_test,Delay_ps,9.8,< 15p,1,passed\n"
)

SINGLE_POINT_CSV = (
    ",Parameter,Nominal,,,\n"
    "Title,Detail Results,,,,\n"
    "\n"
    "Test,Output,Nominal,Spec,Weight,Pass/Fail\n"
    "TRAN,IN,,,,\n"
    "TRAN,OUT,,,,\n"
    "TRAN,out_max,822.7e-3,< 1,1,passed\n"
)


def test_parse_detail_csv_multi_point():
    out = _parse_detail_csv(SAMPLE_CSV, history="Interactive.7")
    assert out["history"] == "Interactive.7"
    assert out["tests"] == ["inv_test"]
    assert len(out["points"]) == 2
    p1, p2 = out["points"]
    assert p1["parameters"] == {"VDD": "0.9"}
    assert p2["parameters"] == {"VDD": "1.1"}
    assert p1["outputs"]["Gain_dB"]["value"] == "21.63"
    assert p2["outputs"]["Delay_ps"]["pass_fail"] == "passed"
    # back-compat flat list
    assert len(out["outputs"]) == 4


def test_parse_detail_csv_empty_input():
    out = _parse_detail_csv("", history="Interactive.1")
    assert out["points"] == []
    assert out["outputs"] == []
    assert out["tests"] == []


def test_parse_detail_csv_single_point_without_point_column():
    out = _parse_detail_csv(SINGLE_POINT_CSV, history="Interactive.0")
    assert out["history"] == "Interactive.0"
    assert out["tests"] == ["TRAN"]
    assert len(out["points"]) == 1
    assert out["points"][0]["parameters"] == {}
    assert "Detail Results" not in out["points"][0]["outputs"]
    assert out["points"][0]["outputs"]["out_max"] == {
        "value": "822.7e-3",
        "spec": "< 1",
        "weight": "1",
        "pass_fail": "passed",
    }
    assert len(out["outputs"]) == 3


class _FakeSkillResult:
    def __init__(self, output: str) -> None:
        self.output = output


class _FakeClient:
    """Minimal stand-in for VirtuosoClient.

    ``skill_responses`` is consulted in-order for each ``execute_skill``
    call; if the queue empties the last response is reused.  The
    ``download_file`` hook lets a test pre-stage CSV content (or fail).
    """

    def __init__(self, *, skill_responses, on_download) -> None:
        self._responses = list(skill_responses)
        self._on_download = on_download
        self.skill_calls: list[str] = []

    def execute_skill(self, skill_code, timeout=None):
        self.skill_calls.append(skill_code)
        if len(self._responses) > 1:
            r = self._responses.pop(0)
        else:
            r = self._responses[0] if self._responses else ""
        return _FakeSkillResult(r)

    def download_file(self, remote, local):
        self._on_download(remote, local)


def _csv_dropper(content: str):
    def _drop(remote, local):
        Path(local).write_text(content, encoding="utf-8")
    return _drop


def _download_explodes(remote, local):
    raise FileNotFoundError(remote)


def test_read_results_success_does_not_gate_on_skill_return_string(tmp_path):
    """Regression for #81: SKILL returning ``"t"`` (no "/tmp/" in it)
    must NOT cause an empty-dict return when the CSV is actually
    fetchable.  Prior to the fix this test would fail."""
    # Order of execute_skill calls when history is provided:
    #   1. _get_test          -> maeGetSetup
    #   2. maeExportOutputView (via _q)
    #   3. deleteFile          (finally-block cleanup)
    #   4. maeGetOverallSpecStatus (via _q)
    #   5. maeGetOverallYield (via _q)
    client = _FakeClient(
        skill_responses=[
            '("inv_test")',   # _get_test: maeGetSetup
            "t",              # maeExportOutputView returns just `t`
            "t",              # deleteFile cleanup
            '"passed"',       # maeGetOverallSpecStatus
            "nil",            # maeGetOverallYield (no yield computed)
        ],
        on_download=_csv_dropper(SAMPLE_CSV),
    )
    out = read_results(client, session="sess_1",
                       lib="test_LIB", cell="inverter",
                       history="Interactive.7")
    assert out, "read_results returned empty despite valid CSV (issue #81)"
    assert out["history"] == "Interactive.7"
    assert len(out["points"]) == 2
    assert out["overall_spec"] == "passed"
    assert out["overall_yield"] is None


def test_read_results_logs_warning_when_csv_missing(tmp_path, caplog):
    """If maeExportOutputView fails silently and the CSV never lands,
    we want a warning that names the remote path + SKILL output, not a
    silent ``{}``."""
    client = _FakeClient(
        skill_responses=[
            '("inv_test")',
            "nil",                 # maeExportOutputView failed
        ],
        on_download=_download_explodes,
    )
    with caplog.at_level(logging.WARNING,
                        logger="virtuoso_bridge.virtuoso.maestro.reader.runs"):
        out = read_results(client, session="sess_1",
                           lib="test_LIB", cell="inverter",
                           history="Interactive.7")
    assert out == {}
    assert any("maeExportOutputView did not produce" in r.message
               for r in caplog.records), \
        "Expected a diagnostic warning naming maeExportOutputView"


def test_read_results_logs_when_test_lookup_fails(caplog):
    """ADE Explorer flows that never registered a test via
    ``maeSetupTest`` would hit _get_test == "" -- we want this surfaced
    rather than swallowed."""
    client = _FakeClient(
        skill_responses=["nil"],   # maeGetSetup returns nil
        on_download=lambda *_: None,
    )
    with caplog.at_level(logging.WARNING,
                        logger="virtuoso_bridge.virtuoso.maestro.reader.runs"):
        out = read_results(client, session="sess_x",
                           lib="LIB", cell="cell",
                           history="Interactive.1")
    assert out == {}
    assert any("maeGetSetup returned no test" in r.message
               for r in caplog.records)


def _waveform_client(history: str = "Interactive.7") -> _FakeClient:
    return _FakeClient(
        skill_responses=[
            "t",
            f'"/tmp/maestro/results/maestro/{history}/psf/"',
            "t",
        ],
        on_download=lambda *_: None,
    )


def test_export_waveform_preserves_ocean_format_defaults():
    client = _waveform_client()

    export_waveform(
        client,
        session="sess_1",
        expression='v("/OUT")',
        local_path="wave.txt",
        history="Interactive.7",
    )

    ocn_print = next(call for call in client.skill_calls
                     if call.startswith("ocnPrint("))
    assert "?numberNotation 'scientific" in ocn_print
    assert "?precision" not in ocn_print
    assert "?width" not in ocn_print


def test_export_waveform_passes_explicit_format_options():
    client = _waveform_client()

    export_waveform(
        client,
        session="sess_1",
        expression='v("/OUT")',
        local_path="wave.txt",
        history="Interactive.7",
        precision=12,
        width=20,
        number_notation="none",
    )

    ocn_print = next(call for call in client.skill_calls
                     if call.startswith("ocnPrint("))
    assert "?precision 12" in ocn_print
    assert "?width 20" in ocn_print
    assert "?numberNotation 'none" in ocn_print


@pytest.mark.parametrize(
    "notation",
    ["suffix", "engineering", "scientific", "none"],
)
def test_export_waveform_accepts_supported_number_notations(notation):
    client = _waveform_client()

    export_waveform(
        client,
        session="sess_1",
        expression='v("/OUT")',
        local_path="wave.txt",
        history="Interactive.7",
        number_notation=notation,
    )

    ocn_print = next(call for call in client.skill_calls
                     if call.startswith("ocnPrint("))
    assert f"?numberNotation '{notation}" in ocn_print


@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("precision", True, TypeError),
        ("precision", "6", TypeError),
        ("precision", 0, ValueError),
        ("precision", 17, ValueError),
        ("width", False, TypeError),
        ("width", 14.0, TypeError),
        ("width", 3, ValueError),
        ("number_notation", True, TypeError),
        ("number_notation", ["none"], TypeError),
        ("number_notation", "automatic", ValueError),
    ],
)
def test_export_waveform_rejects_invalid_format_options(option, value, error):
    client = _waveform_client()

    with pytest.raises(error):
        export_waveform(
            client,
            session="sess_1",
            expression='v("/OUT")',
            local_path="wave.txt",
            history="Interactive.7",
            **{option: value},
        )

    assert client.skill_calls == []


# A run whose points never reached Spectre (netlisting failed, say) exports
# the Detail view without its value column. Pinned from IC23.1: the fixed
# column positions read the Spec column as each output's value.
NO_VALUE_CSV = (
    "Point,Test,Output,Spec,Weight,Pass/Fail\n"
    "Parameters: vdd=0.8,,,,,\n"
    "1,CODE,t_conv,< 914p,,\n"
    "1,CODE,dout1,,,\n"
)


def test_parse_detail_csv_without_value_column_reads_no_value():
    out = _parse_detail_csv(NO_VALUE_CSV, history="Interactive.0")
    outputs = out["points"][0]["outputs"]
    assert outputs["t_conv"] == {"value": "", "spec": "< 914p", "weight": "", "pass_fail": ""}
    assert outputs["dout1"]["value"] == ""
    assert out["tests"] == ["CODE"]


MULTI_POINT_NO_PARAMETERS_CSV = (
    "Point,Test,Output,Nominal,Spec,Weight,Pass/Fail\n"
    "1,TRAN,Gain,20.1,,,\n"
    "1,TRAN,BW,10M,,,\n"
    "2,TRAN,Gain,21.5,,,\n"
    "2,TRAN,BW,11M,,,\n"
)


def test_parse_detail_csv_multi_point_without_parameters_headers():
    out = _parse_detail_csv(MULTI_POINT_NO_PARAMETERS_CSV, history="Interactive.2")
    assert out["tests"] == ["TRAN"]
    assert len(out["points"]) == 2
    p1, p2 = out["points"]
    assert p1["point"] == 1
    assert p1["outputs"]["Gain"]["value"] == "20.1"
    assert p1["outputs"]["BW"]["value"] == "10M"
    assert p2["point"] == 2
    assert p2["outputs"]["Gain"]["value"] == "21.5"
    assert p2["outputs"]["BW"]["value"] == "11M"
    assert len(out["outputs"]) == 4


def test_parse_detail_csv_preserves_non_contiguous_point_numbers_after_parameters():
    csv_text = (
        "Point,Test,Output,Nominal,Spec,Weight,Pass/Fail\n"
        "Parameters: VDD=0.9,,,,,,\n"
        "5,TRAN,Gain,20.1,,,\n"
        "5,TRAN,BW,10M,,,\n"
        "Parameters: VDD=1.1,,,,,,\n"
        "10,TRAN,Gain,21.5,,,\n"
    )
    out = _parse_detail_csv(csv_text, history="Interactive.8")
    assert [(p["point"], p["parameters"]) for p in out["points"]] == [
        (5, {"VDD": "0.9"}),
        (10, {"VDD": "1.1"}),
    ]
    assert out["points"][0]["outputs"]["Gain"]["value"] == "20.1"
    assert out["points"][0]["outputs"]["BW"]["value"] == "10M"
    assert out["points"][1]["outputs"]["Gain"]["value"] == "21.5"


def test_reader_escapes_session_and_history_in_skill_calls():
    client = _FakeClient(
        skill_responses=['("inv_test")', "t", "t", '"passed"', "nil"],
        on_download=_csv_dropper(SAMPLE_CSV),
    )
    out = read_results(
        client,
        session='sess"1',
        lib="test_LIB",
        cell="inverter",
        history='Hist"7',
    )
    assert out["history"] == 'Hist"7'
    assert 'maeGetSetup(?session "sess\\"1")' == client.skill_calls[0]
    assert '?session "sess\\"1" ?historyName "Hist\\"7"' in client.skill_calls[1]
    assert 'maeGetOverallYield("Hist\\"7")' in client.skill_calls[-1]
