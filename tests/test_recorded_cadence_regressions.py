"""Regressions that replay output recorded from a real Virtuoso session.

The data under ``tests/fixtures/cadence`` comes from plain Virtuoso (analogLib
and SKILL forms only, no PDK), recorded with ``tools/record_cadence_fixtures.py``;
``manifest.json`` names the release. Anyone with Virtuoso can re-record it,
and the scrubbed files contain no host, user or path information.

Two inputs are modelled rather than recorded and say so where they are
defined: small modal dialogs reported by ``inspect-dialogs``, and the CDF
structure of a foundry NMOS (PDK data that cannot be published verbatim).

Cases marked ``xfail(strict=True)`` reproduce bugs that were open when the
data was recorded. A fix makes them pass; strict mode then fails the run until
the marker is removed, so the case becomes a normal regression.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.maestro import MaestroJobManager
from virtuoso_bridge.virtuoso.schematic.diagnostics import _classify_log_lines
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

from tests.test_maestro_jobs import FakeClient, portable_local_process_identity  # noqa: F401

SRC = Path(__file__).parents[1] / "src" / "virtuoso_bridge"
FIXTURES = Path(__file__).parent / "fixtures" / "cadence"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# X11 dialog inspection
# ---------------------------------------------------------------------------

def _load_x11_helper():
    path = SRC / "resources" / "x11_dismiss_dialog.py"
    spec = importlib.util.spec_from_file_location("x11_recorded_regressions", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def x11():
    return _load_x11_helper()


WINDOWS = json.loads(_fixture("windows.json"))
VIRTUOSO_PID = WINDOWS["virtuoso_pid"]
HOST = "eda-host"


def _record(x11, window_id, xprop, geometry, *, mapped=True):
    """Build an inventory record the way the X11 inventory does from xprop."""
    metadata = x11._parse_xprop_inspection(xprop)
    record = {"id": window_id, "frame_id": window_id, "geometry": geometry,
              "mapped": mapped, "client_shell": True}
    for key in ("pid", "transient_for", "client_leader", "client_machine", "modal"):
        record[key] = metadata[key]
    record["window_types"] = metadata.get("window_types") or []
    record["title"] = metadata.get("title") or ""
    record["class"] = metadata.get("class") or []
    return record


def _ciw(x11):
    return _record(x11, WINDOWS["ciw"]["id"], _fixture("ciw.xprop"), WINDOWS["ciw"]["geometry"])


def _leader_id(x11):
    return x11._parse_xprop_inspection(_fixture("ciw.xprop"))["client_leader"]


def _modeled_dialog(x11, window_id, title, geometry):
    """A small modal dialog of the recorded process (title/geometry/modality
    as reported by ``inspect-dialogs``; ownership as in the recorded xprop)."""
    leader = _leader_id(x11)
    return {"id": window_id, "frame_id": window_id, "title": title,
            "class": ["virtuoso", "virtuoso"], "geometry": geometry, "mapped": True,
            "client_shell": True, "pid": VIRTUOSO_PID, "transient_for": leader,
            "client_leader": leader, "client_machine": HOST, "modal": True}


def _inspect(monkeypatch, x11, dialogs):
    leader = {"id": _leader_id(x11), "frame_id": _leader_id(x11), "title": "",
              "class": ["virtuoso", "virtuoso"], "geometry": {"x": 10, "y": 10, "w": 10, "h": 10},
              "mapped": False, "pid": VIRTUOSO_PID, "transient_for": None,
              "client_leader": None, "client_machine": HOST, "modal": False}
    inventory = [_ciw(x11), leader, *dialogs]
    monkeypatch.setattr(x11, "_read_process_x11_env", lambda _pid: {
        "DISPLAY": ":99", "XAUTHORITY": "/tmp/Xauthority", "HOSTNAME": HOST})
    monkeypatch.setattr(x11, "_inspect_window_inventory",
                        lambda selected_display, process_env, timeout=15: list(inventory))
    return x11.inspect_dialogs(VIRTUOSO_PID)


def _tall_form(x11):
    return _record(x11, WINDOWS["form"]["id"], _fixture("tall_form.xprop"), WINDOWS["form"]["geometry"])


def test_recorded_tall_form_has_dialog_ownership(x11):
    metadata = x11._parse_xprop_inspection(_fixture("tall_form.xprop"))
    assert metadata["pid"] == VIRTUOSO_PID
    assert metadata["transient_for"] == metadata["client_leader"] == _leader_id(x11)
    assert WINDOWS["form"]["geometry"]["h"] > 420


def test_tall_blocking_form_blocks_ciw(monkeypatch, x11):
    result = _inspect(monkeypatch, x11, [_tall_form(x11)])
    assert result["status"] == "blocked"
    assert [d["window_id"] for d in result["dialogs"]] == [WINDOWS["form"]["id"]]


@pytest.mark.parametrize("window_id, title, geometry", [
    # deOpenCellView on a missing cellview: 'Data file for "lib/cell/schematic" does not exist.'
    ("0x4001c1", "Not Found", {"x": 613, "y": 362, "w": 354, "h": 96}),
    # schHiReplace with no match: "(SCH-1145): No objects match the search criteria."
    ("0x4005c2", "Warning", {"x": 636, "y": 369, "w": 308, "h": 82}),
    # XStream Out completion box.
    ("0x4006b3", "Stream out translation complete", {"x": 682, "y": 372, "w": 216, "h": 74}),
])
def test_small_modal_dialogs_block_ciw(monkeypatch, x11, window_id, title, geometry):
    result = _inspect(monkeypatch, x11, [_modeled_dialog(x11, window_id, title, geometry)])
    assert result["status"] == "blocked"
    assert [d["window_id"] for d in result["dialogs"]] == [window_id]
    assert result["dialogs"][0]["ownership"] == "target"


# ---------------------------------------------------------------------------
# schCheck diagnostics (analogLib resistor with two floating nets)
# ---------------------------------------------------------------------------

SCHCHECK_LINES = tuple(_fixture("schcheck_floating.lines").splitlines())
SCHCHECK_COUNTS = json.loads(_fixture("schcheck_floating.json"))


def test_recorded_schcheck_info_line_keeps_its_code():
    diagnostics = _classify_log_lines(SCHCHECK_LINES[:1], source="schCheck")
    assert [(d.severity, d.code) for d in diagnostics] == [("info", "SCH-1170")]


def test_recorded_schcheck_warnings_become_diagnostics():
    diagnostics = _classify_log_lines(SCHCHECK_LINES, source="schCheck")
    warnings = [d.message for d in diagnostics if d.severity == "warning"]
    assert len(warnings) == SCHCHECK_COUNTS["check_warning_count"]
    assert any(message.startswith('Warning: Pin "PLUS" on instance "R0"') for message in warnings)


# ---------------------------------------------------------------------------
# CDF parameters of a foundry NMOS (modelled; PDK data is not redistributable)
# ---------------------------------------------------------------------------

# Structure observed on a 0.18um foundry NMOS, paraphrased: "w" is the
# per-finger width, "wf" the total width, "nf" is derived (not editable) and
# "fingers" is the editable finger count. There is no "Wfg".
PDK180_NMOS_CDF = {
    "w": ("width per finger", "t"),
    "wf": ("total width", "t"),
    "l": ("length", "t"),
    "fingers": ("number of fingers", "t"),
    "nf": ("finger count (derived)", "nil"),
    "m": ("multiplier (derived)", "nil"),
    "simM": ("multiplier", "schematic only"),
    "totalM": ("total multiplier (derived)", "nil"),
}


class _Pdk180Client:
    """Answers set_instance_params' queries for that NMOS."""

    def __init__(self):
        self.skill: list[str] = []

    def execute_skill(self, code, timeout=None, **_kwargs):
        self.skill.append(code)
        if "geGetEditCellView()" in code and "libName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"scratch|nmos_test"')
        if "inst~>libName inst~>cellName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"pdk180|nmos_core"')
        if "cdfGetInstCDF(inst)" in code and "p~>prompt" in code:
            rows = " ".join(f'("{n}" "{prompt}" "{editable}")'
                            for n, (prompt, editable) in PDK180_NMOS_CDF.items())
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=f"({rows})")
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")

    def updates(self) -> list[str]:
        return [code for code in self.skill if "setarray(paramVals" in code]


