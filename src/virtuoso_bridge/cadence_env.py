"""Run POSIX commands in a site's explicitly selected Cadence environment.

Source in the selected shell, then inherit its exported variables directly.
Never parse or eval ``env`` output: values are data, not shell source.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class CadenceEnvironment:
    script: str = ""
    shell: str = "csh"

    def __post_init__(self) -> None:
        if self.shell not in ("csh", "sh"):
            raise ValueError("VB_CADENCE_ENV_SHELL must be csh or sh")

    @classmethod
    def from_env(cls, profile: str | None = None) -> CadenceEnvironment:
        """Read already-loaded settings; explicit profile values override globals."""
        suffix = f"_{profile}" if profile else ""

        def get(name: str) -> str:
            return os.environ.get(name + suffix, "").strip() or os.environ.get(name, "").strip()

        return cls(get("VB_CADENCE_CSHRC"), get("VB_CADENCE_ENV_SHELL") or "csh")

    def wrap(self, command: str) -> str:
        """Return a command inheriting exported variables from the selector.

        Preserve normal csh source semantics, including optional failed probes;
        selectors must exit nonzero when their own initialization fails.
        """
        if not self.script:
            return f"sh -c {shlex.quote(command)}"
        if self.shell == "csh":
            csh_body = (
                'if ( ! -r "$_VB_ENV_SCRIPT" ) exec sh -c "exit 1"; '
                'source "$_VB_ENV_SCRIPT" > /dev/stderr; '
                "set vb_source_status = $status; "
                'if ($vb_source_status != 0) exec sh -c "exit $vb_source_status"; '
                'exec sh -c "$_VB_ENV_COMMAND"'
            )
            body = (
                f"exec env _VB_ENV_SCRIPT={shlex.quote(self.script)} "
                f"_VB_ENV_COMMAND={shlex.quote(command)} "
                f"csh -f -c {shlex.quote(csh_body)}"
            )
        else:
            body = (
                f". {shlex.quote(self.script)} >&2 && "
                f"exec sh -c {shlex.quote(command)}"
            )
        body = (
            'HOSTNAME=$(hostname 2>/dev/null || echo localhost); export HOSTNAME; '
            'LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}; export LD_LIBRARY_PATH; '
            + body
        )
        return f"sh -c {shlex.quote(body)}"
