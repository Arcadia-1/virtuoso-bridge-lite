"""End-to-end checks against a real Virtuoso/Spectre installation.

Skipped unless ``VB_RUN_LIVE_TESTS=1``; see tests/README.md. Everything here
works on plain Virtuoso (analogLib, SKILL forms, Spectre primitives); PDK
checks are opt-in. Run against a dedicated Virtuoso session (for example one
started under ``cdsXvfb-run``), never a CIW someone is using: tests open
windows and briefly show a blocking form. Every cell created here is named
``VB_LIVE_<id>`` inside ``VB_TEST_LIB`` and is deleted at the end.

Required:
  VB_RUN_LIVE_TESTS=1
  VB_TEST_LIB            writable scratch library visible in the CIW
Optional:
  VB_TEST_ENV_FILE       bridge .env to use (otherwise normal discovery)
  VB_TEST_LAYER, VB_TEST_LAYER2
                         drawing layers for layout checks; by default the first
                         two drawing layers of VB_TEST_LIB's technology
  VB_TEST_PDK_LIB, VB_TEST_NMOS_CELL
                         MOS symbol with a fingers/nf CDF for callback checks
  VB_TEST_STREAM_MAP     caller-host stream layer map; GDS export is checked in
                         test_bridge_live_gds.py, which runs after this module
  VB_TEST_MAESTRO=1      also run the Maestro checks (needs ADE licenses)

Cases marked ``xfail(strict=True)`` reproduce bugs observed on IC6.1.8; a fix
makes them pass and strict mode then requires removing the marker.
"""

from __future__ import annotations

import importlib.util
import math
import os
import posixpath
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.getenv("VB_RUN_LIVE_TESTS") != "1",
        reason="set VB_RUN_LIVE_TESTS=1 to run against a live Virtuoso session",
    ),
    # Transfers must use the client's real local tar (e.g. macOS bsdtar options).
    pytest.mark.usefixtures("real_tar_detection"),
]

if os.getenv("VB_RUN_LIVE_TESTS") == "1" and os.getenv("VB_TEST_ENV_FILE"):
    from virtuoso_bridge.env import set_runtime_env_file

    set_runtime_env_file(os.path.expanduser(os.environ["VB_TEST_ENV_FILE"]))

from virtuoso_bridge import VirtuosoClient  # noqa: E402
from virtuoso_bridge.models import ExecutionStatus  # noqa: E402
from virtuoso_bridge.virtuoso.schematic.ops import (  # noqa: E402
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin as pin,
    schematic_label_instance_term as label,
)

PREFIX = "VB_LIVE_"
RUN = uuid.uuid4().hex[:8]


def _cell(name: str) -> str:
    return f"{PREFIX}{name}_{RUN}"


def _ok(result):
    assert result.status == ExecutionStatus.SUCCESS, result.errors
    return result.output


def _require(*names: str) -> list[str]:
    values = [os.getenv(name) for name in names]
    if not all(values):
        pytest.skip(f"set {', '.join(names)} for this live check")
    return values  # type: ignore[return-value]


@pytest.fixture(scope="module")
def lib() -> str:
    value = os.getenv("VB_TEST_LIB")
    if not value:
        pytest.fail("VB_TEST_LIB is required for live tests")
    return value


