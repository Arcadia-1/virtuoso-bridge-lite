from __future__ import annotations

import json
import shlex

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.sos import SOSOps, SOSCellViewTarget
from virtuoso_bridge.virtuoso.sos.cellview import _skill


ROOT = "/work area"
PATH = "lib/cell/schematic_Vt"
DIRECTORY = f"{ROOT}/{PATH}"
MASTER = f"{DIRECTORY}/sch.oa"


def _skill_ok(output):
    return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)


def _state(state="O", change="M", revision="3", *, lock="-", newer="-", rso="-",
           path=PATH, kind="p"):
    return CommandResult(
        0, f"{kind}\t{state}\t{change}\t{lock}\t{newer}\t{rso}\t{revision}\t./{path}\n", "",
    )


def _nobj(status=5, kind=3, revision="3", *, modified="0", ci_modified="0",
          out_of_date="0", reference="", path=PATH, attributes=None):
    values = attributes if attributes is not None else {
        "CurrentVer": revision,
        "Modified": modified,
        "CiModified": ci_modified,
        "OutOfDate": out_of_date,
        "Reference": reference,
        "Revision": revision,
    }
    rows = ["!nObjStatus! 1", "!Record!", "./" + path, str(status), str(kind)]
    for key, value in values.items():
        rows.extend([key, str(len(value.encode("utf-8")))])
        if value:
            rows.append(value)
    return CommandResult(0, "\n".join(rows) + "\n", "")


class _Client:
    _timeout = 60

    def __init__(self, *, before=None, after=None, dirty=False, view_type="schematic",
                 mutation=None, native=False, native_dispatch=None, native_result=None,
                 maestro=False, discard=None, nobj=None):
        self.sos_runner = self
        self.commands = []
        self.skill_calls = []
        self.states = iter([
            before if before is not None else _state(),
            after if after is not None else _state("-", "-", "4"),
        ])
        self.nobj = iter(nobj if nobj is not None else [])
        self.resolution = _skill_ok(
            f'("ok" {json.dumps(DIRECTORY)} {json.dumps(MASTER)} '
            f'{json.dumps(view_type)} {"t" if dirty else "nil"})'
        )
        self.mutation = mutation if mutation is not None else _skill_ok('("ok")')
        self.native = native
        self.native_dispatch = (native_dispatch if native_dispatch is not None
                                else _skill_ok('("scheduled" "sos_design_manager")'))
        self.native_result = (native_result if native_result is not None
                              else _skill_ok('("ok" t t t nil)'))
        self.maestro = maestro
        self.discard = discard if discard is not None else CommandResult(0, "", "")
        self.root = CommandResult(0, ROOT + "\n", "")
        self.sos = SOSOps(self)

    def run_command(self, command, *, timeout=None):
        self.commands.append(command)
        if command.startswith("vb_sos="):
            return CommandResult(0, "/opt/sos/bin/soscmd\n", "")
        parts = shlex.split(command)
        assert parts[:2] == ["cd", DIRECTORY if "findwaroot" in parts else ROOT]
        if parts[-1] == "findwaroot":
            return self.root
        if parts[3:5] == ["/opt/sos/bin/soscmd", "discardco"]:
            assert parts[5:] == ["./" + PATH]
            if isinstance(self.discard, Exception):
                raise self.discard
            return self.discard
        if parts[3:5] == ["/opt/sos/bin/soscmd", "nobjstatus"]:
            assert parts[5:-1] == [
                "-gaRevision", "-gaCurrentVer", "-gaModified",
                "-gaCiModified", "-gaOutOfDate",
                "-gaReference",
            ]
            state = next(self.nobj)
            if isinstance(state, Exception):
                raise state
            return state
        assert parts[3:6] == ["/opt/sos/bin/soscmd", "status", "-Nhdr"]
        assert parts[-1] == "./" + PATH
        state = next(self.states)
        if isinstance(state, Exception):
            raise state
        return state

    def execute_skill(self, code, timeout=None, *, retry_connect=True):
        if not retry_connect:
            self.skill_calls.append((code, {"one_shot": True}))
            if "VB_SOS_NATIVE_DISPATCH" in code:
                if isinstance(self.native_dispatch, Exception):
                    raise self.native_dispatch
                return self.native_dispatch
            if isinstance(self.mutation, Exception):
                raise self.mutation
            return self.mutation
        if "ddCheckin(" in code or "ddCheckout(" in code:
            raise AssertionError("mutations must disable connection retries")
        self.skill_calls.append((code, {}))
        if "VB_SOS_NATIVE_PROBE" in code:
            return _skill_ok(f'("ok" {"t" if self.native else "nil"})')
        if "VB_SOS_NATIVE_RESULT" in code:
            if isinstance(self.native_result, Exception):
                raise self.native_result
            if isinstance(self.native_result, list):
                if len(self.native_result) > 1:
                    return self.native_result.pop(0)
                return self.native_result[0]
            return self.native_result
        if "VB_SOS_MAESTRO_PROBE" in code:
            return _skill_ok(f'("ok" {"t" if self.maestro else "nil"})')
        return self.resolution

    @property
    def writes(self):
        return [code for code, _ in self.skill_calls
                if "ddCheckin(" in code or "ddCheckout(" in code
                or "VB_SOS_NATIVE_DISPATCH" in code] + [
                    command for command in self.commands if " discardco " in command]


