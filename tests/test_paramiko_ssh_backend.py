from __future__ import annotations

import io
import queue
import socket
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import virtuoso_bridge.transport.tunnel as tunnel_module
from virtuoso_bridge.transport.paramiko_backend import (
    ParamikoSessionBackend,
    _Deadline,
    _Endpoint,
    _Socks5Proxy,
    _default_known_hosts_files,
)
from virtuoso_bridge.transport.ssh import (
    SSHRunner,
    SshBackendEnv,
    ssh_backend_env_from_os,
)
from virtuoso_bridge.transport.transfer import (
    FileDownloadPlan,
    TarDownloadPlan,
    TarUploadPlan,
    TextUploadPlan,
    build_file_download_plan,
    build_tar_download_plan,
    build_tar_upload_plans,
    build_text_upload_plan,
)
from virtuoso_bridge.transport.tunnel import SSHClient


class _DispatchBackend:
    instance: "_DispatchBackend | None" = None

    def __init__(self, **kwargs) -> None:
        self.init_args = kwargs
        self.calls: list[tuple] = []
        _DispatchBackend.instance = self

    def test_connection(self, timeout) -> bool:
        self.calls.append(("test_connection", timeout))
        return True

    def run_command(self, command, *, timeout):
        self.calls.append(("run_command", command, timeout))
        return 0, "command output", ""

    def upload_tar(self, plan, *, timeout):
        self.calls.append(("upload_tar", plan, timeout))
        return 0, "", ""

    def upload_text(self, plan, contents, *, timeout):
        self.calls.append(("upload_text", plan, contents, timeout))
        return 0, "", ""

    def download_tar(self, plan, *, timeout):
        self.calls.append(("download_tar", plan, timeout))
        return 0, "", ""

    def download_file(self, plan, *, timeout):
        self.calls.append(("download_file", plan, timeout))
        return 0, "", ""

    def close(self) -> None:
        self.calls.append(("close",))


def test_ssh_runner_dispatches_to_explicit_paramiko_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    monkeypatch.setattr(
        "virtuoso_bridge.transport.paramiko_backend.ParamikoSessionBackend",
        _DispatchBackend,
    )

    runner = SSHRunner(
        host="compute",
        user="designer",
        jump_host="bastion",
        jump_user="designer",
        backend="paramiko",
        max_sessions=7,
        proxy_url="socks5://127.0.0.1:10800",
    )
    backend = _DispatchBackend.instance
    assert backend is not None
    assert backend.init_args["max_sessions"] == 7
    assert backend.init_args["proxy_url"] == "socks5://127.0.0.1:10800"
    assert runner.backend == "paramiko"
    assert runner.max_sessions == 7
    assert not runner._use_control_master
    assert not runner.persistent_shell_enabled

    local_file = tmp_path / "input.scs"
    local_file.write_text("simulator lang=spectre\n", encoding="utf-8")
    assert runner.test_connection(timeout=2)
    assert runner.run_command("printf ok", timeout=3).stdout == "command output"
    assert runner.upload(local_file, "/tmp/input.scs", timeout=4).returncode == 0
    assert runner.upload_batch(
        [(local_file, "/tmp/batch.scs")], timeout=5
    ).returncode == 0
    assert runner.upload_text("payload", "/tmp/payload", timeout=6).returncode == 0
    assert runner.download(
        "/tmp/results", tmp_path / "results", recursive=True, timeout=7
    ).returncode == 0
    assert runner.download(
        "/tmp/result.txt", tmp_path / "result.txt", timeout=8
    ).returncode == 0
    runner.close()

    assert [call[0] for call in backend.calls] == [
        "test_connection",
        "run_command",
        "upload_tar",
        "upload_tar",
        "upload_text",
        "download_tar",
        "download_file",
        "close",
    ]
    assert isinstance(backend.calls[2][1], TarUploadPlan)
    assert isinstance(backend.calls[3][1], TarUploadPlan)
    assert isinstance(backend.calls[4][1], TextUploadPlan)
    assert isinstance(backend.calls[5][1], TarDownloadPlan)
    assert isinstance(backend.calls[6][1], FileDownloadPlan)


