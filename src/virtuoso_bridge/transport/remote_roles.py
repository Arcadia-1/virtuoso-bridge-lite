"""Resolve the remote hosts that participate in a bridge profile.

``VB_REMOTE_HOST`` remains the one-host compatibility setting.  The explicit
role variables let deployments describe installations where the Virtuoso GUI,
the RAMIC daemon, file staging, and Spectre do not run on the same machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.profile import resolve_profile


@dataclass(frozen=True)
class RemoteHostRoles:
    """Resolved host roles for one profile.

    ``legacy_host`` is the configured ``VB_REMOTE_HOST`` value, if any.  The
    other fields are fully resolved fallbacks and may therefore be populated
    even when their role-specific environment variable was not set.
    """

    legacy_host: str | None
    gui_host: str | None
    deploy_host: str | None
    daemon_host: str | None
    spectre_host: str | None
    remote_user: str | None
    jump_host: str | None
    jump_user: str | None

    def jump_for(self, target_host: str | None) -> str | None:
        """Return the shared jump host unless it is the target itself."""
        if not target_host or not self.jump_host:
            return self.jump_host
        if _same_host_text(target_host, self.jump_host):
            return None
        return self.jump_host


def _same_host_text(left: str, right: str) -> bool:
    return left.strip().rstrip(".").lower() == right.strip().rstrip(".").lower()


def remote_host_roles_from_os(
    profile: str | None = None,
    *,
    load: bool = True,
) -> RemoteHostRoles:
    """Read and resolve profile-aware host roles from ``VB_*`` variables.

    Fallback order preserves the historical one-host model:

    - GUI defaults to ``VB_REMOTE_HOST``.
    - deployment defaults to the GUI host.
    - daemon defaults to ``VB_REMOTE_HOST``, then the deployment host.
    - Spectre defaults to ``VB_REMOTE_HOST``, then the daemon host.

    When ``VB_REMOTE_HOST`` is omitted, a pair such as ``VB_GUI_HOST`` plus
    ``VB_DAEMON_HOST`` is sufficient to describe a split-host installation.
    """
    profile = resolve_profile(profile)
    if load:
        load_vb_env()
    suffix = f"_{profile}" if profile else ""

    def _get(name: str) -> str | None:
        value = os.environ.get(f"{name}{suffix}", "").strip()
        return value or None

    legacy = _get("VB_REMOTE_HOST")
    explicit_gui = _get("VB_GUI_HOST")
    explicit_deploy = _get("VB_DEPLOY_HOST")
    explicit_daemon = _get("VB_DAEMON_HOST")
    explicit_spectre = _get("VB_SPECTRE_HOST")

    gui = explicit_gui or legacy or explicit_deploy or explicit_daemon or explicit_spectre
    deploy = explicit_deploy or explicit_gui or legacy or explicit_daemon or explicit_spectre
    daemon = explicit_daemon or legacy or explicit_deploy or explicit_gui or explicit_spectre
    spectre = explicit_spectre or legacy or explicit_daemon or explicit_deploy or explicit_gui

    return RemoteHostRoles(
        legacy_host=legacy,
        gui_host=gui,
        deploy_host=deploy,
        daemon_host=daemon,
        spectre_host=spectre,
        remote_user=_get("VB_REMOTE_USER"),
        jump_host=_get("VB_JUMP_HOST"),
        jump_user=_get("VB_JUMP_USER"),
    )