@pytest.fixture(scope="module")
def client(lib):
    c = VirtuosoClient.from_env(timeout=120)
    assert lib in c.library.list(), f"{lib} is not visible in the CIW"
    # Session-only: never stop on the license next-level prompt (sites that
    # only own XL seats otherwise block on the first schematic/layout edit).
    _ok(c.execute_skill(
        'progn(foreach(k list("VSEL_UseNextLicense" "VLSL_UseNextLicense" "VLSXL_UseNextLicense" '
        '"ADEL_UseNextLicense" "ADEXL_UseNextLicense" "ADE_UseNextLicense" '
        '"Explorer_UseNextLicense") envSetVal("license" k \'string "always")) t)'))
    yield c
    # Close and delete only the cells this run created.
    c.execute_skill(
        f'let((cells) '
        f'foreach(w hiGetWindowList() let((cv) cv = car(errset(geGetWindowCellView(w))) '
        # Save first: closing a window with unsaved edits raises "Save Changes".
        f'when(cv && cv~>libName == "{lib}" && index(cv~>cellName "{RUN}") '
        f'when(cv~>modifiedButNotSaved dbSave(cv)) hiCloseWindow(w)))) '
        f'foreach(cv dbGetOpenCellViews() when(cv~>libName == "{lib}" && index(cv~>cellName "{RUN}") '
        f'dbPurge(cv))) '
        f'cells = setof(x ddGetObj("{lib}")~>cells rexMatchp("^{PREFIX}" x~>name) && index(x~>name "{RUN}")) '
        f'foreach(x cells ddDeleteObj(x)) length(cells))', timeout=120)


def _assert_export_left_nothing(client, cell, exported):
    """export_netlist netlists in its own run directory and removes it: neither
    that directory nor a <projectDir>/<cell> tree may remain on the host."""
    project = _ok(client.execute_skill('simplifyFilename(envGetVal("asimenv.startup" "projectDir"))'))
    for path in (exported["run_dir"], posixpath.join(project.strip().strip('"'), cell)):
        found = _ok(client.execute_skill(f'isDir("{path}")')).strip()
        assert found == "nil", f"export left {path} on the host"


