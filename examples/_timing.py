"""Small timing helpers for CLI examples."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def timed_call(fn: Callable[[], T]) -> tuple[float, T]:
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


def format_elapsed(seconds: float) -> str:
    return f"{seconds:.3f}s"


def print_elapsed(label: str, seconds: float) -> None:
    print(f"[elapsed] {label}: {format_elapsed(seconds)}")


def decode_skill(raw: str) -> str:
    """Decode a SKILL string return value (quoted + escape sequences)."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw) if raw.startswith('"') else raw
    except json.JSONDecodeError:
        return raw


def print_load_il(result: object) -> None:
    meta = result.metadata  # type: ignore[union-attr]
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}"
          f"  [{format_elapsed(result.execution_time or 0.0)}]")  # type: ignore[union-attr]


def print_execute(label: str, result: object) -> None:
    print(f"[{label}] [{format_elapsed(result.execution_time or 0.0)}]")  # type: ignore[union-attr]


def print_result(result: object) -> None:
    """Print output and errors from a VirtuosoResult."""
    output = result.output  # type: ignore[union-attr]
    errors = result.errors or []  # type: ignore[union-attr]
    if output:
        print(output)
    for e in errors:
        print(f"[error] {e}")
    if not output and not errors:
        print(f"[status] {result.status.value}")  # type: ignore[union-attr]
