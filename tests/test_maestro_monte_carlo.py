from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from virtuoso_bridge.virtuoso.maestro import monte_carlo as mc
from virtuoso_bridge.virtuoso.maestro.history import (
    MaestroHistory,
    MaestroHistoryLockResult,
)
from virtuoso_bridge.virtuoso.maestro.monte_carlo import (
    MC_RUN_MODE,
    MaestroMonteCarloError,
    MaestroMonteCarloOutcomeUnknown,
    MonteCarloConfig,
    MonteCarloModule,
    MonteCarloModuleFilter,
    configure_monte_carlo,
    export_monte_carlo_results,
    get_monte_carlo_config,
    run_monte_carlo_and_wait,
)
from virtuoso_bridge.virtuoso.maestro.reader.state import MaestroSessionState


def _result(output: str = "t", *, ok: bool = True, errors=()):
    return SimpleNamespace(ok=ok, output=output, errors=list(errors))


def _payload(*rows: str, mode: str = MC_RUN_MODE, status: str = "ok") -> str:
    rendered = " ".join(rows)
    return f'("{mc._OPTIONS_TAG}" "{status}" "{mode}" ({rendered}))'


def _row(name: str, value: str) -> str:
    return f'("{name}" "{value}")'


def _base_payload(**overrides: str) -> str:
    options = {
        "mcmethod": "all",
        "mcnumpoints": "100",
        "samplingmode": "random",
        "montecarloseed": "12345",
        "mcstartingrunnumber": "1",
        "donominal": "1",
        "saveallplots": "0",
        "saveprocess": "1",
        "savemismatch": "0",
        "mcnumbins": "",
    }
    options.update(overrides)
    return _payload(*(_row(name, value) for name, value in options.items()))


