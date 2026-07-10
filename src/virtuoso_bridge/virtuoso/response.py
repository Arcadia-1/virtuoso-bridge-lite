"""Shared helpers for normalizing SKILL transport responses."""

from __future__ import annotations

from typing import Any


def response_fields(response: Any) -> tuple[Any, Any, str]:
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
        return errors, status, output or ""

    return (
        getattr(response, "errors", None),
        getattr(response, "status", None),
        getattr(response, "output", "") or "",
    )


__all__ = ["response_fields"]