def test_openssh_backend_does_not_parse_paramiko_proxy_setting(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    runner = SSHRunner(
        host="compute",
        user="designer",
        backend="openssh",
        proxy_url="not-a-proxy-url",
    )

    assert runner.backend == "openssh"
    assert runner._paramiko_backend is None


def test_profile_specific_paramiko_settings_reach_ssh_runner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CapturedRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(tunnel_module, "load_vb_env", lambda: None)
    monkeypatch.setattr(tunnel_module, "resolve_profile", lambda profile: profile)
    monkeypatch.setattr(tunnel_module, "SSHRunner", _CapturedRunner)
    monkeypatch.setenv("VB_REMOTE_HOST_worker", "compute")
    monkeypatch.setenv("VB_REMOTE_USER_worker", "designer")
    monkeypatch.setenv("VB_JUMP_HOST_worker", "bastion")
    monkeypatch.setenv("VB_SSH_BACKEND_worker", "paramiko")
    monkeypatch.setenv("VB_SSH_MAX_SESSIONS_worker", "255")
    monkeypatch.setenv("VB_SSH_PROXY_worker", "socks5://127.0.0.1:10800")

    client = SSHClient.from_env(profile="worker")

    assert client.ssh_runner is not None
    assert captured["backend"] == "paramiko"
    assert captured["max_sessions"] == 255
    assert captured["proxy_url"] == "socks5://127.0.0.1:10800"


def test_profile_ssh_proxy_uses_global_fallback(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_SSH_PROXY_worker", raising=False)
    monkeypatch.setenv("VB_SSH_PROXY", "socks5://127.0.0.1:10800")

    settings = ssh_backend_env_from_os("worker")

    assert settings.proxy_url == "socks5://127.0.0.1:10800"


def test_ssh_backend_env_keeps_two_argument_construction_compatible() -> None:
    settings = SshBackendEnv("paramiko", 10)

    assert settings.proxy_url is None


def _configured_backend(
    config_path: Path,
    *,
    host: str = "compute",
    user: str | None = None,
    jump_host: str | None = None,
    jump_user: str | None = None,
    ssh_key_path: Path | None = None,
    proxy_url: str | None = None,
) -> ParamikoSessionBackend:
    return ParamikoSessionBackend(
        host=host,
        user=user,
        jump_host=jump_host,
        jump_user=jump_user,
        ssh_key_path=ssh_key_path,
        ssh_config_path=config_path,
        ssh_cmd="ssh",
        connect_timeout=5,
        max_sessions=10,
        proxy_url=proxy_url,
    )


def _mock_ssh_g(
    monkeypatch,
    responses: dict[tuple[str, str | None, int | None], str],
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        user = command[command.index("-l") + 1] if "-l" in command else None
        port = int(command[command.index("-p") + 1]) if "-p" in command else None
        output = responses[(command[-1], user, port)]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=output.encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(
        "virtuoso_bridge.transport.paramiko_backend.subprocess.run",
        fake_run,
    )
    return calls


def _resolved_config(**values: str) -> str:
    defaults = {
        "hostname": "compute",
        "user": "local-user",
        "port": "22",
        "identitiesonly": "no",
        "stricthostkeychecking": "ask",
        "revokedhostkeys": "none",
        "userknownhostsfile": "none",
        "globalknownhostsfile": "none",
    }
    defaults.update(values)
    return "\n".join(f"{key} {value}" for key, value in defaults.items()) + "\n"


def test_openssh_config_resolves_explicit_user_tokens_and_single_proxyjump(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target_key = tmp_path / "id_env-target"
    jump_key = tmp_path / "jump_key"
    config = tmp_path / "config"
    config.touch()
    calls = _mock_ssh_g(
        monkeypatch,
        {
            ("compute", "env-target", None): _resolved_config(
                hostname="compute.internal",
                user="env-target",
                port="2201",
                identitiesonly="yes",
                identityfile=str(tmp_path / "id_%r"),
                proxyjump="%r@bastion:2202",
            ),
            ("bastion", "env-target", 2202): _resolved_config(
                hostname="bastion.internal",
                user="env-target",
                port="2202",
                identityfile=str(jump_key),
            ),
        },
    )

    backend = _configured_backend(config, user="env-target")

    assert backend._target_endpoint == _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="env-target",
        port=2201,
        key_filenames=(str(target_key),),
        identities_only=True,
        known_hosts_files=(),
    )
    assert backend._jump_endpoint == _Endpoint(
        host_alias="bastion",
        hostname="bastion.internal",
        username="env-target",
        port=2202,
        key_filenames=(str(jump_key),),
        known_hosts_files=(),
    )
    assert calls[0][-3:] == ["-l", "env-target", "compute"]
    assert calls[1][-5:] == ["-l", "env-target", "-p", "2202", "bastion"]


def test_default_config_resolution_is_delegated_to_openssh(
    monkeypatch,
) -> None:
    calls = _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                hostname="compute.internal",
                user="resolved-user",
                port="2207",
            ),
        },
    )

    backend = ParamikoSessionBackend(
        host="compute",
        user=None,
        jump_host=None,
        jump_user=None,
        ssh_key_path=None,
        ssh_config_path=None,
        ssh_cmd="ssh",
        connect_timeout=5,
        max_sessions=10,
    )

    assert backend._target_endpoint.hostname == "compute.internal"
    assert backend._target_endpoint.port == 2207
    assert backend._target_endpoint.username == "resolved-user"
    assert calls == [["ssh", "-G", "compute"]]


