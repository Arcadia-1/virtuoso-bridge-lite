"""Unified local site configuration for SMIC180 IO Ring skills.

The primary user-edited file is ``_local/site.yaml`` at repository root.
Existing environment variables remain the highest-priority runtime override.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class SiteConfigError(RuntimeError):
    """Raised when site configuration exists but is malformed."""


_ENV_MAP: dict[tuple[str, ...], tuple[str, ...]] = {
    ("project", "output_root"): ("AMS_OUTPUT_ROOT",),
    ("generator", "draft_editor"): ("AMS_DRAFT_EDITOR",),
    ("generator", "layout_editor"): ("AMS_LAYOUT_EDITOR",),
    ("bridge", "fs_mode"): ("VB_FS_MODE",),
    ("cadence", "cds_lib_180"): ("CDS_LIB_PATH_180", "SIM_CDS_LIB"),
    ("cadence", "ic_root"): ("SIM_IC_ROOT",),
    ("cadence", "mmsim_root"): ("SIM_MMSIM_ROOT",),
    ("calibre", "mgc_home"): ("MGC_HOME",),
    ("calibre", "pdk_layermap_180"): ("PDK_LAYERMAP_180",),
    ("calibre", "lvs_include_180"): ("incFILE_180",),
    ("spectre", "io_model_include"): ("SIM_PDK_IO_SPECTRE_INCLUDE",),
    ("spectre", "core_model_include"): ("SIM_PDK_CORE_SPECTRE_INCLUDE",),
    ("spectre", "core_sections"): ("SIM_PDK_CORE_SPECTRE_SECTIONS",),
    ("spectre", "lm_license_file"): ("SIM_LM_LICENSE_FILE", "LM_LICENSE_FILE"),
    ("spectre", "cds_lic_file"): ("SIM_CDS_LIC_FILE", "CDS_LIC_FILE"),
}

_REQUIRED_KEYS: tuple[tuple[str, ...], ...] = (
    ("project", "output_root"),
    ("bridge", "fs_mode"),
    ("cadence", "cds_lib_180"),
    ("cadence", "ic_root"),
    ("cadence", "mmsim_root"),
    ("calibre", "mgc_home"),
    ("calibre", "pdk_layermap_180"),
    ("calibre", "lvs_include_180"),
    ("spectre", "io_model_include"),
    ("spectre", "core_model_include"),
    ("spectre", "core_sections"),
    ("spectre", "lm_license_file"),
    ("spectre", "cds_lic_file"),
)

_MODE_KEYS = {
    ("generator", "draft_editor"),
    ("generator", "layout_editor"),
}


def find_repo_root(start: str | Path | None = None) -> Path:
    """Find the repository root from ``start`` or current working directory."""
    if start is None:
        start_path = Path.cwd().resolve(strict=False)
    else:
        start_path = Path(start).resolve(strict=False)
        if start_path.is_file():
            start_path = start_path.parent

    for candidate in [start_path, *start_path.parents]:
        if (candidate / "skills").is_dir() and (
            (candidate / ".git").exists() or (candidate / "_local").exists()
        ):
            return candidate
        if (candidate / "_local" / "site.yaml").is_file():
            return candidate
    return start_path


def site_config_path(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root).resolve(strict=False) if repo_root else find_repo_root()
    return root / "_local" / "site.yaml"


def _yaml_load(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise SiteConfigError(
            "PyYAML is required to read _local/site.yaml. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SiteConfigError(f"Failed to parse {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SiteConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def load_site_config(
    repo_root: str | Path | None = None,
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Load ``_local/site.yaml``.

    Returns an empty dict when the file is absent unless ``required`` is true.
    """
    path = site_config_path(repo_root)
    if not path.is_file():
        if required:
            raise SiteConfigError(
                f"Missing {path}. Copy _local/site.yaml.template to _local/site.yaml "
                "and fill in site-specific values."
            )
        return {}
    return _yaml_load(path)


def get_site_value(config: dict[str, Any], key_path: tuple[str, ...]) -> Any:
    value: Any = config
    for part in key_path:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _bool_string(value: Any) -> str:
    """Convert a boolean-like value to ``"1"`` or ``"0"``."""
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return "1"
    if text in {"0", "false", "no", "off"}:
        return "0"
    return _stringify(value)


def _mode_string(value: Any) -> str:
    """Normalise YAML ``on``/``off`` booleans to ``"on"``/``"off"`` strings."""
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return "on"
    if text in {"0", "false", "no", "off"}:
        return "off"
    return text


