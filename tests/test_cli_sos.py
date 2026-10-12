from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import virtuoso_bridge
from virtuoso_bridge import cli
from virtuoso_bridge.virtuoso.sos import SOSCellViewResult, SOSCellViewTarget


def _install(monkeypatch, outcome="dry_run"):
    calls = []

    class Ops:
        def __getattr__(self, method):
            def invoke(lib, cell, view, **kwargs):
                calls.append((method, lib, cell, view, kwargs))
                print("SSH diagnostic")
                return SOSCellViewResult("ci", outcome, SOSCellViewTarget(lib, cell, view))
            return invoke

    def factory(*, profile=None):
        calls.append(("profile", profile))
        return SimpleNamespace(sos=Ops())

    monkeypatch.setattr(virtuoso_bridge.VirtuosoClient, "from_env", factory)
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: print("environment diagnostic"))
    return calls


def test_cli_routes_profile_env_message_and_clean_json(monkeypatch, capsys):
    calls = _install(monkeypatch)
    env = []
    monkeypatch.setattr(cli, "set_runtime_env_file", env.append)
    rc = cli.main(["sos", "ci", "lib", "cell", "schematic_Vt", "-m", "Fix gain",
                   "--dry-run", "--json", "-p", "worker1", "--env", "custom.env", "--timeout", "12"])
    captured = capsys.readouterr()
    assert rc == 0 and json.loads(captured.out)["outcome"] == "dry_run"
    assert "SSH diagnostic" in captured.err
    assert env == ["custom.env"]
    assert calls == [("profile", "worker1"), ("checkin_cellview", "lib", "cell", "schematic_Vt",
                    {"message": "Fix gain", "dry_run": True, "timeout": 12})]


@pytest.mark.parametrize("outcome,code", [("success", 0), ("noop", 0), ("blocked", 1), ("failed", 1), ("unknown", 3)])
def test_cli_exit_status(monkeypatch, capsys, outcome, code):
    _install(monkeypatch, outcome)
    assert cli.main(["sos", "co", "lib", "cell", "schematic", "--json"]) == code
    assert json.loads(capsys.readouterr().out)["outcome"] == outcome


def test_cli_status_does_not_forward_mutation_flags(monkeypatch, capsys):
    calls = _install(monkeypatch, "success")
    assert cli.main(["sos", "status", "lib", "cell", "schematic"]) == 0
    assert calls[-1][0] == "status_cellview"
    assert calls[-1][-1] == {"timeout": 60}


def test_cli_cancel_checkout_routes_dry_run_profile_and_executable(monkeypatch, capsys):
    calls = _install(monkeypatch)
    rc = cli.main([
        "sos", "cancel-co", "lib", "cell", "schematic_Vt", "--dry-run", "--json",
        "-p", "worker1", "--soscmd", "/site/sos wrapper",
    ])
    assert rc == 0 and json.loads(capsys.readouterr().out)["outcome"] == "dry_run"
    assert calls == [("profile", "worker1"), (
        "cancel_checkout_cellview", "lib", "cell", "schematic_Vt",
        {"dry_run": True, "timeout": 60, "soscmd": "/site/sos wrapper"},
    )]


