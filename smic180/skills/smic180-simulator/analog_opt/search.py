"""Normalized search adapters for analog optimization."""

from __future__ import annotations

import importlib
import json
import math
import random
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

from .evaluator import EvaluationResult, atomic_write_json


_NONFINITE_PENALTY = 1e308


class _BudgetComplete(Exception):
    pass


@dataclass(frozen=True)
class SearchConfig:
    method: str = "random"
    evaluations: int = 20
    seed: int = 0

    def __post_init__(self) -> None:
        if self.method not in ("random", "scipy", "turbo"):
            raise ValueError("unsupported search method: %s" % self.method)
        if isinstance(self.evaluations, bool) or not isinstance(self.evaluations, int) or self.evaluations < 1:
            raise ValueError("evaluations must be a positive integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")


@dataclass(frozen=True)
class SearchResult:
    history: Tuple[EvaluationResult, ...]
    best: Optional[EvaluationResult]
    all_failed: bool


def _candidate_id(index: int) -> str:
    return "candidate-%06d" % index


def _reject_constant(value: str) -> None:
    raise ValueError("history contains non-finite JSON constant: %s" % value)


def _strict_float(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("history %s must be a finite number" % location)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("history %s must be a finite number" % location)
    return result


def _strict_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("history %s must be a mapping" % location)
    return dict(value)


def _load_history(path: Path, config: SearchConfig, dimension: int) -> Tuple[List[EvaluationResult], List[Mapping[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("history could not be parsed: %s" % exc) from exc
    if not isinstance(data, Mapping):
        raise ValueError("history top level must be a mapping")
    stored = _strict_mapping(data.get("config"), "config")
    if stored.get("method") != config.method or stored.get("seed") != config.seed:
        raise ValueError("resume configuration is incompatible")
    if type(data.get("dimension")) is not int or data["dimension"] != dimension:
        raise ValueError("resume history dimension is incompatible")
    raw_history = data.get("history")
    if not isinstance(raw_history, list):
        raise ValueError("history entries must be a list")
    results = []
    records = []
    for index, raw in enumerate(raw_history):
        item = _strict_mapping(raw, "entry %d" % index)
        if item.get("candidate_id") != _candidate_id(index):
            raise ValueError("resume history candidate IDs are incompatible")
        if type(item.get("success")) is not bool:
            raise ValueError("history success must be a bool")
        vector = item.get("normalized_vector")
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ValueError("history normalized_vector dimension is incompatible")
        normalized = [_strict_float(value, "normalized_vector") for value in vector]
        if any(value < 0.0 or value > 1.0 for value in normalized):
            raise ValueError("history normalized_vector must be within [0, 1]")
        metrics = _strict_mapping(item.get("metrics"), "metrics")
        specs = _strict_mapping(item.get("specs"), "specs")
        metadata = _strict_mapping(item.get("metadata"), "metadata")
        physical = _strict_mapping(item.get("physical_candidate"), "physical_candidate")
        objective = _strict_float(item.get("objective"), "objective")
        failure = item.get("failure")
        if failure is not None:
            failure = _strict_mapping(failure, "failure")
        result = EvaluationResult(item["candidate_id"], objective, item["success"], metrics, metadata, failure, specs)
        results.append(result)
        records.append({
            "candidate_id": item["candidate_id"], "objective": objective,
            "success": item["success"], "metrics": metrics, "specs": specs,
            "metadata": metadata, "failure": failure,
            "normalized_vector": normalized, "physical_candidate": physical,
        })
    return results, records


def _safe_result(result: EvaluationResult) -> EvaluationResult:
    try:
        objective = float(result.objective)
    except (TypeError, ValueError, OverflowError):
        objective = float("nan")
    if result.success and math.isfinite(objective):
        return result
    if math.isfinite(objective) and not result.success:
        return result
    failure = {"category": "objective", "message": "objective must be finite"}
    return replace(result, objective=_NONFINITE_PENALTY, success=False, failure=failure)


def _best_artifact(best: Optional[EvaluationResult], records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if best is None:
        return {"best": None, "status": "all_failed"}
    record = next(item for item in records if item["candidate_id"] == best.candidate_id)
    return {
        "status": "success", "candidate_id": best.candidate_id,
        "objective": best.objective, "parameters": record["physical_candidate"],
        "candidate_path": "candidates/%s" % best.candidate_id,
    }


def run_search(
    run_dir: Union[str, Path], space: Any, evaluator: Any, config: SearchConfig,
    resume: bool = False, differential_evolution: Optional[Callable[..., Any]] = None,
    turbo_runner: Optional[Callable[..., Any]] = None,
) -> SearchResult:
    """Run a search while materializing each newly evaluated candidate exactly once."""
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".search.lock"
    try:
        lock.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise RuntimeError("search is already active for this run directory") from exc
    try:
        history_path = root / "search_history.json"
        dimension = len(space.specs)
        if resume:
            if not history_path.exists():
                raise ValueError("resume history does not exist")
            history, records = _load_history(history_path, config, dimension)
        else:
            history, records = [], []
        if len(history) > config.evaluations:
            raise ValueError("resume evaluations cannot be less than completed history")
        if resume and history and config.method != "random":
            raise ValueError("%s method does not support resume" % config.method)

        def persist() -> None:
            atomic_write_json(history_path, {
                "config": asdict(config), "dimension": dimension, "history": records,
            })

        def evaluate_vector(vector: Sequence[float], index: int) -> float:
            normalized = [float(value) for value in vector]
            if len(normalized) != dimension or any(not math.isfinite(value) for value in normalized):
                raise ValueError("normalized vector is invalid")
            bounded = [min(1.0, max(0.0, value)) for value in normalized]
            physical = space.materialize(bounded)
            result = _safe_result(evaluator.evaluate(root, _candidate_id(index), physical))
            history.append(result)
            records.append({
                "candidate_id": result.candidate_id, "objective": result.objective,
                "success": result.success, "metrics": dict(result.metrics),
                "specs": dict(result.specs), "metadata": dict(result.metadata),
                "failure": result.failure, "normalized_vector": bounded,
                "physical_candidate": dict(physical),
            })
            persist()
            return result.objective

        completed = len(history)
        remaining = config.evaluations - completed
        if config.method == "random":
            rng = random.Random(config.seed)
            vectors = [[rng.random() for _ in range(dimension)] for _ in range(config.evaluations)]
            if resume:
                for index, record in enumerate(records):
                    if record["normalized_vector"] != vectors[index]:
                        raise ValueError("resume history does not match the seeded random sequence")
            for index in range(completed, config.evaluations):
                evaluate_vector(vectors[index], index)
        elif config.method == "scipy" and remaining:
            runner = differential_evolution
            if runner is None:
                runner = importlib.import_module("scipy.optimize").differential_evolution
            next_index = [completed]
            def objective(vector: Sequence[float]) -> float:
                if next_index[0] >= config.evaluations:
                    raise _BudgetComplete()
                value = evaluate_vector(vector, next_index[0])
                next_index[0] += 1
                return value
            try:
                runner(objective, [(0.0, 1.0)] * dimension, seed=config.seed,
                       polish=False, workers=1, updating="immediate")
            except _BudgetComplete:
                pass
        elif config.method == "turbo" and remaining:
            runner = turbo_runner
            if runner is None:
                try:
                    runner = importlib.import_module("turbo").run
                except (ImportError, AttributeError) as exc:
                    raise ImportError("TuRBO search requires an installed or injected runner") from exc
            next_index = [completed]
            def objective(vector: Sequence[float]) -> float:
                if next_index[0] >= config.evaluations:
                    raise _BudgetComplete()
                value = evaluate_vector(vector, next_index[0])
                next_index[0] += 1
                return value
            try:
                runner(objective, dimension=dimension, evaluations=remaining, seed=config.seed)
            except _BudgetComplete:
                pass

        persist()
        successful = [item for item in history if item.success and math.isfinite(item.objective)]
        best = min(successful, key=lambda item: item.objective) if successful else None
        atomic_write_json(root / "best_candidate.json", _best_artifact(best, records))
        return SearchResult(tuple(history), best, best is None)
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass
