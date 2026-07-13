from analog_design.live import create_backend
from analog_design.simulation.direct_spectre import BridgeSpectreRunner


def test_default_live_backend_requires_only_metrics_proven_by_current_testbench(tmp_path):
    backend = create_backend(tmp_path, simulator_factory=lambda run_dir: object())

    assert isinstance(backend.runner, BridgeSpectreRunner)
    assert backend.required_measurements == (
        "gain",
        "ugbw",
        "output_dc",
        "supply_current",
        "power",
        "open_loop_slew_rate",
    )
    assert "slew_rate" not in backend.required_measurements
    assert backend.required_analyses == ("op", "ac", "tran")