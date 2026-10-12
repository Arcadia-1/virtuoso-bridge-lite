from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.transport import transfer
from virtuoso_bridge.transport.transfer import (
    build_file_download_plan,
    build_tar_download_plan,
    build_tar_upload_plans,
    build_text_upload_plan,
    install_staged_item,
    install_staged_path,
)

# Cadence IC23.1 netlist directories hold real files whose names macOS bsdtar
# would otherwise read as AppleDouble metadata for the following entry.
_CADENCE_NETLIST_FILES = {
    "netlist/.__master.optionFile": b"simulatorOptions options reltol=1e-3\n" * 13,
    "netlist/designInfo": b"designInfo contents\n",
    "netlist/.__master.termorder": b"termorder\n" * 7,
    "netlist/input.scs": b"simulator lang=spectre\n",
}


def _cadence_netlist_archive() -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        directory = tarfile.TarInfo("netlist")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        for name, contents in _CADENCE_NETLIST_FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(contents))
    return payload.getvalue()


def _set_test_xattr(path: Path) -> None:
    xattr = shutil.which("xattr")
    if xattr is None:
        pytest.skip("requires the macOS xattr command")
    subprocess.run([xattr, "-w", "com.example.test", "1", str(path)], check=True)
    shown = subprocess.run(
        [xattr, "-p", "com.example.test", str(path)],
        capture_output=True,
        check=True,
    )
    assert shown.stdout.strip() == b"1"


def _fake_tar_version(monkeypatch, output: bytes) -> list[list[str]]:
    probes: list[list[str]] = []

    def run(command, **kwargs):
        probes.append(list(command))
        assert kwargs["timeout"] > 0
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(transfer, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transfer.subprocess, "run", run)
    return probes


def _decode_remote_script(command: str) -> str:
    argv = shlex.split(command)
    assert argv[:2] == ["bash", "-c"]
    match = re.fullmatch(
        r'eval "\$\(printf %s ([A-Za-z0-9+/=]+) \| base64 -d\)"',
        argv[2],
    )
    assert match is not None
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_file_download_plan_stages_beside_target(tmp_path: Path) -> None:
    local_path = tmp_path / "downloads" / "result.raw"

    plan = build_file_download_plan("/remote/results/output.raw", local_path)

    assert plan.remote_path == "/remote/results/output.raw"
    assert plan.local_path == local_path
    assert plan.stage_path.parent == local_path.parent
    assert plan.stage_path.name.startswith(".vbtmp-")
    assert plan.staged_item == plan.stage_path / "result.raw"


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
    script = _decode_remote_script(plan.remote_command)
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

    windows_script = _decode_remote_script(windows_plan.remote_command)
    posix_script = _decode_remote_script(posix_plan.remote_command)
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
    inner_command = _decode_remote_script(plan.remote_command)
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
        _decode_remote_script(plans[0].remote_command)
    )
    assert 'mv -- "$payload"/input.scs /remote/second.scs' in (
        _decode_remote_script(plans[1].remote_command)
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


def test_local_tar_probe_runs_only_on_macos(monkeypatch, real_tar_detection) -> None:
    probes = _fake_tar_version(monkeypatch, b"bsdtar 3.5.3 - libarchive 3.7.4\n")
    for platform in ("linux", "win32"):
        monkeypatch.setattr(transfer, "sys", SimpleNamespace(platform=platform))

        assert transfer._is_macos_bsdtar("/usr/bin/tar") is False

    assert probes == []


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (b"bsdtar 3.5.3 - libarchive 3.7.4 zlib/1.2.12 liblzma/5.4.3\n", True),
        (b"tar (GNU tar) 1.35\nCopyright (C) 2023 Free Software Foundation\n", False),
    ],
    ids=["bsdtar", "gnu-tar"],
)
def test_local_tar_probe_identifies_macos_bsdtar_once_per_path(
    monkeypatch,
    real_tar_detection,
    version: bytes,
    expected: bool,
) -> None:
    probes = _fake_tar_version(monkeypatch, version)

    assert transfer._is_macos_bsdtar("/usr/bin/tar") is expected
    assert transfer._is_macos_bsdtar("/usr/bin/tar") is expected
    assert transfer._is_macos_bsdtar("/opt/tools/bin/tar") is expected

    assert probes == [
        ["/usr/bin/tar", "--version"],
        ["/opt/tools/bin/tar", "--version"],
    ]