def test_explicit_jump_and_key_override_target_proxy_configuration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit_key = tmp_path / "explicit_key"
    configured_target_key = tmp_path / "configured-target-key"
    configured_jump_key = tmp_path / "configured-jump-key"
    config = tmp_path / "config"
    config.touch()
    calls = _mock_ssh_g(
        monkeypatch,
        {
            ("compute", "target-user", None): _resolved_config(
                user="target-user",
                identityfile=(
                    f"{explicit_key}\nidentityfile {configured_target_key}"
                ),
                proxycommand="nc proxy.example %h %p",
            ),
            ("bridge-jump", "env-jump", None): _resolved_config(
                hostname="jump.internal",
                user="env-jump",
                identityfile=(
                    f"{explicit_key}\nidentityfile {configured_jump_key}"
                ),
            ),
        },
    )

    backend = _configured_backend(
        config,
        user="target-user",
        jump_host="bridge-jump",
        jump_user="env-jump",
        ssh_key_path=explicit_key,
    )

    assert backend._target_endpoint.key_filenames == (
        str(explicit_key),
        str(configured_target_key),
    )
    assert backend._jump_endpoint is not None
    assert backend._jump_endpoint.hostname == "jump.internal"
    assert backend._jump_endpoint.username == "env-jump"
    assert backend._jump_endpoint.key_filenames == (
        str(explicit_key),
        str(configured_jump_key),
    )
    assert all("-i" in call and str(explicit_key) in call for call in calls)


@pytest.mark.parametrize(
    ("routing_line", "message"),
    [
        ("ProxyCommand nc proxy.example %h %p", "does not support ProxyCommand"),
        ("ProxyJump first,second", "supports one ProxyJump hop"),
    ],
)
def test_unsupported_paramiko_config_routing_fails_before_connect(
    monkeypatch,
    tmp_path: Path,
    routing_line: str,
    message: str,
) -> None:
    config = tmp_path / "config"
    config.touch()
    key, value = routing_line.split(" ", 1)
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                **{key.lower(): value}
            ),
        },
    )

    with pytest.raises(ValueError, match=message):
        _configured_backend(config)


def test_explicit_socks_proxy_overrides_proxycommand(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.touch()
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                proxycommand=(
                    "ncat --proxy 127.0.0.1:10800 "
                    "--proxy-type socks5 %h %p"
                )
            ),
        },
    )

    backend = _configured_backend(
        config,
        proxy_url="socks5://127.0.0.1:10800",
    )

    assert backend._proxy == _Socks5Proxy("127.0.0.1", 10800)
    assert backend._jump_endpoint is None


def test_nested_proxyjump_fails_before_connect(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.touch()
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(proxyjump="bastion"),
            ("bastion", None, None): _resolved_config(
                hostname="bastion",
                proxyjump="outer",
            ),
        },
    )

    with pytest.raises(ValueError, match="supports one jump hop"):
        _configured_backend(config)


def test_missing_explicit_ssh_config_fails_before_connect(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="SSH config file not found"):
        _configured_backend(tmp_path / "missing-config")


def test_paramiko_config_resolves_known_hosts_files_and_alias(
    monkeypatch,
    tmp_path: Path,
) -> None:
    known_hosts_dir = tmp_path / "known_hosts"
    user_hosts = known_hosts_dir / "user_hosts"
    secondary_hosts = known_hosts_dir / "secondary_hosts"
    config = tmp_path / "config"
    config.touch()
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                hostname="compute.internal",
                hostkeyalias="recorded-compute",
                userknownhostsfile=(
                    f"{user_hosts.as_posix()} {secondary_hosts.as_posix()}"
                ),
            ),
        },
    )

    backend = _configured_backend(config)

    assert backend._target_endpoint.host_key_alias == "recorded-compute"
    assert backend._target_endpoint.known_hosts_files == (
        user_hosts,
        secondary_hosts,
    )


def test_paramiko_rejects_non_strict_host_key_configuration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.touch()
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                stricthostkeychecking="accept-new"
            ),
        },
    )

    with pytest.raises(ValueError, match="requires recorded host keys"):
        _configured_backend(config)