def _rc(client, lib, cell, *, extra_pin=True):
    with client.schematic.create(lib, cell) as sch:
        sch.add(inst("analogLib", "vdc", "symbol", "V0", 0.0, 0.0, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R0", 1.5, 0.5, "R0"))
        sch.add(inst("analogLib", "cap", "symbol", "C0", 3.0, 0.0, "R0"))
        sch.add(inst("analogLib", "gnd", "symbol", "G0", 0.0, -1.0, "R0"))
        for name, term, net in (("V0", "PLUS", "VIN"), ("V0", "MINUS", "gnd!"), ("R0", "PLUS", "VIN"),
                                ("R0", "MINUS", "OUT"), ("C0", "PLUS", "OUT"), ("C0", "MINUS", "gnd!")):
            sch.add(label(name, term, net))
        if extra_pin:
            sch.add(pin("OUT", 4.5, 0.5, "R0", direction="output"))


# ---------------------------------------------------------------- transport


def _local_tech_db_size(client, library):
    """Size of a library-local tech.db, or -1 when the library has none."""
    return int(_ok(client.execute_skill(
        f'let((p) p = strcat(ddGetObj("{library}")~>readPath "/tech.db") '
        f'if(isFile(p) fileLength(p) -1))')))


def test_scratch_library_technology_binding_is_intact(client, lib):
    # A zero-byte tech.db makes OA treat the library as owning an empty
    # technology database; XStream then fails with XSTRM-2057/XSTRM-21 long
    # after whatever wrote it (seen after running spiceIn outside the CIW's
    # cds.lib). Fail fast with the cause instead.
    assert _local_tech_db_size(client, lib) != 0, f"{lib}/tech.db is empty; remove it and rebind the tech"





def test_skill_values_round_trip(client):
    assert _ok(client.execute_skill("1+2")) == "3"
    assert _ok(client.execute_skill('"中文 ✓ µA"')) == '"中文 ✓ µA"'
    assert _ok(client.execute_skill(r'"a\"b\\c"')) == r'"a\"b\\c"'
    assert len(_ok(client.execute_skill("let((l) for(i 1 20000 l = cons(i l)) l)", timeout=60)).split()) == 20000
    assert _ok(client.execute_skill("let((x)\n  x = 40 ; comment ) with paren\n  x + 2\n)")) == "42"


def test_multiline_skill_error_reports_the_real_message(client):
    result = client.execute_skill("progn(\n  vbLiveNoSuchFunction()\n)")
    assert result.status == ExecutionStatus.ERROR
    assert any("vbLiveNoSuchFunction" in error for error in result.errors), result.errors


def test_single_line_with_two_forms_returns_last_value(client):
    assert _ok(client.execute_skill("vbLiveTmp = 1 vbLiveTmp + 41")) == "42"


def test_file_transfer_is_byte_exact(client, tmp_path):
    payload = os.urandom(200_000)
    local = tmp_path / "blob.bin"
    local.write_bytes(payload)
    remote = f"/tmp/{PREFIX}{RUN}.bin"
    try:
        _ok(client.upload_file(local, remote))
        _ok(client.download_file(remote, tmp_path / "back.bin"))
        assert (tmp_path / "back.bin").read_bytes() == payload
    finally:
        client.execute_skill(f'deleteFile("{remote}")')


def test_wrong_daemon_token_is_refused(client):
    stranger = VirtuosoClient(host=client.host, port=client.port, timeout=10, daemon_token="ab" * 32)
    result = stranger.execute_skill("1+1")
    assert result.status == ExecutionStatus.ERROR
    assert "token" in " ".join(result.errors).lower()


def test_idle_ciw_inspects_clear(client):
    report = client.dialogs.inspect(pid=client.get_virtuoso_pid())
    assert report.status == "clear", report


def _tall_form_skill(tenths: int) -> str:
    path = Path(__file__).parents[1] / "tools" / "record_cadence_fixtures.py"
    spec = importlib.util.spec_from_file_location("vb_record_cadence_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TALL_FORM_SKILL.format(tenths=tenths)


def test_tall_blocking_form_is_reported_blocked(client):
    # The form cancels itself after 15 s, so the CIW recovers either way.
    guarded = VirtuosoClient.from_env(timeout=60)
    pid = guarded.get_virtuoso_pid()
    guarded.dialogs.enable_guard(pid=pid, protect_inflight=True, timeout=15)
    pending = guarded.execute_skill(_tall_form_skill(150), timeout=6)
    report = client.dialogs.inspect(pid=pid)
    handle = pending.metadata.get("request_handle")
    deadline = time.monotonic() + 60
    while handle and time.monotonic() < deadline:
        if guarded.requests.receipt(handle, timeout=10).metadata.get("request_state") == "completed":
            break
        time.sleep(2)
    assert report.status == "blocked", report


def test_recoverable_request_survives_client_timeout(client):
    guarded = VirtuosoClient.from_env(timeout=60)
    guarded.dialogs.enable_guard(pid=guarded.get_virtuoso_pid(), protect_inflight=True, timeout=15)
    first = guarded.execute_skill('progn(sh("sleep 12") "slept")', timeout=6)
    assert first.metadata.get("outcome") == "unknown"
    handle = first.metadata["request_handle"]
    second = guarded.execute_skill("1+1", timeout=10)
    assert second.metadata.get("outcome") == "not_started"
    deadline = time.monotonic() + 60
    while True:
        receipt = guarded.requests.receipt(handle, timeout=10)
        if receipt.metadata.get("request_state") == "completed" or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert receipt.status == ExecutionStatus.SUCCESS and receipt.output == '"slept"'
    assert _ok(guarded.execute_skill("1+1", timeout=10)) == "2"


def test_concurrent_request_is_never_duplicated_and_lane_recovers(client):
    other = VirtuosoClient.from_env(timeout=60)
    marker = f"vbLiveCount_{RUN}"
    _ok(client.execute_skill(f"{marker} = 0"))
    slow = {}

    def run_slow():
        slow["r"] = client.execute_skill(f'progn(sh("sleep 4") {marker} = {marker} + 1)', timeout=30)

    thread = threading.Thread(target=run_slow)
    thread.start()
    time.sleep(1.0)
    fast = other.execute_skill(f"{marker} = {marker} + 10", timeout=20)
    thread.join()
    total = int(_ok(other.execute_skill(marker)))
    assert slow["r"].status == ExecutionStatus.SUCCESS
    # Either queued and executed once (11) or refused before sending (1).
    assert total == (11 if fast.status == ExecutionStatus.SUCCESS else 1), (total, fast.errors)


# ---------------------------------------------------------------- library


def _drop_libraries(client, *names):
    for name in names:
        if name in client.library.list():
            client.library.delete(name)


def test_library_create_rename_delete_with_technology(client, lib):
    parent = posixpath.dirname(client.library.get(lib).path)
    tech = client.library.get_technology_library(lib)
    if not tech:
        pytest.skip(f"{lib} has no technology library to bind")
    name = f"{PREFIX}lib_{RUN}"
    try:
        created = client.library.create(name, f"{parent}/{name}", technology_library=tech)
        assert created.technology_library == tech
        assert client.library.rename(name, name + "_r").name == name + "_r"
        client.library.delete(name + "_r")
        assert {name, name + "_r"}.isdisjoint(client.library.list())
    finally:
        _drop_libraries(client, name, name + "_r")


def test_failed_library_rename_never_leaves_both_names(client, lib):
    parent = posixpath.dirname(client.library.get(lib).path)
    name = f"{PREFIX}empty_{RUN}"
    try:
        client.library.create(name, f"{parent}/{name}")
        try:
            client.library.rename(name, name + "_r")
        except RuntimeError:
            pass
        names = set(client.library.list())
        assert (name in names) != (name + "_r" in names), "rename left a half-renamed library"
    finally:
        _drop_libraries(client, name, name + "_r")


# ---------------------------------------------------------------- schematic, netlist, symbol


def test_schematic_create_modify_read_netlist_and_symbol(client, lib, tmp_path):
    cell = _cell("rc")
    _rc(client, lib, cell)
    with client.schematic.modify(lib, cell) as sch:
        sch.add(inst("analogLib", "cap", "symbol", "C1", 4.5, -1.0, "R0"))
        sch.add(label("C1", "PLUS", "OUT"))
        sch.add(label("C1", "MINUS", "gnd!"))
    data = client.schematic.read(lib, cell)
    assert sorted(i["name"] for i in data["instances"] if i["cell"] != "opin") == ["C0", "C1", "G0", "R0", "V0"]
    report = client.schematic.check_and_save(lib, cell)
    assert report.status == "saved" and report.check_error_count == 0
    exported = client.schematic.export_netlist(lib, cell, tmp_path / "netlist")
    _assert_export_left_nothing(client, cell, exported)
    text = (tmp_path / "netlist" / "input.scs").read_text()
    assert re.search(r"^R0 \(VIN OUT\) resistor", text, re.M), text
    assert re.search(r"^C1 \(OUT 0\) capacitor", text, re.M), text
    symbol = client.symbol.generate_from_schematic(lib, cell)
    assert symbol.terminal_names == ("OUT",)
    assert [t["name"] for t in client.symbol.read_ports(lib, cell)["terms"]] == ["OUT"]


def test_netlist_import_through_api_keeps_library_technology(client, lib, tmp_path):
    cell = _cell("div")  # spiceIn imports the subckt whose name equals the target cell
    netlist = tmp_path / "divider.scs"
    netlist.write_text(f"subckt {cell} IN OUT GND\n  R1 (IN OUT) resistor r=1k\n"
                       f"  R2 (OUT GND) resistor r=3k\n  C1 (OUT GND) capacitor c=1p\nends {cell}\n")
    devmap = tmp_path / "devmap"
    devmap.write_text("devselect := resistor res\ndevselect := capacitor cap\n")
    before = _local_tech_db_size(client, lib)
    client.schematic.import_netlist(lib, cell, netlist, dev_map_file=devmap)
    data = client.schematic.read(lib, cell)
    assert sorted(i["cell"] for i in data["instances"] if i["lib"] == "analogLib") == ["cap", "res", "res"]
    assert _local_tech_db_size(client, lib) == before


def test_check_and_save_reports_schcheck_warning_text(client, lib):
    cell = _cell("floating")
    with client.schematic.create(lib, cell) as sch:
        sch.add(inst("analogLib", "res", "symbol", "R0", 0.0, 0.0, "R0"))
        sch.add(label("R0", "PLUS", "A"))
        sch.add(label("R0", "MINUS", "B"))
    report = client.schematic.check_and_save(lib, cell)
    assert report.check_warning_count > 0
    assert sum(d.severity == "warning" and "schCheck reported" not in d.message
               for d in report.diagnostics) == report.check_warning_count


def test_pdk_finger_count_reaches_netlist_through_callbacks(client, lib, tmp_path):
    from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

    pdk_lib, nmos = _require("VB_TEST_PDK_LIB", "VB_TEST_NMOS_CELL")
    cell = _cell("nmos")
    with client.schematic.create(lib, cell) as sch:
        sch.add(inst(pdk_lib, nmos, "symbol", "M0", 0.0, 0.0, "R0"))
        sch.add_net_label_to_transistor("M0", drain_net="D", gate_net="G", source_net="gnd!", body_net="gnd!")
    _ok(client.open_window(lib, cell))
    # Kits whose w is the total width (e.g. a TotalWidth dimension mode) keep
    # nf=1 when two fingers would drop below the minimum finger width, as the
    # GUI does; doubling the default width is harmless where w is per finger.
    width = client.execute_skill(
        'car(setof(x geGetEditCellView()~>instances x~>name == "M0"))~>w'
    ).output.strip().strip('"')
    doubled = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zA-Z]*)", width)
    extra = {"w": f"{2 * float(doubled[1]):g}{doubled[2]}"} if doubled else {}
    set_instance_params(client, "M0", nf="2", param_filters=None, **extra)
    client.schematic.check_and_save(lib, cell)
    _assert_export_left_nothing(client, cell, client.schematic.export_netlist(lib, cell, tmp_path / "nl"))
    line = next(l for l in (tmp_path / "nl" / "input.scs").read_text().splitlines() if l.startswith("M0 "))
    assert re.search(r"\bnf=2\b", line), line


# ---------------------------------------------------------------- layout and GDS


@pytest.fixture(scope="module")
def layers(client, lib):
    env = (os.getenv("VB_TEST_LAYER"), os.getenv("VB_TEST_LAYER2"))
    if all(env):
        return env
    names = _ok(client.execute_skill(
        f'let((tf) tf = techGetTechFile(ddGetObj("{lib}")) '
        f'when(tf setof(lp tf~>lps lp~>purpose == "drawing" && lp~>valid)~>name))'))
    found = list(dict.fromkeys(re.findall(r'"([^"]+)"', names)))[:2]
    if len(found) < 2:
        pytest.skip(f"{lib} has no technology with two drawing layers; set VB_TEST_LAYER/VB_TEST_LAYER2")
    return tuple(found)


def _layout_shapes(client, lib, cell):
    return _ok(client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{cell}" "layout" nil "r") '
        f'list(length(cv~>shapes) length(cv~>vias) cv~>shapes~>layerName))'))