def _ci(client, **kwargs):
    return client.sos.checkin_cellview("lib", "cell", "schematic_Vt", message="Saved changes", **kwargs)


def _cancel(client, **kwargs):
    return client.sos.cancel_checkout_cellview("lib", "cell", "schematic_Vt", **kwargs)


def _unmanaged(kind="d", path=PATH):
    return CommandResult(0, f"{kind}\t?\t?\t?\t?\t?\t?\t./{path}\n", "")


def _registration_client(*, states=None, **kwargs):
    client = _Client(**kwargs)
    client.states = iter(states if states is not None else [
        _unmanaged(), _unmanaged(), _state("-", "-", "1"),
    ])
    return client


def _register(client, **kwargs):
    return client.sos.register_cellview("lib", "cell", "schematic_Vt", message="Initial version", **kwargs)


@pytest.mark.parametrize("kind", ["d", "p"])
@pytest.mark.parametrize("view_type", ["schematic", "schematicSymbol", "maskLayout"])
def test_register_unmanaged_oa_view_via_gdm(kind, view_type):
    client = _registration_client(view_type=view_type, states=[
        _unmanaged(kind), _unmanaged(kind), _state("-", "-", "1"),
    ])
    result = _register(client)
    assert result.outcome == "success" and result.action == "register"
    assert result.before.state == "?" and result.before.revision == "?"
    assert result.after.revision == "1" and result.after.object_type == "p"
    assert len(client.writes) == 1
    assert 'ddCheckin(vbFile "Initial version")' in client.writes[0]
    assert "ddCheckout(" not in client.writes[0]
    status_commands = [shlex.split(cmd) for cmd in client.commands if "status" in cmd]
    assert len(status_commands) == 3
    assert all(parts[-3:] == ["-sall", "-sNr", "./" + PATH] for parts in status_commands)
    assert all("-sr" not in parts for parts in status_commands)


def test_register_dry_run_reads_explicit_unmanaged_state_without_write():
    client = _registration_client(states=[_unmanaged()])
    result = _register(client, dry_run=True)
    assert result.outcome == "dry_run" and result.before.revision == "?"
    assert not client.writes
    assert "'ddCheckin" in client.skill_calls[0][0]


@pytest.mark.parametrize("before", [
    _state("-", "-", "1"), _state("O", "M", "1"), _state("N", "?", "?"),
    _state("X", "?", "?"), _state("W", "M", "1"), _state(lock="L"),
    _unmanaged("D"), _unmanaged("P"), _unmanaged("s"), _unmanaged("f"),
    _state("?", "?", "1", kind="d", lock="?"),
])
def test_register_rejects_existing_reference_unpopulated_and_ambiguous_objects(before):
    client = _registration_client(states=[before])
    result = _register(client)
    assert result.outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("response,expected", [
    (CommandResult(0, "!! Warning: No objects selected to display status.\n", ""), "blocked"),
    (CommandResult(0, "", ""), "failed"),
    (CommandResult(1, "", "License unavailable"), "failed"),
    (CommandResult(0, _unmanaged().stdout * 2, ""), "failed"),
    (_unmanaged(path=PATH + "/sibling"), "failed"),
    (TimeoutError("status timed out"), "failed"),
])
def test_register_never_treats_missing_or_failed_status_as_new(response, expected):
    client = _registration_client(states=[response])
    result = _register(client)
    assert result.outcome == expected and not client.writes


