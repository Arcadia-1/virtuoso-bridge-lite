"""Maestro (ADE Assembler) session management, config reading, and writing."""

from virtuoso_bridge.virtuoso.maestro.lifecycle import (
    open_session,
    close_session,
    find_open_session,
    open_gui_session,
    close_gui_session,
    _purge_maestro_cellviews as purge_maestro_cellviews,
)
from virtuoso_bridge.virtuoso.maestro.reader import (
    snapshot,
    filter_sdb_xml,
    filter_active_state_xml,
    read_results,
    export_waveform,
)
from virtuoso_bridge.virtuoso.maestro.writer import (
    # test
    create_test,
    set_design,
    # analysis
    set_analysis,
    # outputs
    add_output,
    set_spec,
    # variables
    set_var,
    get_var,
    delete_var,
    # parameters (parametric sweep)
    get_parameter,
    set_parameter,
    # env/sim options
    set_env_option,
    set_sim_option,
    # corners
    set_corner,
    setup_corner,
    load_corners,
    # run mode / job control
    set_current_run_mode,
    set_job_control_mode,
    set_job_policy,
    # simulation
    run_simulation,
    run_and_wait,
    # export
    create_netlist_for_corner,
    export_output_view,
    write_script,
    # migration
    migrate_adel_to_maestro,
    migrate_adexl_to_maestro,
    # save
    save_setup,
    # GUI
    open_maestro_gui_with_history,
)

__all__ = [
    # session
    "open_session",
    "close_session",
    "find_open_session",
    "open_gui_session",
    "close_gui_session",
    "purge_maestro_cellviews",
    # read — aggregator (pass output_root to also write disk dump)
    "snapshot",
    # read — XML filters
    "filter_sdb_xml",
    "filter_active_state_xml",
    # read — runtime ops
    "read_results",
    "export_waveform",
    # write - test
    "create_test",
    "set_design",
    # write - analysis
    "set_analysis",
    # write - outputs
    "add_output",
    "set_spec",
    # write - variables
    "set_var",
    "get_var",
    "delete_var",
    # write - parameters
    "get_parameter",
    "set_parameter",
    # write - env/sim options
    "set_env_option",
    "set_sim_option",
    # write - corners
    "set_corner",
    "setup_corner",
    "load_corners",
    # write - run mode / job control
    "set_current_run_mode",
    "set_job_control_mode",
    "set_job_policy",
    # write - simulation
    "run_simulation",
    "run_and_wait",
    # write - export
    "create_netlist_for_corner",
    "export_output_view",
    "write_script",
    # write - migration
    "migrate_adel_to_maestro",
    "migrate_adexl_to_maestro",
    # write - save
    "save_setup",
    # write - GUI
    "open_maestro_gui_with_history",
]