def test_layout_create_and_modify(client, lib, layers):
    from virtuoso_bridge.virtuoso.layout import layout_create_path as path_
    from virtuoso_bridge.virtuoso.layout import layout_create_rect as rect

    layer, layer2 = layers
    cell = _cell("lay")
    with client.layout.create(lib, cell) as lay:
        lay.add(rect(layer, "drawing", 0, 0, 4, 0.5))
        lay.add(path_(layer2, "drawing", [(0, 1), (4, 1), (4, 3)], 0.3))
    with client.layout.modify(lib, cell) as lay:
        lay.add(rect(layer2, "drawing", 0, 4, 4, 4.5))
    assert _layout_shapes(client, lib, cell).startswith("(3 0")


def test_layout_delete_on_layer_targets_the_edited_cell(client, lib, layers):
    from virtuoso_bridge.virtuoso.layout import layout_create_rect as rect
    from virtuoso_bridge.virtuoso.layout.ops import layout_delete_shapes_on_layer

    layer, layer2 = layers
    cell = _cell("laydel")
    with client.layout.create(lib, cell) as lay:
        lay.add(rect(layer, "drawing", 0, 0, 1, 1))
        lay.add(rect(layer2, "drawing", 2, 2, 3, 3))
    with client.layout.modify(lib, cell) as lay:
        lay.add(layout_delete_shapes_on_layer(layer2, "drawing"))
    assert _layout_shapes(client, lib, cell).startswith("(1 0")


