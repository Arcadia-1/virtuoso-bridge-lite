"""Shared helpers for normalizing SKILL transport responses."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def response_fields(response: Any) -> tuple[list[str], Any, str]:
    """Return errors, status, and output from object or dictionary responses."""
    if isinstance(response, dict):
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        errors = response.get("errors") or result.get("errors")
        status = response.get("status") or result.get("status")
        output = response.get("output")
        if output is None:
            output = result.get("output", "")
        if response.get("ok") is False and not errors:
            errors = [response.get("error") or result.get("error") or "request failed"]
        return _error_messages(errors), status, _output_text(output)

    return (
        _error_messages(getattr(response, "errors", None)),
        getattr(response, "status", None),
        _output_text(getattr(response, "output", "")),
    )


def _error_messages(errors: Any) -> list[str]:
    if errors is None or errors == "":
        return []
    if isinstance(errors, str):
        return [errors]
    if isinstance(errors, Iterable):
        return [str(error) for error in errors]
    return [str(errors)]


def _output_text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return str(output)


__all__ = ["response_fields"]