def test_register_detects_changed_state_on_final_refresh():
    client = _registration_client(states=[_unmanaged(), _state("-", "-", "1")])
    result = _register(client)
    assert result.outcome == "blocked" and "changed" in result.diagnostics[0]
    assert not client.writes


@pytest.mark.parametrize("view_type", ["maestro", "config", "verilogA"])
def test_register_rejects_unsupported_views_before_cli(view_type):
    client = _registration_client(view_type=view_type)
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


def test_register_rejects_dirty_view_before_cli():
    client = _registration_client(dirty=True)
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


@pytest.mark.parametrize("target", [
    ("Calibre", "cell", "layout"), ("lib", "calibre_cell", "symbol"),
    ("lib", "cell", "Calibre_PEX"),
])
def test_register_cannot_bypass_calibre_guard(target):
    client = _registration_client()
    result = client.sos.register_cellview(*target, message="Initial version")
    assert result.outcome == "blocked"
    assert not client.skill_calls and not client.commands


def test_register_rejects_resolved_calibre_path():
    client = _registration_client()
    client.resolution = _skill_ok('("ok" "/work/calibre/view" "/work/calibre/view/sch.oa" "schematic" nil)')
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


@pytest.mark.parametrize("message", ["", " ", "two\nlines", "bad\x00"])
def test_register_requires_message_before_any_remote_call(message):
    client = _registration_client()
    with pytest.raises(ValueError):
        client.sos.register_cellview("lib", "cell", "schematic", message=message)
    assert not client.skill_calls and not client.commands


@pytest.mark.parametrize("after", [
    _unmanaged(), _state("-", "-", "?"), _state("-", "-", "-"),
    _state("-", "-", "invalid"), _state("-", "-", " 1 "),
    _state("O", "-", "1"), _state("-", "M", "1"),
    _state("-", "-", "1", kind="d"), _state("-", "-", "1", lock="L"),
])
def test_register_requires_confirmed_managed_checked_in_postcondition(after):
    client = _registration_client(states=[_unmanaged(), _unmanaged(), after])
    assert _register(client).outcome == "failed"
    assert len(client.writes) == 1


def test_register_does_not_hardcode_first_revision_number():
    client = _registration_client(states=[_unmanaged(), _unmanaged(), _state("-", "-", "1.1")])
    assert _register(client).ok


@pytest.mark.parametrize("mutation", [TimeoutError("response lost"), ConnectionResetError("reset")])
def test_register_lost_response_reads_state_but_never_retries(mutation):
    client = _registration_client(mutation=mutation)
    result = _register(client)
    assert result.outcome == "unknown" and result.after.revision == "1"
    assert len(client.writes) == 1


def test_register_gdm_failure_is_not_masked_by_post_state():
    client = _registration_client(mutation=_skill_ok('("failed" "GDM returned nil.")'))
    assert _register(client).outcome == "failed"
    assert len(client.writes) == 1


def test_register_missing_post_state_is_unknown():
    client = _registration_client(states=[_unmanaged(), _unmanaged(), TimeoutError("status")])
    assert _register(client).outcome == "unknown"
    assert len(client.writes) == 1


def test_register_repeat_is_blocked_without_another_revision():
    client = _registration_client(states=[_state("-", "-", "1")])
    assert _register(client).outcome == "blocked"
    assert not client.writes


def test_normal_checkin_still_rejects_unmanaged_view():
    client = _Client(before=_unmanaged())
    assert _ci(client).outcome == "blocked"
    assert not client.writes


def test_status_uses_server_queried_nobjstatus_fallback():
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(status=3, revision="7", modified="1", out_of_date="1")],
    )
    result = client.sos.status_cellview("lib", "cell", "schematic_Vt")
    assert result.outcome == "success"
    assert result.after is None
    assert (result.before.object_type, result.before.state, result.before.change,
            result.before.lock, result.before.newer, result.before.rso,
            result.before.revision) == (
                "p", "O", "M", "-", "N", "-", "7",
            )
    fallback = [command for command in client.commands if " nobjstatus " in command]
    assert len(fallback) == 1 and " -ucl " not in fallback[0]


def test_nobjstatus_allows_unavailable_cimodified_attribute():
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(status=3, revision="7", attributes={
            "CurrentVer": "7", "Revision": "7", "Modified": "1",
            "OutOfDate": "0", "Reference": "",
        })],
    )
    result = client.sos.status_cellview("lib", "cell", "schematic_Vt")
    assert result.outcome == "success"
    assert result.before.state == "O" and result.before.change == "M"


