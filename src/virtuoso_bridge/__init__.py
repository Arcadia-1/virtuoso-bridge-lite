"""virtuoso-bridge – Python bridge for executing SKILL in Cadence Virtuoso."""

from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("virtuoso-bridge")
except Exception:
    __version__ = "0.0.0-unknown"

from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.models import (
    ExecutionStatus,
    SimulationResult,
    SkillResult,
    VirtuosoResult,
)
from virtuoso_bridge.spectre.runner import SpectreSimulator
from virtuoso_bridge.wrappers import SanitizingClient

def decode_skill_output(raw: str | None) -> str:
    """Decode raw SKILL output: strip outer quotes, unescape \\\\n and \\\\"."""
    text = (raw or "").strip().strip('"')
    return text.replace("\\n", "\n").replace('\\"', '"')


__all__ = [
    "VirtuosoClient",
    "SSHClient",
    "SpectreSimulator",
    "SanitizingClient",
    "VirtuosoResult",
    "ExecutionStatus",
    "SkillResult",
    "SimulationResult",
    "decode_skill_output",
]
