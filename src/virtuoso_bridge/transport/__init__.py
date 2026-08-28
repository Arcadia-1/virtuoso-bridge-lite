"""Shared SSH transport utilities."""

from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    remote_scratch_root,
    resolve_remote_username,
)
from virtuoso_bridge.transport.remote_roles import (
    RemoteHostRoles,
    remote_host_roles_from_os,
)
from virtuoso_bridge.transport.ssh import (
    SSHRunner,
    RemoteTaskResult,
    RemoteSshEnv,
    SshBackendEnv,
    run_remote_task,
    remote_ssh_env_from_os,
    ssh_backend_env_from_os,
    ssh_proxy_url_from_os,
)

__all__ = [
    "SSHRunner",
    "RemoteTaskResult",
    "RemoteSshEnv",
    "SshBackendEnv",
    "run_remote_task",
    "remote_ssh_env_from_os",
    "ssh_backend_env_from_os",
    "ssh_proxy_url_from_os",
    "default_virtuoso_bridge_dir",
    "remote_scratch_root",
    "resolve_remote_username",
    "RemoteHostRoles",
    "remote_host_roles_from_os",
]