def test_finger_count_and_multiplier_target_the_editable_params():
    client = _Pdk180Client()
    applied = set_instance_params(client, "M0", l="360n", nf="2", m="3", param_filters=None)
    assert applied == {"l": "360n", "fingers": "2", "simM": "3"}
    assert 'setarray(paramVals "fingers" "2")' in client.updates()[-1]


def test_finger_width_shorthand_is_refused_without_wfg():
    # "wf" exists here but is the *total* width; mapping the finger-width
    # shorthand onto it would silently size the device wrong.
    client = _Pdk180Client()
    with pytest.raises(ValueError, match=r"wf=.*Wfg.*width parameters: w \('width per finger'\), wf \('total width'\)"):
        set_instance_params(client, "M0", wf="8u", param_filters=None)
    assert client.updates() == []


# ---------------------------------------------------------------------------
# Maestro durable job completion marker
# ---------------------------------------------------------------------------

# Written by the completion callback registered through
# maeRunSimulation(?callback ...) after an analogLib RC run on IC6.1.8: the
# callback receives (session x_runID), so "history" holds the integer run ID
# while the job's history is "Interactive.N".
RECORDED_MARKER = 'completed\nsession="{session}"\nhistory=1\n'


def test_job_with_recorded_cadence_marker_reports_completed(tmp_path, portable_local_process_identity):  # noqa: F811
    client = FakeClient()
    jobs = MaestroJobManager.from_client(
        client, local_root=tmp_path / "runs", local_work_root=tmp_path / "work", local_gui=True,
    )
    job = jobs.submit(session="fnxSession4", run_id="recorded-run-001")
    (Path(job.work_dir) / "completed").write_text(
        RECORDED_MARKER.format(session="fnxSession4"), encoding="utf-8",
    )
    observer = MaestroJobManager.local(local_root=tmp_path / "runs", work_root=tmp_path / "work")
    assert observer.status(observer.load(job.run_id)).state == "completed"


