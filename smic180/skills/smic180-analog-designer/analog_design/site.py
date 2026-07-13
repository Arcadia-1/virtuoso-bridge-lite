"""Site configuration adapter for the analog design skill."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


class SiteError(ValueError):
    """Raised when required shared site configuration is unavailable."""


def _load_repository_config() -> None:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "tools" / "smic180_site_config").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            from tools.smic180_site_config import apply_site_config
            apply_site_config(candidate, override=False, required=False)
            return


@dataclass(frozen=True)
class DesignSite:
    output_root: Path
    cds_lib: str
    ic_root: str
    mmsim_root: str
    model_include: str

    @classmethod
    def from_environment(cls, *, load_repository: bool = True) -> "DesignSite":
        if load_repository:
            try:
                _load_repository_config()
            except Exception as exc:
                raise SiteError(f"failed to load repository configuration: {exc}") from exc
        values = {
            "output_root": os.getenv("AMS_OUTPUT_ROOT", ""),
            "cds_lib": os.getenv("SIM_CDS_LIB", "") or os.getenv("CDS_LIB_PATH_180", ""),
            "ic_root": os.getenv("SIM_IC_ROOT", ""),
            "mmsim_root": os.getenv("SIM_MMSIM_ROOT", ""),
            "model_include": os.getenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "") or os.getenv("SIM_PDK_SPECTRE_INCLUDE", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise SiteError(f"shared site configuration is missing: {', '.join(missing)}")
        return cls(Path(values["output_root"]), values["cds_lib"], values["ic_root"], values["mmsim_root"], values["model_include"])
