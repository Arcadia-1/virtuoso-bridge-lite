"""Install a new, administrator-owned shared bootstrap on the Cadence host.

Run with the project's Python environment. Never overwrites an installation;
use a new versioned destination for upgrades, then update .cdsinit explicitly.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

from virtuoso_bridge.transport.tunnel import _find_ramic_helper, _generate_virtuoso_setup_il


def install(destination: Path, python: str = "python3", scratch_root: str | None = None) -> Path:
    destination = destination.absolute()
    if not sys.platform.startswith("linux"):
        raise ValueError("Install the shared daemon bundle on its Linux Cadence host")
    for parent in destination.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022):
            raise ValueError("Shared code requires real parents owned by root or the trusted installing UID, "
                             "without group/other write access: %s" % parent)
    if scratch_root is not None and not (scratch_root.startswith("/") or scratch_root.startswith("~/")):
        raise ValueError("Scratch root must be absolute or start with ~/")
    destination.mkdir(mode=0o700)  # exclusive; keep incomplete contents private
    destination.chmod(0o700)
    for name in ("ramic_bridge.il", "ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py",
                 "ramic_owner.py", "ramic_request_recovery.py", "ramic_registry.py"):
        target = destination / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_find_ramic_helper(name).read_bytes())
            os.fchmod(stream.fileno(), 0o644)
    # The launcher contains no user, project, identity-file or fixed-port data.
    setup = destination / "virtuoso_setup.il"
    major = "27" if python.endswith("2.7") else "3"
    source = _generate_virtuoso_setup_il(
        (destination / ("ramic_bridge_daemon_%s.py" % major)).as_posix(),
        (destination / "ramic_bridge.il").as_posix(), python, port=0,
        scratch_root=scratch_root)
    fd = os.open(setup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(source)
        os.fchmod(stream.fileno(), 0o644)
    destination.chmod(0o755)
    return setup.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New protected directory, e.g. /opt/virtuoso-bridge/v1")
    parser.add_argument("--python", default="python3", help="Python executable on the Cadence host")
    parser.add_argument("--scratch-root", help="Same scratch root as clients; defaults to daemon environment or ~/.virtuoso-bridge")
    args = parser.parse_args()
    print("Trusted installer owns this bundle; all loading accounts trust that account.")
    print("Scratch root: " + (args.scratch_root or "daemon environment, then each UID's passwd home/.virtuoso-bridge"))
    print('load("%s")' % install(args.destination, args.python, args.scratch_root).as_posix())
