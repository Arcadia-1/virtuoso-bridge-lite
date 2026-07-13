import sys
import types

from analog_design.cli import main
from analog_design.workflow import DesignWorkflow
from test_direct_spectre_backend import FakeRunner, good_result
from test_ir_builder import load_spec
from analog_design.simulation.direct_spectre import DirectSpectreBackend


def prepare(tmp_path):
    load_spec(tmp_path)
    run_dir = tmp_path / "run"
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", run_dir)
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    workflow.build_ir()
    workflow.render_netlist()
    return run_dir


def test_cli_simulate_uses_injected_live_backend_and_freezes(tmp_path, monkeypatch):
    run_dir = prepare(tmp_path)
    module = types.ModuleType("fake_designer_live")
    module.create_backend = lambda _run_dir: DirectSpectreBackend(FakeRunner(good_result()), ("gain", "ugbw", "slew_rate"), ("op", "ac", "tran"))
    sys.modules[module.__name__] = module
    monkeypatch.setenv("ANALOG_DESIGN_LIVE_MODULE", module.__name__)
    try:
        assert main(["simulate", "--run-dir", str(run_dir), "--iteration", "1"]) == 0
        assert main(["freeze", "--run-dir", str(run_dir)]) == 0
    finally:
        sys.modules.pop(module.__name__, None)
    assert DesignWorkflow.resume(run_dir).state.current == "candidate_frozen"
    assert (run_dir / "windows_sim" / "measurement_scopes.json").is_file()