def test_paramiko_fails_closed_when_revoked_host_keys_are_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.touch()
    revoked_keys = tmp_path / "revoked_host_keys"
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                revokedhostkeys=revoked_keys.as_posix()
            ),
        },
    )

    with pytest.raises(ValueError, match="cannot enforce RevokedHostKeys"):
        _configured_backend(config)


def test_identityfile_none_preserves_agent_without_local_key_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.touch()
    _mock_ssh_g(
        monkeypatch,
        {
            ("compute", None, None): _resolved_config(
                identityfile="none",
                identitiesonly="no",
            ),
        },
    )

    backend = _configured_backend(config)

    assert backend._target_endpoint.key_filenames == ()
    assert not backend._target_endpoint.identities_only


def test_default_known_hosts_paths_cover_windows_and_posix(tmp_path: Path) -> None:
    windows = _default_known_hosts_files(
        home=tmp_path / "home",
        platform_name="nt",
        program_data=str(tmp_path / "ProgramData"),
    )
    posix = _default_known_hosts_files(
        home=tmp_path / "home",
        platform_name="posix",
    )

    assert windows == (
        tmp_path / "home" / ".ssh" / "known_hosts",
        tmp_path / "home" / ".ssh" / "known_hosts2",
        tmp_path / "ProgramData" / "ssh" / "ssh_known_hosts",
        tmp_path / "ProgramData" / "ssh" / "ssh_known_hosts2",
    )
    assert posix[-2:] == (
        Path("/etc/ssh/ssh_known_hosts"),
        Path("/etc/ssh/ssh_known_hosts2"),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "ssh://jump-user@bastion.example:2200",
            ("bastion.example", "jump-user", 2200),
        ),
        ("jump-user@[2001:db8::1]:2200", ("2001:db8::1", "jump-user", 2200)),
        ("2001:db8::1", ("2001:db8::1", None, None)),
    ],
)
def test_proxyjump_parser_accepts_openssh_single_hop_forms(
    value: str,
    expected: tuple[str, str | None, int | None],
) -> None:
    parsed = ParamikoSessionBackend._parse_proxy_jump(value, "compute")

    assert parsed is not None
    assert (parsed.host, parsed.username, parsed.port) == expected


def test_proxyjump_token_expansion_preserves_url_percent_encoding() -> None:
    endpoint = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="target-user",
        port=2201,
        key_filenames=(),
    )

    expanded = ParamikoSessionBackend._expand_connection_tokens(
        "ssh://jump%40realm@%h:%p",
        endpoint=endpoint,
    )

    assert expanded == "ssh://jump%40realm@compute.internal:2201"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("socks5://127.0.0.1:10800", ("127.0.0.1", 10800)),
        ("socks5://proxy.example:1080", ("proxy.example", 1080)),
        ("socks5://[2001:db8::1]:1080", ("2001:db8::1", 1080)),
    ],
)
def test_socks5_proxy_parser_accepts_explicit_host_and_port(
    value: str,
    expected: tuple[str, int],
) -> None:
    parsed = ParamikoSessionBackend._parse_socks5_proxy(value)

    assert parsed is not None
    assert (parsed.host, parsed.port) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:1080",
        "socks5://127.0.0.1",
        "socks5://127.0.0.1:0",
        "socks5://127.0.0.1:65536",
        "socks5://user:password@127.0.0.1:1080",
        "socks5://127.0.0.1:1080/route",
        "socks5://127.0.0.1:1080?rdns=false",
        "socks5://127.0.0.1:invalid",
        "socks5://[::1:1080",
    ],
)
def test_socks5_proxy_parser_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ValueError, match="VB_SSH_PROXY"):
        ParamikoSessionBackend._parse_socks5_proxy(value)


def test_blank_socks5_proxy_preserves_direct_routing() -> None:
    assert ParamikoSessionBackend._parse_socks5_proxy(None) is None
    assert ParamikoSessionBackend._parse_socks5_proxy("  ") is None


class _RoutingSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RoutingTransport:
    def __init__(self, channel: object | None = None) -> None:
        self.channel = channel
        self.open_channel_calls: list[tuple] = []

    def is_active(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def open_channel(self, *args, **kwargs):
        self.open_channel_calls.append((args, kwargs))
        return self.channel


class _RoutingClient:
    def __init__(self, transport: _RoutingTransport) -> None:
        self.transport = transport
        self.closed = False

    def get_transport(self) -> _RoutingTransport:
        return self.transport

    def close(self) -> None:
        self.closed = True


class _FakeSocksModule:
    SOCKS5 = object()

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, int], dict[str, object]]] = []
        self.sockets: list[_RoutingSocket] = []

    def create_connection(self, destination, **kwargs):
        self.calls.append((destination, kwargs))
        proxy_socket = _RoutingSocket()
        self.sockets.append(proxy_socket)
        return proxy_socket


