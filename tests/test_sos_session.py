from __future__ import annotations

import shlex

from virtuoso_bridge.transport.ssh import CommandResult
from tests.test_sos_cellview import PATH, ROOT, _Client, _nobj, _state


def _lock_nobj(*, status=5, owner="", checkout_time="", checkout_path="",
               workarea=ROOT, revision="3"):
    return _nobj(status=status, revision=revision, attributes={
        "CurrentVer": revision,
        "Modified": "0",
        "CiModified": "0",
        "OutOfDate": "0",
        "Reference": "",
        "Revision": revision,
        "CheckedOutBy": owner,
        "CheckOutTime": checkout_time,
        "chkout_path": checkout_path,
        "WaRoot": workarea,
    })


class SessionClient(_Client):
    def __init__(self, *, running=("1",), native_status=None, nobj=None,
                 exit_replies=None, **kwargs):
        super().__init__(**kwargs)
        self.query_values = {
            "is_running": iter(running),
            "is_offline": iter(["0"] * 8),
            "is_nowin": iter(["2"] * 8),
            "wa_root": iter([ROOT] * 8),
            "project": iter(["project"] * 8),
            "server": iter(["server"] * 8),
            "rso": iter(["main"] * 8),
            "last_update_time": iter(["2026/09/29 10:00:00"] * 8),
        }
        self.native_status = native_status or _state("-", "-", "3")
        self.session_nobj = iter(nobj or [_lock_nobj()] * 8)
        self.exit_replies = iter(exit_replies or [])

    def run_command(self, command, *, timeout=None):
        self.commands.append(command)
        if command.startswith("vb_sos="):
            return CommandResult(0, "/opt/sos/bin/soscmd\n", "")
        parts = shlex.split(command)
        assert parts[:3] == ["cd", ROOT if parts[1] == ROOT else parts[1], "&&"]
        assert parts[3] == "/opt/sos/bin/soscmd"
        if parts[4] == "query":
            return CommandResult(0, next(self.query_values[parts[5]]) + "\n", "")
        if parts[4] == "nobjstatus":
            return next(self.session_nobj)
        if parts[4] == "status":
            return self.native_status
        if parts[4] == "exitsos":
            return next(self.exit_replies)
        raise AssertionError(parts)


def test_session_doctor_checks_existing_session_without_mutation():
    client = SessionClient()
    result = client.sos.diagnose_session_cellview("lib", "cell", "schematic_Vt")
    assert result["ok"] and result["session"]["running"] == 1
    assert result["session"]["project"] == "project"
    assert result["target"]["workarea"] == ROOT
    assert all(check["ok"] for check in result["checks"])
    assert not any(" exitsos" in command for command in client.commands)


def test_session_doctor_stopped_does_not_start_sos():
    client = SessionClient(running=("0",))
    result = client.sos.diagnose_session_cellview("lib", "cell", "schematic_Vt")
    assert not result["ok"] and result["session"]["running"] == 0
    assert not any(" nobjstatus " in command or " status " in command
                   for command in client.commands)


def test_session_doctor_distinguishes_native_status_failure_from_server_query():
    client = SessionClient(native_status=CommandResult(1, "", "status denied"))
    result = client.sos.diagnose_session_cellview("lib", "cell", "schematic_Vt")
    checks = {check["name"]: check for check in result["checks"]}
    assert not result["ok"]
    assert checks["server_object_status"]["ok"]
    assert not checks["native_status"]["ok"]


def test_lock_info_reports_exact_other_workarea_owner():
    client = SessionClient(nobj=[_lock_nobj(
        status=4, owner="other", checkout_time="2026/09/29 09:00:00",
        checkout_path="/other/wa/lib/cell/schematic_Vt", workarea="/other/wa",
    )])
    result = client.sos.lock_info_cellview("lib", "cell", "schematic_Vt").to_dict()
    assert result["lock"] == {
        "scope": "other_workarea",
        "owner": "other",
        "checkout_time": "2026/09/29 09:00:00",
        "checkout_path": "/other/wa/lib/cell/schematic_Vt",
        "workarea": "/other/wa",
    }
    assert result["before"]["lock"] == "L"


def test_lock_info_accepts_unavailable_attributes_for_unlocked_object():
    client = SessionClient(nobj=[_nobj(
        status=5, revision="3", attributes={
            "CurrentVer": "3",
            "Modified": "0",
            "OutOfDate": "0",
            "Reference": "",
            "Revision": "3",
        },
    )])
    result = client.sos.lock_info_cellview("lib", "cell", "schematic_Vt").to_dict()
    assert result["lock"] == {
        "scope": "none",
        "owner": "",
        "checkout_time": "",
        "checkout_path": "",
        "workarea": "",
    }


