"""Small typed AST for deterministic Spectre output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, order=True)
class SpectreInclude:
    path: str
    section: str | None = None


@dataclass(frozen=True)
class SpectreInstance:
    name: str
    nodes: tuple[str, ...]
    model: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class SpectreAnalysis:
    name: str
    kind: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class SpectreDeck:
    title: str
    digest: str
    includes: tuple[SpectreInclude, ...]
    ports: tuple[str, ...]
    instances: tuple[SpectreInstance, ...]
    analyses: tuple[SpectreAnalysis, ...]
    saves: tuple[str, ...]