class _Client:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []

    def execute_skill(self, expression: str, **kwargs):
        self.calls.append((expression, kwargs))
        if not self.results:
            raise AssertionError("unexpected execute_skill call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _editing_state(session: str = "fnxSession4") -> MaestroSessionState:
    return MaestroSessionState(
        context="gui",
        access="editing",
        unsaved=False,
        session=session,
        window_num=7,
        application="assembler",
        lib="MC_LIB",
        cell="tb_amp",
        view="maestro_V1",
        title="ADE Assembler Editing: MC_LIB tb_amp maestro_V1",
        current=False,
        source="window_title",
    )


def test_get_config_reads_explicit_session_run_options() -> None:
    client = _Client(_result(_base_payload(
        mcmethod="mismatch",
        mcnumpoints="64",
        samplingmode="lds",
        saveprocess="0",
        futureOption="kept",
    )))

    config = get_monte_carlo_config(client, 'session"4')

    assert config is not None
    assert config.variation == "mismatch"
    assert config.points == 64
    assert config.sampling == "lds"
    assert config.save_process is False
    assert config.extra_options == {"futureOption": "kept"}
    skill, kwargs = client.calls[0]
    assert 'axlGetMainSetupDB("session\\"4")' in skill
    assert f'axlGetRunOptions(vbSdb "{MC_RUN_MODE}")' in skill
    assert "axlGetRunOptionValue" in skill
    assert kwargs == {"timeout": 30}


def test_get_config_parses_module_include_and_exclude() -> None:
    summary = "T%/I0%lib/cell/schematic%Master"
    include = get_monte_carlo_config(_Client(_result(_base_payload(
        dutsummary=summary,
        ignoreflag="0",
    ))), "s")
    exclude = get_monte_carlo_config(_Client(_result(_base_payload(
        dutsummary=summary,
        ignoreflag="1",
    ))), "s")

    assert include is not None and include.module_filter is not None
    assert include.module_filter.mode == "include"
    assert include.module_filter.modules[0].instance == "/I0"
    assert exclude is not None and exclude.module_filter is not None
    assert exclude.module_filter.mode == "exclude"


def test_get_config_accepts_ic618_trailing_module_separator() -> None:
    config = get_monte_carlo_config(_Client(_result(_base_payload(
        dutsummary="T%/I0%lib/cell/schematic%Master#",
        ignoreflag="0",
    ))), "s")

    assert config is not None and config.module_filter is not None
    assert config.module_filter.modules[0].instance == "/I0"


@pytest.mark.parametrize(
    "summary",
    [
        "#T%/I0%lib/cell/schematic%Master",
        "T%/I0%lib/cell/schematic%Master##",
        "T%/I0%lib/cell/schematic%Master##T%/I1%lib/cell/schematic%Master",
    ],
)
def test_get_config_rejects_empty_module_records(summary: str) -> None:
    with pytest.raises(MaestroMonteCarloError, match="Malformed dutSummary"):
        get_monte_carlo_config(_Client(_result(_base_payload(
            dutsummary=summary,
            ignoreflag="0",
        ))), "s")


def test_get_config_distinguishes_unconfigured_mode() -> None:
    raw = f'("{mc._OPTIONS_TAG}" "not_configured" "Single Run" nil)'
    assert get_monte_carlo_config(_Client(_result(raw)), "s") is None


@pytest.mark.parametrize(
    "payload",
    [
        "nil",
        f'("{mc._OPTIONS_TAG}" "ok" "{MC_RUN_MODE}" (("x")))',
        f'("{mc._OPTIONS_TAG}" "ok" "{MC_RUN_MODE}" (("x" t)))',
        f'("{mc._OPTIONS_TAG}" "ok" "{MC_RUN_MODE}" '
        '(("mcnumpoints" "1") ("MCNUMPOINTS" "2")))',
        f'("{mc._OPTIONS_TAG}" "unsupported" "axlGetRunOptions" nil)',
    ],
)
def test_get_config_fails_closed_on_malformed_or_failed_payload(payload: str) -> None:
    with pytest.raises(MaestroMonteCarloError):
        get_monte_carlo_config(_Client(_result(payload)), "s")


def test_get_config_rejects_invalid_known_values() -> None:
    with pytest.raises(MaestroMonteCarloError, match="integer"):
        get_monte_carlo_config(
            _Client(_result(_base_payload(mcnumpoints="many"))), "s",
        )
    with pytest.raises(MaestroMonteCarloError, match="'0' or '1'"):
        get_monte_carlo_config(
            _Client(_result(_base_payload(saveprocess="yes"))), "s",
        )


def test_module_filter_rejects_global_variation_and_relative_paths() -> None:
    with pytest.raises(ValueError, match="absolute"):
        MonteCarloModule(test="T", instance="I0", master="x")
    with pytest.raises(ValueError, match="requires mismatch"):
        MonteCarloConfig(
            variation="global",
            module_filter=MonteCarloModuleFilter(
                mode="include",
                modules=(MonteCarloModule(test="T", instance="/I0", master="x"),),
            ),
        )


def test_config_rejects_known_or_case_duplicate_extra_options() -> None:
    with pytest.raises(ValueError, match="duplicates a known option"):
        MonteCarloConfig(extra_options={"mcNumPoints": "20"})
    with pytest.raises(ValueError, match="unique ignoring case"):
        MonteCarloConfig(extra_options={"futureOption": "1", "FUTUREOPTION": "2"})


@pytest.mark.parametrize("value", ["session\n4", "history\x00", " output\x7f"])
def test_public_names_reject_control_characters(value: str) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        get_monte_carlo_config(_Client(), value)


@pytest.mark.parametrize("value", ["future\nvalue", "future\x00value"])
def test_extra_option_values_reject_control_characters(value: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        MonteCarloConfig(extra_options={"futureOption": value})


def test_extra_option_names_reject_control_characters() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MonteCarloConfig(extra_options={"future\nOption": "1"})


def test_mutation_uses_explicit_session_setup_database() -> None:
    config = MonteCarloConfig(
        variation="mismatch",
        points=25,
        sampling="lds",
        module_filter=MonteCarloModuleFilter(
            mode="exclude",
            modules=(MonteCarloModule(
                test="T", instance="/I0/M1, /I0/M2",
                master="Schematic", kind="Schematic",
            ),),
        ),
        extra_options={"futureOption": 'kept "safe"'},
    )

    skill = mc._mutation_skill('s"4', config, save=True)

    assert f'maeSetCurrentRunMode(?runMode "{MC_RUN_MODE}"' in skill
    assert 'axlGetMainSetupDB("s\\"4")' in skill
    assert "axlGetActiveSetup(vbSdb)" in skill
    assert f'axlPutRunOption(vbActive "{MC_RUN_MODE}" "mcnumpoints")' in skill
    assert 'axlSetRunOptionValue(vbHandle "25")' in skill
    assert f'axlPutRunOption(vbActive "{MC_RUN_MODE}" "samplingmode")' in skill
    assert 'axlSetRunOptionValue(vbHandle "lds")' in skill
    assert f'axlPutRunOption(vbActive "{MC_RUN_MODE}" "ignoreflag")' in skill
    assert 'axlSetRunOptionValue(vbHandle "1")' in skill
    assert 'axlPutRunOption(vbActive "Monte Carlo Sampling" "futureOption")' in skill
    assert 'axlSetRunOptionValue(vbHandle "kept \\"safe\\"")' in skill
    assert "maeSaveSetup" in skill
    assert "maeSetRunOption" not in skill
    assert "ocnxlMonteCarloOptions" not in skill


def test_configure_dry_run_checks_session_without_mutation(monkeypatch) -> None:
    before = MonteCarloConfig(points=100)
    requested = MonteCarloConfig(points=200)
    client = SimpleNamespace(execute_skill=lambda *args, **kwargs: pytest.fail("mutation"))
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: before)
    monkeypatch.setattr(mc, "list_session_states", lambda *args, **kwargs: [_editing_state()])
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: MC_RUN_MODE)

    result = configure_monte_carlo(
        client, requested, session="fnxSession4", dry_run=True,
    )

    assert result.outcome == "dry_run"
    assert result.changed is True
    assert "axlPutRunOption" in result.planned_skill
    assert 'axlGetMainSetupDB("fnxSession4")' in result.planned_skill


