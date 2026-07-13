"""Library mapping guard for Virtuoso-backed simulator edits."""

from __future__ import annotations

import shlex

from virtuoso_bridge import VirtuosoClient

from sim_io.site_config import SiteConfig


def _skill_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_remote_path(path: str) -> str:
    return (path or "").strip().strip('"').rstrip("/")


def _expected_lib_path_from_cds_lib(cds_lib: str, lib: str) -> str:
    """Return the direct DEFINE path for ``lib`` in configured cds.lib, if any."""
    try:
        from virtuoso_bridge import SSHClient

        ssh = SSHClient.from_env()
        cmd = (
            "awk "
            + shlex.quote(f'$1=="DEFINE" && $2=="{lib}" {{print $3; exit}}')
            + " "
            + shlex.quote(cds_lib)
        )
        result = ssh.run_command(cmd, timeout=15)
        if getattr(result, "returncode", 1) != 0:
            return ""
        return _normalize_remote_path(getattr(result, "stdout", "") or "")
    except Exception:
        return ""


def require_configured_library_mapping(client: VirtuosoClient, lib: str) -> None:
    """Fail if Virtuoso's loaded library table disagrees with configured cds.lib."""
    site = SiteConfig.from_env()
    lib_s = _skill_string(lib)
    result = client.execute_skill(
        f'let((libObj) libObj=ddGetObj("{lib_s}") '
        f'if(libObj sprintf(nil "%s" libObj~>readPath) "nil"))',
        timeout=15,
    )
    actual = _normalize_remote_path(result.output or "")
    if not actual or actual == "nil":
        raise RuntimeError(
            f"Virtuoso library '{lib}' is not loaded. Start Virtuoso with "
            f"CDS_LIB={site.cds_lib} or add the library before running simulation."
        )

    expected = _expected_lib_path_from_cds_lib(site.cds_lib, lib)
    if expected and actual != expected:
        cwd = client.execute_skill("getWorkingDir()", timeout=10).output or ""
        session_cds = client.execute_skill('getShellEnvVar("CDS_LIB")', timeout=10).output or ""
        raise RuntimeError(
            "Virtuoso library mapping mismatch for "
            f"{lib}: active session maps to {actual}, but configured cds.lib maps to "
            f"{expected}. Session cwd={cwd.strip()}, CDS_LIB={session_cds.strip()}. "
            "Restart Virtuoso from the configured project/cds.lib or reload the correct "
            "library mapping before running simulation."
        )
