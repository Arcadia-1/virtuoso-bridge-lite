"""Shared .env resolution for CLI and Python entry points."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_RUNTIME_ENV_FILE: Path | None = None


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
    # (contains VB_REMOTE_HOST or VB_LOCAL_PORT).  Skips unrelated .env files
    # belonging to other projects between the user's cwd and the VB workspace.
    for parent in [base_cwd, *base_cwd.parents]:
        candidate = parent / ".env"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "VB_REMOTE_HOST" in text or "VB_LOCAL_PORT" in text:
            return candidate

    user_env = default_user_env_path()
    if user_env.is_file():
        return user_env

    return None


def load_vb_env(explicit: str | Path | None = None, *, override: bool = True, cwd: Path | None = None) -> Path | None:
    env_path = resolve_env_path(explicit, cwd=cwd)
    if env_path is None:
        return None
    load_dotenv(env_path, override=override)
    return env_path
