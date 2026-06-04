"""Tests for :func:`virtuoso_bridge.virtuoso.schematic.reader.read_schematic`.

Pinned by issue #99 -- ``read_schematic`` silently returned an empty
``{instances:[], nets:{}, pins:{}, notes:[]}`` for the populated
``AI_LIB/L3_FCT`` schematic.  A direct DFII probe on the same cellview
reported **181 instances / 18 terminals / 123 nets / 1215 shapes**, yet
the bridge reader returned zero.

Reproduced live: the SKILL completes in 73s and produces 500 KB of
output (163 INST lines + 21k PARAM lines), but the prior hardcoded
``timeout=60`` killed it mid-flight.  The bridge daemon then sent
SIGINT to Virtuoso and returned an empty payload with a ``Socket
timeout after 60s`` error, but ``read_schematic`` did not check
``r.errors`` and fed the empty payload straight through the parser,
yielding the all-zero dict.  Issue #99 fix: surface every SKILL-side
failure as a logging warning, and bump the default timeout to 300s.

The tests below pin the three failure modes the fix has to cover:

* ``test_read_schematic_warns_on_timeout`` -- the actual #99 surface,
  daemon returns a Socket-timeout error.
* ``test_read_schematic_warns_when_skill_says_error`` -- the
  ``unless(cv return("ERROR"))`` prelude path (cv = nil under the
  bridge: lib path / strict viewType / edit-lock).
* ``test_read_schematic_warns_on_empty_no_error`` -- IPC swallowed the
  body with no daemon-level error.  Defensive guard.

Plus parser-only tests proving ``_parse_schematic`` is fine on the
shape the affected schematic produces -- so future empty returns can
still be traced to caller-side failures, not parser bugs.
"""
from __future__ import annotations

import logging

import pytest

from virtuoso_bridge.virtuoso.schematic.reader import (
    _DEFAULT_TIMEOUT_S,
    _parse_schematic,
    read_schematic,
)


# =======================================================================
# Synthetic SKILL output matching the issue #99 schematic shape.
# Hand-rolled to mirror what _SKILL_TOPOLOGY would emit for a small
# L3_FCT-like cellview: arrayed-bus instances, bus nets, bus terminals,
# wildcarded instTerm net names, scalar pins.
# =======================================================================

SAMPLE_TOPOLOGY_OUT = (
    "INSTANCES\n"
    "INST|I222<1:14>|FIRAS|LB_FCT_cunit\n"
    "TERM|CINP|<*14>FCT_NTUNE_D<2:0>\n"
    "TERM|VSSANA|VSSANA\n"
    "INST|I218<1:5>|FIRAS|LB_FCT_cunit\n"
    "TERM|CINP|<*5>FCT_NTUNE_D<2:0>\n"
    "INST|C58<1:5>|analogLib|cap\n"
    "TERM|PLUS|VCM\n"
    'PARAM|c|"1.2p"\n'
    "NETS\n"
    "NET|FCT_NTUNE_D<2:0>|3|signal|nil|I222.CINP|I218.CINP\n"
    "NET|VCM|1|signal|nil|C58.PLUS\n"
    "PINS\n"
    "PIN|VINP<1:0>|input|2\n"
    "PIN|FCT_NTUNE_D<2:0>|input|3\n"
    "PIN|VCM|inputOutput|1\n"
    "NOTES\n"
    "END\n"
)


# =======================================================================
# Parser-only tests -- positive control that the bug is NOT here.
# =======================================================================

def test_parser_handles_arrayed_instances_and_bus_nets():
    """If the parser were the issue #99 culprit, this would return zero.

    Pins the positive control: parser is fine on the exact shape the
    affected schematic produces.  Any future empty return must be
    coming from the SKILL side, not the parser.
    """
    out = _parse_schematic(
        SAMPLE_TOPOLOGY_OUT,
        include_positions=False,
        filter_config=None,
    )

    names = [i["name"] for i in out["instances"]]
    assert names == ["I222<1:14>", "I218<1:5>", "C58<1:5>"], names

    # Wildcarded instTerm net names survive the | split.
    i222 = out["instances"][0]
    assert i222["terms"]["CINP"] == "<*14>FCT_NTUNE_D<2:0>"

    # Bus net registered with bus-width numBits and both connections.
    bus_net = out["nets"]["FCT_NTUNE_D<2:0>"]
    assert bus_net["numBits"] == 3
    assert bus_net["connections"] == ["I222.CINP", "I218.CINP"]

    # Bus pin keeps its <a:b> name and numBits.
    assert out["pins"]["FCT_NTUNE_D<2:0>"]["numBits"] == 3
    assert out["pins"]["VCM"]["direction"] == "inputOutput"


def test_parser_silently_empty_on_skill_error_marker():
    """The parser doesn't try to detect failure markers; the caller does.

    Documents that ``_parse_schematic("ERROR")`` returns a zeroed dict
    without complaint -- the warning has to be emitted upstream in
    ``read_schematic`` itself (which the fix below does).
    """
    out = _parse_schematic("ERROR", include_positions=False, filter_config=None)
    assert out == {"instances": [], "nets": {}, "pins": {}, "notes": []}


def test_parser_silently_empty_on_empty_input():
    """Same surface as the ERROR marker: empty in -> empty out, no noise."""
    out = _parse_schematic("", include_positions=False, filter_config=None)
    assert out == {"instances": [], "nets": {}, "pins": {}, "notes": []}


# =======================================================================
# End-to-end with a fake client -- pins the caller-side warning behaviour.
# =======================================================================

