"""Install a new, administrator-owned shared bootstrap on the Cadence host.

Run with the project's Python environment. Never overwrites an installation;
use a new versioned destination for upgrades, then update .cdsinit explicitly.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil

from virtuoso_bridge.transport.tunnel import _find_ramic_helper, _generate_virtuoso_setup_il


def install(destination: Path, python: str = "python3", scratch_root: str | None = None) -> Path:
    destination = destination.absolute()
    if os.name == "posix":
        for parent in (destination.parent, *destination.parents):
            info = parent.lstat()
            if parent.is_symlink() or info.st_mode & 0o022:
                raise ValueError("Shared code requires parents that other users cannot write")
    destination.mkdir(mode=0o755)  # exclusive; no partial/old bundle replacement
    for name in ("ramic_bridge.il", "ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py",
                 "ramic_owner.py", "ramic_request_recovery.py", "ramic_registry.py", "ramic_token.py"):
        target = destination / name
        shutil.copyfile(_find_ramic_helper(name), target)
        target.chmod(0o644)
    # The launcher contains no user, project, identity-file or fixed-port data.
    setup = destination / "virtuoso_setup.il"
    major = "27" if python.endswith("2.7") else "3"
    setup.write_text(_generate_virtuoso_setup_il(
        (destination / ("ramic_bridge_daemon_%s.py" % major)).as_posix(),
        (destination / "ramic_bridge.il").as_posix(), python, port=0,
        scratch_root=scratch_root), encoding="utf-8")
    setup.chmod(0o644)
    destination.chmod(0o755)
    return setup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New protected directory, e.g. /opt/virtuoso-bridge/v1")
    parser.add_argument("--python", default="python3", help="Python executable on the Cadence host")
    parser.add_argument("--scratch-root", help="Same scratch root as clients; defaults to daemon environment or ~/.virtuoso-bridge")
    args = parser.parse_args()
    print('load("%s")' % install(args.destination, args.python, args.scratch_root).as_posix())