def _proxy_routing_backend(
    *,
    target: _Endpoint,
    jump: _Endpoint | None = None,
) -> tuple[ParamikoSessionBackend, _FakeSocksModule]:
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    socks_module = _FakeSocksModule()
    backend._proxy = _Socks5Proxy("127.0.0.1", 10800)
    backend._socks = socks_module
    backend._host = target.host_alias
    backend._connect_timeout = 5.0
    backend._max_sessions = 8
    backend._connect_lock = threading.RLock()
    backend._target_endpoint = target
    backend._jump_endpoint = jump
    backend._target_client = None
    backend._jump_client = None
    backend._jump_channel = None
    return backend, socks_module


def test_socks5_proxy_connects_target_once_and_reuses_transport() -> None:
    target = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="designer",
        port=2201,
        key_filenames=(),
        host_key_alias="recorded-compute",
    )
    backend, socks_module = _proxy_routing_backend(target=target)
    target_client = _RoutingClient(_RoutingTransport())
    connect_calls: list[tuple[_Endpoint, object | None]] = []

    def connect_client(endpoint, _deadline, *, sock=None):
        connect_calls.append((endpoint, sock))
        return target_client

    backend._connect_client = connect_client  # type: ignore[method-assign]

    backend.ensure_connected()
    backend.ensure_connected()

    assert len(socks_module.calls) == 1
    destination, proxy_args = socks_module.calls[0]
    assert destination == ("compute.internal", 2201)
    assert proxy_args["proxy_type"] is socks_module.SOCKS5
    assert proxy_args["proxy_addr"] == "127.0.0.1"
    assert proxy_args["proxy_port"] == 10800
    assert proxy_args["proxy_rdns"] is True
    assert connect_calls == [(target, socks_module.sockets[0])]


def test_socks5_proxy_connects_only_the_jump_host_first_hop() -> None:
    target = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="designer",
        port=22,
        key_filenames=(),
    )
    jump = _Endpoint(
        host_alias="bastion",
        hostname="bastion.internal",
        username="designer",
        port=2202,
        key_filenames=(),
    )
    backend, socks_module = _proxy_routing_backend(target=target, jump=jump)
    jump_channel = _RoutingSocket()
    jump_transport = _RoutingTransport(jump_channel)
    jump_client = _RoutingClient(jump_transport)
    target_client = _RoutingClient(_RoutingTransport())
    connect_calls: list[tuple[_Endpoint, object | None]] = []

    def connect_client(endpoint, _deadline, *, sock=None):
        connect_calls.append((endpoint, sock))
        return jump_client if endpoint is jump else target_client

    backend._connect_client = connect_client  # type: ignore[method-assign]

    backend.ensure_connected()

    assert socks_module.calls[0][0] == ("bastion.internal", 2202)
    assert connect_calls == [
        (jump, socks_module.sockets[0]),
        (target, jump_channel),
    ]
    assert jump_transport.open_channel_calls[0][0][:2] == (
        "direct-tcpip",
        ("compute.internal", 22),
    )


def test_socks5_proxy_socket_closes_when_ssh_connection_fails() -> None:
    target = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="designer",
        port=22,
        key_filenames=(),
    )
    backend, socks_module = _proxy_routing_backend(target=target)

    def connect_client(_endpoint, _deadline, *, sock=None):
        raise OSError("SSH handshake failed")

    backend._connect_client = connect_client  # type: ignore[method-assign]

    with pytest.raises(OSError, match="SSH handshake failed"):
        backend.ensure_connected()

    assert socks_module.sockets[0].closed


class _FakeSSHException(Exception):
    pass


class _SessionTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.opened = 0

    def open(self) -> None:
        with self.lock:
            self.active += 1
            self.opened += 1
            self.maximum = max(self.maximum, self.active)

    def close(self) -> None:
        with self.lock:
            self.active -= 1


class _FakeChannel:
    def __init__(self, tracker: _SessionTracker) -> None:
        self._tracker = tracker
        self._ready_at = time.monotonic() + 0.05
        self._closed = False
        tracker.open()

    def settimeout(self, _timeout) -> None:
        pass

    def exec_command(self, _command) -> None:
        pass

    def sendall(self, _payload) -> None:
        pass

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return False

    def recv_stderr_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return time.monotonic() >= self._ready_at

    @property
    def eof_received(self) -> bool:
        return self.exit_status_ready()

    @property
    def closed(self) -> bool:
        return self._closed

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._tracker.close()


