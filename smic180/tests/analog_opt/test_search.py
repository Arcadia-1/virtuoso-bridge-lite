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