# ---------------------------------------------------------------------------
# SKILL functions that do not exist on the recorded release
# ---------------------------------------------------------------------------

# isCallable('getpid) is nil on IC6.1.8; the SKILL function is ipcGetPid.
def test_bridge_never_sends_nonexistent_getpid_skill():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.parent.name == "resources":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["'(]getpid\(\)""", line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert offenders == []


# ---------------------------------------------------------------------------
# SKILL error replies (errset.errset as the CIW handler sends it)
# ---------------------------------------------------------------------------

SKILL_ERRORS = {item["skill"]: item["reply"] for item in json.loads(_fixture("skill_errors.json"))}


@pytest.mark.parametrize("skill, message", [
    ('error("VB fixture error %d" 3)', "VB fixture error 3"),
    ("car(1)", 'car: argument #1 should be a list (type template = "l") - 1'),
    ("vbFixtureUndefinedFunction()", "eval: undefined function - vbFixtureUndefinedFunction"),
    # Multi-line requests are re-raised by the daemon with the arguments
    # already folded into the message.
    ("progn(\n  car(1)\n)", 'car: argument #1 should be a list (type template = "l") - 1'),
])
def test_recorded_skill_error_replies_read_like_the_ciw(skill, message):
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

    result = VirtuosoClient._parse_response("\x15" + SKILL_ERRORS[skill], 0.1)

    assert result.errors == [message]
    assert result.metadata["skill_error"] == SKILL_ERRORS[skill]


@pytest.mark.parametrize("reply", [
    "IPC lane is busy",
    "AuthError: bad token",
    '("error" 0 t nil nil)',
    '("not" "an" "errset")',
    "(unbalanced",
])
def test_other_error_replies_are_passed_through(reply):
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

    result = VirtuosoClient._parse_response("\x15" + reply, 0.1)

    assert result.errors == [reply]
    assert result.metadata == {}