class _FakeTransport:
    def __init__(self, tracker: _SessionTracker) -> None:
        self._tracker = tracker

    def is_active(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def open_session(self, timeout):
        assert timeout > 0
        return _FakeChannel(self._tracker)


class _FakeClient:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport
        self.closed = False

    def get_transport(self) -> _FakeTransport:
        return self._transport

    def close(self) -> None:
        self.closed = True


def test_paramiko_backend_bounds_channels_on_one_transport() -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(2)
    backend._connect_lock = threading.RLock()
    backend._target_client = _FakeClient(transport)
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: backend.run_command(f"job {i}", 2), range(6)))

    assert all(returncode == 0 for returncode, _stdout, _stderr in results)
    assert tracker.opened == 6
    assert tracker.maximum == 2
    assert tracker.active == 0


class _RejectOnceTransport(_FakeTransport):
    def __init__(self, tracker: _SessionTracker) -> None:
        super().__init__(tracker)
        self._rejected = False

    def open_session(self, timeout):
        if not self._rejected:
            self._rejected = True
            raise _FakeSSHException("server refused one channel")
        return super().open_session(timeout)


def test_channel_rejection_does_not_close_shared_transport() -> None:
    tracker = _SessionTracker()
    transport = _RejectOnceTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(255)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None

    rejected = backend.run_command("first", 2)
    accepted = backend.run_command("second", 2)

    assert rejected == (255, "", "server refused one channel")
    assert accepted[0] == 0
    assert not client.closed


def test_collect_channel_waits_for_eof_after_early_exit_status() -> None:
    class _LateOutputChannel:
        def __init__(self) -> None:
            self.started = time.monotonic()
            self.stdout_read = False

        @property
        def eof_received(self) -> bool:
            return time.monotonic() - self.started >= 0.03

        @property
        def closed(self) -> bool:
            return False

        def exit_status_ready(self) -> bool:
            return True

        def recv_ready(self) -> bool:
            return self.eof_received and not self.stdout_read

        def recv(self, _size: int) -> bytes:
            self.stdout_read = True
            return b"late output"

        def recv_stderr_ready(self) -> bool:
            return False

        def recv_exit_status(self) -> int:
            return 0

    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)

    result = backend._collect_channel(
        _LateOutputChannel(),
        _Deadline.start(1),
        "printf late output",
    )

    assert result == (0, "late output", "")


@pytest.mark.parametrize(
    "operation",
    ["run_command", "upload_tar", "download_tar", "upload_text", "download_file"],
)
def test_paramiko_socket_timeout_uses_subprocess_timeout_contract(
    tmp_path: Path,
    operation: str,
) -> None:
    tracker = _SessionTracker()

    class _TimeoutTransport(_FakeTransport):
        def open_session(self, timeout):
            raise socket.timeout("channel timed out")

    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(1)
    backend._connect_lock = threading.RLock()
    backend._target_client = _FakeClient(_TimeoutTransport(tracker))
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None

    local_file = tmp_path / "input.scs"
    local_file.write_text("payload", encoding="utf-8")
    upload_plan = build_tar_upload_plans(
        "tar",
        [(local_file, "/remote/input.scs")],
    )[0]
    download_plan = build_tar_download_plan(
        "tar",
        "/remote/results",
        tmp_path / "results",
    )
    text_plan = build_text_upload_plan("/remote/input.scs", b"payload")
    file_plan = build_file_download_plan(
        "/remote/output.raw",
        tmp_path / "output.raw",
    )

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        if operation == "run_command":
            backend.run_command("sleep 30", timeout=2)
        elif operation == "upload_tar":
            backend.upload_tar(upload_plan, timeout=2)
        elif operation == "download_tar":
            backend.download_tar(download_plan, timeout=2)
        elif operation == "upload_text":
            backend.upload_text(text_plan, b"payload", timeout=2)
        else:
            backend.download_file(file_plan, timeout=2)

    assert caught.value.timeout == 2
    assert not list(tmp_path.glob(".vbtmp-*"))


def test_sftp_file_error_does_not_close_active_transport(tmp_path: Path) -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None

    class _MissingRemoteFile:
        def get(self, _remote_path, _local_path, callback=None) -> None:
            raise FileNotFoundError(2, "remote file not found")

    @contextmanager
    def missing_sftp(_deadline, _command):
        yield _MissingRemoteFile()

    backend._sftp = missing_sftp
    local_path = tmp_path / "missing.fc"
    local_path.write_text("existing result\n", encoding="utf-8")
    plan = build_file_download_plan("/remote/missing.fc", local_path)

    result = backend.download_file(plan, timeout=2)

    assert result[0] == 1
    assert "remote file not found" in result[2]
    assert not client.closed
    assert backend._target_client is client
    assert local_path.read_text(encoding="utf-8") == "existing result\n"
    assert not list(tmp_path.glob(".vbtmp-*"))


