import json
import math

import pytest

from analog_opt.evaluator import CandidateEvaluator, EvaluationFailure, EvaluationResult


def test_evaluator_passes_and_saves_physical_candidate_unchanged(tmp_path):
    seen = []
    def backend(candidate, candidate_dir):
        seen.append((candidate, candidate_dir))
        return {"objective": 1.25, "metrics": {"gain": 12.0}, "metadata": {"corner": "tt"}}
    candidate = {"M7_M": 20}
    result = CandidateEvaluator(backend, failure_penalty=999.0).evaluate(tmp_path, "candidate-0001", candidate)
    assert result.success and result.objective == 1.25
    assert seen[0][0] is candidate
    assert json.loads((tmp_path / "candidates/candidate-0001/parameters.json").read_text()) == candidate
    assert json.loads((tmp_path / "candidates/candidate-0001/result.json").read_text())["metrics"] == {"gain": 12.0}


def test_evaluator_records_failure_and_finite_penalty(tmp_path):
    def backend(candidate, candidate_dir):
        raise EvaluationFailure("convergence", "spectre did not converge")
    result = CandidateEvaluator(backend, failure_penalty=1234.0).evaluate(tmp_path, "c0", {"x": 1.0})
    failure = json.loads((tmp_path / "candidates/c0/failure.json").read_text())
    assert not result.success and result.objective == 1234.0 and math.isfinite(result.objective)
    assert failure["category"] == "convergence"


@pytest.mark.parametrize("candidate_id", ["../escape", "a/b", "a\\b", ".", ""])
def test_evaluator_rejects_unsafe_candidate_ids(tmp_path, candidate_id):
    with pytest.raises(ValueError):
        CandidateEvaluator(lambda *_: {"objective": 0}).evaluate(tmp_path, candidate_id, {"x": 1})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_evaluator_rejects_nonfinite_json_values(tmp_path, bad):
    evaluator = CandidateEvaluator(lambda *_: {"objective": 1.0, "metadata": {"bad": bad}})
    result = evaluator.evaluate(tmp_path, "c0", {"x": 1})
    assert not result.success
    assert json.loads((tmp_path / "candidates/c0/failure.json").read_text())["category"] == "artifact"


def test_evaluator_accepts_result_objects_and_string_metadata(tmp_path):
    expected = EvaluationResult("c0", 2.0, True, {"gain": 3.0}, {"note": "ok"}, None)
    result = CandidateEvaluator(lambda *_: expected).evaluate(tmp_path, "c0", {"x": 1})
    assert result == expected


def test_candidate_nonfinite_value_becomes_artifact_failure(tmp_path):
    result = CandidateEvaluator(lambda *_: {"objective": 0}).evaluate(tmp_path, "c0", {"x": float("nan")})
    assert not result.success
    assert json.loads((tmp_path / "candidates/c0/failure.json").read_text())["category"] == "artifact"


def test_backend_nonfinite_objective_becomes_artifact_failure(tmp_path):
    result = CandidateEvaluator(lambda *_: {"objective": float("inf")}).evaluate(tmp_path, "c0", {"x": 1})
    assert not result.success and math.isfinite(result.objective)


def test_atomic_artifacts_leave_no_temp_files(tmp_path):
    CandidateEvaluator(lambda *_: {"objective": 1.0}).evaluate(tmp_path, "c0", {"x": 1})
    assert not list((tmp_path / "candidates/c0").glob("*.tmp"))


def test_success_writes_complete_artifact_set(tmp_path):
    backend = lambda *_: {"objective": 2.0, "metrics": {"gain": 4.0}, "specs": {"passed": True}, "metadata": {"corner": "tt"}}
    CandidateEvaluator(backend).evaluate(tmp_path, "c0", {"x": 1})
    candidate_dir = tmp_path / "candidates/c0"
    assert json.loads((candidate_dir / "metrics.json").read_text()) == {"gain": 4.0}
    assert json.loads((candidate_dir / "specs.json").read_text()) == {"passed": True}
    assert (candidate_dir / "result.json").exists()
    assert not (candidate_dir / "failure.json").exists()


