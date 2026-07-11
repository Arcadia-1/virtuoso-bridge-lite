"""Physical candidate evaluation boundary and atomic artifact handling."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class EvaluationFailure(Exception):
    """A categorized candidate evaluation failure."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = str(category)
        self.message = str(message)


@dataclass(frozen=True)
class EvaluationResult:
    candidate_id: str
    objective: float
    success: bool
    metrics: Mapping[str, Any]
    metadata: Mapping[str, Any]
    failure: Optional[Mapping[str, str]]


def atomic_write_json(path: Union[str, Path], value: Any) -> None:
    """Serialize strict JSON and atomically replace the destination."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _finite(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("%s must be a finite number" % location)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("%s must be a finite number" % location)
    return result


def _result_from_backend(candidate_id: str, raw: Any) -> EvaluationResult:
    if isinstance(raw, EvaluationResult):
        if raw.candidate_id != candidate_id:
            raise ValueError("backend result candidate_id does not match")
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("backend must return a mapping or EvaluationResult")
    objective = _finite(raw.get("objective"), "objective")
    metrics = raw.get("metrics", {})
    metadata = raw.get("metadata", {})
    if not isinstance(metrics, Mapping) or not isinstance(metadata, Mapping):
        raise TypeError("metrics and metadata must be mappings")
    return EvaluationResult(candidate_id, objective, True, dict(metrics), dict(metadata), None)


class CandidateEvaluator:
    """Evaluate one already-materialized physical candidate."""

    def __init__(self, backend: Callable[[Mapping[str, Any], Path], Any], failure_penalty: float = 1e9) -> None:
        self.backend = backend
        self.failure_penalty = _finite(failure_penalty, "failure_penalty")
        if self.failure_penalty < 0:
            raise ValueError("failure_penalty must be nonnegative")

    @staticmethod
    def _candidate_dir(run_dir: Union[str, Path], candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or not _SAFE_ID.fullmatch(candidate_id) or candidate_id in (".", ".."):
            raise ValueError("candidate_id is unsafe")
        return Path(run_dir) / "candidates" / candidate_id

    def _failure(self, candidate_dir: Path, candidate_id: str, category: str, message: str) -> EvaluationResult:
        failure = {"category": str(category), "message": str(message)}
        atomic_write_json(candidate_dir / "failure.json", failure)
        return EvaluationResult(candidate_id, self.failure_penalty, False, {}, {}, failure)

    def evaluate(self, run_dir: Union[str, Path], candidate_id: str, physical_candidate: Mapping[str, Any]) -> EvaluationResult:
        candidate_dir = self._candidate_dir(run_dir, candidate_id)
        candidate_dir.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_json(candidate_dir / "parameters.json", physical_candidate)
        except (TypeError, ValueError, OSError) as exc:
            return self._failure(candidate_dir, candidate_id, "artifact", str(exc))
        try:
            result = _result_from_backend(candidate_id, self.backend(physical_candidate, candidate_dir))
            _finite(result.objective, "objective")
            atomic_write_json(candidate_dir / "result.json", asdict(result))
            return result
        except EvaluationFailure as exc:
            return self._failure(candidate_dir, candidate_id, exc.category, exc.message)
        except (TypeError, ValueError, OSError) as exc:
            return self._failure(candidate_dir, candidate_id, "artifact", str(exc))
        except Exception as exc:
            return self._failure(candidate_dir, candidate_id, "backend", str(exc))