def test_nobjstatus_rejects_invalid_cimodified_when_returned():
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(status=3, revision="7", ci_modified="invalid")],
    )
    result = client.sos.status_cellview("lib", "cell", "schematic_Vt")
    assert result.outcome == "failed"
    assert "invalid CiModified" in result.diagnostics[0]


def test_cancel_checkout_can_verify_through_nobjstatus_fallback():
    denied = CommandResult(1, "", "status denied")
    client = _Client(nobj=[
        _nobj(status=3, revision="5"),
        _nobj(status=3, revision="5"),
        _nobj(status=5, revision="5"),
    ])
    client.states = iter([denied, denied, denied])
    result = _cancel(client)
    assert result.outcome == "success"
    assert result.before.revision == result.after.revision == "5"
    assert len(client.writes) == 1


def test_register_can_verify_through_nobjstatus_fallback():
    denied = CommandResult(1, "", "status denied")
    client = _registration_client()
    client.states = iter([denied, denied, denied])
    client.nobj = iter([
        _nobj(status=2), _nobj(status=2), _nobj(status=5, revision="1"),
    ])
    result = _register(client)
    assert result.outcome == "success"
    assert result.after.revision == "1" and result.after.rso == "-"
    assert len(client.writes) == 1


@pytest.mark.parametrize("action", ["co", "cancel_co", "ci"])
def test_nobjstatus_reference_objects_never_trigger_writes(action):
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(status=5 if action == "co" else 3, reference="linked")],
    )
    if action == "co":
        result = client.sos.checkout_cellview("lib", "cell", "schematic_Vt")
    elif action == "cancel_co":
        result = _cancel(client)
    else:
        result = _ci(client)
    assert result.outcome == "blocked" and result.before.rso == "R"
    assert not client.writes


def test_nobjstatus_unlocked_checkout_remains_blocked():
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(status=6, revision="5")],
    )
    result = _cancel(client)
    assert result.outcome == "blocked"
    assert result.before.lock == "?"
    assert not client.writes


def test_malformed_nobjstatus_fallback_never_triggers_write():
    client = _Client(
        before=CommandResult(1, "", "status denied"),
        nobj=[_nobj(attributes={
            "CurrentVer": "4", "Revision": "5", "Modified": "0",
            "CiModified": "0", "OutOfDate": "0", "Reference": "",
        })],
    )
    result = _cancel(client)
    assert result.outcome == "failed"
    assert "inconsistent revision" in result.diagnostics[0]
    assert not client.writes


@pytest.mark.parametrize('notice', [
    '** The flags and attributes have been updated.\n',
    "## All servers for project 'FCGMASH' are online.\n",
])
def test_custom_view_status_resolves_workarea_and_refresh_notice(notice):
    state = _state("-", "-", "4")
    client = _Client(before=state._replace(stdout=state.stdout + notice))
    result = client.sos.status_cellview("lib", "cell", "schematic_Vt")
    assert result.ok
    assert result.target.view_type == "schematic"
    assert result.target.workarea == ROOT
    assert result.target.master == MASTER
    assert result.before.revision == "4"
    assert not client.writes


@pytest.mark.parametrize('notice', [
    "## All servers for project 'FCGMASH' are offline.\n",
    "## ERROR: cache unavailable\n",
    "## All servers for project 'FCGMASH' are online. unexpected\n",
])
def test_unknown_notices_remain_errors(notice):
    state = _state('-', '-', '4')
    client = _Client(before=state._replace(stdout=state.stdout + notice))
    assert not client.sos.status_cellview('lib', 'cell', 'schematic_Vt').ok
    assert not client.writes


@pytest.mark.parametrize("action", ["co", "ci"])
def test_dry_run_does_not_dispatch_gdm(action):
    client = _Client(before=_state("-", "-") if action == "co" else _state())
    result = (client.sos.checkout_cellview("lib", "cell", "schematic_Vt", dry_run=True)
              if action == "co" else _ci(client, dry_run=True))
    assert result.outcome == "dry_run"
    assert not client.writes


def test_checkin_checks_gdm_and_revision_postcondition():
    client = _Client()
    result = _ci(client)
    assert result.outcome == "success"
    assert result.before.revision == "3" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert 'ddCheckin(vbFile "Saved changes")' in client.writes[0]
    assert json.loads(json.dumps(result.to_dict()))["ok"] is True


