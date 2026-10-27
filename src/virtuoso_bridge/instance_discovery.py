"""Client-side selection of private daemon instance records."""
from __future__ import annotations

import posixpath
import re

DAEMON_BUILD = "instance-discovery-2"


def compatibility_message(caps: dict) -> str:
    build = caps.get("build")
    if build == DAEMON_BUILD:
        return "daemon build: " + DAEMON_BUILD
    return ("Daemon build %r differs from client bundle %s; compatibility is determined by "
            "signed protocol/capabilities. To upgrade, RBStop() in the intended idle CIW, "
            "wait for its child to exit, then load the new bootstrap. No automatic replacement."
            % (build if build is not None else "unknown (older daemon)", DAEMON_BUILD))


class InstanceSelectionError(RuntimeError):
    pass


class DiscoveryError(RuntimeError):
    """Transport, platform, or registry failure, rather than an empty selection."""


def validate_selector(work_dir: str = "", port: int | None = None) -> None:
    if work_dir and not work_dir.startswith("/"):
        raise InstanceSelectionError("VB_REMOTE_WORK_DIR must be an absolute remote path")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Port must be between 1 and 65535")


def validate_record(record: object) -> dict:
    """Validate untrusted discovery/saved metadata before endpoint or cache use."""
    if not isinstance(record, dict):
        raise ValueError("Instance record must be an object")
    for field in ("port", "pid", "daemon_pid", "uid"):
        value = record.get(field)
        if type(value) is not int or value < (0 if field == "uid" else 1):
            raise ValueError("Invalid instance record " + field)
    if record["port"] > 65535:
        raise ValueError("Invalid instance record port")
    if not isinstance(record.get("work_dir"), str) or not record["work_dir"].startswith("/"):
        raise ValueError("Invalid instance record working directory")
    if not isinstance(record.get("user"), str) or not record["user"]:
        raise ValueError("Invalid instance record user")
    if not isinstance(record.get("instance"), str) or not re.fullmatch(r"[0-9a-f]{32}", record["instance"]):
        raise ValueError("Invalid instance record identity")
    return record


def format_instances(rows: list[dict]) -> str:
    """Escape terminal control characters without obscuring ordinary paths."""
    def escaped(value: str) -> str:
        return value.encode("unicode_escape").decode("ascii")
    return "\n".join("  %s  %s  %s  %s" % (
        row["port"], row["pid"], escaped(row["work_dir"]), row.get("instance", "")) for row in rows)


class InstanceConnectionError(RuntimeError):
    """The selected daemon did not authenticate through the open endpoint."""


def select_instance(rows: list[dict], work_dir: str = "", port: int | None = None) -> dict | None:
    validate_selector(work_dir, port)
    selected = [row for row in rows
                if (not work_dir or posixpath.normpath(row["work_dir"]) == posixpath.normpath(work_dir))
                and (port is None or row["port"] == port)]
    if len(selected) > 1:
        listing = format_instances(selected)
        raise InstanceSelectionError(
            "Multiple Virtuoso instances match (port / Virtuoso PID / working directory / instance):\n"
            + listing + "\nRun virtuoso-bridge start --port PORT to select exactly one instance."
        )
    if not selected and port is not None:
        raise InstanceSelectionError(
            "No valid instance matches --port/VB_REMOTE_PORT %d and the configured user/working directory. "
            "Unset an old VB_REMOTE_PORT intentionally to use automatic selection.\nAvailable instances:\n%s"
            % (port, format_instances(rows)))
    return selected[0] if selected else None
