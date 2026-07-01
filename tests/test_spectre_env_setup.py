"""Tests for the Cadence/Spectre csh environment prelude (Lmod + cshrc)."""

from __future__ import annotations

import pytest

from virtuoso_bridge.spectre.runner import (
    DEFAULT_LMOD_INIT_CSH,
    cadence_env_setup_csh,
)

_ENV_VARS = (
    "VB_LMOD_MODULES",
    "VB_LMOD_INIT",
    "VB_CADENCE_CSHRC",
    "VB_MENTOR_CSHRC",
    "VB_LMOD_MODULES_worker1",
    "VB_CADENCE_CSHRC_worker1",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_empty_when_nothing_configured():
    assert cadence_env_setup_csh() == ""


def test_lmod_modules_use_default_init(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "cadence/IC25.1 spectre/23.1")
    out = cadence_env_setup_csh()
    assert f"source {DEFAULT_LMOD_INIT_CSH}" in out
    # Load via the Lmod backend ($LMOD_CMD), not the parse-time `module` alias.
    assert "eval `$LMOD_CMD csh load cadence/IC25.1 spectre/23.1`" in out
    assert "module load" not in out
    # Guarded source so a wrong path is harmless when Lmod is already defined.
    assert out.startswith(f"if ( -f {DEFAULT_LMOD_INIT_CSH} )")
    assert "if ( $?LMOD_CMD )" in out


def test_lmod_modules_accept_comma_separator(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "cadence/IC25.1, spectre/23.1")
    assert "csh load cadence/IC25.1 spectre/23.1`" in cadence_env_setup_csh()


def test_lmod_init_override(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_LMOD_INIT", "/opt/lmod/init/csh")
    out = cadence_env_setup_csh()
    assert "/opt/lmod/init/csh" in out
    assert DEFAULT_LMOD_INIT_CSH not in out


def test_modules_then_cshrc_layering(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    monkeypatch.setenv("VB_MENTOR_CSHRC", "/home/x/mentor.cshrc")
    out = cadence_env_setup_csh()
    # Modules load first, then cshrc files source (later layers win).
    assert out.index("csh load") < out.index("source /home/x/cad.cshrc")
    assert out.index("/home/x/cad.cshrc") < out.index("/home/x/mentor.cshrc")


def test_cshrc_only_without_modules(monkeypatch):
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    out = cadence_env_setup_csh()
    assert out == "source /home/x/cad.cshrc"
    assert "module load" not in out


def test_profile_suffix_overrides_with_fallback(monkeypatch):
    # Suffixed modules win; unsuffixed cshrc still falls through.
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/base")
    monkeypatch.setenv("VB_LMOD_MODULES_worker1", "spectre/24")
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    out = cadence_env_setup_csh("_worker1")
    assert "csh load spectre/24`" in out
    assert "spectre/base" not in out
    assert "source /home/x/cad.cshrc" in out


def test_paths_with_spaces_are_quoted(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_LMOD_INIT", "/opt/my lmod/init/csh")
    out = cadence_env_setup_csh()
    assert "'/opt/my lmod/init/csh'" in out