def test_checkin_prefers_sos_design_manager_when_available():
    client = _Client(native=True)
    result = _ci(client)
    assert result.outcome == "success"
    assert result.before.revision == "3" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert "VB_SOS_NATIVE_DISPATCH" in client.writes[0]
    assert "ddCheckin(" not in client.writes[0]
    assert '"lib" "cell" "schematic_Vt"' in client.writes[0]
    assert 'vbForm->descItem->value="Saved changes"' in client.writes[0]
    assert "vbSosBridgeFormsBefore=hiFormList()" in client.writes[0]
    assert "vbForm->cellViewListBox->choices" in client.writes[0]
    assert "hiIsFormDisplayed(vbForm)" in client.writes[0]
    assert "hiSetCurrentForm(vbForm)" in client.writes[0]
    assert "SosObjStatusCreateFormMain" not in client.writes[0]


def test_native_checkin_lost_dispatch_reply_is_unknown_and_not_retried():
    client = _Client(native=True, native_dispatch=TimeoutError("response lost"))
    result = _ci(client)
    assert result.outcome == "unknown" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert not any("VB_SOS_NATIVE_RESULT" in code for code, _ in client.skill_calls)


def test_native_checkin_requires_completion_receipt():
    client = _Client(native=True, native_result=TimeoutError("completion unavailable"))
    result = _ci(client, timeout=0.01)
    assert result.outcome == "unknown" and result.after.revision == "4"
    assert len(client.writes) == 1


def test_native_failed_completion_stops_polling_without_retry():
    before = _state()
    client = _Client(
        before=before,
        after=before,
        native=True,
        native_result=_skill_ok('("ok" t nil nil "SOS form failed.")'),
    )
    result = _ci(client, timeout=5)
    assert result.outcome == "failed"
    assert result.diagnostics == ("SOS form failed.",)
    assert len(client.writes) == 1
    status_commands = [command for command in client.commands if " status " in command]
    assert len(status_commands) == 2


def test_native_waits_for_callback_after_sos_state_changes():
    complete = _skill_ok('("ok" t t t nil)')
    client = _Client(
        native=True,
        native_result=[_skill_ok('("ok" nil t nil nil)'), complete, complete],
    )
    client.states = iter([_state(), _state("-", "-", "4"), _state("-", "-", "4")])
    result = _ci(client)
    assert result.outcome == "success"
    status_commands = [command for command in client.commands if " status " in command]
    assert len(status_commands) == 3


def test_gdm_fallback_is_blocked_while_maestro_is_open():
    client = _Client(native=False, maestro=True)
    result = _ci(client)
    assert result.outcome == "blocked"
    assert "Maestro" in result.diagnostics[0]
    assert not client.writes


def test_register_prefers_sos_design_manager_when_available():
    client = _registration_client(native=True)
    result = _register(client)
    assert result.outcome == "success" and result.after.revision == "1"
    assert len(client.writes) == 1
    assert "VB_SOS_NATIVE_DISPATCH" in client.writes[0]
    assert 'vbForm->descItem->value="Initial version"' in client.writes[0]


def test_checkout_verified():
    client = _Client(before=_state("-", "-"), after=_state("O", "-"))
    result = client.sos.checkout_cellview("lib", "cell", "schematic_Vt")
    assert result.ok and result.after.state == "O"
    assert len(client.writes) == 1


def test_cancel_checkout_releases_only_clean_package_without_force():
    client = _Client()
    client.states = iter([
        _state("O", "-", "3"), _state("O", "-", "3"), _state("-", "-", "3"),
    ])
    result = _cancel(client)
    assert result.outcome == "success" and result.after.revision == "3"
    assert len(client.writes) == 1
    command = shlex.split(client.writes[0])
    assert command[3:] == ["/opt/sos/bin/soscmd", "discardco", "./" + PATH]
    assert "-F" not in command


@pytest.mark.parametrize("state,expected", [
    (_state("O", "M"), "blocked"),
    (_state("-", "-"), "noop"),
    (_state("O", "-", lock="L"), "blocked"),
])
def test_cancel_checkout_preconditions_never_discard(state, expected):
    client = _Client(before=state)
    result = _cancel(client)
    assert result.outcome == expected
    assert not client.writes


def test_cancel_checkout_rechecks_sos_state_before_discard():
    client = _Client()
    client.states = iter([_state("O", "-"), _state("O", "M")])
    result = _cancel(client)
    assert result.outcome == "blocked"
    assert not client.writes