def test_configure_dry_run_supports_previously_unconfigured_setup(monkeypatch) -> None:
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mc, "list_session_states", lambda *args, **kwargs: [_editing_state()])
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: "Single Run")

    result = configure_monte_carlo(
        SimpleNamespace(), MonteCarloConfig(points=10),
        session="fnxSession4", dry_run=True,
    )

    assert result.before is None
    assert f'?runMode "{MC_RUN_MODE}"' in result.planned_skill


def test_configure_is_idempotent_when_mode_and_options_already_match(monkeypatch) -> None:
    config = MonteCarloConfig(points=10)
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: MC_RUN_MODE)
    monkeypatch.setattr(
        mc,
        "list_session_states",
        lambda *args, **kwargs: pytest.fail("state probe should not run"),
    )

    result = configure_monte_carlo(
        SimpleNamespace(), config, session="fnxSession4",
    )

    assert result.outcome == "already_satisfied"
    assert result.changed is False


@pytest.mark.parametrize(
    "state, message",
    [
        (_editing_state().model_copy(update={"context": "headless"}), "GUI session"),
        (_editing_state().model_copy(update={"access": "reading"}), "editing mode"),
        (_editing_state().model_copy(update={"unsaved": True}), "unsaved"),
    ],
)
def test_configure_fails_closed_on_unsafe_session(monkeypatch, state, message) -> None:
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: MonteCarloConfig())
    monkeypatch.setattr(mc, "list_session_states", lambda *args, **kwargs: [state])
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: MC_RUN_MODE)
    with pytest.raises(MaestroMonteCarloError, match=message):
        configure_monte_carlo(
            SimpleNamespace(), MonteCarloConfig(points=101),
            session="fnxSession4", dry_run=True,
        )


def test_configure_applies_once_without_retry_and_verifies(monkeypatch) -> None:
    before = MonteCarloConfig(points=100)
    requested = MonteCarloConfig(points=200)
    reads = iter((before, requested))
    modes = iter((MC_RUN_MODE, MC_RUN_MODE))
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: next(reads))
    monkeypatch.setattr(mc, "list_session_states", lambda *args, **kwargs: [_editing_state()])
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: next(modes))
    client = _Client(_result(f'("{mc._MUTATION_TAG}" "ok" t)'))

    result = configure_monte_carlo(
        client, requested, session="fnxSession4", save=False,
    )

    assert result.outcome == "applied"
    assert result.after == requested
    assert result.saved is False
    assert len(client.calls) == 1
    assert client.calls[0][1] == {"timeout": 30, "retry_connect": False}
    assert "maeSaveSetup" not in client.calls[0][0]


