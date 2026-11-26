from __future__ import annotations

import pytest
import shlex

from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.sos import SOSWorkarea


class _Runner:
    def __init__(self) -> None:
        self.command: str | None = None
        self.commands = []
        self.kind = "f"

    def run_command(self, command: str, *, timeout: float | None = None) -> CommandResult:
        self.command = command
        self.commands.append(command)
        if "status -Nhdr" in command:
            return CommandResult(0, self.kind + "\t" + shlex.split(command)[-1] + "\n", "")
        return CommandResult(0, "", "")


class _Owner:
    _timeout = 30

    def __init__(self) -> None:
        self.sos_runner = _Runner()


def _workarea() -> tuple[SOSWorkarea, _Owner]:
    owner = _Owner()
    return SOSWorkarea(owner, "/workarea", soscmd="soscmd"), owner


def test_virtuoso_client_exposes_sos_facade() -> None:
    from virtuoso_bridge import VirtuosoClient
    from virtuoso_bridge.virtuoso.sos import SOSOps

    assert isinstance(VirtuosoClient().sos, SOSOps)


def test_status_uses_explicit_workarea_relative_path() -> None:
    sos, owner = _workarea()

    result = sos.status("TEST/V1")

    assert result.returncode == 0
    assert owner.sos_runner.command == "cd /workarea && soscmd status ./TEST/V1"


def test_status_can_recursively_select_checked_out_objects() -> None:
    sos, owner = _workarea()

    sos.status(".", checked_out_only=True, recursive=True)

    assert owner.sos_runner.command == "cd /workarea && soscmd status -sco -sr ."


def test_status_does_not_add_filters_unless_requested() -> None:
    sos, owner = _workarea()

    sos.status(["TEST/V1", "TEST/V2"], recursive=True)

    assert owner.sos_runner.command == (
        "cd /workarea && soscmd status -sr ./TEST/V1 ./TEST/V2"
    )


@pytest.mark.parametrize("root,path", [
    ("/workarea", "TEST/cell/Calibre"), ("/work/Calibre", "TEST/cell/layout"),
])
def test_raw_checkin_does_not_bypass_calibre_guard(root, path):
    owner = _Owner()
    area = SOSWorkarea(owner, root, soscmd="soscmd")
    with pytest.raises(ValueError, match="Calibre"):
        area.checkin(path, change_summary="Never submit")
    assert owner.sos_runner.command is None


def test_ordinary_file_checkin_still_quotes_paths_and_message():
    area, owner = _workarea()
    area.checkin((path for path in ["rtl/my block.v"]), change_summary="Fix gain")
    assert owner.sos_runner.command.endswith(
        "soscmd ci '-achange_summary=Fix gain' '-aLog=Fix gain' './rtl/my block.v'")
    assert "for vb_file in './rtl/my block.v'" in owner.sos_runner.command
    assert '[ ! -L "$vb_file" ]' in owner.sos_runner.command
    assert '[ ! -e "$vb_parent/master.tag" ]' in owner.sos_runner.command
    assert "-sall -sNr" in owner.sos_runner.commands[0]


def test_checked_out_filter_does_not_imply_recursion():
    area, owner = _workarea()
    area.status(".", checked_out_only=True)
    assert owner.sos_runner.command == "cd /workarea && soscmd status -sco -sNr ."


@pytest.mark.parametrize("action", ["co", "ci"])
@pytest.mark.parametrize("path", [".", "TEST/cell/schematic/sch.oa", "view/master.tag", "Calibre/result"])
def test_raw_oa_and_calibre_paths_rejected_before_any_command(action, path):
    area, owner = _workarea()
    with pytest.raises(ValueError):
        if action == "co":
            area.checkout(path)
        else:
            area.checkin(path, change_summary="test")
    assert owner.sos_runner.commands == []


@pytest.mark.parametrize("kind", ["d", "p", "P", "F", "s", "?"])
@pytest.mark.parametrize("action", ["co", "ci"])
def test_only_positive_ordinary_file_type_can_be_written(kind, action):
    area, owner = _workarea()
    owner.sos_runner.kind = kind
    with pytest.raises(RuntimeError, match="ordinary SOS file"):
        if action == "co":
            area.checkout("rtl/block.v")
        else:
            area.checkin("rtl/block.v", change_summary="test")
    assert all("status -Nhdr" in command for command in owner.sos_runner.commands)


def test_all_files_are_validated_before_any_mutation():
    area, owner = _workarea()
    area.checkout(["rtl/one.v", "rtl/two.v"])
    assert len(owner.sos_runner.commands) == 3
    assert all("status -Nhdr" in command for command in owner.sos_runner.commands[:2])
    assert "for vb_file in ./rtl/one.v ./rtl/two.v" in owner.sos_runner.commands[-1]


@pytest.mark.parametrize("action", ["co", "ci"])
def test_ordinary_file_check_ignores_cliosoft_online_project_banner(action):
    area, owner = _workarea()
    orig = owner.sos_runner.run_command

    def with_banners(command: str, *, timeout: float | None = None) -> CommandResult:
        res = orig(command, timeout=timeout)
        if "status -Nhdr" in command:
            return CommandResult(
                0,
                "** The flags and attributes have been updated.\n"
                "## All servers for project 'FCGMASH' are online.\n"
                + res.stdout,
                "",
            )
        return res

    owner.sos_runner.run_command = with_banners
    if action == "co":
        result = area.checkout("rtl/block.v")
    else:
        result = area.checkin("rtl/block.v", change_summary="Update block")
    assert result.returncode == 0
    assert len(owner.sos_runner.commands) == 2


@pytest.mark.parametrize(
    "banner",
    [
        "## All servers for project 'FCGMASH' are offline.\n",
        "## All servers for project 'FCGMASH' are online. unexpected\n",
    ],
)
def test_ordinary_file_check_rejects_non_online_or_unexpected_banners(banner):
    area, owner = _workarea()
    orig = owner.sos_runner.run_command

    def with_bad_banner(command: str, *, timeout: float | None = None) -> CommandResult:
        res = orig(command, timeout=timeout)
        if "status -Nhdr" in command:
            return CommandResult(0, banner + res.stdout, "")
        return res

    owner.sos_runner.run_command = with_bad_banner
    with pytest.raises(RuntimeError, match="ordinary SOS file"):
        area.checkout("rtl/block.v")
    assert len(owner.sos_runner.commands) == 1