def test_cancel_checkout_lost_reply_is_unknown_and_never_retried():
    client = _Client(discard=TimeoutError("response lost"))
    client.states = iter([
        _state("O", "-", "3"), _state("O", "-", "3"), _state("-", "-", "3"),
    ])
    result = _cancel(client)
    assert result.outcome == "unknown" and result.after.state == "-"
    assert len(client.writes) == 1


def test_cancel_checkout_nonzero_reply_with_matching_state_is_unknown():
    client = _Client(discard=CommandResult(1, "", "server error"))
    client.states = iter([
        _state("O", "-", "3"), _state("O", "-", "3"), _state("-", "-", "3"),
    ])
    result = _cancel(client)
    assert result.outcome == "unknown" and "server error" in result.diagnostics[0]
    assert len(client.writes) == 1


def test_cancel_checkout_requires_same_revision_after_discard():
    client = _Client()
    client.states = iter([
        _state("O", "-", "3"), _state("O", "-", "3"), _state("-", "-", "4"),
    ])
    result = _cancel(client)
    assert result.outcome == "failed"
    assert len(client.writes) == 1


def test_cancel_checkout_dry_run_does_not_discard():
    client = _Client(before=_state("O", "-"))
    assert _cancel(client, dry_run=True).outcome == "dry_run"
    assert not client.writes


def test_cancel_checkout_blocks_unsaved_connected_buffer():
    client = _Client(dirty=True, before=_state("O", "-"))
    result = _cancel(client)
    assert result.outcome == "blocked" and not client.writes


def test_cancel_checkout_blocks_calibre_before_remote_access():
    client = _Client()
    result = client.sos.cancel_checkout_cellview("lib", "cell", "Calibre_PEX")
    assert result.outcome == "blocked"
    assert not client.skill_calls and not client.commands


@pytest.mark.parametrize("view_type", ["schematic", "schematicSymbol", "maskLayout"])
@pytest.mark.parametrize("action", ["co", "ci"])
def test_supported_oa_types_use_gdm(view_type, action):
    client = _Client(view_type=view_type,
                     before=_state("-", "-") if action == "co" else _state(),
                     after=_state("O", "-") if action == "co" else _state("-", "-", "4"))
    result = (_ci(client) if action == "ci"
              else client.sos.checkout_cellview("lib", "cell", "custom_view"))
    assert result.outcome == "success"
    assert len(client.writes) == 1


@pytest.mark.parametrize("view_type", ["maestro", "config", "verilog", "verilogA"])
@pytest.mark.parametrize("action", ["status", "co", "ci"])
def test_other_types_are_read_only(view_type, action):
    client = _Client(view_type=view_type)
    if action == "status":
        result = client.sos.status_cellview("lib", "cell", "custom_view")
    elif action == "co":
        result = client.sos.checkout_cellview("lib", "cell", "custom_view")
    else:
        result = _ci(client)
    assert result.outcome == ("success" if action == "status" else "blocked")
    assert result.target.unsaved is None
    assert not client.writes


@pytest.mark.parametrize("target", [
    ("lib", "cell", "calibre"), ("lib", "cell", "Calibre_PEX"),
    ("CALIBRE_LIB", "cell", "layout"), ("lib", "cell_calibre", "schematic"),
])
@pytest.mark.parametrize("dry_run", [True, False])
def test_calibre_checkin_blocked_before_remote_access(target, dry_run):
    client = _Client()
    result = client.sos.checkin_cellview(*target, message="Never submit", dry_run=dry_run)
    assert result.outcome == "blocked" and "Calibre" in result.diagnostics[0]
    assert not client.skill_calls and not client.commands


def test_calibre_status_remains_read_only():
    client = _Client()
    assert client.sos.status_cellview("lib", "cell", "calibre").outcome == "success"
    assert not client.writes


@pytest.mark.parametrize("directory,master", [
    ("/work/Calibre/lib/cell/layout", "/work/Calibre/lib/cell/layout/layout.oa"),
    (DIRECTORY, DIRECTORY + "/calibre.oa"),
])
def test_calibre_resolved_path_blocks_checkin_even_for_allowed_oa_type(directory, master):
    client = _Client(view_type="maskLayout")
    client.resolution = _skill_ok(
        f'("ok" {json.dumps(directory)} {json.dumps(master)} "maskLayout" nil)'
    )
    result = _ci(client)
    assert result.outcome == "blocked" and not client.writes and not client.commands


