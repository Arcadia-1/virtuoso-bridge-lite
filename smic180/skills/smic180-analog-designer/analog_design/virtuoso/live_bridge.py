"""Default live Virtuoso materialization boundary."""

from __future__ import annotations

from pathlib import Path

from .live import NativeVirtuosoMaterializationClient


def create_client(run_dir: str | Path) -> NativeVirtuosoMaterializationClient:
    from virtuoso_bridge import VirtuosoClient
    from sim_io.sim.run import export_netlist
    from sim_io.site_config import SiteConfig

    bridge = VirtuosoClient.from_env()
    site = SiteConfig.from_env()

    def exporter(client, library: str, cell: str, output: Path) -> Path | None:
        generated = export_netlist(client, library, cell, Path(run_dir) / "virtuoso" / "si_export", site=site)
        if generated is None or not Path(generated).is_file():
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(Path(generated).read_bytes())
        return output

    return NativeVirtuosoMaterializationClient(bridge, exporter=exporter)