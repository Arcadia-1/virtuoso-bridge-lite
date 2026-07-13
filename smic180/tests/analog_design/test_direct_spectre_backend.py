import json

import pytest

from analog_design.simulation.direct_spectre import BridgeSpectreRunner, DirectSpectreBackend, SimulationError


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, deck, run_dir):
        self.calls.append((deck, run_dir))
        run_dir.mkdir(parents=True)
        (run_dir / "spectre.out").write_text("fresh run", encoding="utf-8")
        (run_dir / "raw").mkdir()
        (run_dir / "raw" / "stamp").write_text("fresh", encoding="utf-8")
        return dict(self.result)


def good_result():
    return {
        "exit_code": 0,
        "measurements": {"gain": 65.0, "ugbw": 12e6, "slew_rate": 6e6, "supply_current": 200e-6, "output_dc": 1.65},
        "measurement_scopes": {"gain": "open_loop_differential_ac", "ugbw": "open_loop_differential_ac", "slew_rate": "unity_gain_closed_loop_step", "supply_current": "dc_operating_point", "output_dc": "dc_operating_point"},
        "operating_points": {"M_IN_P": {"region": "saturation", "gm": 200e-6, "gds": 2e-6, "vds": 1.0, "vdsat": 0.2, "id": 20e-6}},
        "sample_counts": {"ac": 101, "tran": 500, "op": 1},
    }


def test_backend_creates_fresh_iteration_and_persists_validated_results(tmp_path):
    deck = tmp_path / "design.scs"
    deck.write_text("simulator lang=spectre\n", encoding="utf-8")
    runner = FakeRunner(good_result())
    backend = DirectSpectreBackend(runner, required_measurements=("gain", "ugbw", "slew_rate"), required_analyses=("op", "ac", "tran"))
    result = backend.run(deck, tmp_path / "iterations", 1)
    assert result.success is True
    assert (tmp_path / "iterations" / "0001" / "measurements.json").is_file()
    assert (tmp_path / "iterations" / "0001" / "operating_points.json").is_file()
    assert (tmp_path / "iterations" / "0001" / "measurement_scopes.json").is_file()
    assert result.diagnostics["M_IN_P"]["saturation_margin"] == pytest.approx(0.8)


def test_backend_refuses_existing_iteration_directory(tmp_path):
    deck = tmp_path / "design.scs"
    deck.write_text("deck", encoding="utf-8")
    existing = tmp_path / "iterations" / "0001"
    existing.mkdir(parents=True)
    with pytest.raises(SimulationError, match="already exists"):
        DirectSpectreBackend(FakeRunner(good_result()), ("gain",), ("op",)).run(deck, tmp_path / "iterations", 1)


@pytest.mark.parametrize(
    "mutation,pattern",
    [
        (lambda result: result.update(exit_code=1), "exit code"),
        (lambda result: result["measurements"].pop("gain"), "missing measurement"),
        (lambda result: result["measurements"].update(gain=float("nan")), "finite"),
        (lambda result: result["measurement_scopes"].pop("gain"), "scope"),
        (lambda result: result["sample_counts"].update(ac=0), "samples"),
        (lambda result: result.update(operating_points={}), "operating point"),
    ],
)
def test_backend_rejects_incomplete_or_invalid_results(tmp_path, mutation, pattern):
    result = good_result()
    mutation(result)
    deck = tmp_path / "design.scs"
    deck.write_text("deck", encoding="utf-8")
    backend = DirectSpectreBackend(FakeRunner(result), ("gain",), ("op", "ac"))
    with pytest.raises(SimulationError, match=pattern):
        backend.run(deck, tmp_path / "iterations", 1)
class FakeBridgeResult:
    def __init__(self, data):
        self.ok = True
        self.data = data
        self.errors = ["convergence failure"]
        self.warnings = []
        self.metadata = {"returncode": 0}


class FakeSimulator:
    def __init__(self, run_dir, data):
        self.run_dir = run_dir
        self.data = data

    def run_simulation(self, deck, params):
        (self.run_dir / "spectre.out").write_text("spectre completes with 0 errors, 0 warnings", encoding="utf-8")
        (self.run_dir / f"{deck.stem}.raw").mkdir()
        return FakeBridgeResult(self.data)


def test_bridge_runner_uses_success_status_and_fresh_psf_data_instead_of_false_positive_error_text(tmp_path):
    deck = tmp_path / "design.scs"
    deck.write_text("simulator lang=spectre\n", encoding="utf-8")
    run_dir = tmp_path / "live"
    data = {
        "ac_freq": [1.0, 10.0],
        "ac_VINP": [0.5 + 0j, 0.5 + 0j],
        "ac_VINN": [-0.5 + 0j, -0.5 + 0j],
        "ac_VOUT": [10.0 + 0j, 1.0 + 0j],
        "dc_VOUT": 1.0,
        "dc_VDD": 3.3,
        "dc_VDD_SRC:p": -1e-6,
        "time": [0.0, 1e-6],
        "VOUT": [1.0, 1.1],
        "dc_X_DUT.M1:region": 2.0,
        "dc_X_DUT.M1:gm": 1e-3,
        "dc_X_DUT.M1:gds": 1e-5,
        "dc_X_DUT.M1:vds": 1.0,
        "dc_X_DUT.M1:vdsat": 0.2,
        "dc_X_DUT.M1:id": 1e-4,
    }
    runner = BridgeSpectreRunner(lambda target: FakeSimulator(target, data))

    result = runner.run(deck, run_dir)

    assert result["exit_code"] == 0
    assert result["measurements"]["gain"] == pytest.approx(20.0)
    assert result["raw_dir"] == str(run_dir / "design.raw")
    assert result["backend_errors"] == ["convergence failure"]
