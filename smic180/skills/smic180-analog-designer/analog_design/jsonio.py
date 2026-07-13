"""Strict JSON helpers for workflow inputs and manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StrictJsonError(ValueError):
    """Raised when JSON is invalid or contains non-finite constants."""


def load_strict_json(path: str | Path) -> Any:
    def reject(value: str) -> None:
        raise StrictJsonError(f"JSON contains non-finite value: {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=reject)
    except OSError as exc:
        raise StrictJsonError(f"cannot read JSON {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StrictJsonError(f"invalid JSON {path}: {exc}") from exc
