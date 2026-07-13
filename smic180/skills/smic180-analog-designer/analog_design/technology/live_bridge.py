"""Default Virtuoso bridge construction for live technology discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .discovery import VirtuosoDiscoveryClient


def create_client(evidence_dir: str | Path, roundtrip: Mapping[str, Mapping[str, Any]]) -> VirtuosoDiscoveryClient:
    from virtuoso_bridge import VirtuosoClient

    return VirtuosoDiscoveryClient(VirtuosoClient.from_env(), evidence_dir, roundtrip)
