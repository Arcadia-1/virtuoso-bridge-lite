import json
import math

import pytest

from analog_opt.parameters import ParameterSpace, ParameterSpec
from analog_opt.search import SearchConfig, run_search


class CountingSpace(ParameterSpace):
    def __init__(self, specs):
        super().__init__(specs)
        self.calls = 0
    def materialize(self, values):
        self.calls += 1
        return super().materialize(values)


class Evaluator:
    def __init__(self):
        self.seen = []
    def evaluate(self, run_dir, candidate_id, candidate):
        from analog_opt.evaluator import EvaluationResult
        self.seen.append((candidate_id, candidate))
        return EvaluationResult(candidate_id, float(candidate["x"]), True, {}, {}, None)


def space():
    return CountingSpace([ParameterSpec("x", "design_variable", 10.0, 20.0)])


def test_seeded_random_search_is_bounded_deterministic_and_atomic(tmp_path):
    s1, e1 = space(), Evaluator()
    result1 = run_search(tmp_path, s1, e1, SearchConfig(method="random", evaluations=5, seed=7))
    s2, e2 = space(), Evaluator()
    result2 = run_search(tmp_path / "other", s2, e2, SearchConfig(method="random", evaluations=5, seed=7))
    assert e1.seen == e2.seen
    assert s1.calls == 5
    assert all(10 <= item[1]["x"] <= 20 for item in e1.seen)
    assert result1.best.objective == min(r.objective for r in result1.history)
    assert result1.best.objective == result2.best.objective
    assert len(json.loads((tmp_path / "search_history.json").read_text())["history"]) == 5


def test_resume_adds_candidates_without_repeating_ids(tmp_path):
    s, evaluator = space(), Evaluator()
    run_search(tmp_path, s, evaluator, SearchConfig(method="random", evaluations=3, seed=11))
    result = run_search(tmp_path, s, evaluator, SearchConfig(method="random", evaluations=5, seed=11), resume=True)
    ids = [item.candidate_id for item in result.history]
    assert ids == ["candidate-%06d" % i for i in range(5)]
    assert len(set(ids)) == 5
    assert s.calls == 5


def test_resume_rejects_incompatible_configuration(tmp_path):
    run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 1))
    with pytest.raises(ValueError, match="resume"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 3, 2), resume=True)


def test_nonfinite_objectives_are_not_best(tmp_path):
    class BadEvaluator(Evaluator):
        def evaluate(self, run_dir, candidate_id, candidate):
            from analog_opt.evaluator import EvaluationResult
            return EvaluationResult(candidate_id, float("nan"), True, {}, {}, None)
    result = run_search(tmp_path, space(), BadEvaluator(), SearchConfig("random", 2, 1))
    assert result.best is None and result.all_failed
    assert all(math.isfinite(item.objective) for item in result.history)


def test_scipy_receives_unit_bounds_and_materializes_once(tmp_path):
    captured = {}
    def de(objective, bounds, **kwargs):
        captured["bounds"] = bounds
        objective([0.25])
        objective([0.75])
    s = space()
    run_search(tmp_path, s, Evaluator(), SearchConfig("scipy", 2, 4), differential_evolution=de)
    assert captured["bounds"] == [(0.0, 1.0)] and s.calls == 2


def test_turbo_is_lazy_and_requires_injected_runner(tmp_path):
    with pytest.raises(ImportError):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("turbo", 1, 1))


def test_all_explicit_failures_have_no_best(tmp_path):
    class FailedEvaluator(Evaluator):
        def evaluate(self, run_dir, candidate_id, candidate):
            from analog_opt.evaluator import EvaluationResult
            return EvaluationResult(candidate_id, 99.0, False, {}, {}, {"category": "convergence"})
    result = run_search(tmp_path, space(), FailedEvaluator(), SearchConfig("random", 2, 3))
    assert result.best is None and result.all_failed


def test_resume_rejects_tampered_candidate_ids(tmp_path):
    run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 1))
    path = tmp_path / "search_history.json"
    data = json.loads(path.read_text())
    data["history"][0]["candidate_id"] = "candidate-999999"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="candidate IDs"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 3, 1), resume=True)


