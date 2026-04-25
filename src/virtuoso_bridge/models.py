"""Data models and abstract interface for Virtuoso SKILL execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

class ExecutionStatus(str, Enum):
    """Execution status."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    ERROR = "error"

class VirtuosoResult(BaseModel):
    """Result from executing a SKILL command in Virtuoso."""

    status: ExecutionStatus
    output: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    execution_time: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True if execution succeeded."""
        return self.status == ExecutionStatus.SUCCESS

    @property
    def is_nil(self) -> bool:
        """True if SKILL returned ``nil``.

        In SKILL, ``nil`` is simultaneously boolean false, the empty list,
        and the "no value" sentinel.  Many Maestro/ADE functions (e.g.
        ``maeEnableTests``, ``maeSetVar``) return ``t`` on success and
        ``nil`` on failure or no-op — indistinguishable from a void return
        unless the caller checks explicitly.

        This property makes that check idiomatic::

            r = client.execute_skill('maeEnableTests()')
            if r.ok and not r.is_nil:
                ...  # something was actually enabled
        """
        return self.ok and (self.output or "").strip().strip('"') in ("nil", "")

    def save_json(self, path: Path, *, indent: int = 2, encoding: str = "utf-8") -> None:
        """Write result to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=indent), encoding=encoding)

# Compatibility alias
SkillResult = VirtuosoResult

class SimulationResult(BaseModel):
    """Result from running a Spectre (or other SPICE) simulation."""

    status: ExecutionStatus
    tool_version: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True if simulation succeeded."""
        return self.status == ExecutionStatus.SUCCESS

    def save_json(self, path: Path, *, indent: int = 2, encoding: str = "utf-8") -> None:
        """Write result to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=indent), encoding=encoding)

class VirtuosoInterface(ABC):
    """Abstract interface for Virtuoso SKILL execution."""

    @abstractmethod
    def ensure_ready(self, timeout: int = 10) -> VirtuosoResult:
        """Ensure bridge is ready (remote setup, tunnel, daemon reachable)."""

    @abstractmethod
    def execute_skill(self, skill_code: str, timeout: int = 30) -> VirtuosoResult:
        """Execute SKILL code in Virtuoso."""

    @abstractmethod
    def test_connection(self, timeout: int = 10) -> bool:
        """Test whether the daemon is reachable."""