def test_nonrecursive_download_replaces_existing_file_after_success(
    tmp_path: Path,
) -> None:
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)

    class _SuccessfulSftp:
        def get(self, _remote_path, local_path, callback=None) -> None:
            Path(local_path).write_text("new result\n", encoding="utf-8")
            if callback is not None:
                callback(11, 11)

    @contextmanager
    def successful_sftp(_deadline, _command):
        yield _SuccessfulSftp()

    backend._sftp = successful_sftp
    local_path = tmp_path / "spectre.fc"
    local_path.write_text("old result\n", encoding="utf-8")
    plan = build_file_download_plan("/remote/spectre.fc", local_path)

    result = backend.download_file(plan, timeout=2)

    assert result == (0, "", "")
    assert local_path.read_text(encoding="utf-8") == "new result\n"
    assert not list(tmp_path.glob(".vbtmp-*"))


class _TarChannel:
    def __init__(self, stdout: bytes = b"", *, wait_for_input: bool = False) -> None:
        self.stdout = stdout
        self.received = bytearray()
        self.command: str | None = None
        self._ready = not wait_for_input
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def exec_command(self, command: str) -> None:
        self.command = command

    def makefile(self, _mode: str):
        return io.BytesIO(self.stdout)

    def makefile_stderr(self, _mode: str):
        return io.BytesIO()

    def sendall(self, payload: bytes) -> None:
        self.received.extend(payload)

    def shutdown_write(self) -> None:
        self._ready = True

    def exit_status_ready(self) -> bool:
        return self._ready

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


class _TarTransport:
    def __init__(self, channel: _TarChannel) -> None:
        self.channel = channel

    def is_active(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def open_session(self, timeout: float) -> _TarChannel:
        assert timeout > 0
        return self.channel


def _tar_backend(channel: _TarChannel) -> ParamikoSessionBackend:
    transport = _TarTransport(channel)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(1)
    backend._connect_lock = threading.RLock()
    backend._target_client = _FakeClient(transport)
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None
    return backend


def test_paramiko_tar_wait_checks_worker_failure_after_workers_exit() -> None:
    failures: "queue.Queue[BaseException]" = queue.Queue()
    expected = OSError("late worker failure")

    class _CompletedChannel:
        @staticmethod
        def exit_status_ready() -> bool:
            return True

        @staticmethod
        def recv_exit_status() -> int:
            raise AssertionError("worker failure must win over exit status")

    class _CompletedProcess:
        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait(*, timeout: float) -> int:
            raise AssertionError("worker failure must win over process status")

    class _FailingWorker:
        @staticmethod
        def is_alive() -> bool:
            failures.put(expected)
            return False

    with pytest.raises(OSError, match="late worker failure") as caught:
        ParamikoSessionBackend._wait_tar_transfer(
            _CompletedChannel(),
            _CompletedProcess(),
            [_FailingWorker()],
            failures,
            _Deadline.start(5),
            "tar transfer",
        )

    assert caught.value is expected


def test_paramiko_tar_upload_streams_the_shared_archive_plan(tmp_path: Path) -> None:
    local_path = tmp_path / "input.scs"
    local_path.write_bytes(b"simulator lang=spectre\n\x00binary")
    plan = build_tar_upload_plans(
        "tar",
        [(local_path, "/remote/renamed.scs")],
    )[0]
    channel = _TarChannel(wait_for_input=True)

    result = _tar_backend(channel).upload_tar(plan, timeout=5)

    assert result == (0, "", "")
    assert channel.command == plan.remote_command
    with tarfile.open(fileobj=io.BytesIO(channel.received), mode="r:") as archive:
        member = archive.extractfile("input.scs")
        assert member is not None
        assert member.read() == local_path.read_bytes()


def test_paramiko_text_upload_executes_shared_atomic_plan() -> None:
    class _TextChannel(_FakeChannel):
        def __init__(self, tracker: _SessionTracker) -> None:
            super().__init__(tracker)
            self.command: str | None = None
            self.payload = bytearray()

        def exec_command(self, command: str) -> None:
            self.command = command

        def sendall(self, payload: bytes) -> None:
            self.payload.extend(payload)

    tracker = _SessionTracker()
    channel = _TextChannel(tracker)

    class _TextTransport(_FakeTransport):
        def open_session(self, timeout):
            assert timeout > 0
            return channel

    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(1)
    backend._connect_lock = threading.RLock()
    backend._target_client = _FakeClient(_TextTransport(tracker))
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None
    payload = b"exact payload"
    plan = build_text_upload_plan("/remote/input.scs", payload)

    result = backend.upload_text(plan, payload, timeout=5)

    assert result == (0, "", "")
    assert channel.command == plan.remote_command
    assert channel.payload == b"exact payload"
    assert channel._closed


def test_paramiko_tar_download_installs_only_completed_archive(tmp_path: Path) -> None:
    payload = io.BytesIO()
    contents = b"raw\x00result\n"
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        info = tarfile.TarInfo("results/output.raw")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))

    local_path = tmp_path / "installed"
    local_path.mkdir()
    (local_path / "old.txt").write_text("old\n", encoding="utf-8")
    plan = build_tar_download_plan("tar", "/remote/results", local_path)
    channel = _TarChannel(payload.getvalue())

    result = _tar_backend(channel).download_tar(plan, timeout=5)

    assert result == (0, "", "")
    assert channel.command == plan.remote_command
    assert (local_path / "output.raw").read_bytes() == contents
    assert not (local_path / "old.txt").exists()
    assert not list(tmp_path.glob(".vbtmp-*"))
    assert not list(tmp_path.glob(".vbbak-*"))


