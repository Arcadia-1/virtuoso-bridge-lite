"""Atomic artifact and confirmation management."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from collections.abc import Mapping
import tempfile
from typing import Any


class ArtifactError(ValueError):
    """Raised when an artifact cannot be safely created or verified."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_value(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ArtifactError("JSON values must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArtifactError("JSON object keys must be strings")
            _strict_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _strict_value(item)
        return
    raise ArtifactError(f"unsupported JSON value: {type(value).__name__}")


class ArtifactStore:
    LAYOUT = (
        "inputs", "topology", "sizing", "ir", "windows_sim/generated",
        "windows_sim/iterations", "frozen", "virtuoso", "equivalence",
        "simulator", "optimizer", "reports", "manifests", "audit",
    )

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def create_run(self, name: str) -> Path:
        root = self.output_root / "analog_design"
        run = root / name
        if run.exists():
            raise ArtifactError(f"run directory already exists: {run}")
        run.mkdir(parents=True)
        for relative in self.LAYOUT:
            (run / relative).mkdir(parents=True, exist_ok=True)
        self.write_text(root / ".latest_run", str(run.resolve()) + "\n")
        return run

    def write_text(self, path: str | Path, text: str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return target

    def write_json(self, path: str | Path, value: Any) -> Path:
        _strict_value(value)
        def plain(item: Any) -> Any:
            if isinstance(item, Mapping):
                return {str(key): plain(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [plain(child) for child in item]
            return item
        return self.write_text(path, json.dumps(plain(value), allow_nan=False, indent=2, sort_keys=True) + "\n")

    def confirm(self, path: str | Path, gate: str, artifacts: list[str | Path]) -> Path:
        records = []
        marker = Path(path)
        for artifact in artifacts:
            resolved = Path(artifact).resolve()
            if not resolved.is_file():
                raise ArtifactError(f"confirmation artifact is missing: {resolved}")
            records.append({"path": str(resolved), "sha256": file_sha256(resolved)})
        return self.write_json(marker, {"gate": gate, "artifacts": records})

    def verify_confirmation(self, path: str | Path) -> None:
        marker = Path(path)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"invalid confirmation marker {marker}: {exc}") from exc
        for record in data.get("artifacts", []):
            artifact = Path(record["path"])
            if not artifact.is_file() or file_sha256(artifact) != record.get("sha256"):
                raise ArtifactError(f"confirmation hash mismatch: {artifact}")