def test_cli_requires_explicit_view_and_checkin_message():
    for args in (["sos", "co", "lib", "cell"], ["sos", "ci", "lib", "cell", "schematic"],
                 ["sos", "register", "lib", "cell", "schematic"],
                 ["sos", "register", "lib", "cell", "-m", "Initial"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2


def test_cli_register_routes_message_dry_run_profile_and_executable(monkeypatch, capsys):
    calls = _install(monkeypatch)
    rc = cli.main(["sos", "register", "lib", "new_cell", "schematic_Vt", "-m", "Initial version",
                   "--dry-run", "--json", "-p", "worker1", "--soscmd", "/site/sos wrapper"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["outcome"] == "dry_run"
    assert calls == [("profile", "worker1"), ("register_cellview", "lib", "new_cell", "schematic_Vt", {
        "message": "Initial version", "dry_run": True, "timeout": 60, "soscmd": "/site/sos wrapper",
    })]


def test_human_status_includes_target_state_and_revision(capsys):
    cli._print_result({"target": {"lib": "LIB", "cell": "CELL", "view": "symbol", "workarea": "/wa"},
                       "before": {"state": "O", "revision": "4", "change": "M", "lock": "-", "newer": "-", "rso": "-"}})
    text = capsys.readouterr().out
    assert "LIB/CELL/symbol" in text and "workarea: /wa" in text
    assert "checked out" in text and "revision=4" in text and "change=M" in text


@pytest.mark.parametrize("action,outcome,code", [("doctor", "success", 0), ("reconcile", "unknown", 3)])
def test_diagnostic_cli_routes_read_only_commands(monkeypatch, capsys, tmp_path, action, outcome, code):
    calls = []
    payload = {"ok": code == 0, "action": action, "outcome": outcome}
    def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        return payload
    monkeypatch.setattr(
        virtuoso_bridge.VirtuosoClient,
        "from_env",
        lambda **kw: SimpleNamespace(
            sos=SimpleNamespace(diagnose_cellview=invoke, reconcile_cellview=invoke)
        ),
    )
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: None)
    args = ["sos", action, "lib", "cell", "schematic", "--json", "--soscmd", "/opt/soscmd"]
    if action == "reconcile":
        from tests.test_sos_diagnostics import receipt
        previous = receipt()
        previous["target"]["view"] = "schematic"
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(previous), encoding="utf-8-sig")
        args += ["--receipt", str(path)]
    assert cli.main(args) == code
    assert json.loads(capsys.readouterr().out) == payload
    assert calls[0][0] == ("lib", "cell", "schematic")
    assert calls[0][1]["soscmd"] == "/opt/soscmd"
    if action == "reconcile":
        assert calls[0][1]["receipt"] == previous


def test_reconcile_connection_failure_preserves_unknown(monkeypatch, capsys, tmp_path):
    from tests.test_sos_diagnostics import receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt()))
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: None)
    def offline(**kwargs):
        raise RuntimeError("CIW busy")
    monkeypatch.setattr(virtuoso_bridge.VirtuosoClient, "from_env", offline)
    assert cli.main(["sos", "reconcile", "lib", "cell", "schematic_Vt", "--receipt", str(path), "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "unknown" and result["assessment"] == "unavailable"
    assert not result["operation_confirmed"] and "CIW busy" in result["diagnostics"][0]


@pytest.mark.parametrize("action,method", [
    ("session-doctor", "diagnose_session_cellview"),
    ("lock-info", "lock_info_cellview"),
])
def test_cli_routes_session_and_lock_queries(monkeypatch, capsys, action, method):
    calls = []
    payload = {"ok": True, "action": action.replace("-", "_"), "outcome": "success"}

    class Result:
        def to_dict(self):
            return payload

    def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        return Result() if action == "lock-info" else payload

    monkeypatch.setattr(
        virtuoso_bridge.VirtuosoClient,
        "from_env",
        lambda **kw: SimpleNamespace(sos=SimpleNamespace(**{method: invoke})),
    )
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: None)
    assert cli.main(["sos", action, "lib", "cell", "schematic", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert calls == [(('lib', 'cell', 'schematic'), {'timeout': 60})]


def test_cli_lock_query_failure_is_structured_blocked(monkeypatch, capsys):
    def invoke(*args, **kwargs):
        raise RuntimeError("malformed server lock record")

    monkeypatch.setattr(
        virtuoso_bridge.VirtuosoClient,
        "from_env",
        lambda **kw: SimpleNamespace(
            sos=SimpleNamespace(lock_info_cellview=invoke),
        ),
    )
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: None)
    assert cli.main([
        "sos", "lock-info", "lib", "cell", "schematic", "--json",
    ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "blocked"
    assert result["lock"] is None
    assert result["diagnostics"] == ["malformed server lock record"]


def test_cli_session_restart_requires_confirmation_and_routes_force(monkeypatch, capsys):
    calls = []

    def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "action": "session_restart", "outcome": "dry_run"}

    monkeypatch.setattr(
        virtuoso_bridge.VirtuosoClient,
        "from_env",
        lambda **kw: SimpleNamespace(sos=SimpleNamespace(restart_session_cellview=invoke)),
    )
    monkeypatch.setattr(cli, "set_runtime_env_file", lambda value: None)
    assert cli.main(["sos", "session-restart", "lib", "cell", "schematic"]) == 1
    assert "ERROR: SOS session-restart requires --dry-run or --yes" in capsys.readouterr().err
    assert not calls

    assert cli.main([
        "sos", "session-restart", "lib", "cell", "schematic", "--dry-run",
        "--force-cadence-disconnect", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "dry_run"
    assert calls == [(('lib', 'cell', 'schematic'), {
        'dry_run': True, 'force_cadence_disconnect': True, 'timeout': 60,
    })]
