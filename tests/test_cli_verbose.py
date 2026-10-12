"""SSH command echoes are opt-in: --verbose or VB_VERBOSE=1.

Every command used to print each ssh/scp invocation as ``[cmd] ...`` on
stderr. Commands are still written to the command log either way.
"""

from __future__ import annotations

import pytest

from virtuoso_bridge.cli import build_parser
from virtuoso_bridge.transport.ssh import verbose_from_env
from virtuoso_bridge.transport.tunnel import SSHClient


@pytest.mark.parametrize("value, expected", [
    ("", False), ("0", False), ("no", False), ("1", True), ("true", True), ("ON", True),
])
def test_vb_verbose_values(monkeypatch, value, expected):
    monkeypatch.setenv("VB_VERBOSE", value)
    assert verbose_from_env() is expected


@pytest.mark.parametrize("argv, expected", [
    (["start"], False),
    (["start", "--verbose"], True),
    (["--verbose", "start"], True),
    (["eval", "1", "-v"], True),
    (["sos", "status", "lib", "cell", "view", "--verbose"], True),
])
def test_every_command_accepts_verbose(argv, expected):
    assert getattr(build_parser().parse_args(argv), "verbose", False) is expected


def test_ssh_runners_are_quiet_unless_asked(monkeypatch):
    monkeypatch.delenv("VB_VERBOSE", raising=False)
    assert SSHClient(remote_host="eda-host", remote_user="designer", port=65000)._ssh_runner._verbose is False
    monkeypatch.setenv("VB_VERBOSE", "1")
    assert SSHClient(remote_host="eda-host", remote_user="designer", port=65000)._ssh_runner._verbose is True


def test_verbose_flag_turns_echoes_on_for_the_command(monkeypatch):
    from virtuoso_bridge import cli

    monkeypatch.delenv("VB_VERBOSE", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "cli_status", lambda: seen.setdefault("verbose", verbose_from_env()) and 0)
    cli.main(["status", "--verbose"])
    assert seen == {"verbose": True}