def test_checkout_missing_package_after_gdm_is_not_success():
    client = _Client(before=_state("-", "-"), after=_state("O", "!"))
    assert client.sos.checkout_cellview("lib", "cell", "schematic_Vt").outcome == "failed"


def test_non_oa_master_blocks_writes():
    client = _Client()
    client.resolution = _skill_ok(
        f'("ok" {json.dumps(DIRECTORY)} {json.dumps(DIRECTORY + "/sch.cdb")} "schematic" nil)'
    )
    assert _ci(client).outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("action,state,expected", [
    ("co", _state(), "noop"),
    ("ci", _state("O", "-"), "noop"),
    ("ci", _state("-", "-"), "blocked"),
    ("co", _state("-", "M"), "blocked"),
    ("co", _state("-", "-", lock="L"), "blocked"),
    ("ci", _state(lock="L"), "blocked"),
    ("ci", _state(kind="P"), "blocked"),
    ("ci", _state("W"), "blocked"),
    ("ci", _state(change="!"), "blocked"),
])
def test_preconditions_and_noops_never_write(action, state, expected):
    client = _Client(before=state)
    result = (_ci(client) if action == "ci" else client.sos.checkout_cellview("lib", "cell", "schematic_Vt"))
    assert result.outcome == expected
    assert not client.writes


