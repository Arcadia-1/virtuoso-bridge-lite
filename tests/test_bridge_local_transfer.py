from __future__ import annotations

from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient


class _RecordingTunnel:
    ssh_runner = object()
    port = 65432

    def __init__(self) -> None:
        self.downloads: list[tuple[str, Path, int | None, bool]] = []
        self.uploads: list[tuple[Path, str, int | None]] = []

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        timeout: int | None = None,
        recursive: bool = False,
    ) -> CommandResult:
        self.downloads.append((remote_path, local_path, timeout, recursive))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text("downloaded through tunnel\n", encoding="utf-8")
        return CommandResult(
            returncode=0,
            stdout="",
            stderr="",
        )

    def upload_file(self, local_path: Path, remote_path: str, timeout: int | None = None) -> CommandResult:
        self.uploads.append((local_path, remote_path, timeout))
        return CommandResult(
            returncode=0,
            stdout="",
            stderr="",
        )


def _local_tunnel_client() -> VirtuosoClient:
    tunnel = SSHClient(remote_host="localhost", port=65432)
    assert tunnel.ssh_runner is None
    return VirtuosoClient.from_tunnel(tunnel)


def test_download_file_copies_directly_when_tunnel_is_local(tmp_path):
    source = tmp_path / "remote.csv"
    target = tmp_path / "local" / "results.csv"
    source.write_text("point,value\n1,0.8\n", encoding="utf-8")

    client = _local_tunnel_client()
    result = client.download_file(source, target)

    assert result.status == ExecutionStatus.SUCCESS
    assert Path(result.output) == target
    assert target.read_text(encoding="utf-8") == "point,value\n1,0.8\n"


def test_download_file_copies_directories_when_tunnel_is_local(tmp_path):
    source = tmp_path / "remote_netlist"
    target = tmp_path / "local" / "netlist"
    source.mkdir()
    (source / "input.scs").write_text('include "ade_e.scs"\n', encoding="utf-8")
    (source / "ade_e.scs").write_text("simulatorOptions options\n", encoding="utf-8")

    client = _local_tunnel_client()
    result = client.download_file(source, target, recursive=True)

    assert result.status == ExecutionStatus.SUCCESS
    assert Path(result.output) == target
    assert (target / "input.scs").read_text(encoding="utf-8") == 'include "ade_e.scs"\n'
    assert (target / "ade_e.scs").read_text(encoding="utf-8") == "simulatorOptions options\n"


def test_download_file_rejects_overlapping_local_recursive_paths(tmp_path):
    source = tmp_path / "remote_netlist"
    source.mkdir()
    (source / "input.scs").write_text('include "ade_e.scs"\n', encoding="utf-8")

    client = _local_tunnel_client()
    same_path = client.download_file(source, source, recursive=True)
    nested_path = client.download_file(source, source / "nested", recursive=True)

    assert same_path.status == ExecutionStatus.ERROR
    assert nested_path.status == ExecutionStatus.ERROR
    assert (source / "input.scs").read_text(encoding="utf-8") == 'include "ade_e.scs"\n'


def test_download_file_rejects_nested_overlap_without_creating_parent(tmp_path):
    source = tmp_path / "remote_netlist"
    source.mkdir()
    (source / "input.scs").write_text('include "ade_e.scs"\n', encoding="utf-8")

    client = _local_tunnel_client()
    result = client.download_file(source, source / "nested" / "copy", recursive=True)

    assert result.status == ExecutionStatus.ERROR
    assert not (source / "nested").exists()
    assert (source / "input.scs").read_text(encoding="utf-8") == 'include "ade_e.scs"\n'


def test_upload_file_copies_directly_when_tunnel_is_local(tmp_path):
    source = tmp_path / "local.scs"
    target = tmp_path / "remote" / "input.scs"
    source.write_text("simulator lang=spectre\n", encoding="utf-8")

    client = _local_tunnel_client()
    result = client.upload_file(source, target)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.output == target.as_posix()
    assert target.read_text(encoding="utf-8") == "simulator lang=spectre\n"


def test_file_transfers_delegate_when_tunnel_has_ssh_runner(tmp_path):
    source = tmp_path / "source.scs"
    download_target = tmp_path / "downloads" / "remote.scs"
    source.write_text("simulator lang=spectre\n", encoding="utf-8")
    tunnel = _RecordingTunnel()
    client = VirtuosoClient.from_tunnel(tunnel)

    download = client.download_file("/remote/result.scs", download_target, timeout=12)
    recursive_download = client.download_file(
        "/remote/netlist",
        tmp_path / "downloads" / "netlist",
        timeout=56,
        recursive=True,
    )
    upload = client.upload_file(source, "/remote/source.scs", timeout=34)

    assert download.status == ExecutionStatus.SUCCESS
    assert recursive_download.status == ExecutionStatus.SUCCESS
    assert upload.status == ExecutionStatus.SUCCESS
    assert tunnel.downloads == [
        ("/remote/result.scs", download_target, 12, False),
        ("/remote/netlist", tmp_path / "downloads" / "netlist", 56, True),
    ]
    assert tunnel.uploads == [(source, "/remote/source.scs", 34)]
    assert download_target.read_text(encoding="utf-8") == "downloaded through tunnel\n"
