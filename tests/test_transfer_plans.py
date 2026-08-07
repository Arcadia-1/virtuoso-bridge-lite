from __future__ import annotations

import re
import shlex
from pathlib import Path

from virtuoso_bridge.transport.transfer import (
    build_tar_download_plan,
    build_tar_upload_plans,
)


def test_batch_upload_plan_stages_all_renames_before_install(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    plans = build_tar_upload_plans(
        "tar",
        [
            (first, "/remote/second.txt"),
            (second, "/remote/first.txt"),
        ],
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.remote_dir == "/remote"
    assert plan.file_count == 2
    assert plan.local_command == (
        "tar",
        "cf",
        "-",
        "-C",
        str(tmp_path.absolute()).replace("\\", "/"),
        "first.txt",
        "-C",
        str(tmp_path.absolute()).replace("\\", "/"),
        "second.txt",
    )
    commands = plan.remote_command.split(" && ")
    assert commands[:2] == ["mkdir -p /remote", "tar xf - -C /remote"]
    assert re.fullmatch(r"mv /remote/first\.txt /remote/\.vbatch-[0-9a-f]{8}-0", commands[2])
    assert re.fullmatch(r"mv /remote/second\.txt /remote/\.vbatch-[0-9a-f]{8}-1", commands[3])
    assert re.fullmatch(r"mv /remote/\.vbatch-[0-9a-f]{8}-0 /remote/second\.txt", commands[4])
    assert re.fullmatch(r"mv /remote/\.vbatch-[0-9a-f]{8}-1 /remote/first\.txt", commands[5])


def test_windows_directory_upload_restores_remote_owner_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source tree"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")

    windows_plan = build_tar_upload_plans(
        "tar",
        [(source, "/remote/renamed tree")],
        platform_name="nt",
    )[0]
    posix_plan = build_tar_upload_plans(
        "tar",
        [(source, "/remote/renamed tree")],
        platform_name="posix",
    )[0]

    assert "find /remote/'source tree' -type d -exec chmod u+w {} +" in (
        windows_plan.remote_command
    )
    assert "find " not in posix_plan.remote_command
    assert windows_plan.remote_command.index("find ") < (
        windows_plan.remote_command.index("mv ")
    )


def test_download_plan_quotes_remote_path_and_stages_beside_target(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "downloads" / "installed"

    plan = build_tar_download_plan(
        "tar",
        "/remote/sim's results/netlist dir/",
        local_path,
    )

    assert plan.local_command == ("tar", "xzf", "-")
    assert plan.stage_path.parent == local_path.parent
    assert plan.stage_path.name.startswith(".vbtmp-")
    assert plan.staged_item == plan.stage_path / "netlist dir"
    inner_command = shlex.split(plan.remote_command)[2]
    assert shlex.split(inner_command)[0].removesuffix(";") == (
        "p=/remote/sim's results/netlist dir"
    )
    assert 'cd "$d" && tar czf - "$b"' in inner_command
