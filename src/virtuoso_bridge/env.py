"""Shared .env resolution for CLI and Python entry points."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

_RUNTIME_ENV_FILE: Path | None = None
_REPORTED_ENV_PATHS: set[Path] = set()


def default_user_env_path() -> Path:
    return Path.home() / ".virtuoso-bridge" / ".env"


def set_runtime_env_file(path: str | Path | None) -> Path | None:
    global _RUNTIME_ENV_FILE
    if path is None:
        _RUNTIME_ENV_FILE = None
        return None
    _RUNTIME_ENV_FILE = _normalize_env_path(path)
    return _RUNTIME_ENV_FILE


def get_runtime_env_file() -> Path | None:
    return _RUNTIME_ENV_FILE


def _normalize_env_path(path: str | Path, *, cwd: Path | None = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (cwd or Path.cwd()) / p
    return p.resolve()


def resolve_env_path(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path | None:
    base_cwd = (cwd or Path.cwd()).resolve()
    requested = explicit if explicit is not None else _RUNTIME_ENV_FILE
    if requested is not None:
        env_path = _normalize_env_path(requested, cwd=base_cwd)
        if not env_path.is_file():
            raise FileNotFoundError(f".env file not found: {env_path}")
        return env_path

    # Walk cwd upward and pick the first .env that looks like a VB config
    # (contains a VB host role or VB_LOCAL_PORT).  Skips unrelated .env files
    # belonging to other projects between the user's cwd and the VB workspace.
    for parent in [base_cwd, *base_cwd.parents]:
        candidate = parent / ".env"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        host_keys = (
            "VB_REMOTE_HOST",
            "VB_GUI_HOST",
            "VB_DEPLOY_HOST",
            "VB_DAEMON_HOST",
            "VB_SPECTRE_HOST",
        )
        if any(key in text for key in host_keys) or "VB_LOCAL_PORT" in text:
            return candidate

    user_env = default_user_env_path()
    if user_env.is_file():
        return user_env

    return None


def is_parent_directory_env(env_path: Path, *, cwd: Path | None = None) -> bool:
    """Whether the upward search took *env_path* from a directory above cwd."""
    if _RUNTIME_ENV_FILE is not None:
        return False
    base_cwd = (cwd or Path.cwd()).resolve()
    return (env_path != default_user_env_path() and env_path.parent != base_cwd
            and env_path.parent in base_cwd.parents)


def mark_env_reported(env_path: Path) -> None:
    """Record that the caller already told the user which .env is in use."""
    _REPORTED_ENV_PATHS.add(env_path)


def _report_parent_env(env_path: Path, cwd: Path | None) -> None:
    # A parent directory may hold another project's configuration (another
    # host, another licence setup); say so once instead of choosing silently.
    if env_path in _REPORTED_ENV_PATHS or not is_parent_directory_env(env_path, cwd=cwd):
        return
    _REPORTED_ENV_PATHS.add(env_path)
    host = dotenv_values(env_path).get("VB_REMOTE_HOST") or ""
    logger.warning(
        "Using .env from a parent directory: %s%s. Pin another file with "
        "--env FILE or virtuoso_bridge.env.set_runtime_env_file().",
        env_path, f" (VB_REMOTE_HOST={host})" if host else "",
    )


def load_vb_env(explicit: str | Path | None = None, *, override: bool = True, cwd: Path | None = None,
                report_parent: bool = True) -> Path | None:
    env_path = resolve_env_path(explicit, cwd=cwd)
    if env_path is None:
        return None
    load_dotenv(env_path, override=override)
    if report_parent and explicit is None and _RUNTIME_ENV_FILE is None:
        _report_parent_env(env_path, cwd)
    return env_path