# ---------------------------------------------------------------- standalone Spectre


def test_spectre_rc_transient_matches_analytic(tmp_path):
    from virtuoso_bridge.spectre.runner import SpectreSimulator

    netlist = tmp_path / "rc.scs"
    netlist.write_text(
        "simulator lang=spectre\nglobal 0\n"
        "V0 (VIN 0) vsource type=pulse val0=0 val1=1 delay=1n rise=1p fall=1p width=100n period=200n\n"
        "R0 (VIN VOUT) resistor r=2k\nC0 (VOUT 0) capacitor c=2p\n"
        "tran1 tran stop=30n maxstep=10p\nsave VIN VOUT\n")
    result = SpectreSimulator.from_env(work_dir=tmp_path / "run", timeout=300).run_simulation(netlist, {})
    assert result.ok, result.errors
    t, v = result.data["time"], result.data["VOUT"]
    i = min(range(len(t)), key=lambda k: abs(t[k] - (1e-9 + 4e-9)))
    assert v[i] == pytest.approx(1 - math.exp(-1), abs=0.01)


# ---------------------------------------------------------------- Maestro


@pytest.fixture(scope="module")
def maestro_rc(client, lib):
    if os.getenv("VB_TEST_MAESTRO") != "1":
        pytest.skip("set VB_TEST_MAESTRO=1 to run Maestro checks")
    from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

    cell = _cell("tb_rc")
    _rc(client, lib, cell)
    _ok(client.open_window(lib, cell))
    set_instance_params(client, "V0", vdc="0", acm="1", param_filters=None)
    set_instance_params(client, "R0", r="1k", param_filters=None)
    set_instance_params(client, "C0", c="c_val", param_filters=None)
    session = client.maestro.open_session(lib, cell)
    client.maestro.create_test("AC", lib=lib, cell=cell, session=session)
    client.maestro.set_analysis("AC", "tran", enable=False, session=session)
    client.maestro.set_analysis(
        "AC", "ac", session=session,
        options='(("start" "1") ("stop" "10G") ("incrType" "Logarithmic") '
                '("stepTypeLog" "Points Per Decade") ("dec" "20"))')
    client.maestro.add_output("BW", "AC", output_type="point",
                              expr='bandwidth(mag(VF("/OUT")) 3 "low")', session=session)
    client.maestro.set_spec("BW", "AC", gt="1G", session=session)
    client.maestro.set_var("c_val", "1p,100f", session=session)
    client.maestro.save_setup(lib, cell, session=session)
    client.maestro.close_session(session)
    gui = client.maestro.open_gui_session(lib, cell)
    yield cell, gui
    client.maestro.close_gui_session(gui, save=False)


