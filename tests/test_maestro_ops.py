from __future__ import annotations

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import MaestroOps
from virtuoso_bridge.virtuoso.maestro import ops as maestro_ops


def test_virtuoso_client_exposes_maestro_ops() -> None:
    client = VirtuosoClient.local()

    assert isinstance(client.maestro, MaestroOps)


def test_maestro_ops_exposes_every_public_client_bound_operation() -> None:
    expected = {
        "open_session", "close_session", "find_open_session",
        "open_gui_session", "close_gui_session", "purge_maestro_cellviews",
        "list_histories", "get_history", "set_history_lock",
        "lock_history", "unlock_history",
        "get_monte_carlo_config", "configure_monte_carlo",
        "run_monte_carlo_and_wait", "export_monte_carlo_results",
        "snapshot", "get_session_state", "list_session_states",
        "read_results", "export_waveform",
        "open_waveform_viewer", "close_waveform_viewer",
        "create_test", "set_design", "set_analysis", "add_output", "set_spec",
        "set_var", "get_var", "delete_var", "get_parameter", "set_parameter",
        "set_env_option", "set_sim_option", "set_corner", "setup_corner", "load_corners",
        "set_current_run_mode", "set_job_control_mode", "set_job_policy",
        "get_engine", "set_engine",
        "run_simulation", "run_and_wait", "create_netlist_for_corner",
        "export_output_view", "write_script", "migrate_adel_to_maestro",
        "migrate_adexl_to_maestro", "save_setup", "open_maestro_gui_with_history",
    }

    assert {name for name in expected if hasattr(MaestroOps, name)} == expected


def test_maestro_ops_forwards_owner_and_arguments(monkeypatch) -> None:
    owner = object()
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake_open(client: object, *args: object, **kwargs: object) -> str:
        calls.append((client, args, kwargs))
        return "fnxSession7"

    monkeypatch.setitem(maestro_ops._DELEGATES, "open_session", fake_open)

    result = MaestroOps(owner).open_session("demoLib", "tb_amp")

    assert result == "fnxSession7"
    assert calls == [(owner, ("demoLib", "tb_amp"), {})]


def test_maestro_ops_forwards_keyword_only_configuration(monkeypatch) -> None:
    owner = object()
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake_set_analysis(client: object, *args: object, **kwargs: object) -> str:
        calls.append((client, args, kwargs))
        return "t"

    monkeypatch.setitem(maestro_ops._DELEGATES, "set_analysis", fake_set_analysis)

    result = MaestroOps(owner).set_analysis(
        "AC",
        "ac",
        enable=False,
        options='(("stop" "10G"))',
        session="fnxSession7",
    )

    assert result == "t"
    assert calls == [
        (
            owner,
            ("AC", "ac"),
            {
                "enable": False,
                "options": '(("stop" "10G"))',
                "session": "fnxSession7",
            },
        )
    ]
