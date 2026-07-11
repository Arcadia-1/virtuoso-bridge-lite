"""Deterministic optimization manifests and Markdown reports."""
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping


def _validate(value: Any, label: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value): raise ValueError(label + " must be finite")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str): raise ValueError("mapping keys must be strings")
            _validate(v, k)
    elif isinstance(value, (list, tuple)):
        for item in value: _validate(item, label)


def _artifact_paths(data: Mapping[str, Any]) -> None:
    artifacts = data.get("artifacts", {})
    if not isinstance(artifacts, Mapping): raise ValueError("artifacts must be a mapping")
    for value in artifacts.values():
        if not isinstance(value, str): raise ValueError("artifact path must be text")
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts: raise ValueError("artifact path escapes run directory")


def _atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    except Exception:
        try: os.unlink(name)
        except OSError: pass
        raise
    return path


def _publishable(data: Mapping[str, Any]) -> bool:
    best = data.get("best")
    pvt = data.get("pvt")
    if not isinstance(best, Mapping) or not isinstance(pvt, Mapping) or type(pvt.get("overall_passed")) is not bool: return False
    specs = best.get("specs", {})
    if not isinstance(specs, Mapping) or not specs: return False
    if not all(isinstance(v, Mapping) and v.get("passed") is True for v in specs.values()): return False
    failures = data.get("failures", [])
    return pvt["overall_passed"] and not any(isinstance(v, Mapping) and v.get("blocking") is True for v in failures)


def write_run_manifest(run_dir: Any, data: Mapping[str, Any]) -> Path:
    if not isinstance(data, Mapping): raise ValueError("report data must be a mapping")
    _validate(data); _artifact_paths(data)
    output = dict(data); output["publishable"] = _publishable(data)
    text = json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    return _atomic(Path(run_dir) / "result_manifest.json", text)


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")

def _table(mapping: Mapping[str, Any]) -> list:
    rows = ["| Name | Value |", "|---|---|"]
    rows.extend("| %s | %s |" % (_escape(k), _escape(mapping[k])) for k in sorted(mapping))
    if len(rows) == 2: rows.append("| _None_ | _Unavailable_ |")
    return rows


def write_report(run_dir: Any, data: Mapping[str, Any]) -> Path:
    if not isinstance(data, Mapping): raise ValueError("report data must be a mapping")
    _validate(data); _artifact_paths(data)
    best = data.get("best", {}); pvt = data.get("pvt", {}); metrics = best.get("metrics", {})
    lines = ["# Analog Optimization Report", "", "Publishable: **%s**" % ("yes" if _publishable(data) else "no"), "",
             "## Best Candidate", "", "Objective: %s" % _escape(best.get("objective", "unavailable")), "", "### Parameters", ""]
    lines += _table(best.get("parameters", {}))
    lines += ["", "### Specifications", "", "| Name | Passed | Violation |", "|---|---|---|"]
    for name in sorted(best.get("specs", {})):
        spec = best["specs"][name]; lines.append("| %s | %s | %s |" % (_escape(name), _escape(spec.get("passed")), _escape(spec.get("violation", "unavailable"))))
    for key, title in (("measured", "Measured Metrics"), ("derived", "Derived Metrics"), ("unavailable", "Unavailable Metrics")):
        lines += ["", "## " + title, ""] + _table(metrics.get(key, {}))
    lines += ["", "## PVT Worst Condition", ""] + _table(pvt.get("worst", {}) or {})
    lines += ["", "## Failures", "", "| Category | Message | Blocking |", "|---|---|---|"]
    failures = list(data.get("failures", [])) + list(pvt.get("failures", []))
    for failure in failures:
        lines.append("| %s | %s | %s |" % (_escape(failure.get("category", "unknown")), _escape(failure.get("message", "")), _escape(failure.get("blocking", True))))
    if not failures: lines.append("| _None_ |  |  |")
    lines += ["", "## Artifact Paths", ""] + _table(data.get("artifacts", {}))
    return _atomic(Path(run_dir) / "optimization_report.md", "\n".join(lines) + "\n")
