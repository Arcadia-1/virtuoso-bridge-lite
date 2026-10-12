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


@pytest.mark.xfail(strict=True, reason=(
    "open bug: windows taller than 420 px are classified main_window and modality is "
    "only read from _NET_WM_STATE_MODAL (never set without a window manager); "
    "_NET_WM_WINDOW_TYPE_DIALOG and WM_TRANSIENT_FOR are ignored, so a blocking "
    "form such as a PDK information panel is reported 'clear'"))
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


@pytest.mark.xfail(strict=True, reason=(
    "open bug: _SEVERITY_RE is anchored at line start, so CDS.log lines prefixed with "
    "'\\o ' and using 'Warning:' are dropped and the result claims per-message text was absent"))
def test_recorded_schcheck_warnings_become_diagnostics():
    diagnostics = _classify_log_lines(SCHCHECK_LINES, source="schCheck")
    warnings = [d.message for d in diagnostics if d.severity == "warning"]
    assert len(warnings) == SCHCHECK_COUNTS["check_warning_count"]
    assert any('Pin "PLUS" on instance "R0"' in message for message in warnings)


# ---------------------------------------------------------------------------
# CDF parameters of a foundry NMOS (modelled; PDK data is not redistributable)
# ---------------------------------------------------------------------------

# Structure observed on a 0.18um foundry NMOS, paraphrased: "w" is the
# per-finger width, "wf" the total width, "nf" is derived (not editable) and
# "fingers" is the editable finger count. There is no "Wfg".
PDK180_NMOS_CDF = {
    "w": ("width per finger", "t"),
    "wf": ("total width", "t"),
    "fingers": ("number of fingers", "t"),
    "nf": ("finger count (derived)", "nil"),
    "m": ("multiplier (derived)", "nil"),
    "simM": ("multiplier", "schematic only"),
    "totalM": ("total multiplier (derived)", "nil"),
}


class _Pdk180Client:
    """Answers set_instance_params' resolution queries for that NMOS."""

    def __init__(self):
        self.skill: list[str] = []

    def execute_skill(self, code, timeout=None, **_kwargs):
        self.skill.append(code)
        if "geGetEditCellView()" in code and "libName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"scratch|nmos_test"')
        if "inst~>libName inst~>cellName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"pdk180|nmos_core"')
        unknown = [n for n in re.findall(r'setarray\(paramVals "([^"]+)"', code)
                   if n not in PDK180_NMOS_CDF]
        if unknown:
            # What the real update SKILL does for a name missing from the cell CDF.
            return VirtuosoResult(status=ExecutionStatus.ERROR,
                                  errors=[f"unknown CDF param: {unknown[0]}"])
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")


def test_finger_count_targets_editable_fingers_param():
    client = _Pdk180Client()
    applied = set_instance_params(client, "M0", l="360n", nf="2", param_filters=None)
    assert applied == {"l": "360n", "fingers": "2"}
    assert 'setarray(paramVals "fingers" "2")' in client.skill[-1]


@pytest.mark.xfail(strict=True, reason=(
    "open bug: _PARAM_MAP hardcodes TSMC28 'wf'->'Wfg'; this PDK has no Wfg and its "
    "'wf' is the total width, so the documented wf= shorthand cannot be applied"))
def test_width_shorthand_only_targets_existing_cdf_params():
    client = _Pdk180Client()
    set_instance_params(client, "M0", wf="8u", param_filters=None)
    sent = re.findall(r'setarray\(paramVals "([^"]+)"', client.skill[-1])
    assert sent and set(sent) <= set(PDK180_NMOS_CDF)


# ---------------------------------------------------------------------------
# Maestro durable job completion marker
# ---------------------------------------------------------------------------

# Written by the completion callback registered through
# maeRunSimulation(?callback ...) after an analogLib RC run on IC6.1.8: the
# callback receives (session x_runID), so "history" holds the integer run ID
# while the job's history is "Interactive.N".
RECORDED_MARKER = 'completed\nsession="{session}"\nhistory=1\n'


@pytest.mark.xfail(strict=True, reason=(
    "open bug: the callback records x_runID as history and status() compares it to the "
    "history name, so a finished job stays 'unknown'"))
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

@pytest.mark.xfail(strict=True, reason=(
    "open bug: isCallable('getpid) is nil on IC6.1.8 (the SKILL function is ipcGetPid); "
    "bridge code still sends getpid()"))
def test_bridge_never_sends_nonexistent_getpid_skill():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.parent.name == "resources":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["'(]getpid\(\)""", line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert offenders == []
