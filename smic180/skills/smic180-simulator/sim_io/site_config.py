"""Site configuration for the SMIC180 IO ring simulator.

Configuration source:
  1. Explicit SIM_* process environment variables.
  2. Repository ``_local/site.yaml``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent


def _apply_unified_site_config() -> None:
    for candidate in [_SIM_IO, *_SIM_IO.parents]:
        if (candidate / "skills").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                from tools.smic180_site_config import apply_site_config

                apply_site_config(candidate, override=False, required=False)
            except Exception as exc:
                raise RuntimeError(f"Failed to load _local/site.yaml: {exc}") from exc
            return


def _load_sim_env() -> None:
    """Load unified site config."""
    _apply_unified_site_config()


@dataclass
class SiteConfig:
    """Site-specific configuration for netlist export and simulation."""

    cds_lib: str
    ic_root: str
    pdk_spectre_include: str
    pdk_core_spectre_include: str = ""
    pdk_io_spectre_include: str = ""
    lm_license_file: str = ""
    cds_lic_file: str = ""
    mmsim_root: str = ""

    @property
    def si_bin(self) -> str:
        return f"{self.ic_root}/tools/dfII/bin/si"

    @property
    def spectre_bin_path(self) -> str:
        if self.mmsim_root:
            return f"{self.mmsim_root}/tools/bin/spectre"
        return ""

    @classmethod
    def from_env(cls) -> "SiteConfig":
        """Create SiteConfig from SIM_* env vars and _local/site.yaml."""
        _load_sim_env()

        cds_lib = os.getenv("SIM_CDS_LIB", "") or os.getenv("CDS_LIB_PATH_180", "")
        ic_root = os.getenv("SIM_IC_ROOT", "")
        mmsim_root = os.getenv("SIM_MMSIM_ROOT", "")

        if not cds_lib:
            raise ValueError("SIM_CDS_LIB not set. Add cadence.cds_lib_180 to _local/site.yaml.")
        if not ic_root:
            raise ValueError("SIM_IC_ROOT not set. Add cadence.ic_root to _local/site.yaml.")
        if not mmsim_root:
            raise ValueError("SIM_MMSIM_ROOT not set. Add cadence.mmsim_root to _local/site.yaml.")

        core_include = os.getenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "") or os.getenv("SIM_PDK_SPECTRE_INCLUDE", "")

        return cls(
            cds_lib=cds_lib,
            ic_root=ic_root,
            pdk_spectre_include=core_include,
            pdk_core_spectre_include=core_include,
            pdk_io_spectre_include=os.getenv("SIM_PDK_IO_SPECTRE_INCLUDE", ""),
            lm_license_file=os.getenv("SIM_LM_LICENSE_FILE", ""),
            cds_lic_file=os.getenv("SIM_CDS_LIC_FILE", ""),
            mmsim_root=mmsim_root,
        )