def test_inactive_transport_is_closed_after_operation_error() -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None
    transport.is_active = lambda: False

    backend._invalidate_if_transport_failed(OSError("connection lost"))

    assert client.closed
    assert backend._target_client is None


class _ChangedHostKey(Exception):
    pass


class _RejectChangedHostKeyClient:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.connect_kwargs: dict[str, object] = {}
        self.policy: object | None = None
        self.closed = False

    def load_system_host_keys(self, filename: str) -> None:
        self.events.append(f"load:{filename}")

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy
        self.events.append("set-policy")

    def connect(self, **kwargs) -> None:
        self.events.append("connect")
        self.connect_kwargs = kwargs
        if not any(event.startswith("load:") for event in self.events):
            raise AssertionError("known_hosts must be loaded before connect")
        raise _ChangedHostKey("target host key changed")

    def close(self) -> None:
        self.closed = True
        self.events.append("close")


def test_connect_uses_strict_recorded_alias_and_propagates_changed_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _RejectChangedHostKeyClient()
    reject_policy = object()
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: reject_policy,
    )
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "recorded-compute ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n",
        encoding="utf-8",
    )

    class _AliasSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    alias_socket = _AliasSocket()
    socket_calls: list[tuple[tuple[str, int], float]] = []

    def create_connection(address, timeout):
        socket_calls.append((address, timeout))
        return alias_socket

    monkeypatch.setattr(
        "virtuoso_bridge.transport.paramiko_backend.socket.create_connection",
        create_connection,
    )
    endpoint = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="designer",
        port=22,
        key_filenames=(),
        identities_only=True,
        host_key_alias="recorded-compute",
        known_hosts_files=(known_hosts,),
    )

    with pytest.raises(_ChangedHostKey, match="target host key changed"):
        backend._connect_client(endpoint, _Deadline.start(2))

    assert client.events[0] == f"load:{known_hosts}"
    assert client.events.index("set-policy") < client.events.index("connect")
    assert all(
        not event.startswith("load")
        for event in client.events[client.events.index("set-policy") + 1 :]
    )
    assert client.connect_kwargs["allow_agent"] is False
    assert client.connect_kwargs["look_for_keys"] is False
    assert client.connect_kwargs["hostname"] == "recorded-compute"
    assert client.connect_kwargs["sock"] is alias_socket
    assert client.policy is reject_policy
    assert socket_calls[0][0] == ("compute.internal", 22)
    assert alias_socket.closed
    assert client.closed


def test_connect_uses_resolved_identity_files_and_does_not_rescan_home(
    tmp_path: Path,
) -> None:
    client = _RejectChangedHostKeyClient()
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    known_hosts = tmp_path / "known_hosts"
    known_hosts.touch()
    available_key = tmp_path / "available-key"
    available_key.touch()
    missing_key = tmp_path / "missing-key"
    endpoint = _Endpoint(
        host_alias="compute",
        hostname="compute.internal",
        username="designer",
        port=22,
        key_filenames=(str(missing_key), str(available_key)),
        identities_only=False,
        known_hosts_files=(known_hosts,),
    )

    with pytest.raises(_ChangedHostKey):
        backend._connect_client(endpoint, _Deadline.start(2), sock=object())

    assert client.connect_kwargs["key_filename"] == str(available_key)
    assert client.connect_kwargs["allow_agent"] is True
    assert client.connect_kwargs["look_for_keys"] is False
