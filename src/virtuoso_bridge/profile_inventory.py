"""Read-only inventory of configured profiles, not running CIW identities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from virtuoso_bridge.env import resolve_env_path
from virtuoso_bridge.transport.remote_roles import HOST_ROLE_FALLBACKS, remote_host_roles_from_os


_HOST_KEYS = set(HOST_ROLE_FALLBACKS["daemon_host"])
_CONNECTION_KEYS = _HOST_KEYS | {"VB_REMOTE_USER", "VB_REMOTE_PORT", "VB_LOCAL_PORT", "VB_JUMP_HOST", "VB_JUMP_USER"}
_GLOBAL_FIELDS = {
    "process": "VB_PROCESS",
    "process_version": "VB_PROCESS_VERSION",
    "process_variant": "VB_PROCESS_VARIANT",
    "process_env": "VB_CADENCE_CSHRC",
    "env_shell": "VB_CADENCE_ENV_SHELL",
    "spectre_bin": "VB_SPECTRE_BIN",
}
_PROFILE_KEYS = _CONNECTION_KEYS | set(_GLOBAL_FIELDS.values())


def list_profiles(*, env_file: str | Path | None = None) -> list[dict[str, Any]]:
    """Resolve a configuration snapshot without changing os.environ or connecting.

    Selected .env values override process settings, matching load_vb_env's
    default policy. Hosts/users/ports remain profile-scoped; Cadence settings
    and opaque process metadata use explicit profile values then globals.
    """
    values = dict(os.environ)
    path = resolve_env_path(env_file)
    file_values = dotenv_values(path) if path is not None else {}
    values.update({key: value for key, value in file_values.items() if value is not None})
    names: set[str] = set()
    for key in values:
        if key in _PROFILE_KEYS:
            continue
        for base in sorted(_PROFILE_KEYS, key=len, reverse=True):
            if key.startswith(base + "_") and values[key].strip():
                names.add(key[len(base) + 1:])
                break
    profiles: list[str | None] = sorted(names)
    if any(values.get(key, "").strip() for key in _CONNECTION_KEYS):
        profiles.insert(0, None)

    rows: list[dict[str, Any]] = []
    for profile in profiles:
        suffix = f"_{profile}" if profile else ""
        sources: dict[str, dict[str, Any]] = {}

        def get(field: str, *keys: str, global_fallback: bool = False) -> str | None:
            candidates = [key + suffix for key in keys]
            if global_fallback and suffix:
                candidates.extend(keys)
            for key in candidates:
                value = values.get(key, "").strip()
                if value:
                    sources[field] = {
                        "key": key,
                        "kind": "env_file" if file_values.get(key) is not None else "environment",
                        "path": str(path) if file_values.get(key) is not None else None,
                        "inherited": bool(suffix and key in keys),
                    }
                    return value
            sources[field] = {"kind": "default", "key": None, "path": None, "inherited": False}
            return None

        roles = remote_host_roles_from_os(profile, load=False, environ=values)
        row: dict[str, Any] = {"profile": profile, "runtime_verified": False}
        for role, keys in HOST_ROLE_FALLBACKS.items():
            row[role] = get(role, *keys)
        row["host"] = roles.daemon_host
        sources["host"] = dict(sources["daemon_host"])
        row["user"] = get("user", "VB_REMOTE_USER")
        row["jump"] = get("jump", "VB_JUMP_HOST")
        row["jump_user"] = get("jump_user", "VB_JUMP_USER")
        for field, key in _GLOBAL_FIELDS.items():
            row[field] = get(field, key, global_fallback=True)
        row["env_shell"] = row["env_shell"] or "csh"

        from virtuoso_bridge.virtuoso.basic.bridge import _default_remote_port
        errors: list[str] = []
        for field, key, default in (
            ("port", "VB_REMOTE_PORT", _default_remote_port(roles.remote_user, environ=values)),
            ("local_port", "VB_LOCAL_PORT", None),
        ):
            raw = get(field, key)
            try:
                port = int(raw) if raw is not None else default
                if port is not None and not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                errors.append(f"{key + suffix} must be an integer between 1 and 65535")
                port = default
            row[field] = port
        if row["local_port"] is None:
            row["local_port"] = row["port"]
            sources["local_port"]["fallback"] = "port"
        if not row["host"]:
            errors.append("No daemon/legacy host configured")
        if row["env_shell"] not in ("csh", "sh"):
            errors.append("VB_CADENCE_ENV_SHELL must be csh or sh")
        row.update(sources=sources, resolved=not errors, errors=errors)
        rows.append(row)
    return rows
