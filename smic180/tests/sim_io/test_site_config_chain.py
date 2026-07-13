import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from analog_opt.live import patch_smic180_corner
from sim_io.site_config import SiteConfig
from tools.smic180_site_config import apply_site_config


def _base_env(monkeypatch):
    monkeypatch.setenv("SIM_CDS_LIB", "/home/IC/cds.lib")
    monkeypatch.setenv("SIM_IC_ROOT", "/opt/cadence/IC")
    monkeypatch.setenv("SIM_MMSIM_ROOT", "/opt/cadence/MMSIM")


def test_site_yaml_exports_core_env_into_site_config_and_live_patcher(tmp_path, monkeypatch):
    core = "/home/IC/pdk/smic180/models/e2r018_v1p8_spe.scs"
    (tmp_path / "skills").mkdir()
    local = tmp_path / "_local"; local.mkdir()
    (local / "site.yaml").write_text(
        "spectre:\n  core_model_include: " + core + "\n  io_model_include: /home/IC/pdk/io/full_io.scs\n",
        encoding="utf-8",
    )
    for name in ("SIM_PDK_CORE_SPECTRE_INCLUDE", "SIM_PDK_SPECTRE_INCLUDE", "SIM_PDK_IO_SPECTRE_INCLUDE"):
        monkeypatch.delenv(name, raising=False)
    apply_site_config(tmp_path, override=True)
    _base_env(monkeypatch)
    monkeypatch.setattr("sim_io.site_config._load_sim_env", lambda: None)
    site = SiteConfig.from_env()
    assert site.pdk_core_spectre_include == core
    assert site.pdk_io_spectre_include.endswith("full_io.scs")
    models = [type("M", (), {"path": core, "section": "tt"})(), type("M", (), {"path": site.pdk_io_spectre_include, "section": "tt"})()]
    patched = patch_smic180_corner(type("D", (), {"model_includes": models})(), "fnsp", core_model_include=site.pdk_core_spectre_include)
    assert [item.section for item in patched.model_includes] == ["fnsp", "tt"]


def test_legacy_core_env_is_fallback_but_io_is_never_core(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setattr("sim_io.site_config._load_sim_env", lambda: None)
    monkeypatch.delenv("SIM_PDK_CORE_SPECTRE_INCLUDE", raising=False)
    monkeypatch.setenv("SIM_PDK_SPECTRE_INCLUDE", "/legacy/e2r018_v1p8_spe.scs")
    monkeypatch.setenv("SIM_PDK_IO_SPECTRE_INCLUDE", "/io/full_models.scs")
    site = SiteConfig.from_env()
    assert site.pdk_core_spectre_include == "/legacy/e2r018_v1p8_spe.scs"
    assert site.pdk_core_spectre_include != site.pdk_io_spectre_include
