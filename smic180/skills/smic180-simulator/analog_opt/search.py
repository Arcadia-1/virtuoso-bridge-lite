"""Normalized search adapters for analog optimization."""

from __future__ import annotations

import importlib
import math
import random
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

from .evaluator import EvaluationResult, atomic_write_json


_NONFINITE_PENALTY = 1e308


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


def _record(result: EvaluationResult) -> Mapping[str, Any]:
    return asdict(result)


def _load_history(path: Path, config: SearchConfig, dimension: int) -> List[EvaluationResult]:
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    stored = data.get("config", {})
    if stored.get("method") != config.method or stored.get("seed") != config.seed or data.get("dimension") != dimension:
        raise ValueError("resume configuration is incompatible")
    results = []
    for item in data.get("history", []):
        results.append(EvaluationResult(
            item["candidate_id"], float(item["objective"]), bool(item["success"]),
            item.get("metrics", {}), item.get("metadata", {}), item.get("failure")
        ))
    expected = [_candidate_id(index) for index in range(len(results))]
    if [item.candidate_id for item in results] != expected:
        raise ValueError("resume history candidate IDs are incompatible")
    return results


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


def run_search(
    run_dir: Union[str, Path],
    space: Any,
    evaluator: Any,
    config: SearchConfig,
    resume: bool = False,
    differential_evolution: Optional[Callable[..., Any]] = None,
    turbo_runner: Optional[Callable[..., Any]] = None,
) -> SearchResult:
    """Run a search while materializing each newly evaluated candidate exactly once."""
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    history_path = root / "search_history.json"
    dimension = len(space.specs)
    history = _load_history(history_path, config, dimension) if resume and history_path.exists() else []
    if resume and not history_path.exists():
        raise ValueError("resume history does not exist")
    if len(history) > config.evaluations:
        raise ValueError("resume evaluations cannot be less than completed history")

    def persist() -> None:
        atomic_write_json(history_path, {
            "config": asdict(config), "dimension": dimension,
            "history": [_record(item) for item in history],
        })

    def evaluate_vector(vector: Sequence[float], index: int) -> float:
        bounded = [min(1.0, max(0.0, float(value))) for value in vector]
        physical = space.materialize(bounded)
        result = _safe_result(evaluator.evaluate(root, _candidate_id(index), physical))
        history.append(result)
        persist()
        return result.objective

    completed = len(history)
    remaining = config.evaluations - completed
    if config.method == "random":
        rng = random.Random(config.seed)
        vectors = [[rng.random() for _ in range(dimension)] for _ in range(config.evaluations)]
        for index in range(completed, config.evaluations):
            evaluate_vector(vectors[index], index)
    elif config.method == "scipy" and remaining:
        runner = differential_evolution
        if runner is None:
            runner = importlib.import_module("scipy.optimize").differential_evolution
        next_index = [completed]
        def objective(vector: Sequence[float]) -> float:
            if next_index[0] >= config.evaluations:
                return _NONFINITE_PENALTY
            value = evaluate_vector(vector, next_index[0])
            next_index[0] += 1
            return value
        runner(objective, [(0.0, 1.0)] * dimension, seed=config.seed, maxiter=remaining)
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
                return _NONFINITE_PENALTY
            value = evaluate_vector(vector, next_index[0])
            next_index[0] += 1
            return value
        runner(objective, dimension=dimension, evaluations=remaining, seed=config.seed)

    persist()
    successful = [item for item in history if item.success and math.isfinite(item.objective)]
    best = min(successful, key=lambda item: item.objective) if successful else None
    return SearchResult(tuple(history), best, best is None)
