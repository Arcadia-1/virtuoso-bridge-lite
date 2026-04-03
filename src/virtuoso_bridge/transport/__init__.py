"""Shared SSH transport utilities."""

from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    remote_scratch_root,
    resolve_remote_username,
)
from virtuoso_bridge.transport.ssh import (
    SSHRunner,
    RemoteTaskResult,
    RemoteSshEnv,
    run_remote_task,
    remote_ssh_env_from_os,
)

__all__ = [
    "SSHRunner",
    "RemoteTaskResult",
    "RemoteSshEnv",
    "run_remote_task",
    "remote_ssh_env_from_os",
    "default_virtuoso_bridge_dir",
    "remote_scratch_root",
    "resolve_remote_username",
]
