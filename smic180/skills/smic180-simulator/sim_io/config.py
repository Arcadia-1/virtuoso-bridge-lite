"""Path helpers for the SMIC180 IO ring simulator skill."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent


def _apply_unified_site_config() -> None:
    for candidate in [SKILL_ROOT, *SKILL_ROOT.parents]:
        if (candidate / "skills").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                from tools.smic180_site_config import apply_site_config

                apply_site_config(candidate, override=False, required=False)
            except Exception:
                pass
            return


def resolve_output_root() -> Path:
    """Resolve the shared SMIC180 output root.

    Priority:
    1. AMS_OUTPUT_ROOT
    2. AMS_IO_AGENT_PATH/output
    3. Repository root output when installed under <repo>/skills/<skill>
    4. Current working directory output
    """
    _apply_unified_site_config()

    env_root = os.environ.get("AMS_OUTPUT_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve(strict=False)

    agent_root = os.environ.get("AMS_IO_AGENT_PATH", "").strip()
    if agent_root:
        return (Path(agent_root).expanduser().resolve(strict=False) / "output")

    if SKILL_ROOT.parent.name == "skills":
        return (SKILL_ROOT.parent.parent / "output").resolve(strict=False)

    return (Path.cwd() / "output").resolve(strict=False)


def resolve_simulation_root() -> Path:
    """Return the simulator artifact root under the shared output root."""
    return resolve_output_root() / "simulation"


def latest_run_path() -> Path:
    """Return the preferred latest-run marker path."""
    return resolve_simulation_root() / ".latest_run"


def write_latest_run(run_dir: Path) -> None:
    """Write the latest-run marker under output/simulation."""
    marker = latest_run_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(run_dir.resolve(strict=False)), encoding="utf-8")


def read_latest_run() -> Path:
    """Read the current run directory from output/simulation/.latest_run."""
    marker = latest_run_path()
    if marker.exists():
        return Path(marker.read_text(encoding="utf-8").strip())

    raise FileNotFoundError(
        ".latest_run not found - run symbol_export first or pass --run-dir"
    )


def create_run_dir() -> Path:
    """Create a timestamped simulator run directory."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = resolve_simulation_root() / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    write_latest_run(run_dir)
    return run_dir