def test_history_records_normalized_vectors_and_best_artifact(tmp_path):
    result = run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 9))
    data = json.loads((tmp_path / "search_history.json").read_text())
    assert all(len(item["normalized_vector"]) == 1 for item in data["history"])
    best = json.loads((tmp_path / "best_candidate.json").read_text())
    assert best["status"] == "success"
    assert best["candidate_id"] == result.best.candidate_id
    assert best["objective"] == result.best.objective
    assert best["parameters"]["x"] >= 10.0


def test_all_failed_writes_explicit_null_best(tmp_path):
    class Failed(Evaluator):
        def evaluate(self, run_dir, candidate_id, candidate):
            from analog_opt.evaluator import EvaluationResult
            return EvaluationResult(candidate_id, 10.0, False, {}, {}, {"category": "failed"})
    run_search(tmp_path, space(), Failed(), SearchConfig("random", 1, 1))
    best = json.loads((tmp_path / "best_candidate.json").read_text())
    assert best == {"best": None, "status": "all_failed"}


def test_run_lock_rejects_concurrent_search(tmp_path):
    (tmp_path / ".search.lock").mkdir()
    with pytest.raises(RuntimeError, match="already active"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 1, 1))


def test_run_lock_is_released_after_failure(tmp_path):
    class ExplodingSpace:
        specs = [object()]
        def materialize(self, vector):
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        run_search(tmp_path, ExplodingSpace(), Evaluator(), SearchConfig("random", 1, 1))
    assert not (tmp_path / ".search.lock").exists()


@pytest.mark.parametrize("mutation", [
    lambda data: data.update({"dimension": "1"}),
    lambda data: data["history"][0].update({"success": 1}),
    lambda data: data["history"][0].update({"metrics": []}),
    lambda data: data["history"][0].update({"normalized_vector": [1.1]}),
    lambda data: data["history"][0].update({"normalized_vector": []}),
])
def test_resume_strictly_validates_history_fields(tmp_path, mutation):
    run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 1, 5))
    path = tmp_path / "search_history.json"
    data = json.loads(path.read_text())
    mutation(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="history|resume"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 5), resume=True)


def test_resume_rejects_nonstandard_json_constants(tmp_path):
    run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 1, 5))
    path = tmp_path / "search_history.json"
    path.write_text(path.read_text().replace('"objective":', '"objective": NaN, "old_objective":'))
    with pytest.raises(ValueError, match="history"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 5), resume=True)


def test_random_resume_validates_recorded_vector(tmp_path):
    run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 1, 5))
    path = tmp_path / "search_history.json"
    data = json.loads(path.read_text())
    data["history"][0]["normalized_vector"] = [0.25]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="random sequence"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig("random", 2, 5), resume=True)


@pytest.mark.parametrize("method", ["scipy", "turbo"])
def test_nonrandom_resume_is_explicitly_unsupported(tmp_path, method):
    history = {"config": {"method": method, "evaluations": 1, "seed": 2}, "dimension": 1, "history": [{
        "candidate_id": "candidate-000000", "objective": 1.0, "success": True,
        "metrics": {}, "specs": {}, "metadata": {}, "failure": None,
        "normalized_vector": [0.5], "physical_candidate": {"x": 15.0}
    }]}
    (tmp_path / "search_history.json").write_text(json.dumps(history))
    with pytest.raises(ValueError, match="does not support resume"):
        run_search(tmp_path, space(), Evaluator(), SearchConfig(method, 2, 2), resume=True)


def test_scipy_stops_exactly_at_budget_and_sets_safe_options(tmp_path):
    captured = {}
    def fake_de(objective, bounds, **kwargs):
        captured.update(kwargs)
        for index in range(20):
            objective([index / 20.0])
    s = space()
    result = run_search(tmp_path, s, Evaluator(), SearchConfig("scipy", 3, 4), differential_evolution=fake_de)
    assert len(result.history) == 3 and s.calls == 3
    assert captured["polish"] is False
    assert captured["workers"] == 1
    assert captured["updating"] == "immediate"
