"""Helpers for writing Spectre example outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from virtuoso_bridge.models import SimulationResult


TIMING_KEYS = (
    "upload_total",
    "remote_exec",
    "download_total",
    "parse_results",
    "total",
)


def save_waveforms_csv(data: dict[str, Any], path: Path) -> None:
    """Write parsed waveform arrays to a rectangular CSV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    columns: list[str] = []
    series: dict[str, list[Any]] = {}
    max_len = 0

    if "time" in data:
        columns.append("time")
    for key in data:
        if key != "time":
            columns.append(key)

    for key in columns:
        values = data.get(key, [])
        if isinstance(values, list):
            series[key] = values
            if len(values) > max_len:
                max_len = len(values)

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for idx in range(max_len):
            row = []
            for key in columns:
                values = series.get(key, [])
                row.append(values[idx] if idx < len(values) else "")
            writer.writerow(row)


def save_summary_json(
    result: SimulationResult,
    path: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a compact JSON summary without duplicating full waveform arrays."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lengths: dict[str, int] = {}
    for key, values in result.data.items():
        if isinstance(values, list):
            lengths[key] = len(values)

    payload = {
        "status": result.status.value,
        "ok": result.ok,
        "tool_version": result.tool_version,
        "signals": list(result.data.keys()),
        "lengths": lengths,
        "errors": result.errors,
        "warnings": result.warnings,
        "metadata": result.metadata,
    }
    if extra:
        payload.update(extra)

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_timing_summary(result: SimulationResult) -> None:
    """Print runner-provided timing fields in a consistent order."""
    timings = result.metadata.get("timings", {})
    if not isinstance(timings, dict):
        return
    for key in TIMING_KEYS:
        value = timings.get(key)
        if isinstance(value, (int, float)):
            print(f"[Timing] {key} = {value:.3f}s")


def print_result_counts(result: SimulationResult) -> None:
    """Print compact error and warning counts."""
    print(f"[Errors] {len(result.errors)}")
    print(f"[Warnings] {len(result.warnings)}")
