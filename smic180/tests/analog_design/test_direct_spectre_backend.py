import json

import pytest

from analog_design.simulation.direct_spectre import DirectSpectreBackend, SimulationError


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