def test_session_restart_dry_run_never_exitsos():
    client = SessionClient(native_status=CommandResult(1, "", "status denied"))
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", dry_run=True,
    )
    assert result["ok"] and result["outcome"] == "dry_run"
    maestro_probe = next(
        code for code, _options in client.skill_calls
        if "VB_SOS_MAESTRO_PROBE" in code
    )
    assert maestro_probe.startswith("prog((vbSessions vbSessionsResult)\n")
    assert "vbSessionsResult=errset(maeGetSessions() nil)" in maestro_probe
    assert 'return(list("failed" "maeGetSessions failed."))' in maestro_probe
    assert "&& maeGetSessions()" not in maestro_probe
    assert not any(" exitsos" in command for command in client.commands)


def test_session_restart_normal_refusal_requires_explicit_force():
    client = SessionClient(
        running=("1", "1"), native_status=CommandResult(1, "", "status denied"),
        exit_replies=[CommandResult(1, "", "Cadence is connected")],
    )
    result = client.sos.restart_session_cellview("lib", "cell", "schematic_Vt")
    assert result["outcome"] == "blocked"
    assert sum(" exitsos" in command for command in client.commands) == 1
    assert not any("exitsos -F" in command for command in client.commands)


def test_session_restart_force_is_one_shot_and_reverified():
    client = SessionClient(
        running=("1", "1", "0", "1"),
        native_status=CommandResult(1, "", "status denied"),
        nobj=[_lock_nobj(), _lock_nobj(), _lock_nobj()],
        exit_replies=[
            CommandResult(1, "", "Cadence is connected"),
            CommandResult(0, "", ""),
        ],
    )
    original = client.native_status

    def run_command(command, *, timeout=None):
        if " status " in command and sum(" exitsos" in item for item in client.commands) >= 2:
            client.native_status = _state("-", "-", "3")
        return SessionClient.run_command(client, command, timeout=timeout)

    client.run_command = run_command
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", force_cadence_disconnect=True,
    )
    assert original.returncode == 1
    assert result["ok"] and result["outcome"] == "success"
    assert sum(" exitsos" in command for command in client.commands) == 2
    assert sum("exitsos -F" in command for command in client.commands) == 1


def test_session_restart_unknown_normal_exit_never_forces_or_retries():
    class LostReplyClient(SessionClient):
        def run_command(self, command, *, timeout=None):
            if " exitsos" in command:
                self.commands.append(command)
                raise TimeoutError("reply lost")
            return super().run_command(command, timeout=timeout)

    client = LostReplyClient(native_status=CommandResult(1, "", "status denied"))
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", force_cadence_disconnect=True,
    )
    assert result["outcome"] == "unknown"
    assert sum(" exitsos" in command for command in client.commands) == 1


def test_session_restart_forced_exit_nonzero_requeries_and_marks_stopped_as_unknown():
    client = SessionClient(
        running=("1", "1", "0"),
        native_status=CommandResult(1, "", "status denied"),
        exit_replies=[
            CommandResult(1, "", "Cadence is connected"),
            CommandResult(1, "", "exitsos -F exited with warning"),
        ],
    )
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", force_cadence_disconnect=True,
    )
    assert result["ok"] is False
    assert result["outcome"] == "unknown"
    assert sum(" query is_running" in command for command in client.commands) == 3
    assert any("state changed" in diag for diag in result["diagnostics"])


def test_session_restart_forced_exit_nonzero_with_unavailable_requery_is_unknown():
    class FailingRequeryClient(SessionClient):
        def run_command(self, command, *, timeout=None):
            if " query is_running" in command and sum(" exitsos" in c for c in self.commands) >= 2:
                self.commands.append(command)
                raise TimeoutError("ssh lost")
            return super().run_command(command, timeout=timeout)

    client = FailingRequeryClient(
        running=("1", "1"),
        native_status=CommandResult(1, "", "status denied"),
        exit_replies=[
            CommandResult(1, "", "Cadence is connected"),
            CommandResult(1, "", "forced exit error"),
        ],
    )
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", force_cadence_disconnect=True,
    )
    assert result["ok"] is False
    assert result["outcome"] == "unknown"
    assert any("forced exit error" in diag for diag in result["diagnostics"])
    assert any("unavailable" in diag for diag in result["diagnostics"])


def test_session_restart_forced_exit_nonzero_confirmed_still_running_stays_blocked():
    client = SessionClient(
        running=("1", "1", "1"),
        native_status=CommandResult(1, "", "status denied"),
        exit_replies=[
            CommandResult(1, "", "Cadence is connected"),
            CommandResult(1, "", "Forced exit refused"),
        ],
    )
    result = client.sos.restart_session_cellview(
        "lib", "cell", "schematic_Vt", force_cadence_disconnect=True,
    )
    assert result["ok"] is False
    assert result["outcome"] == "blocked"
    assert sum(" query is_running" in command for command in client.commands) == 3
    assert "Forced exit refused" in result["diagnostics"]
