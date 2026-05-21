"""Connection profile resolution and virtualenv binding helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from virtuoso_bridge.env import default_user_env_path, get_runtime_env_file

PROFILE_BINDING_FILENAME = ".virtuoso-bridge-profile"


@dataclass(frozen=True)
class ProfileResolution:
    profile: str | None
    source: str
    path: Path | None = None


def _clean_profile(value: str | None) -> str | None:
    profile = (value or "").strip()
    return profile or None


def venv_profile_path(venv: str | Path | None = None) -> Path | None:
    """Return the profile binding path for a virtualenv, if one is active."""
    raw = venv if venv is not None else os.environ.get("VIRTUAL_ENV", "")
    if not str(raw).strip() and sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        raw = sys.prefix
    if not str(raw).strip():
        return None
    root = Path(raw).expanduser()
    return root.resolve() / PROFILE_BINDING_FILENAME


def _read_profile_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            profile = _clean_profile(line)
            if profile and not profile.startswith("#"):
                return profile
    except OSError:
        return None
    return None


def _profile_from_env_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return _clean_profile(dotenv_values(path).get("VB_PROFILE"))
    except OSError:
        return None


def resolve_profile_info(explicit: str | None = None) -> ProfileResolution:
    """Resolve the active connection profile and explain where it came from.

    Resolution order:
    1. explicit ``profile=`` argument
    2. process environment ``VB_PROFILE``
    3. runtime ``--env`` file, when one was explicitly selected
    4. active virtualenv binding file
    5. user-level ``~/.virtuoso-bridge/.env`` ``VB_PROFILE``
    6. ``None`` for the legacy default profile
    """
    profile = _clean_profile(explicit)
    if profile:
        return ProfileResolution(profile, "explicit")

    profile = _clean_profile(os.environ.get("VB_PROFILE"))
    if profile:
        return ProfileResolution(profile, "environment")

    runtime_env = get_runtime_env_file()
    profile = _profile_from_env_file(runtime_env)
    if profile:
        return ProfileResolution(profile, "runtime_env", runtime_env)

    venv_path = venv_profile_path()
    profile = _read_profile_file(venv_path)
    if profile:
        return ProfileResolution(profile, "venv", venv_path)

    user_env = default_user_env_path()
    profile = _profile_from_env_file(user_env)
    if profile:
        return ProfileResolution(profile, "user_env", user_env)

    return ProfileResolution(None, "default")


def resolve_profile(explicit: str | None = None) -> str | None:
    """Resolve the active connection profile, preserving legacy None fallback."""
    return resolve_profile_info(explicit).profile


def bind_venv_profile(profile: str, *, venv: str | Path | None = None) -> Path:
    """Bind the active virtualenv to a connection profile."""
    cleaned = _clean_profile(profile)
    if not cleaned:
        raise ValueError("profile must be a non-empty string")
    path = venv_profile_path(venv)
    if path is None:
        raise RuntimeError("No active virtualenv. Set VIRTUAL_ENV or pass --venv from an activated venv.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{cleaned}\n", encoding="utf-8")
    return path


def clear_venv_profile(*, venv: str | Path | None = None) -> Path:
    """Remove the active virtualenv profile binding if present."""
    path = venv_profile_path(venv)
    if path is None:
        raise RuntimeError("No active virtualenv. Set VIRTUAL_ENV or pass --venv from an activated venv.")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


def read_venv_profile(*, venv: str | Path | None = None) -> tuple[Path | None, str | None]:
    """Return ``(binding_path, profile)`` for the active virtualenv."""
    path = venv_profile_path(venv)
    return path, _read_profile_file(path)
