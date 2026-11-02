"""Client-bound facade for Maestro operations.

The Maestro implementation remains function-oriented for backwards
compatibility. ``MaestroOps`` binds every public operation that needs a
``VirtuosoClient`` so new code can consistently use ``client.maestro.*``.
Pure SKILL builders and XML/result parsers deliberately remain module-level
functions because they perform no client or remote I/O.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TYPE_CHECKING

from virtuoso_bridge.virtuoso.maestro.engine import get_engine, set_engine

from virtuoso_bridge.virtuoso.maestro.lifecycle import (
    _purge_maestro_cellviews,
    close_gui_session,
    close_session,
    find_open_session,
    open_gui_session,
    open_session,
)
from virtuoso_bridge.virtuoso.maestro.history import (
    get_history,
    list_histories,
    lock_history,
    set_history_lock,
    unlock_history,
)
from virtuoso_bridge.virtuoso.maestro.monte_carlo import (
    configure_monte_carlo,
    export_monte_carlo_results,
    get_monte_carlo_config,
    run_monte_carlo_and_wait,
)
from virtuoso_bridge.virtuoso.maestro.reader import (
    export_waveform,
    get_session_state,
    list_session_states,
    read_results,
    snapshot,
)
from virtuoso_bridge.virtuoso.maestro.waveform_viewer import (
    close_waveform_viewer,
    open_waveform_viewer,
)
from virtuoso_bridge.virtuoso.maestro.writer import (
    add_output,
    create_netlist_for_corner,
    create_test,
    delete_var,
    export_output_view,
    get_parameter,
    get_var,
    load_corners,
    migrate_adel_to_maestro,
    migrate_adexl_to_maestro,
    open_maestro_gui_with_history,
    run_and_wait,
    run_simulation,
    save_setup,
    set_analysis,
    set_corner,
    set_current_run_mode,
    set_design,
    set_env_option,
    set_job_control_mode,
    set_job_policy,
    set_parameter,
    set_sim_option,
    set_spec,
    set_var,
    setup_corner,
    write_script,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


_DELEGATES: dict[str, Callable[..., Any]] = {}


def _client_method(function: Callable[..., Any]) -> Callable[..., Any]:
    """Bind a legacy ``function(client, ...)`` as a facade method."""
    _DELEGATES[function.__name__] = function

    @wraps(function)
    def method(self: "MaestroOps", *args: Any, **kwargs: Any) -> Any:
        return _DELEGATES[function.__name__](self._owner, *args, **kwargs)

    return method


class MaestroOps:
    """Maestro operations attached to :class:`VirtuosoClient` as ``maestro``.

    New code should call this facade, for example
    ``client.maestro.open_session(lib, cell)`` and
    ``client.maestro.set_analysis(test, "ac", session=session)``.
    """

    def __init__(self, owner: "VirtuosoClient") -> None:
        self._owner = owner

    # Session lifecycle
    open_session = _client_method(open_session)
    close_session = _client_method(close_session)
    find_open_session = _client_method(find_open_session)
    open_gui_session = _client_method(open_gui_session)
    close_gui_session = _client_method(close_gui_session)
    purge_maestro_cellviews = _client_method(_purge_maestro_cellviews)

    # Simulation history retention
    list_histories = _client_method(list_histories)
    get_history = _client_method(get_history)
    set_history_lock = _client_method(set_history_lock)
    lock_history = _client_method(lock_history)
    unlock_history = _client_method(unlock_history)

    # Monte Carlo configuration, execution, and export
    get_monte_carlo_config = _client_method(get_monte_carlo_config)
    configure_monte_carlo = _client_method(configure_monte_carlo)
    run_monte_carlo_and_wait = _client_method(run_monte_carlo_and_wait)
    export_monte_carlo_results = _client_method(export_monte_carlo_results)

    # Read results and waveforms
    snapshot = _client_method(snapshot)
    get_session_state = _client_method(get_session_state)
    list_session_states = _client_method(list_session_states)
    read_results = _client_method(read_results)
    export_waveform = _client_method(export_waveform)
    open_waveform_viewer = _client_method(open_waveform_viewer)
    close_waveform_viewer = _client_method(close_waveform_viewer)

    # Test and design setup
    create_test = _client_method(create_test)
    set_design = _client_method(set_design)
    set_analysis = _client_method(set_analysis)
    add_output = _client_method(add_output)
    set_spec = _client_method(set_spec)
    set_var = _client_method(set_var)
    get_var = _client_method(get_var)
    delete_var = _client_method(delete_var)
    get_parameter = _client_method(get_parameter)
    set_parameter = _client_method(set_parameter)
    set_env_option = _client_method(set_env_option)
    set_sim_option = _client_method(set_sim_option)
    set_corner = _client_method(set_corner)
    setup_corner = _client_method(setup_corner)
    load_corners = _client_method(load_corners)

    # Run configuration and execution
    get_engine = _client_method(get_engine)
    set_engine = _client_method(set_engine)
    set_current_run_mode = _client_method(set_current_run_mode)
    set_job_control_mode = _client_method(set_job_control_mode)
    set_job_policy = _client_method(set_job_policy)
    run_simulation = _client_method(run_simulation)
    run_and_wait = _client_method(run_and_wait)

    # Export, migration, and persistence
    create_netlist_for_corner = _client_method(create_netlist_for_corner)
    export_output_view = _client_method(export_output_view)
    write_script = _client_method(write_script)
    migrate_adel_to_maestro = _client_method(migrate_adel_to_maestro)
    migrate_adexl_to_maestro = _client_method(migrate_adexl_to_maestro)
    save_setup = _client_method(save_setup)
    open_maestro_gui_with_history = _client_method(open_maestro_gui_with_history)


__all__ = ["MaestroOps"]
