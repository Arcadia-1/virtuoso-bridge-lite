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


def test_sanitizing_client_skips_sanitization_when_download_fails(tmp_path) -> None:
    sanitize_calls: list[str] = []
    local_path = tmp_path / "stale.scs"
    local_path.write_text("stale secret\n", encoding="utf-8")

    class FailingClient:
        def download_file(self, remote_path, local_path, **kwargs):
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["remote file missing"],
            )

    def sanitize(text: str) -> str:
        sanitize_calls.append(text)
        return text.replace("secret", "REDACTED")

    client = SanitizingClient(FailingClient(), sanitize)
    result = client.download_file("/remote/missing.scs", local_path)

    assert not result.ok
    assert sanitize_calls == []
    assert not (tmp_path / "sanitized").exists()


def test_sanitizing_client_sanitizes_successful_download_and_supports_context_manager(
    tmp_path,
) -> None:
    events: list[str] = []

    class ManagedClient:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append(f"exit:{exc_type.__name__ if exc_type else 'none'}")
            return False

        def download_file(self, remote_path, local_path, **kwargs):
            Path(local_path).write_text("token=secret\n", encoding="utf-8")
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=str(local_path))

    local_path = tmp_path / "netlist.scs"
    wrapper = SanitizingClient(
        ManagedClient(), lambda text: text.replace("secret", "REDACTED")
    )

    with wrapper as entered:
        assert entered is wrapper
        result = entered.download_file("/remote/netlist.scs", local_path)
        assert result.ok

    assert events == ["enter", "exit:none"]
    assert local_path.read_text(encoding="utf-8") == "token=secret\n"
    assert (tmp_path / "sanitized" / "netlist.scs").read_text(encoding="utf-8") == (
        "token=REDACTED\n"
    )
