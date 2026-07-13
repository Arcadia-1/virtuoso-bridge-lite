"""Live Spectre backend factory using the repository Bridge boundary."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .simulation.direct_spectre import BridgeSpectreRunner, DirectSpectreBackend
from .site import DesignSite


_PROVEN_MEASUREMENTS = (
    "gain",
    "ugbw",
    "output_dc",
    "supply_current",
    "power",
    "open_loop_slew_rate",
)


def _default_simulator_factory() -> Callable[[Path], Any]:
    site = DesignSite.from_environment()
    spectre_cmd = os.getenv("SPECTRE_CMD", "").strip() or str(PurePosixPath(site.mmsim_root) / "bin" / "spectre")

    def create(run_dir: Path) -> Any:
        from virtuoso_bridge.spectre.runner import SpectreSimulator

        return SpectreSimulator.from_env(
            spectre_cmd=spectre_cmd,
            spectre_args=(),
            timeout=600,
            work_dir=run_dir,
            output_format="psfascii",
        )

    return create


def create_backend(
    run_dir: str | Path,
    *,
    simulator_factory: Callable[[Path], Any] | None = None,
) -> DirectSpectreBackend:
    """Create the default nominal backend without claiming unsupported metrics."""

    del run_dir
    runner = BridgeSpectreRunner(simulator_factory or _default_simulator_factory())
    return DirectSpectreBackend(runner, _PROVEN_MEASUREMENTS, ("op", "ac", "tran"))