def test_maestro_sweep_bandwidth_matches_analytic(client, lib, maestro_rc):
    cell, session = maestro_rc
    history, _status = client.maestro.run_and_wait(session=session, timeout=600)
    results = client.maestro.read_results(session, lib=lib, cell=cell, history=history.strip('"'))
    by_c = {p["parameters"]["c_val"]: p["outputs"]["BW"] for p in results["points"]}
    for c_val, farad in (("1p", 1e-12), ("100f", 100e-15)):
        expected = 1 / (2 * math.pi * 1e3 * farad)
        assert float(by_c[c_val]["value"]) == pytest.approx(expected, rel=0.02)
    assert by_c["1p"]["pass_fail"] == "fail" and by_c["100f"]["pass_fail"] == "pass"


def test_maestro_durable_job_reports_completed(client, maestro_rc, tmp_path):
    from virtuoso_bridge.virtuoso.maestro import MaestroJobManager

    _cell_name, session = maestro_rc
    jobs = MaestroJobManager.from_client(client, local_root=tmp_path / "jobs")
    job = jobs.submit(session=session, run_id=f"vb-live-{RUN}")
    deadline = time.monotonic() + 600
    while "completed" not in jobs.log(job) and time.monotonic() < deadline:
        time.sleep(5)
    assert jobs.status(job).state == "completed"
