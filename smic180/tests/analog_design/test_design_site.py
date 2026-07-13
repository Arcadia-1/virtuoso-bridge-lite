import os

import pytest

from analog_design.site import DesignSite, SiteError


def test_site_reuses_repository_configuration_environment(monkeypatch):
    monkeypatch.setenv("AMS_OUTPUT_ROOT", "D:/runs")
    monkeypatch.setenv("SIM_CDS_LIB", "/pdk/cds.lib")
    monkeypatch.setenv("SIM_IC_ROOT", "/cadence/IC")
    monkeypatch.setenv("SIM_MMSIM_ROOT", "/cadence/MMSIM")
    monkeypatch.setenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "/pdk/models/tt.scs")
    site = DesignSite.from_environment()
    assert site.output_root.as_posix().endswith("D:/runs")
    assert site.cds_lib == "/pdk/cds.lib"
    assert site.model_include == "/pdk/models/tt.scs"


def test_site_requires_existing_configuration_chain(monkeypatch):
    for name in ("AMS_OUTPUT_ROOT", "SIM_CDS_LIB", "SIM_IC_ROOT", "SIM_MMSIM_ROOT", "SIM_PDK_CORE_SPECTRE_INCLUDE", "SIM_PDK_SPECTRE_INCLUDE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SiteError, match="configuration"):
        DesignSite.from_environment(load_repository=False)
