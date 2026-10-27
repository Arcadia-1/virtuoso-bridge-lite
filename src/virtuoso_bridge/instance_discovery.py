"""Client-side selection of private daemon instance records."""
from __future__ import annotations

import posixpath


class InstanceSelectionError(RuntimeError):
    pass


class InstanceConnectionError(RuntimeError):
    """The selected daemon did not authenticate through the open endpoint."""


def select_instance(rows: list[dict], work_dir: str = "", port: int | None = None) -> dict | None:
    if work_dir and not work_dir.startswith("/"):
        raise InstanceSelectionError("VB_REMOTE_WORK_DIR must be an absolute remote path")
    selected = [row for row in rows
                if (not work_dir or posixpath.normpath(row["work_dir"]) == posixpath.normpath(work_dir))
                and (port is None or row["port"] == port)]
    if len(selected) > 1:
        listing = "\n".join("  {port}  {pid}  {work_dir}".format(**row) for row in selected)
        raise InstanceSelectionError(
            "Multiple Virtuoso instances match (port / PID / working directory):\n"
            + listing + "\nRun start again with --port PORT to select exactly one instance."
        )
    if not selected and port is not None:
        raise InstanceSelectionError("No valid instance matches --port %d and the configured user/working directory" % port)
    return selected[0] if selected else None
