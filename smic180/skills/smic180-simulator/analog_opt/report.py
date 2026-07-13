"""Deterministic optimization manifests, PVT artifacts, and Markdown reports."""
from dataclasses import asdict, is_dataclass
import html
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
from typing import Any, Mapping


class ReportError(RuntimeError):
    """Raised when an optimization report artifact cannot be written."""


def _plain(value: Any) -> Any:
    if is_dataclass(value): return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping): return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    return value


def _validate(value: Any, label: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value): raise ValueError(label + " must be finite")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str): raise ValueError("mapping keys must be strings")
            _validate(item, key)
    elif isinstance(value, (list, tuple)):
        for item in value: _validate(item, label)


def _validate_artifact_path(run_dir: Path, value: Any) -> None:
    if not isinstance(value, str) or value in ("", "."): raise ValueError("artifact path must be non-empty and relative")
    posix = PurePosixPath(value); windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root or ".." in posix.parts or ".." in windows.parts:
        raise ValueError("artifact path escapes run directory")
    root = run_dir.resolve(strict=False)
    candidate = (run_dir / Path(*posix.parts)).resolve(strict=False)
    try: candidate.relative_to(root)
    except ValueError as exc: raise ValueError("artifact path escapes run directory") from exc


def _artifact_paths(run_dir: Path, data: Mapping[str, Any]) -> None:
    artifacts = data.get("artifacts", {})
    if not isinstance(artifacts, Mapping): raise ValueError("artifacts must be a mapping")
    for value in artifacts.values(): _validate_artifact_path(run_dir, value)


def _atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None; temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None; stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path); temporary = None
    except (OSError, UnicodeError) as exc:
        if descriptor is not None: os.close(descriptor)
        if temporary is not None:
            try: os.unlink(temporary)
            except OSError: pass
        raise ReportError("cannot write %s: %s" % (path.name, exc)) from exc
    return path


def _json(path: Path, value: Any) -> Path:
    plain = _plain(value); _validate(plain)
    return _atomic(path, json.dumps(plain, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _normalize_failures(value: Any, label: str = "failures") -> list:
    if not isinstance(value, (list, tuple)):
        raise ReportError(label + " must be a list")
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ReportError("%s[%d] must be a mapping" % (label, index))
        category = item.get("category")
        message = item.get("message")
        blocking = item.get("blocking", True)
        if not isinstance(category, str) or not category.strip():
            raise ReportError("%s[%d].category must be a non-empty string" % (label, index))
        if not isinstance(message, str) or not message.strip():
            raise ReportError("%s[%d].message must be a non-empty string" % (label, index))
        if type(blocking) is not bool:
            raise ReportError("%s[%d].blocking must be bool" % (label, index))
        normalized.append(dict(item, category=category, message=message, blocking=blocking))
    return normalized


def _normalize_result_data(data: Mapping[str, Any]) -> Mapping[str, Any]:
    output = dict(data)
    output["failures"] = _normalize_failures(data.get("failures", []))
    pvt = data.get("pvt")
    if isinstance(pvt, Mapping):
        normalized_pvt = dict(pvt)
        normalized_pvt["failures"] = _normalize_failures(pvt.get("failures", []), "pvt.failures")
        output["pvt"] = normalized_pvt
    return output

def _publishable(data: Mapping[str, Any]) -> bool:
    best = data.get("best"); pvt = data.get("pvt")
    if not isinstance(best, Mapping) or not isinstance(pvt, Mapping) or pvt.get("overall_passed") is not True: return False
    specs = best.get("specs")
    if not isinstance(specs, Mapping) or not specs: return False
    if not all(isinstance(item, Mapping) and item.get("passed") is True for item in specs.values()): return False
    failures = data.get("failures", [])
    return not any(item["blocking"] for item in failures)


def write_run_manifest(run_dir: Any, data: Mapping[str, Any]) -> Path:
    if not isinstance(data, Mapping): raise ValueError("run manifest data must be a mapping")
    directory = Path(run_dir); plain = _plain(data); _validate(plain); _artifact_paths(directory, plain)
    return _json(directory / "run_manifest.json", plain)


def write_result_manifest(run_dir: Any, data: Mapping[str, Any]) -> Path:
    if not isinstance(data, Mapping): raise ValueError("result manifest data must be a mapping")
    directory = Path(run_dir); plain = _normalize_result_data(_plain(data)); _validate(plain); _artifact_paths(directory, plain)
    output = dict(plain); output["publishable"] = _publishable(plain)
    return _json(directory / "result_manifest.json", output)


def write_pvt_results(run_dir: Any, summary: Any) -> Path:
    plain = _plain(summary)
    if not isinstance(plain, Mapping): raise ValueError("PVT summary must be a dataclass or mapping")
    _validate(plain)
    return _json(Path(run_dir) / "pvt_results.json", plain)


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")

def _table(mapping: Mapping[str, Any]) -> list:
    rows = ["| Name | Value |", "|---|---|"]
    rows.extend("| %s | %s |" % (_escape(key), _escape(mapping[key])) for key in sorted(mapping))
    if len(rows) == 2: rows.append("| _None_ | _Unavailable_ |")
    return rows


def write_report(run_dir: Any, data: Mapping[str, Any]) -> Path:
    if not isinstance(data, Mapping): raise ValueError("report data must be a mapping")
    directory = Path(run_dir); plain = _normalize_result_data(_plain(data)); _validate(plain); _artifact_paths(directory, plain)
    best = plain.get("best", {}); pvt = plain.get("pvt", {}); metrics = best.get("metrics", {})
    lines = ["# Analog Optimization Report", "", "Publishable: **%s**" % ("yes" if _publishable(plain) else "no"), "",
             "## Best Candidate", "", "Objective: %s" % _escape(best.get("objective", "unavailable")), "", "### Parameters", ""]
    lines += _table(best.get("parameters", {}))
    lines += ["", "### Specifications", "", "| Name | Passed | Violation |", "|---|---|---|"]
    for name in sorted(best.get("specs", {})):
        spec = best["specs"][name]
        lines.append("| %s | %s | %s |" % (_escape(name), _escape(spec.get("passed")), _escape(spec.get("violation", "unavailable"))))
    for key, title in (("measured", "Measured Metrics"), ("derived", "Derived Metrics"), ("unavailable", "Unavailable Metrics")):
        lines += ["", "## " + title, ""] + _table(metrics.get(key, {}))
    lines += ["", "## PVT Worst Condition", ""] + _table(pvt.get("worst", {}) or {})
    lines += ["", "## Failures", "", "| Category | Message | Blocking |", "|---|---|---|"]
    failures = list(plain.get("failures", [])) + list(pvt.get("failures", []))
    for failure in failures:
        lines.append("| %s | %s | %s |" % (_escape(failure.get("category", "unknown")), _escape(failure.get("message", "")), _escape(failure.get("blocking", True))))
    if not failures: lines.append("| _None_ |  |  |")
    lines += ["", "## Artifact Paths", ""] + _table(plain.get("artifacts", {}))
    return _atomic(directory / "optimization_report.md", "\n".join(lines) + "\n")