def test_configure_does_not_retry_unknown_persistent_write(monkeypatch) -> None:
    requested = MonteCarloConfig(points=200)
    reads = iter((MonteCarloConfig(), requested))
    modes = iter((MC_RUN_MODE, MC_RUN_MODE))
    monkeypatch.setattr(mc, "get_monte_carlo_config", lambda *args, **kwargs: next(reads))
    monkeypatch.setattr(mc, "list_session_states", lambda *args, **kwargs: [_editing_state()])
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: next(modes))
    client = _Client(_result(ok=False, errors=["Socket timeout after 30s"]))

    with pytest.raises(MaestroMonteCarloOutcomeUnknown, match="persistence is unknown"):
        configure_monte_carlo(client, requested, session="fnxSession4")
    assert len(client.calls) == 1


def test_run_requires_mc_mode_then_locks_completed_history(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: MC_RUN_MODE)
    run_kwargs = {}

    def complete(*args, **kwargs):
        run_kwargs.update(kwargs)
        return '"MonteCarlo.8"', "done"

    monkeypatch.setattr(mc, "run_and_wait", complete)
    before = MaestroHistory(name="MonteCarlo.8", locked=False, current=True)
    after = MaestroHistory(name="MonteCarlo.8", locked=True, current=True)
    expected_lock = MaestroHistoryLockResult(
        session="fnxSession4",
        history="MonteCarlo.8",
        requested_locked=True,
        before=before,
        after=after,
        changed=True,
        outcome="applied",
    )
    monkeypatch.setattr(mc, "lock_history", lambda *args, **kwargs: expected_lock)

    result = run_monte_carlo_and_wait(
        SimpleNamespace(), session="fnxSession4", timeout=90, lock_result=True,
    )

    assert result.history == "MonteCarlo.8"
    assert result.status == "done"
    assert result.lock == expected_lock
    assert run_kwargs["run_mode"] == MC_RUN_MODE


def test_run_rejects_non_mc_mode(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: "Single Run")
    with pytest.raises(MaestroMonteCarloError, match="Monte Carlo Sampling"):
        run_monte_carlo_and_wait(SimpleNamespace(), session="s")


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("SKILL error: Socket error: connection reset"),
        TimeoutError("Simulation did not finish within 30s"),
    ],
)
def test_run_reports_unknown_outcome_without_retry(monkeypatch, error) -> None:
    monkeypatch.setattr(mc, "_get_run_mode", lambda *args, **kwargs: MC_RUN_MODE)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(mc, "run_and_wait", fail_once)

    with pytest.raises(MaestroMonteCarloOutcomeUnknown, match="may have started"):
        run_monte_carlo_and_wait(SimpleNamespace(), session="s", timeout=30)
    assert calls == 1


def test_run_preserves_completion_timeout_translation_and_original_receipt() -> None:
    client = _Client(
        _result(f'("{mc._RUN_MODE_TAG}" "ok" "{MC_RUN_MODE}")'),
        _result(),
        _result('"MonteCarlo.9"'),
    )
    client.ssh_runner = SimpleNamespace(run_command=Mock(side_effect=[
        SimpleNamespace(returncode=0, stdout=""),
        TimeoutError("Simulation did not finish within 30s"),
    ]))

    with pytest.raises(MaestroMonteCarloOutcomeUnknown) as caught:
        run_monte_carlo_and_wait(client, session="s", timeout=30)

    cause = caught.value.__cause__
    assert isinstance(cause, TimeoutError)
    assert cause.result.metadata["history"] == "MonteCarlo.9"
    assert cause.result.metadata["session"] == "s"
    assert cause.result.metadata["simulation_start_sent"] is True
    assert sum("maeRunSimulation(" in call[0] for call in client.calls) == 1
    assert len(client.calls) == 3


def test_export_uses_explicit_history_filters_and_output_path() -> None:
    client = _Client(_result(f'("{mc._EXPORT_TAG}" "ok" t)'))

    result = export_monte_carlo_results(
        client,
        "MonteCarlo.2",
        "/tmp/mc csv/",
        session="fnxSession4",
        test="AC",
        corner="C1",
    )

    assert result.output_path == "/tmp/mc csv/"
    skill, kwargs = client.calls[0]
    assert 'axlWriteMonteCarloResultsCSV("fnxSession4" "MonteCarlo.2"' in skill
    assert '?testName "AC"' in skill
    assert '?cornerName "C1"' in skill
    assert '?outputPath "/tmp/mc csv/"' in skill
    assert "?outputName" not in skill
    assert kwargs == {"timeout": 30}