def test_candidate_directory_is_exclusive(tmp_path):
    evaluator = CandidateEvaluator(lambda *_: {"objective": 1.0})
    evaluator.evaluate(tmp_path, "c0", {"x": 1})
    with pytest.raises(EvaluationFailure, match="already exists"):
        evaluator.evaluate(tmp_path, "c0", {"x": 2})


def test_failure_leaves_no_success_artifacts(tmp_path):
    def backend(*_):
        raise EvaluationFailure("convergence", "failed")
    CandidateEvaluator(backend).evaluate(tmp_path, "c0", {"x": 1})
    candidate_dir = tmp_path / "candidates/c0"
    assert (candidate_dir / "failure.json").exists()
    assert not (candidate_dir / "result.json").exists()
    assert not (candidate_dir / "metrics.json").exists()
    assert not (candidate_dir / "specs.json").exists()


def test_unwritable_failure_artifact_raises_evaluation_failure(tmp_path, monkeypatch):
    import analog_opt.evaluator as module
    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(module, "atomic_write_json", broken)
    with pytest.raises(EvaluationFailure, match="failure artifact"):
        CandidateEvaluator(lambda *_: {"objective": 1.0}).evaluate(tmp_path, "c0", {"x": 1})


@pytest.mark.parametrize("raw", [
    {"objective": 1.0, "success": "false", "failure": {"category": "sim", "message": "bad"}},
    EvaluationResult("c0", 1.0, "false", {}, {}, {"category": "sim", "message": "bad"}),
])
def test_backend_success_must_be_exact_bool(tmp_path, raw):
    result = CandidateEvaluator(lambda *_: raw, failure_penalty=77.0).evaluate(tmp_path, "c0", {"x": 1})
    assert not result.success and result.objective == 77.0
    assert json.loads((tmp_path / "candidates/c0/failure.json").read_text())["category"] == "protocol"


@pytest.mark.parametrize("raw", [
    {"objective": 1.0, "success": True, "failure": {"category": "sim", "message": "bad"}},
    EvaluationResult("c0", 1.0, True, {}, {}, {"category": "sim", "message": "bad"}),
])
def test_success_result_must_not_have_failure(tmp_path, raw):
    result = CandidateEvaluator(lambda *_: raw).evaluate(tmp_path, "c0", {"x": 1})
    assert not result.success
    assert json.loads((tmp_path / "candidates/c0/failure.json").read_text())["category"] == "protocol"


@pytest.mark.parametrize("failure", [None, {}, {"category": "", "message": "bad"}, {"category": "sim", "message": ""}, {"category": 3, "message": "bad"}, {"category": "sim", "message": 3}])
def test_failed_result_requires_valid_failure_mapping(tmp_path, failure):
    raw = {"objective": 8.0, "success": False, "failure": failure}
    result = CandidateEvaluator(lambda *_: raw).evaluate(tmp_path, "c0", {"x": 1})
    assert not result.success
    stored = json.loads((tmp_path / "candidates/c0/failure.json").read_text())
    assert stored["category"] == "protocol"


def test_valid_failed_mapping_uses_configured_penalty_and_only_failure_artifact(tmp_path):
    raw = {"objective": 8.0, "success": False, "failure": {"category": "convergence", "message": "no solution"}, "metrics": {"partial": 1}, "specs": {"passed": False}}
    result = CandidateEvaluator(lambda *_: raw, failure_penalty=321.0).evaluate(tmp_path, "c0", {"x": 1})
    candidate_dir = tmp_path / "candidates/c0"
    assert result == EvaluationResult("c0", 321.0, False, {}, {}, {"category": "convergence", "message": "no solution"})
    assert sorted(path.name for path in candidate_dir.iterdir()) == ["failure.json", "parameters.json"]


def test_valid_failed_result_object_uses_failure_artifact_path(tmp_path):
    raw = EvaluationResult("c0", 8.0, False, {"partial": 1}, {"source": "spectre"}, {"category": "timeout", "message": "too slow"}, {"passed": False})
    result = CandidateEvaluator(lambda *_: raw, failure_penalty=44.0).evaluate(tmp_path, "c0", {"x": 1})
    assert result.objective == 44.0 and result.failure["category"] == "timeout"
    assert sorted(path.name for path in (tmp_path / "candidates/c0").iterdir()) == ["failure.json", "parameters.json"]
