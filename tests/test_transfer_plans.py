from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from virtuoso_bridge.transport.transfer import (
    build_tar_download_plan,
    build_tar_upload_plans,
    build_text_upload_plan,
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
        "--",
        "first.txt",
        "second.txt",
    )
    script = shlex.split(plan.remote_command)[2]
    assert re.search(r"work=/remote/\.vbtmp-[0-9a-f]{32}", script)
    assert 'tar xf - -C "$payload"' in script
    assert "chmod 755 /remote" not in script
    assert 'mv -- /remote/second.txt "$state/backup-0"' in script
    assert 'mv -- /remote/first.txt "$state/backup-1"' in script
    first_install = 'mv -- "$payload"/first.txt /remote/second.txt'
    second_install = 'mv -- "$payload"/second.txt /remote/first.txt'
    assert first_install in script
    assert second_install in script
    assert script.index(': > "$state/installed-0"') < script.index(first_install)
    assert script.index(': > "$state/installed-1"') < script.index(second_install)


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

    windows_script = shlex.split(windows_plan.remote_command)[2]
    posix_script = shlex.split(posix_plan.remote_command)[2]
    assert 'find "$payload"/\'source tree\' -type d -exec chmod u+w {} +' in (
        windows_script
    )
    assert "find " not in posix_script
    assert windows_script.index("find ") < windows_script.index(
        'mv -- "$payload"/'
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
    assert 'cd "$d" && tar czf - -- "$b"' in inner_command


def test_upload_plans_split_duplicate_archive_names_across_local_dirs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "input.scs"
    second = tmp_path / "second" / "input.scs"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    plans = build_tar_upload_plans(
        "tar",
        [
            (first, "/remote/first.scs"),
            (second, "/remote/second.scs"),
        ],
    )

    assert len(plans) == 2
    assert [plan.file_count for plan in plans] == [1, 1]
    assert plans[0].local_command[-2:] == ("--", "input.scs")
    assert plans[1].local_command[-2:] == ("--", "input.scs")
    assert 'mv -- "$payload"/input.scs /remote/first.scs' in (
        plans[0].remote_command
    )
    assert 'mv -- "$payload"/input.scs /remote/second.scs' in (
        plans[1].remote_command
    )


def test_upload_plan_uses_option_terminator_for_dash_prefixed_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "-payload.scs"
    source.write_text("payload", encoding="utf-8")

    plan = build_tar_upload_plans(
        "tar",
        [(source, "/remote/-payload.scs")],
    )[0]

    assert plan.local_command[-2:] == ("--", "-payload.scs")


def test_upload_plan_rejects_duplicate_remote_targets(tmp_path: Path) -> None:
    first = tmp_path / "first.scs"
    second = tmp_path / "second.scs"
    first.touch()
    second.touch()

    with pytest.raises(ValueError, match="Duplicate remote upload target"):
        build_tar_upload_plans(
            "tar",
            [
                (first, "/remote/input.scs"),
                (second, "/remote/input.scs"),
            ],
        )


def test_text_upload_plan_stages_in_target_directory_before_install() -> None:
    plan = build_text_upload_plan("/remote/path/input.scs")
    script = shlex.split(plan.remote_command)[2]

    assert plan.work_path.startswith("/remote/path/.vbtmp-")
    assert 'cat > "$payload"' in script
    assert "chmod 755 /remote/path" not in script
    install = 'mv -fT -- "$payload" /remote/path/input.scs'
    assert install in script
    assert script.index('cat > "$payload"') < script.index(install)
    assert 'rm -rf -- "$work"' in script


def test_persistent_text_upload_preserves_exact_payload_via_base64() -> None:
    plan = build_text_upload_plan("/remote/input.scs")

    command = plan.persistent_command("line one\nline two")

    assert "bGluZSBvbmUKbGluZSB0d28=" in command
    assert 'base64 -d > "$payload"' in command
    assert 'mv -fT -- "$payload" /remote/input.scs' in command
