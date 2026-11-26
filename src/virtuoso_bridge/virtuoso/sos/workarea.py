"""Controlled access to Cliosoft SOS workareas over the GUI SSH role."""

from __future__ import annotations

import posixpath
import shlex
from pathlib import PurePosixPath
from typing import Iterable, TYPE_CHECKING

from virtuoso_bridge.transport.ssh import CommandResult
from .environment import resolve_soscmd, sos_runner

if TYPE_CHECKING:
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    from .cellview import SOSCellViewResult


def _is_calibre_target(*values: str) -> bool:
    """Conservatively exclude named Calibre output from checkin, including ancestors."""
    return any("calibre" in value.casefold() for value in values)


def _paths(values: str | Iterable[str]) -> list[str]:
    items = [values] if isinstance(values, str) else list(values)
    if not items:
        raise ValueError("At least one SOS workarea path is required.")

    result: list[str] = []
    for value in items:
        raw = str(value).replace("\\", "/")
        if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
            raise ValueError(f"Invalid SOS path: {value!r}")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("SOS object paths must be relative to the workarea and cannot contain '..'.")
        normalized = path.as_posix()
        result.append("." if normalized == "." else f"./{normalized}")
    return result


def _option_value(value: str, label: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single-line value.")
    return value


class SOSOps:
    """Factory for operations scoped to an explicit SOS workarea."""

    def __init__(self, owner: "VirtuosoClient") -> None:
        self._owner = owner

    def status_cellview(
        self, lib: str, cell: str, view: str, *, timeout: float = 60, soscmd: str | None = None,
    ) -> SOSCellViewResult:
        """Resolve one cellview in CIW and read its SOS state without modifying it."""
        from .cellview import operate_cellview

        return operate_cellview(self._owner, "status", lib, cell, view, timeout=timeout, soscmd=soscmd)

    def checkout_cellview(
        self, lib: str, cell: str, view: str, *, dry_run: bool = False, timeout: float = 60,
        soscmd: str | None = None,
    ) -> SOSCellViewResult:
        """Check out one saved OA cellview through Virtuoso GDM, then verify SOS state."""
        from .cellview import operate_cellview

        return operate_cellview(
            self._owner, "co", lib, cell, view, dry_run=dry_run, timeout=timeout, soscmd=soscmd,
        )

    def cancel_checkout_cellview(
        self, lib: str, cell: str, view: str, *, dry_run: bool = False, timeout: float = 60,
        soscmd: str | None = None,
    ) -> SOSCellViewResult:
        """Release one clean checkout with SOS ``discardco``; never discard modifications."""
        from .cellview import operate_cellview

        return operate_cellview(
            self._owner, "cancel_co", lib, cell, view,
            dry_run=dry_run, timeout=timeout, soscmd=soscmd,
        )

    def checkin_cellview(
        self, lib: str, cell: str, view: str, *, message: str,
        dry_run: bool = False, timeout: float = 60,
        soscmd: str | None = None,
    ) -> SOSCellViewResult:
        """Check in one saved OA cellview through Virtuoso GDM; never retry a mutation."""
        from .cellview import operate_cellview

        return operate_cellview(
            self._owner, "ci", lib, cell, view,
            message=message, dry_run=dry_run, timeout=timeout, soscmd=soscmd,
        )

    def register_cellview(
        self, lib: str, cell: str, view: str, *, message: str,
        dry_run: bool = False, timeout: float = 60, soscmd: str | None = None,
    ) -> SOSCellViewResult:
        """Explicitly register one saved, positively identified unmanaged OA view."""
        from .cellview import operate_cellview

        return operate_cellview(
            self._owner, "register", lib, cell, view,
            message=message, dry_run=dry_run, timeout=timeout, soscmd=soscmd,
        )

    def diagnose_cellview(self, lib: str, cell: str, view: str, *,
                          timeout: float = 60, soscmd: str | None = None) -> dict:
        """Read installation, CIW capability and workarea/target health only."""
        from .diagnostics import diagnose_cellview

        return diagnose_cellview(self._owner, lib, cell, view, timeout=timeout, soscmd=soscmd)

    def diagnose_session_cellview(self, lib: str, cell: str, view: str, *,
                                  timeout: float = 60, soscmd: str | None = None) -> dict:
        """Inspect the existing SOS workarea session without starting a stopped one."""
        from .session import diagnose_session_cellview

        return diagnose_session_cellview(
            self._owner, lib, cell, view, timeout=timeout, soscmd=soscmd,
        )

    def restart_session_cellview(
        self, lib: str, cell: str, view: str, *, dry_run: bool = False,
        force_cadence_disconnect: bool = False, timeout: float = 60,
        soscmd: str | None = None,
    ) -> dict:
        """Restart one unhealthy SOS workarea session with explicit force consent."""
        from .session import restart_session_cellview

        return restart_session_cellview(
            self._owner, lib, cell, view, dry_run=dry_run,
            force_cadence_disconnect=force_cadence_disconnect,
            timeout=timeout, soscmd=soscmd,
        )

    def lock_info_cellview(self, lib: str, cell: str, view: str, *,
                           timeout: float = 60, soscmd: str | None = None):
        """Return exact server-queried checkout ownership for one cellview."""
        from .session import lock_info_cellview

        return lock_info_cellview(
            self._owner, lib, cell, view, timeout=timeout, soscmd=soscmd,
        )

    def reconcile_cellview(self, lib: str, cell: str, view: str, *, receipt: dict,
                           timeout: float = 60, soscmd: str | None = None) -> dict:
        """Compare an uncertain operation receipt with fresh state; never retry it."""
        from .diagnostics import reconcile_cellview

        return reconcile_cellview(self._owner, lib, cell, view, receipt=receipt,
                                  timeout=timeout, soscmd=soscmd)

    def attach(
        self,
        workarea: str,
        *,
        soscmd: str | None = None,
        timeout: float | None = None,
    ) -> "SOSWorkarea":
        """Validate and bind an absolute remote SOS workarea path."""
        executable = resolve_soscmd(self._owner, soscmd, timeout=timeout or self._owner._timeout)
        area = SOSWorkarea(self._owner, workarea, soscmd=executable)
        root_result = area._run("findwaroot", timeout=timeout)
        if root_result.returncode != 0:
            detail = root_result.stderr.strip() or root_result.stdout.strip()
            raise RuntimeError(f"SOS could not identify workarea {area.root}: {detail}")

        actual_root = next(
            (line.strip() for line in reversed(root_result.stdout.splitlines()) if line.strip()),
            "",
        )
        if not actual_root:
            raise RuntimeError(
                f"SOS did not return a workarea root for {area.root}."
            )
        if posixpath.normpath(actual_root) != area.root:
            raise RuntimeError(
                f"SOS workarea mismatch: requested {area.root}, command resolved {actual_root}"
            )

        version_result = area._run("version", timeout=timeout)
        if version_result.returncode != 0:
            detail = version_result.stderr.strip() or version_result.stdout.strip()
            raise RuntimeError(f"Could not read SOS version: {detail}")
        area.version = version_result.stdout.strip()
        return area


class SOSWorkarea:
    """SOS commands bound to one verified remote workarea."""

    def __init__(self, owner: "VirtuosoClient", root: str, *, soscmd: str) -> None:
        raw_root = str(root)
        path = PurePosixPath(raw_root)
        if not path.is_absolute() or ".." in path.parts or "\x00" in raw_root:
            raise ValueError("SOS workarea must be an absolute remote POSIX path without '..'.")
        if not soscmd or "\x00" in soscmd or "\n" in soscmd or "\r" in soscmd:
            raise ValueError("soscmd must be a non-empty executable path or command name.")
        self._owner = owner
        self.root = posixpath.normpath(raw_root)
        self.soscmd = soscmd
        self.version = ""

    def _run(self, *arguments: str, timeout: float | None = None,
             _ordinary_paths: list[str] | None = None) -> CommandResult:
        runner = sos_runner(self._owner)
        if runner is None:
            raise RuntimeError("SOS requires a GUI-host SSH runner or an explicitly local POSIX client.")
        guard = ""
        if _ordinary_paths:
            # Run all physical-file checks in the same shell before ANY mutation.
            guard = '''{ for vb_file in ''' + shlex.join(_ordinary_paths) + '''; do
  [ -f "$vb_file" ] && [ ! -L "$vb_file" ] || { printf '%s\\n' 'SOS writes require regular non-symlink files.' >&2; exit 65; }
  vb_parent=${vb_file%/*}
  while :; do
    [ ! -L "$vb_parent" ] && [ ! -e "$vb_parent/master.tag" ] || { printf '%s\\n' 'OA package or symlink ancestor; use cellview API.' >&2; exit 65; }
    [ "$vb_parent" = '.' ] && break
    vb_parent=${vb_parent%/*}
  done
done; } && '''
        command = "cd {root} && {guard}{argv}".format(
            root=shlex.quote(self.root),
            guard=guard,
            argv=shlex.join([self.soscmd, *arguments]),
        )
        return runner.run_command(command, timeout=timeout or self._owner._timeout)

    def _require_ordinary_files(self, paths: list[str]) -> None:
        if _is_calibre_target(self.root, *paths):
            raise ValueError("Calibre-related targets cannot be mutated through the ordinary-file API.")
        for path in paths:
            if path == "." or any(part.casefold().endswith(".oa") or part == "master.tag"
                                  for part in PurePosixPath(path).parts):
                raise ValueError("Ordinary-file writes reject workareas and OA files/packages.")
        from .cellview import _ONLINE_NOTICE, _STATUS_NOTICES

        # SOS object types distinguish an ordinary file from a package/reference.
        for path in paths:
            result = self._run("status", "-Nhdr", "-f%T\t%P", "-sall", "-sNr", path)
            lines = [
                line for line in result.stdout.splitlines()
                if line.strip() and line.strip() not in _STATUS_NOTICES
                and not _ONLINE_NOTICE.fullmatch(line.strip())
            ]
            if result.returncode or len(lines) != 1 or lines[0] not in {
                "f\t" + path, "f\t" + path[2:],
            }:
                raise RuntimeError("Target is not one positively identified ordinary SOS file; refusing write.")

    def status(
        self,
        paths: str | Iterable[str],
        *,
        checked_out_only: bool = False,
        recursive: bool = False,
    ) -> CommandResult:
        """Return SOS status for explicit paths, optionally filtering checkouts recursively."""
        args = ["status"]
        if checked_out_only:
            args.append("-sco")
        if recursive:
            args.append("-sr")
        elif checked_out_only:
            args.append("-sNr")
        args.extend(_paths(paths))
        return self._run(*args)

    def object_status(
        self,
        path: str,
        *,
        revision: str | None = None,
        attributes: Iterable[str] = (),
    ) -> CommandResult:
        """Return detailed status and optional attributes for one SOS object."""
        args = ["objstatus"]
        if revision is not None:
            args.append("-rev" + _option_value(revision, "revision"))
        for attribute in attributes:
            args.append("-ga" + _option_value(attribute, "attribute"))
        args.extend(_paths(path))
        return self._run(*args)

    def history(
        self,
        paths: str | Iterable[str],
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        user: str | None = None,
        commands: Iterable[str] = (),
        flat: bool = True,
    ) -> CommandResult:
        """Read history for explicit paths; SOS may also write a report in the workarea."""
        args = ["history"]
        if flat:
            args.append("-fs")
        if from_date is not None:
            args.append("-from" + _option_value(from_date, "from_date"))
        if to_date is not None:
            args.append("-to" + _option_value(to_date, "to_date"))
        if user is not None:
            args.append("-user" + _option_value(user, "user"))
        allowed_commands = {"create", "co", "ci", "tag", "termbranch"}
        for command in commands:
            command = _option_value(command, "history command").lower()
            if command not in allowed_commands:
                raise ValueError(f"Unsupported SOS history command filter: {command}")
            args.append("-cmd" + command)
        args.extend(_paths(paths))
        return self._run(*args)

    def diff(self, path: str, other: str | None = None) -> CommandResult:
        """Run SOS diff; SOS may also write diff.out in the remote workarea."""
        args = ["diff", *_paths(path)]
        if other is not None:
            args.extend(_paths(other))
        return self._run(*args)

    def checkout(
        self,
        paths: str | Iterable[str],
        *,
        branch: str | None = None,
        change_summary: str | None = None,
        concurrent: bool = False,
    ) -> CommandResult:
        """Check out ordinary SOS paths; use checkout_cellview for Virtuoso cellviews."""
        args = ["co"]
        if branch is not None:
            branch = _option_value(branch, "branch")
            if any(char.isspace() for char in branch):
                raise ValueError("SOS branch names cannot contain whitespace.")
            args.append("-b" + branch)
        if change_summary is not None:
            args.append("-achange_summary=" + _option_value(change_summary, "change_summary"))
        if concurrent:
            args.append("-C")
        paths = _paths(paths)
        self._require_ordinary_files(paths)
        args.extend(paths)
        return self._run(*args, _ordinary_paths=paths)

    def checkin(
        self,
        paths: str | Iterable[str],
        *,
        change_summary: str,
        log: str | None = None,
        keep_checked_out: bool = False,
    ) -> CommandResult:
        """Check in ordinary SOS paths; use checkin_cellview for Virtuoso cellviews."""
        paths = _paths(paths)
        if _is_calibre_target(self.root, *paths):
            raise ValueError("Calibre-related targets cannot be checked in through this API.")
        summary = _option_value(change_summary, "change_summary")
        checkin_log = _option_value(log if log is not None else summary, "log")
        args = [
            "ci",
            "-achange_summary=" + summary,
            "-aLog=" + checkin_log,
        ]
        if keep_checked_out:
            args.append("-kco")
        self._require_ordinary_files(paths)
        args.extend(paths)
        return self._run(*args, _ordinary_paths=paths)
