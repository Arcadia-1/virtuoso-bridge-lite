"""Initial sizing data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class SizingError(ValueError):
    """Raised when an engineering seed cannot be calculated safely."""


@dataclass(frozen=True)
class CalculationRecord:
    name: str
    formula_id: str
    inputs: Mapping[str, float]
    assumptions: tuple[str, ...]
    dimension: str
    value: float
    status: str = "estimate"
    confidence: str = "low"

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))


@dataclass(frozen=True)
class SizingResult:
    records: Mapping[str, CalculationRecord]
    confirmed_values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))
        object.__setattr__(self, "confirmed_values", MappingProxyType(dict(self.confirmed_values)))

    def value(self, name: str) -> float:
        try:
            return self.records[name].value
        except KeyError as exc:
            raise SizingError(f"unknown sizing result: {name}") from exc
