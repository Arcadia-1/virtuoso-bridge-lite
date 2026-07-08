from __future__ import annotations

from pathlib import Path

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.wrappers import SanitizingClient


def test_sanitizing_client_skips_recursive_directory_download(tmp_path) -> None:
    class Client:
        def download_file(self, remote_path, local_path, **kwargs):
            local = Path(local_path)
            local.mkdir()
            (local / "input.scs").write_text("secret netlist\n", encoding="utf-8")
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=str(local),
            )

    local_path = tmp_path / "netlist"
    client = SanitizingClient(Client(), lambda text: text.replace("secret", "REDACTED"))

    result = client.download_file("/remote/netlist", local_path, recursive=True)

    assert result.status == ExecutionStatus.SUCCESS
    assert (local_path / "input.scs").read_text(encoding="utf-8") == "secret netlist\n"
    assert not (tmp_path / "sanitized").exists()