@pytest.mark.parametrize("dirty,view_type", [(True, "schematic"), (False, "maestro"), (False, "config")])
def test_unsaved_or_unsupported_view_blocks_writes(dirty, view_type):
    client = _Client(dirty=dirty, view_type=view_type)
    assert _ci(client).outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("reply,outcome", [
    (_skill_ok('("failed" "GDM returned nil.")'), "failed"),
    (_skill_ok('("blocked" "Target changed.")'), "blocked"),
    (_skill_ok('("ok") trailing'), "unknown"),
    (_skill_ok('("ok" "extra")'), "unknown"),
    (_skill_ok('("CHECKIN_OK")'), "unknown"),
    (VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Socket timeout"]), "unknown"),
    (ConnectionResetError("lost response"), "unknown"),
])
def test_failure_or_ambiguous_reply_is_never_retried(reply, outcome):
    client = _Client(mutation=reply)
    result = _ci(client)
    assert result.outcome == outcome
    assert len(client.writes) == 1
    assert result.after.revision == "4"


@pytest.mark.parametrize("after,outcome", [
    (_state("O", "M"), "failed"),
    (_state("-", "-", "3"), "failed"),
    (TimeoutError("SSH status unavailable"), "unknown"),
])
def test_gdm_success_alone_is_not_operation_success(after, outcome):
    client = _Client(after=after)
    assert _ci(client).outcome == outcome
    assert len(client.writes) == 1


@pytest.mark.parametrize("status", [
    _state(path="lib/other/schematic_Vt"),
    _state()._replace(stdout=_state().stdout * 2),
    _state()._replace(stdout="** Error: refresh failed\n" + _state().stdout),
    _state()._replace(stdout="pOM--- 3 ./" + PATH),
    CommandResult(1, "error", "SOS unreachable"),
])
def test_untrusted_status_does_not_trigger_write(status):
    client = _Client(before=status)
    assert _ci(client).outcome == "failed"
    assert not client.writes


@pytest.mark.parametrize("action", ["status", "ci"])
def test_unmanaged_object_reports_unavailable_without_a_write(action):
    client = _Client(before=CommandResult(
        0, "** The flags and attributes have been updated.\n"
        "!! Warning: No objects selected to display status.\n", "",
    ))
    result = (_ci(client) if action == "ci"
              else client.sos.status_cellview("lib", "cell", "schematic_Vt"))
    assert result.outcome == "blocked" and "unmanaged" in result.diagnostics[0]
    assert not client.writes


def test_workarea_mismatch_does_not_trigger_write():
    client = _Client()
    client.root = CommandResult(0, "/other/workarea\n", "")
    assert _ci(client).outcome == "failed"
    assert not client.writes


@pytest.mark.parametrize("output", [
    '("ok" "/work area/lib/cell/schematic_Vt" "/other/sch.oa" "schematic" nil)',
    '("ok" "relative" "relative/sch.oa" "schematic" nil)',
    '("ok" "/work/../other" "/work/../other/sch.oa" "schematic" nil)',
    '("ok" "/work" "/work/sch.oa" "schematic" "nil")',
    '("ok")',
    '("blocked")',
    '(("ok"))',
])
def test_invalid_resolution_fails_before_ssh_or_mutation(output):
    client = _Client()
    client.resolution = _skill_ok(output)
    assert _ci(client).outcome == "failed"
    assert not client.commands and not client.writes


@pytest.mark.parametrize("message", ["", "   ", "hello\nworld", "bad\x00"])
def test_invalid_message_rejected_without_remote_access(message):
    client = _Client()
    with pytest.raises(ValueError):
        client.sos.checkin_cellview("lib", "cell", "schematic", message=message)
    assert not client.skill_calls


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_rejected_without_remote_access(timeout):
    client = _Client()
    with pytest.raises(ValueError):
        _ci(client, timeout=timeout)
    assert not client.skill_calls


def test_skill_escapes_identifiers_message_and_rechecks_paths_and_dirty_flag():
    target = SOSCellViewTarget('lib"name', 'cell\\name', "schematic_Vt", DIRECTORY, MASTER, "schematic")
    code = _skill(target, "ci", 'Fix "gain"\\test')
    assert 'ddGetObj("lib\\"name")' in code
    assert '"cell\\\\name"' in code
    assert 'ddCheckin(vbFile "Fix \\"gain\\"\\\\test")' in code
    assert code.index("Target path or view type changed") < code.index("ddCheckin(")
    assert code.index("when(vbDirty") < code.index("ddCheckin(")
    assert "dbIsCellViewModified" in code
    assert "dbSave" not in code and "schCheck" not in code


def test_native_checkin_reads_fresh_post_completion_state_not_stale_snapshot(monkeypatch):
    from virtuoso_bridge.virtuoso.sos import cellview as sos_cellview

    monkeypatch.setattr(sos_cellview.time, "sleep", lambda _s: None)
    complete = _skill_ok('("ok" t t t nil)')
    # Poll 1: still running -> intermediate status reads stale rev "3".
    # Poll 2: native completes -> must perform a fresh status read returning rev "4".
    client = _Client(
        native=True,
        native_result=[_skill_ok('("ok" nil t nil nil)'), complete, complete],
    )
    client.states = iter([_state("O", "M", "3"), _state("O", "M", "3"), _state("-", "-", "4")])

    result = _ci(client)

    assert result.outcome == "success"
    assert result.ok
    assert result.before.revision == "3"
    assert result.after.revision == "4"
    assert len(client.writes) == 1


@pytest.mark.parametrize(
    ("dispatch_reply", "expected_outcome", "expected_message"),
    [
        (
            _skill_ok('("blocked" "A modal form is already active in the connected CIW.")'),
            "blocked",
            "modal form",
        ),
        (
            _skill_ok('("failed" "SOS Design Manager forms are unavailable.")'),
            "failed",
            "forms are unavailable",
        ),
    ],
)
def test_native_blocked_or_failed_dispatch_exits_immediately_without_waiting(
    monkeypatch, dispatch_reply, expected_outcome, expected_message,
):
    from virtuoso_bridge.virtuoso.sos import cellview as sos_cellview

    def fail_sleep(_seconds):
        raise AssertionError("should not sleep when native dispatch did not schedule")

    monkeypatch.setattr(sos_cellview.time, "sleep", fail_sleep)
    client = _Client(
        before=_state("O", "M", "3"),
        after=_state("O", "M", "3"),
        native=True,
        native_dispatch=dispatch_reply,
        native_result=_skill_ok('("ok" nil nil nil nil)'),
    )

    result = _ci(client, timeout=60)

    assert result.outcome == expected_outcome
    assert expected_message in result.diagnostics[0]
    assert not any("VB_SOS_NATIVE_RESULT" in code for code, _ in client.skill_calls)


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_read_post_state_zero_or_expired_timeout_raises_timeout_error_not_assertion(timeout):
    from virtuoso_bridge.virtuoso.sos.cellview import SOSCellViewState, _read_post_state
    from virtuoso_bridge.virtuoso.sos.workarea import SOSWorkarea

    client = _Client(native=True)
    area = SOSWorkarea(client, ROOT, soscmd="/opt/sos/bin/soscmd")
    target = SOSCellViewTarget("lib", "cell", "schematic_Vt", DIRECTORY, MASTER, "schematic", ROOT, PATH)
    before = SOSCellViewState("p", "O", "M", "-", "-", "-", "3")

    with pytest.raises(TimeoutError, match="Timed out waiting for SOS Design Manager completion"):
        _read_post_state(client, area, target, "ci", before, timeout, native=True)