class _FakeSkillResult:
    def __init__(self, output: str, errors: list | None = None) -> None:
        self.output = output
        self.errors = errors or []


class _FakeClient:
    """Minimal stand-in for ``VirtuosoClient.execute_skill``.

    Captures each SKILL invocation + the timeout it was given so we can
    assert on both the open-call shape and that ``timeout`` threads
    through from ``read_schematic(..., timeout=...)`` to the bridge.
    """

    def __init__(self, output: str = "", errors: list | None = None) -> None:
        self._output = output
        self._errors = errors
        self.skill_calls: list[str] = []
        self.timeouts_seen: list[int | None] = []

    def execute_skill(self, skill_code: str, timeout: int | None = None):
        self.skill_calls.append(skill_code)
        self.timeouts_seen.append(timeout)
        return _FakeSkillResult(self._output, self._errors)


def test_skill_expression_uses_explicit_schematic_viewtype():
    """The bridge opens via
    ``dbOpenCellViewByType("lib" "cell" "schematic" "schematic" "r")``.

    Pinning the call shape so any future change (drop viewType arg,
    add ``dbOpenCellView`` fallback) is detectable.
    """
    client = _FakeClient()
    read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None)
    assert len(client.skill_calls) == 1
    assert (
        'dbOpenCellViewByType("AI_LIB" "L3_FCT" "schematic" "schematic" "r")'
        in client.skill_calls[0]
    )


def test_read_schematic_default_timeout_is_at_least_300s():
    """Issue #99: the prior 60s default silently truncated a real cell.

    Pin the default at >= 300s so an accidental regression to a short
    timeout surfaces in CI.
    """
    client = _FakeClient()
    read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None)
    assert client.timeouts_seen == [_DEFAULT_TIMEOUT_S]
    assert _DEFAULT_TIMEOUT_S >= 300, (
        f"Default timeout regressed to {_DEFAULT_TIMEOUT_S}s -- issue #99"
    )


def test_read_schematic_threads_timeout_kwarg_to_execute_skill():
    """``read_schematic(..., timeout=N)`` must propagate N to the bridge.

    Without this the caller has no way to extend the budget for very
    large cellviews; the kwarg is the fix's primary public knob.
    """
    client = _FakeClient()
    read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None, timeout=900)
    assert client.timeouts_seen == [900]


def test_read_schematic_warns_on_timeout(caplog):
    """The actual issue #99 surface.

    Daemon timed out, returned the ``Socket timeout after Ns`` error
    string with empty output.  Prior to the fix this silently became
    ``{instances:[], nets:{}, pins:{}, notes:[]}`` with no diagnostic;
    after the fix a WARNING names the cell, the timeout value, and
    flags the issue number so the caller can find it.
    """
    client = _FakeClient(output="", errors=["Socket timeout after 60s"])
    with caplog.at_level(
        logging.WARNING,
        logger="virtuoso_bridge.virtuoso.schematic.reader",
    ):
        out = read_schematic(
            client, "AI_LIB", "L3_FCT", param_filters=None, timeout=60,
        )

    assert out == {"instances": [], "nets": {}, "pins": {}, "notes": []}
    messages = [r.message for r in caplog.records]
    assert any("timed out" in m for m in messages), messages
    assert any("AI_LIB/L3_FCT" in m for m in messages), messages
    assert any("60" in m for m in messages), messages
    assert any("issue #99" in m for m in messages), messages


def test_read_schematic_warns_when_skill_says_error(caplog):
    """``unless(cv return("ERROR"))`` prelude path.

    cv = nil under the bridge (lib path / strict viewType / edit-lock).
    SKILL emits the literal token ``ERROR``; the reader must surface
    a warning rather than silently zeroing out.
    """
    client = _FakeClient(output='"ERROR"')  # daemon-wire form
    with caplog.at_level(
        logging.WARNING,
        logger="virtuoso_bridge.virtuoso.schematic.reader",
    ):
        out = read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None)

    assert out == {"instances": [], "nets": {}, "pins": {}, "notes": []}
    messages = [r.message for r in caplog.records]
    assert any("cv = nil" in m or "could not open" in m for m in messages), messages
    assert any("AI_LIB/L3_FCT" in m for m in messages), messages


def test_read_schematic_warns_on_empty_no_error(caplog):
    """Defensive: empty body, no daemon error, no SKILL marker.

    Shouldn't normally happen, but if IPC swallows the body silently
    the caller still gets a warning -- never an unflagged empty dict.
    """
    client = _FakeClient(output="")
    with caplog.at_level(
        logging.WARNING,
        logger="virtuoso_bridge.virtuoso.schematic.reader",
    ):
        out = read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None)

    assert out == {"instances": [], "nets": {}, "pins": {}, "notes": []}
    messages = [r.message for r in caplog.records]
    assert any("empty response" in m or "IPC truncation" in m for m in messages), messages


def test_read_schematic_no_warning_on_genuinely_populated_cell(caplog):
    """The positive complement: a healthy SKILL response must NOT warn.

    Without this guard the warning helpers could end up firing on
    every call (false-positive flood).
    """
    client = _FakeClient(output=SAMPLE_TOPOLOGY_OUT)
    with caplog.at_level(
        logging.WARNING,
        logger="virtuoso_bridge.virtuoso.schematic.reader",
    ):
        out = read_schematic(client, "AI_LIB", "L3_FCT", param_filters=None)

    assert len(out["instances"]) == 3, "parser failed on healthy input"
    assert caplog.records == [], (
        "spurious warning on healthy input: "
        f"{[r.message for r in caplog.records]}"
    )