def _set_if_unset(name: str, value: str, *, override: bool) -> None:
    if not value:
        return
    if override or not os.environ.get(name, "").strip():
        os.environ[name] = value


def apply_site_config(
    repo_root: str | Path | None = None,
    *,
    override: bool = False,
    required: bool = False,
) -> dict[str, Any]:
    """Apply ``_local/site.yaml`` values to ``os.environ``.

    Existing environment variables win by default. Use ``override=True`` only for
    explicit tooling such as config export commands.
    """
    config = load_site_config(repo_root, required=required)
    if not config:
        return {}

    for key_path, env_names in _ENV_MAP.items():
        raw_value = get_site_value(config, key_path)
        value = _mode_string(raw_value) if key_path in _MODE_KEYS else _stringify(raw_value)
        for env_name in env_names:
            _set_if_unset(env_name, value, override=override)

    disable_cm = get_site_value(config, ("bridge", "disable_control_master"))
    if disable_cm is not None:
        _set_if_unset("VB_DISABLE_CONTROL_MASTER", _bool_string(disable_cm), override=override)

    return config


def read_config_value(key: str, repo_root: str | Path | None = None) -> str:
    """Read a mapped value from ``os.environ`` or ``site.yaml``.

    Explicit environment variables are the highest-priority one-off override.
    When they are absent, this reads the raw value from ``site.yaml`` so remote
    Linux paths do not pass through shell path conversion.
    """
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val

    config = load_site_config(repo_root, required=False)
    for key_path, env_names in _ENV_MAP.items():
        if key in env_names:
            raw_value = get_site_value(config, key_path)
            value = _mode_string(raw_value) if key_path in _MODE_KEYS else _stringify(raw_value)
            if value:
                return value
    if key == "VB_DISABLE_CONTROL_MASTER":
        value = get_site_value(config, ("bridge", "disable_control_master"))
        if value is not None:
            return _bool_string(value)
    return ""


def validate_site_config(repo_root: str | Path | None = None) -> list[str]:
    """Return a list of human-readable validation errors."""
    errors: list[str] = []
    try:
        config = load_site_config(repo_root, required=True)
    except SiteConfigError as exc:
        return [str(exc)]

    for key_path in _REQUIRED_KEYS:
        value = _stringify(get_site_value(config, key_path))
        if not value:
            errors.append("missing required key: " + ".".join(key_path))

    fs_mode = _stringify(get_site_value(config, ("bridge", "fs_mode")))
    if fs_mode and fs_mode not in {"remote", "shared"}:
        errors.append("bridge.fs_mode must be 'remote' or 'shared'")

    for key_path in (("generator", "draft_editor"), ("generator", "layout_editor")):
        raw_value = get_site_value(config, key_path)
        value = _mode_string(raw_value)
        if value and value not in {"on", "off"}:
            errors.append(".".join(key_path) + " must be 'on' or 'off'")

    output_root = _stringify(get_site_value(config, ("project", "output_root")))
    if output_root:
        try:
            Path(output_root).expanduser().resolve(strict=False)
        except Exception as exc:
            errors.append(f"project.output_root is not a valid local path: {exc}")

    return errors


def _csh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_calibre_site_local_csh(repo_root: str | Path | None = None) -> str:
    """Render a csh env file for Calibre from ``_local/site.yaml``.

    Returns an empty string when no site config exists.
    """
    config = load_site_config(repo_root, required=False)
    if not config:
        return ""

    lines = [
        "#!/bin/csh",
        "# Generated from _local/site.yaml. Do not edit this runtime copy.",
    ]

    for key_path, env_name in (
        (("calibre", "mgc_home"), "MGC_HOME"),
        (("calibre", "pdk_layermap_180"), "PDK_LAYERMAP_180"),
        (("calibre", "lvs_include_180"), "incFILE_180"),
        (("cadence", "cds_lib_180"), "CDS_LIB_PATH_180"),
    ):
        value = _stringify(get_site_value(config, key_path))
        if value:
            lines.append(f"setenv {env_name} {_csh_quote(value)}")

    for key_path in (("cadence", "cadence_cshrc"), ("calibre", "mentor_cshrc")):
        value = _stringify(get_site_value(config, key_path))
        if value:
            lines.append(f"if ( -f {_csh_quote(value)} ) source {_csh_quote(value)}")

    return "\n".join(lines) + "\n"