def test_unrunnable_local_tar_keeps_portable_commands(
    monkeypatch,
    real_tar_detection,
    tmp_path: Path,
) -> None:
    def missing_tar(command, **_kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(transfer, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transfer.subprocess, "run", missing_tar)

    plan = build_tar_download_plan(
        "/missing/tar",
        "/remote/netlist",
        tmp_path / "netlist",
    )

    assert plan.local_command == ("/missing/tar", "xzf", "-")


def test_macos_bsdtar_download_reads_appledouble_names_as_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checked: list[str] = []

    def is_macos_bsdtar(tar_command: str) -> bool:
        checked.append(tar_command)
        return True

    monkeypatch.setattr(transfer, "_is_macos_bsdtar", is_macos_bsdtar)

    plan = build_tar_download_plan(
        "/usr/bin/tar",
        "/remote/sim/netlist",
        tmp_path / "netlist",
    )

    assert checked == ["/usr/bin/tar"]
    assert plan.local_command == (
        "/usr/bin/tar",
        "--options",
        "!mac-ext",
        "-xzf",
        "-",
    )
    inner_command = _decode_remote_script(plan.remote_command)
    assert 'cd "$d" && tar czf - -- "$b"' in inner_command


def test_macos_bsdtar_upload_omits_appledouble_members(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "-second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    checked: list[str] = []

    def is_macos_bsdtar(tar_command: str) -> bool:
        checked.append(tar_command)
        return True

    monkeypatch.setattr(transfer, "_is_macos_bsdtar", is_macos_bsdtar)

    plans = build_tar_upload_plans(
        "/usr/bin/tar",
        [(first, "/remote/first.txt"), (second, "/remote/-second.txt")],
    )

    assert checked == ["/usr/bin/tar"]
    assert [plan.local_command for plan in plans] == [
        (
            "/usr/bin/tar",
            "--no-mac-metadata",
            "--no-xattrs",
            "-cf",
            "-",
            "-C",
            str(tmp_path.absolute()).replace("\\", "/"),
            "--",
            "first.txt",
            "-second.txt",
        )
    ]


def test_macos_bsdtar_download_command_keeps_appledouble_named_files(
    macos_bsdtar: str,
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "downloads" / "netlist"
    local_path.parent.mkdir()
    plan = build_tar_download_plan(macos_bsdtar, "/remote/sim/netlist", local_path)
    assert plan.local_command[1:3] == ("--options", "!mac-ext")
    plan.stage_path.mkdir()

    completed = subprocess.run(
        plan.local_command,
        input=_cadence_netlist_archive(),
        capture_output=True,
        cwd=plan.stage_path,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    install_staged_path(plan)
    assert sorted(path.name for path in local_path.iterdir()) == sorted(
        name.split("/", 1)[1] for name in _CADENCE_NETLIST_FILES
    )
    for name, contents in _CADENCE_NETLIST_FILES.items():
        assert (local_path / name.split("/", 1)[1]).read_bytes() == contents
    assert not list(local_path.parent.glob(".vbtmp-*"))


def test_macos_bsdtar_upload_command_omits_appledouble_members(
    macos_bsdtar: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    tree = source / "tree"
    tree.mkdir(parents=True)
    nested = tree / "nested.scs"
    nested.write_text("nested\n", encoding="utf-8")
    single = source / "single.scs"
    single.write_text("single\n", encoding="utf-8")
    for path in (tree, nested, single):
        _set_test_xattr(path)

    plans = build_tar_upload_plans(
        macos_bsdtar,
        [(tree, "/remote/tree"), (single, "/remote/single.scs")],
    )
    assert len(plans) == 1
    assert plans[0].local_command[1:4] == ("--no-mac-metadata", "--no-xattrs", "-cf")
    archive = subprocess.run(
        plans[0].local_command,
        capture_output=True,
        check=True,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(archive)) as listing:
        members = listing.getmembers()
    assert sorted(m.name for m in members) == ["single.scs", "tree", "tree/nested.scs"]
    # GNU tar warns about every macOS xattr record it cannot apply.
    assert not [key for m in members for key in m.pax_headers if "xattr" in key.lower()]


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
    payload = "simulator lang=spectre\n".encode("utf-8")
    plan = build_text_upload_plan("/remote/path/input.scs", payload)
    script = _decode_remote_script(plan.remote_command)

    assert plan.work_path.startswith("/remote/path/.vbtmp-")
    assert plan.payload_size == len(payload)
    assert plan.payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert 'cat > "$payload"' in script
    assert 'actual_size=$(LC_ALL=C wc -c < "$payload"' in script
    assert 'actual_sha256=$(sha256sum -- "$payload")' in script
    assert "chmod 755 /remote/path" not in script
    assert 'stat -c %a /remote/path/input.scs' in script
    assert 'stat -f %Lp /remote/path/input.scs' in script
    assert 'chmod "$existing_mode" "$payload"' in script
    install = 'mv -f "$payload" /remote/path/input.scs'
    assert install in script
    assert script.index('cat > "$payload"') < script.index("actual_size=")
    assert script.index("actual_sha256=") < script.index(install)
    assert 'rm -rf -- "$work"' in script


def test_persistent_text_upload_preserves_exact_payload_via_base64() -> None:
    payload = b"line one\nline two"
    plan = build_text_upload_plan("/remote/input.scs", payload)

    command = plan.persistent_command(payload)

    assert "bGluZSBvbmUKbGluZSB0d28=" in command
    assert 'base64 -d > "$payload"' in command
    assert f"expected_size={len(payload)}" in command
    assert hashlib.sha256(payload).hexdigest() in command
    assert 'mv -f "$payload" /remote/input.scs' in command


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX sh and sha256sum")
@pytest.mark.parametrize(
    ("sent_payload", "error_message"),
    [
        (b"expected payloa", "size mismatch"),
        (b"expected payloae", "SHA-256 mismatch"),
    ],
)
def test_text_upload_rejects_incomplete_or_corrupt_payload_before_install(
    tmp_path: Path,
    sent_payload: bytes,
    error_message: str,
) -> None:
    expected_payload = b"expected payload"
    target = tmp_path / "input.scs"
    target.write_bytes(b"original payload")
    plan = build_text_upload_plan(target.as_posix(), expected_payload)

    completed = subprocess.run(
        shlex.split(plan.remote_command),
        input=sent_payload,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 65
    assert error_message in completed.stderr.decode("utf-8")
    assert target.read_bytes() == b"original payload"
    assert not list(tmp_path.glob(".vbtmp-*"))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX file modes")
def test_text_upload_preserves_existing_target_mode(tmp_path: Path) -> None:
    target = tmp_path / "input.scs"
    target.write_bytes(b"original payload")
    target.chmod(0o640)
    payload = b"replacement payload"
    plan = build_text_upload_plan(target.as_posix(), payload)

    completed = subprocess.run(
        shlex.split(plan.remote_command),
        input=payload,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8")
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o7777 == 0o640
    assert not list(tmp_path.glob(".vbtmp-*"))


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("csh") is None,
    reason="requires POSIX tar and csh",
)
def test_tar_upload_command_survives_csh_login_shell(tmp_path: Path) -> None:
    source = tmp_path / "source" / "input.scs"
    source.parent.mkdir()
    source.write_text("simulator lang=spectre\n", encoding="utf-8")
    target = tmp_path / "remote" / "input.scs"
    plan = build_tar_upload_plans("tar", [(source, target.as_posix())])[0]
    archive = subprocess.run(
        plan.local_command,
        capture_output=True,
        check=True,
    ).stdout

    completed = subprocess.run(
        [shutil.which("csh") or "csh", "-fc", plan.remote_command],
        input=archive,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8")
    assert target.read_text(encoding="utf-8") == "simulator lang=spectre\n"
    assert not list(target.parent.glob(".vbtmp-*"))


def test_installed_item_remains_successful_when_backup_cleanup_fails(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    local_path = tmp_path / "installed"
    local_path.mkdir()
    (local_path / "old.txt").write_text("old", encoding="utf-8")
    stage_path = tmp_path / ".vbtmp-stage"
    staged_item = stage_path / "installed"
    staged_item.mkdir(parents=True)
    (staged_item / "new.txt").write_text("new", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def fail_backup_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".vbbak-"):
            raise PermissionError("backup is busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "virtuoso_bridge.transport.transfer.shutil.rmtree",
        fail_backup_cleanup,
    )

    with caplog.at_level(
        logging.WARNING,
        logger="virtuoso_bridge.transport.transfer",
    ):
        install_staged_item(stage_path, staged_item, local_path)

    assert (local_path / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (local_path / "old.txt").exists()
    backups = list(tmp_path.glob(".vbbak-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
    assert not stage_path.exists()
    assert "could not remove previous content" in caplog.text
    assert str(backups[0]) in caplog.text
