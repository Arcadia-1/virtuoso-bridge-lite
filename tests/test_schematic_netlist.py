from __future__ import annotations

import logging
import os
import shlex
import shutil
import stat
import re
import subprocess
from pathlib import Path

import pytest

from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.schematic import (
    SchematicOps,
    export_schematic_netlist,
    import_netlist_schematic,
    schematic_import_netlist_skill,
    schematic_export_netlist_skill,
)
from virtuoso_bridge.virtuoso.schematic import netlist as schematic_netlist_module

_SOURCE_FILE = "/tmp/generated/netlist/input.scs"
_SPECTRE_TEXT = "simulator lang=spectre\n"
_IMPORTED = {"status": "success", "output": '("imported" "demoLib" "nand2")'}


_PROJECT_DIR = re.compile(r'"projectDir" \'string "([^"]+)"\) error')


class _ExportClient:
    """Fake Virtuoso for exports. By default it netlists into the project
    directory the SKILL redirects to, as IC 6.1.8 does, and a fake SSH runner
    records the run-directory commands instead of touching this machine."""

    def __init__(
        self,
        output: str | None = None,
        *,
        files: dict[str, str] | None = None,
        download_errors: list[str] | None = None,
        download_raises: BaseException | None = None,
        ssh_runner: "_Runner | None | str" = "fake",
    ) -> None:
        self.output = output
        self.files = {"input.scs": _SPECTRE_TEXT} if files is None else files
        self.download_errors = download_errors
        self.download_raises = download_raises
        self.ssh_runner = _Runner() if ssh_runner == "fake" else ssh_runner
        self.skill: str | None = None
        self.timeout: int | None = None
        self.downloads: list[tuple[str, Path, int | None, bool]] = []

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        output = self.output
        if output is None:
            project = _PROJECT_DIR.search(skill)
            root = project.group(1) if project else "/home/designer/simulation"
            output = f'"{root}/tb_inv/spectre/schematic/netlist/input.scs"'
        return {"status": "success", "output": output}

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        *,
        timeout: int | None = None,
        recursive: bool = False,
    ):
        self.downloads.append((remote_path, local_path, timeout, recursive))
        if self.download_raises is not None:
            raise self.download_raises
        if self.download_errors is not None:
            return {"status": "error", "errors": self.download_errors, "output": ""}
        assert not local_path.exists()
        local_path.mkdir()
        for name, text in self.files.items():
            (local_path / name).write_text(text, encoding="utf-8")
        return {"status": "success", "output": str(local_path)}


class _Runner:
    def __init__(self, *, test_returncode: int = 0) -> None:
        self.test_returncode = test_returncode
        self.commands: list[str] = []

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        if command.startswith("test -f "):
            return CommandResult(self.test_returncode, "", "")
        return CommandResult(0, "", "")


class _ImportClient:
    def __init__(
        self,
        work_dir: Path | str,
        *,
        ssh_runner: _Runner | None = None,
        license_env: bool = True,
    ) -> None:
        # Without license_env, the optional environment values come back empty.
        env = '"/cad/lib" "27080@lic" "/lic.dat"' if license_env else '"" "" ""'
        context = {
            "status": "success",
            "output": f'("{work_dir}" "/cad/IC/bin:/usr/bin" {env} "/cad/IC")',
        }
        preflight = {"status": "success", "output": "t"}
        self.responses = [context, preflight, _IMPORTED]
        self.ssh_runner = ssh_runner
        self.skills: list[str] = []
        self.timeouts: list[int] = []
        self.uploads: list[tuple[Path, str, int | None]] = []

    def execute_skill(self, skill: str, *, timeout: int):
        self.skills.append(skill)
        self.timeouts.append(timeout)
        return self.responses.pop(0)

    def upload_file(self, local_path: Path, remote_path: str, *, timeout: int | None = None):
        self.uploads.append((Path(local_path), remote_path, timeout))
        return {"status": "success", "output": remote_path}


@pytest.fixture
def local_import(monkeypatch, tmp_path):
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()
    (work_dir / "cds.lib").write_text("DEFINE demoLib ./demoLib\n", encoding="utf-8")
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    calls: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="spiceIn ok\n", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)
    return work_dir, netlist_file, calls


def _assert_no_shell_from_skill(skills: list[str]) -> None:
    assert all("spiceIn -param" not in skill and "system(" not in skill for skill in skills)


def test_schematic_export_netlist_skill_escapes_names_and_maps_options() -> None:
    skill = schematic_export_netlist_skill('demo"Lib', "tb\\inv", recreate_all=False)

    assert "simulator('spectre)" in skill
    assert 'design("demo\\"Lib" "tb\\\\inv" "schematic" "r")' in skill
    assert "createNetlist(?recreateAll nil ?display nil)" in skill


def test_schematic_export_netlist_skill_rejects_unsafe_simulator_symbol() -> None:
    with pytest.raises(ValueError, match="simulator"):
        schematic_export_netlist_skill(
            "demoLib",
            "tb_inv",
            simulator='spectre") system("rm -rf /")',
        )


@pytest.mark.parametrize(
    "export",
    [
        lambda client, output_dir: export_schematic_netlist(
            client,
            "demoLib",
            "tb_inv",
            output_dir,
            timeout=45,
        ),
        lambda client, output_dir: SchematicOps(client).export_netlist(
            "demoLib",
            "tb_inv",
            output_dir,
            timeout=45,
        ),
    ],
    ids=["function", "schematic_ops"],
)
def test_export_schematic_netlist_downloads_generated_netlist_directory(export, tmp_path) -> None:
    client = _ExportClient()
    output_dir = tmp_path / "tb_inv_netlist"

    result = export(client, output_dir)

    run_dir = _default_export_run_dir(client.ssh_runner.commands)
    source_dir = f"{run_dir}/tb_inv/spectre/schematic/netlist"
    assert result == {
        "source_file": f"{source_dir}/input.scs",
        "source_dir": source_dir,
        "output_dir": str(output_dir),
        "input_file": str(output_dir / "input.scs"),
        "run_dir": run_dir,
        "skill_result": {"status": "success", "output": f'"{source_dir}/input.scs"'},
        "download_result": {"status": "success", "output": str(output_dir)},
    }
    assert client.timeout == 45
    assert client.skill == schematic_export_netlist_skill("demoLib", "tb_inv", project_dir=run_dir)
    assert len(client.downloads) == 1
    remote_path, local_path, timeout, recursive = client.downloads[0]
    assert remote_path == source_dir
    assert local_path.parent == tmp_path
    assert local_path.name.startswith(".tb_inv_netlist.tmp-")
    assert timeout == 45
    assert recursive is True


def _default_export_run_dir(commands: list[str]) -> str:
    created = [
        match.group(1)
        for command in commands
        for match in re.finditer(r"^mkdir -m 700 (\S+) \|\| exit \$\?$", command, re.MULTILINE)
    ]
    assert len(created) == 1
    assert created[0].startswith("/tmp/virtuoso_bridge_netlist_export_demoLib_tb_inv_")
    return created[0]


def test_export_netlist_skill_redirects_project_dir_only_around_simulator() -> None:
    skill = schematic_export_netlist_skill("demoLib", "tb_inv", project_dir='/tmp/run "x"')

    # OCEAN reads projectDir when simulator() starts the session (checked on IC 6.1.8),
    # so the user's setting is changed only for that call and restored even on error.
    saved = 'vbProjectDir = envGetVal("asimenv.startup" "projectDir")'
    redirect = 'envSetVal("asimenv.startup" "projectDir" \'string "/tmp/run \\"x\\"")'
    restore = 'envSetVal("asimenv.startup" "projectDir" \'string vbProjectDir))'
    assert skill.index(saved) < skill.index("unwindProtect(progn(") < skill.index(redirect)
    assert skill.index(redirect) < skill.index("simulator('spectre)") < skill.index(restore)
    assert skill.index(restore) < skill.index('design("demoLib"')
    assert 'unless(stringp(vbProjectDir) error("cannot read asimenv.startup projectDir"))' in skill


def test_export_netlist_skill_without_project_dir_leaves_the_setting_alone() -> None:
    skill = schematic_export_netlist_skill("demoLib", "tb_inv")
    assert "envSetVal" not in skill and "envGetVal" not in skill
    assert "vbSimResult = errset(simulator('spectre) nil)" in skill


def test_export_netlist_creates_a_private_run_dir_and_removes_it_after_download(tmp_path) -> None:
    client = _ExportClient()

    result = export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

    commands = client.ssh_runner.commands
    run_dir = result["run_dir"]
    assert commands == [
        schematic_netlist_module._remote_run_dir_create_command(run_dir, "netlist export"),
        schematic_netlist_module._remote_run_dir_remove_command(run_dir, "netlist export"),
    ]
    assert commands[-1].endswith(f"\nvb_verify_dir {run_dir} || exit $?\nrm -rf {run_dir}\n")
    assert result["source_dir"].startswith(run_dir + "/")
    assert (tmp_path / "netlist" / "input.scs").read_text(encoding="utf-8") == _SPECTRE_TEXT


def test_export_netlist_uses_a_new_run_dir_each_time(tmp_path) -> None:
    first = export_schematic_netlist(_ExportClient(), "demoLib", "tb_inv", tmp_path / "a")
    second = export_schematic_netlist(_ExportClient(), "demoLib", "tb_inv", tmp_path / "b")
    assert first["run_dir"] != second["run_dir"]


def test_export_netlist_keeps_the_run_dir_and_names_it_on_failure(tmp_path) -> None:
    client = _ExportClient(download_errors=["network failed"])

    with pytest.raises(RuntimeError, match="network failed") as excinfo:
        export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

    run_dir = _default_export_run_dir(client.ssh_runner.commands)
    assert str(excinfo.value).endswith(f"\nnetlist export files kept in {run_dir}")
    assert type(excinfo.value.__cause__) is RuntimeError
    assert len(client.ssh_runner.commands) == 1, "nothing is removed after a failure"


def test_export_netlist_failure_of_another_type_is_reraised_unchanged(tmp_path, caplog) -> None:
    timeout_error = subprocess.TimeoutExpired(["scp"], 30)
    client = _ExportClient(download_raises=timeout_error)

    with caplog.at_level(logging.WARNING, logger=schematic_netlist_module.__name__):
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

    run_dir = _default_export_run_dir(client.ssh_runner.commands)
    assert excinfo.value is timeout_error
    assert f"netlist export files kept in {run_dir}" in caplog.text
    assert len(client.ssh_runner.commands) == 1


def test_export_netlist_keeps_a_caller_run_dir(tmp_path) -> None:
    client = _ExportClient()

    result = export_schematic_netlist(
        client, "demoLib", "tb_inv", tmp_path / "netlist", run_dir="/work/nl export"
    )

    assert client.ssh_runner.commands == ["mkdir -p '/work/nl export'"]
    assert result["run_dir"] == "/work/nl export"
    assert result["source_dir"] == "/work/nl export/tb_inv/spectre/schematic/netlist"
    assert 'envSetVal("asimenv.startup" "projectDir" \'string "/work/nl export")' in client.skill


def test_export_netlist_refuses_a_relative_run_dir_before_netlisting(tmp_path) -> None:
    client = _ExportClient()

    with pytest.raises(ValueError, match="absolute path on the Virtuoso host"):
        export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist", run_dir="nl")

    assert client.skill is None and client.ssh_runner.commands == []


def test_export_netlist_outside_the_run_dir_is_left_in_place(tmp_path, caplog) -> None:
    # A Virtuoso that ignored the redirect netlists into the user's project
    # directory: the export still works, deletes nothing there, and says so.
    outside = "/home/designer/simulation/tb_inv/spectre/schematic/netlist"
    client = _ExportClient(f'"{outside}/input.scs"')

    with caplog.at_level(logging.WARNING, logger=schematic_netlist_module.__name__):
        result = export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

    assert result["source_dir"] == outside
    assert f"createNetlist wrote {outside} outside the export run directory" in caplog.text
    assert not any(outside in command or "simulation" in command for command in client.ssh_runner.commands)
    assert client.ssh_runner.commands[-1] == schematic_netlist_module._remote_run_dir_remove_command(
        result["run_dir"], "netlist export"
    )


def test_local_export_runs_in_a_private_run_dir_and_removes_it(tmp_path) -> None:
    client = _ExportClient(ssh_runner=None)
    seen: dict = {}
    real_execute = client.execute_skill

    def execute_skill(skill: str, *, timeout: int):
        run_dir = Path(_PROJECT_DIR.search(skill).group(1))
        seen["run_dir"], seen["mode"] = run_dir, stat.S_IMODE(os.lstat(run_dir).st_mode)
        return real_execute(skill, timeout=timeout)

    client.execute_skill = execute_skill  # type: ignore[method-assign]
    try:
        result = export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

        assert result["run_dir"] == str(seen["run_dir"])
        assert seen["run_dir"].name.startswith("virtuoso_bridge_netlist_export_demoLib_tb_inv_")
        if os.name != "nt":
            assert seen["mode"] == 0o700
        assert not seen["run_dir"].exists()
    finally:
        if "run_dir" in seen:
            shutil.rmtree(seen["run_dir"], ignore_errors=True)


def test_schematic_import_netlist_skill_overwrites_through_temporary_view() -> None:
    skill = schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        overwrite=True,
        param_file="/tmp/import-nand2/spiceIn.il",
        spicein_log_file="/tmp/import-nand2/spiceIn.log",
    )

    _assert_no_shell_from_skill([skill])
    assert "conn2sch -" not in skill
    assert "ddDeleteObj(vbSchematicObj)" not in skill
    assert 'vbDestViewName = "__vb_import_' in skill
    assert skill.index("?destViewName vbDestViewName ?block t) nil)") < skill.index(
        'dbCopyCellView(vbTempCv "demoLib" "nand2" "schematic" nil nil t)'
    )


def test_overwrite_conversion_raises_its_failure_only_after_cleanup() -> None:
    # An error raised inside unwindProtect reached the bridge as "nil" once
    # the cleanup's own errset reset errset.errset.
    skill = schematic_import_netlist_skill("demoLib", "nand2", overwrite=True)

    assert "unwindProtect" not in skill
    assert 'vbFailure = "target schematic copy failed"' in skill
    cleanup = skill.index("errset(ddDeleteObj(vbTempObj) nil)")
    assert skill.index('vbFailure = "target schematic copy failed"') < cleanup
    assert cleanup < skill.index("when(vbFailure error(vbFailure))")


def test_overwrite_refuses_a_target_open_in_virtuoso_before_spicein() -> None:
    open_check = "when(setof(vbOpenCv dbGetOpenCellViews() "
    message = "target demoLib/nand2/schematic is open for editing in Virtuoso; close it before overwriting"
    preflight = schematic_netlist_module._schematic_import_preflight_skill(
        "demoLib", "nand2", netlist_view="netlist", schematic_view="schematic", overwrite=True,
    )
    conversion = schematic_import_netlist_skill("demoLib", "nand2", overwrite=True)

    for skill in (preflight, conversion):
        assert open_check in skill and message in skill
        assert 'vbOpenCv~>viewName == "schematic" && vbOpenCv~>mode != "r")' in skill
    assert conversion.index(open_check) < conversion.index("conn2Sch(")
    assert open_check not in schematic_netlist_module._schematic_import_preflight_skill(
        "demoLib", "nand2", netlist_view="netlist", schematic_view="schematic", overwrite=False,
    )


def test_schematic_import_netlist_skill_rejects_same_target_views() -> None:
    skill = schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        netlist_view="schematic",
        schematic_view="schematic",
    )

    assert 'when("schematic" == "schematic" error("netlist and schematic views must differ"))' in skill


def test_import_netlist_schematic_runs_spicein_outside_skill(local_import, tmp_path) -> None:
    work_dir, netlist_file, subprocess_calls = local_import
    run_dir = tmp_path / "import-nand2"
    client = _ImportClient(work_dir)

    result = import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        netlist_file,
        language="Spectre",
        run_dir=run_dir,
        timeout=90,
    )

    assert result == _IMPORTED
    assert len(subprocess_calls) == 1
    args, kwargs = subprocess_calls[0]
    assert args[0] == ["/cad/IC/bin/spiceIn", "-param", str(run_dir / "spiceIn.il")]
    assert kwargs["cwd"] == run_dir
    assert kwargs["timeout"] == 90
    assert kwargs["env"]["PATH"].startswith("/cad/IC/bin")
    assert (run_dir / "cds.lib").read_text(encoding="utf-8") == f"INCLUDE {work_dir / 'cds.lib'}\n"
    param_text = (run_dir / "spiceIn.il").read_text(encoding="utf-8")
    assert f'  \'netlistFile "{netlist_file}"' in param_text
    assert '  \'outputViewName "netlist"' in param_text
    _assert_no_shell_from_skill(client.skills)
    assert client.skills[-1] == schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        param_file=run_dir / "spiceIn.il",
        spicein_log_file=run_dir / "spiceIn.log",
    )


def test_import_netlist_schematic_rejects_local_control_file_collision(local_import, tmp_path) -> None:
    work_dir, _, subprocess_calls = local_import
    run_dir = tmp_path / "import-nand2"
    run_dir.mkdir()
    netlist_file = run_dir / "spiceIn.il"
    netlist_file.write_text("original netlist\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="local input file conflicts"):
        import_netlist_schematic(
            _ImportClient(work_dir),
            "demoLib",
            "nand2",
            netlist_file,
            run_dir=run_dir,
            timeout=30,
        )

    assert subprocess_calls == []
    assert netlist_file.read_text(encoding="utf-8") == "original netlist\n"


def test_import_netlist_schematic_rejects_missing_local_input(tmp_path) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()

    with pytest.raises(RuntimeError, match="local input file not found"):
        import_netlist_schematic(
            _ImportClient(work_dir),
            "demoLib",
            "nand2",
            tmp_path / "missing.scs",
            run_dir=tmp_path / "import-nand2",
            timeout=30,
        )


def test_import_netlist_schematic_uses_unique_default_run_dirs(local_import) -> None:
    work_dir, netlist_file, subprocess_calls = local_import
    client = _ImportClient(work_dir, license_env=False)
    client.responses = client.responses * 2

    import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=30)
    import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=30)

    run_dirs = [kwargs["cwd"] for _, kwargs in subprocess_calls]
    assert len(run_dirs) == 2
    assert run_dirs[0] != run_dirs[1]
    assert all(path.name.startswith("virtuoso_bridge_netlist_import_demoLib_nand2_") for path in run_dirs)
    tmp_root = Path("/tmp").resolve()
    assert all(path.resolve().is_relative_to(tmp_root) for path in run_dirs)
    assert not any(path.exists() for path in run_dirs), "default run dirs are removed after success"


def _spicein_sees_run_dir(monkeypatch, seen: dict, *, raises: BaseException | None = None) -> None:
    def fake_run(*args, **kwargs):
        run_path = Path(kwargs["cwd"])
        seen["run_dir"] = run_path
        seen["mode"] = stat.S_IMODE(os.lstat(run_path).st_mode)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args[0], 0, stdout="spiceIn ok\n", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)


def test_local_import_runs_in_a_private_default_run_dir_and_removes_it(local_import, monkeypatch) -> None:
    work_dir, netlist_file, _ = local_import
    seen: dict = {}
    _spicein_sees_run_dir(monkeypatch, seen)

    result = import_netlist_schematic(_ImportClient(work_dir), "demoLib", "nand2", netlist_file, timeout=30)

    assert result == _IMPORTED
    assert seen["run_dir"].name.startswith("virtuoso_bridge_netlist_import_demoLib_nand2_")
    if os.name != "nt":
        assert seen["mode"] == 0o700
    assert not seen["run_dir"].exists()


def test_local_import_keeps_the_default_run_dir_and_names_it_on_failure(local_import, monkeypatch) -> None:
    work_dir, netlist_file, _ = local_import
    seen: dict = {}
    _spicein_sees_run_dir(monkeypatch, seen)
    client = _ImportClient(work_dir)
    client.responses[-1] = {"status": "error", "errors": ["conn2Sch failed"], "output": ""}

    try:
        with pytest.raises(RuntimeError, match="conn2Sch failed") as excinfo:
            import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=30)

        run_dir = seen["run_dir"]
        assert str(excinfo.value).endswith(f"\nnetlist import files kept in /tmp/{run_dir.name}")
        assert type(excinfo.value.__cause__) is RuntimeError
        assert (run_dir / "spiceIn.il").is_file()
    finally:
        if "run_dir" in seen:
            shutil.rmtree(seen["run_dir"], ignore_errors=True)


def test_local_import_failure_of_another_type_is_reraised_unchanged(local_import, monkeypatch, caplog) -> None:
    work_dir, netlist_file, _ = local_import
    timeout_error = subprocess.TimeoutExpired(["spiceIn"], 30)
    seen: dict = {}
    _spicein_sees_run_dir(monkeypatch, seen, raises=timeout_error)

    try:
        with caplog.at_level(logging.WARNING, logger=schematic_netlist_module.__name__):
            with pytest.raises(subprocess.TimeoutExpired) as excinfo:
                import_netlist_schematic(_ImportClient(work_dir), "demoLib", "nand2", netlist_file, timeout=30)

        assert excinfo.value is timeout_error
        assert f"netlist import files kept in /tmp/{seen['run_dir'].name}" in caplog.text
        assert seen["run_dir"].is_dir()
    finally:
        if "run_dir" in seen:
            shutil.rmtree(seen["run_dir"], ignore_errors=True)


def test_local_import_cleanup_failure_only_warns(local_import, monkeypatch, caplog) -> None:
    work_dir, netlist_file, subprocess_calls = local_import
    original_rmtree = shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path).name.startswith("virtuoso_bridge_netlist_import_"):
            raise OSError("busy")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(schematic_netlist_module.shutil, "rmtree", failing_rmtree)

    try:
        with caplog.at_level(logging.WARNING, logger=schematic_netlist_module.__name__):
            result = import_netlist_schematic(
                _ImportClient(work_dir), "demoLib", "nand2", netlist_file, timeout=30,
            )

        assert result == _IMPORTED
        assert "was not removed: busy" in caplog.text
    finally:
        for _, kwargs in subprocess_calls:
            original_rmtree(kwargs["cwd"], ignore_errors=True)


def test_local_import_keeps_an_explicit_run_dir(local_import, tmp_path) -> None:
    work_dir, netlist_file, _ = local_import
    run_dir = tmp_path / "import-nand2"

    import_netlist_schematic(_ImportClient(work_dir), "demoLib", "nand2", netlist_file, run_dir=run_dir)

    assert (run_dir / "spiceIn.il").is_file()


def test_import_netlist_schematic_uploads_inputs_and_runs_remote_shell(tmp_path) -> None:
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    dev_map_file = tmp_path / "devmap.txt"
    dev_map_file.write_text("devselect := resistor res\n", encoding="utf-8")
    client = _ImportClient("/remote/work", ssh_runner=_Runner())

    result = import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        netlist_file,
        dev_map_file=dev_map_file,
        run_dir="/remote/import-nand2",
        timeout=120,
    )

    assert result["status"] == "success"
    uploaded_names = {(path.name, remote, timeout) for path, remote, timeout in client.uploads}
    assert ("nand2.scs", "/remote/import-nand2/inputs/netlist_nand2.scs", 120) in uploaded_names
    assert ("devmap.txt", "/remote/import-nand2/inputs/devmap_devmap.txt", 120) in uploaded_names
    assert ("spiceIn.il", "/remote/import-nand2/spiceIn.il", 120) in uploaded_names
    assert ("cds.lib", "/remote/import-nand2/cds.lib", 120) in uploaded_names
    commands = client.ssh_runner.commands
    assert commands[0] == "mkdir -p /remote/import-nand2"
    assert not any("mkdir -m 700" in command or "rm -rf" in command for command in commands)
    spicein_commands = [command for command in commands if "spiceIn" in command]
    assert len(spicein_commands) == 1
    assert "bash -lc" in spicein_commands[0]
    assert "/cad/IC/bin/spiceIn -param /remote/import-nand2/spiceIn.il" in spicein_commands[0]
    _assert_no_shell_from_skill(client.skills)
    assert client.skills[-1] == schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        param_file="/remote/import-nand2/spiceIn.il",
        spicein_log_file="/remote/import-nand2/spiceIn.log",
    )


@pytest.mark.parametrize(
    ("netlist_file", "dev_map_file", "run_dir"),
    [
        ("/already/remote/nand2.scs", "/already/remote/devmap.txt", "/remote/import-nand2"),
        ("/already remote/nand'2.scs", "/already remote/dev map's.txt", "/remote/import dir/nand'2"),
    ],
    ids=["plain", "needs_quoting"],
)
def test_import_netlist_schematic_checks_assumed_remote_inputs(
    netlist_file: str,
    dev_map_file: str,
    run_dir: str,
) -> None:
    client = _ImportClient("/remote/work", ssh_runner=_Runner(), license_env=False)

    import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        netlist_file,
        dev_map_file=dev_map_file,
        run_dir=run_dir,
        timeout=120,
    )

    commands = client.ssh_runner.commands
    assert f"mkdir -p {shlex.quote(run_dir)}" in commands
    assert f"test -f {shlex.quote(netlist_file)}" in commands
    assert f"test -f {shlex.quote(dev_map_file)}" in commands
    assert not any("rm -rf" in command for command in commands)
    uploaded_names = [path.name for path, _, _ in client.uploads]
    assert sorted(uploaded_names) == ["cds.lib", "spiceIn.il"]


class _FailingRunner(_Runner):
    """Fails every command that contains *marker*."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        if self.marker in command:
            self.commands.append(command)
            return CommandResult(1, "", f"{self.marker} failed")
        return super().run_command(command, timeout)


def _default_remote_run_dir(commands: list[str]) -> str:
    # The shared prelude itself mentions "mkdir -m 700"; match the top-level line.
    created = [
        match.group(1)
        for command in commands
        for match in re.finditer(r"^mkdir -m 700 (\S+) \|\| exit \$\?$", command, re.MULTILINE)
    ]
    assert len(created) == 1
    assert created[0].startswith("/tmp/virtuoso_bridge_netlist_import_demoLib_nand2_")
    return created[0]


def test_remote_import_creates_a_private_default_run_dir_and_removes_it(tmp_path) -> None:
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    client = _ImportClient("/remote/work", ssh_runner=_Runner())

    result = import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=120)

    assert result == _IMPORTED
    commands = client.ssh_runner.commands
    run_dir = _default_remote_run_dir(commands)
    assert commands[0] == schematic_netlist_module._remote_run_dir_create_command(run_dir)
    assert f"\nvb_verify_dir {run_dir} || exit $?\n" in commands[0]
    assert f"mkdir -p {run_dir}" not in commands
    assert commands[-1] == schematic_netlist_module._remote_run_dir_remove_command(run_dir)
    assert commands[-1].endswith(f"\nvb_verify_dir {run_dir} || exit $?\nrm -rf {run_dir}\n")
    assert {remote for _, remote, _ in client.uploads} == {
        f"{run_dir}/inputs/netlist_nand2.scs",
        f"{run_dir}/inputs/devmap_default.devmap",
        f"{run_dir}/spiceIn.il",
        f"{run_dir}/cds.lib",
    }


def test_remote_import_keeps_the_default_run_dir_and_names_it_on_failure(tmp_path) -> None:
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    client = _ImportClient("/remote/work", ssh_runner=_FailingRunner("bash -lc"))

    with pytest.raises(RuntimeError, match="spiceIn failed") as excinfo:
        import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=120)

    run_dir = _default_remote_run_dir(client.ssh_runner.commands)
    assert str(excinfo.value).endswith(f"\nnetlist import files kept in {run_dir}")
    assert not any("rm -rf" in command for command in client.ssh_runner.commands)


def test_remote_import_cleanup_failure_only_warns(tmp_path, caplog) -> None:
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    client = _ImportClient("/remote/work", ssh_runner=_FailingRunner("rm -rf"))

    with caplog.at_level(logging.WARNING, logger=schematic_netlist_module.__name__):
        result = import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=120)

    assert result == _IMPORTED
    run_dir = _default_remote_run_dir(client.ssh_runner.commands)
    assert f"netlist import run directory {run_dir} was not removed" in caplog.text


def test_remote_import_does_not_stage_when_the_run_dir_cannot_be_created(tmp_path) -> None:
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")
    client = _ImportClient("/remote/work", ssh_runner=_FailingRunner("mkdir -m 700"))

    with pytest.raises(RuntimeError, match="cannot create netlist import run directory") as excinfo:
        import_netlist_schematic(client, "demoLib", "nand2", netlist_file, timeout=120)

    assert "kept in" not in str(excinfo.value)
    assert len(client.ssh_runner.commands) == 1
    assert client.uploads == []


_needs_posix_sh = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("sh"), reason="requires a POSIX shell"
)


def _run_sh(command: str) -> subprocess.CompletedProcess:
    """Run a generated remote command with the local POSIX sh, as the SSH runner does."""
    return subprocess.run(["sh", "-s"], input=command, capture_output=True, text=True, timeout=30)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@_needs_posix_sh
@pytest.mark.parametrize("name", ["run", "run dir's"])
def test_remote_run_dir_create_command_makes_a_private_directory(tmp_path, name) -> None:
    run_dir = tmp_path / name

    proc = _run_sh(schematic_netlist_module._remote_run_dir_create_command(str(run_dir)))

    assert proc.returncode == 0, proc.stderr
    assert run_dir.is_dir() and not run_dir.is_symlink()
    assert _mode(run_dir) == 0o700


@_needs_posix_sh
@pytest.mark.parametrize("existing", ["directory", "symlink", "dangling_symlink"])
def test_remote_run_dir_create_command_refuses_an_existing_path(tmp_path, existing) -> None:
    run_dir = tmp_path / "run"
    planted = tmp_path / "planted"
    if existing == "directory":
        run_dir.mkdir()
        run_dir.chmod(0o755)
    else:
        if existing == "symlink":
            planted.mkdir()
            planted.chmod(0o755)
        run_dir.symlink_to(planted)

    proc = _run_sh(schematic_netlist_module._remote_run_dir_create_command(str(run_dir)))

    assert proc.returncode != 0
    if existing == "directory":
        assert _mode(run_dir) == 0o755, "an existing directory is not adopted"
    else:
        assert run_dir.is_symlink()
    if existing == "symlink":
        assert _mode(planted) == 0o755 and list(planted.iterdir()) == []
    if existing == "dangling_symlink":
        assert not planted.exists()


@_needs_posix_sh
def test_remote_run_dir_remove_command_deletes_the_directory(tmp_path) -> None:
    run_dir = tmp_path / "run dir's"
    assert _run_sh(schematic_netlist_module._remote_run_dir_create_command(str(run_dir))).returncode == 0
    (run_dir / "inputs").mkdir()
    (run_dir / "inputs" / "netlist_nand2.scs").write_text(_SPECTRE_TEXT, encoding="utf-8")
    (run_dir / "spiceIn.log").write_text("log\n", encoding="utf-8")

    proc = _run_sh(schematic_netlist_module._remote_run_dir_remove_command(str(run_dir)))

    assert proc.returncode == 0, proc.stderr
    assert not run_dir.exists() and not run_dir.is_symlink()


@_needs_posix_sh
def test_remote_run_dir_remove_command_refuses_a_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.symlink_to(target)

    proc = _run_sh(schematic_netlist_module._remote_run_dir_remove_command(str(run_dir)))

    assert proc.returncode != 0
    assert "unsafe remote netlist import directory" in proc.stderr
    assert run_dir.is_symlink()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"


@_needs_posix_sh
def test_export_run_dir_remove_command_refuses_a_symlink_and_names_the_export(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.symlink_to(target)

    proc = _run_sh(schematic_netlist_module._remote_run_dir_remove_command(str(run_dir), "netlist export"))

    assert proc.returncode != 0
    assert "unsafe remote netlist export directory" in proc.stderr
    assert (target / "keep.txt").is_file()


@pytest.mark.parametrize(
    ("netlist_file", "test_returncode", "message"),
    [
        ("relative/nand2.scs", 0, "remote input file must be absolute"),
        ("/missing/remote/nand2.scs", 1, "remote input file not found"),
    ],
)
def test_import_netlist_schematic_rejects_invalid_remote_input(
    netlist_file: str,
    test_returncode: int,
    message: str,
) -> None:
    client = _ImportClient("/remote/work", ssh_runner=_Runner(test_returncode=test_returncode))

    with pytest.raises(RuntimeError, match=message):
        import_netlist_schematic(
            client,
            "demoLib",
            "nand2",
            netlist_file,
            run_dir="/remote/import-nand2",
            timeout=120,
        )


def test_import_netlist_schematic_raises_on_conn2sch_error(local_import, tmp_path) -> None:
    work_dir, netlist_file, _ = local_import
    client = _ImportClient(work_dir)
    client.responses[-1] = {"status": "error", "errors": ["conn2Sch failed"], "output": ""}

    with pytest.raises(RuntimeError, match="conn2Sch failed"):
        import_netlist_schematic(
            client,
            "demoLib",
            "nand2",
            netlist_file,
            run_dir=tmp_path / "import-nand2",
            timeout=30,
        )


def test_schematic_ops_import_netlist_delegates_to_client(monkeypatch, tmp_path) -> None:
    calls = []
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text(_SPECTRE_TEXT, encoding="utf-8")

    def fake_import(client, lib, cell, netlist_file_arg, **kwargs):
        calls.append((client, lib, cell, netlist_file_arg, kwargs))
        return {"status": "success"}

    monkeypatch.setattr(schematic_netlist_module, "import_netlist_schematic", fake_import)

    client = object()
    result = SchematicOps(client).import_netlist(
        "demoLib",
        "nand2",
        netlist_file,
        timeout=12,
    )

    assert result == {"status": "success"}
    assert calls[0][:4] == (client, "demoLib", "nand2", netlist_file)
    assert calls[0][4] == {
        "language": "Spectre",
        "sim_name": "spectre",
        "output_sim_name": "spectre",
        "ref_libs": ("analogLib", "basic"),
        "netlist_view": "netlist",
        "schematic_view": "schematic",
        "overwrite": False,
        "dev_map_file": None,
        "run_dir": None,
        "timeout": 12,
    }


def test_export_schematic_netlist_replaces_existing_output_directory(tmp_path) -> None:
    output_dir = tmp_path / "netlist"
    output_dir.mkdir()
    (output_dir / "stale.scs").write_text("old\n", encoding="utf-8")

    result = export_schematic_netlist(_ExportClient(), "demoLib", "tb_inv", output_dir)

    assert result["input_file"] == str(output_dir / "input.scs")
    assert not (output_dir / "stale.scs").exists()
    assert (output_dir / "input.scs").read_text(encoding="utf-8") == _SPECTRE_TEXT


def test_export_schematic_netlist_preserves_existing_output_on_download_failure(tmp_path) -> None:
    output_dir = tmp_path / "netlist"
    output_dir.mkdir()
    (output_dir / "input.scs").write_text("old netlist\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="network failed"):
        export_schematic_netlist(
            _ExportClient(download_errors=["network failed"]),
            "demoLib",
            "tb_inv",
            output_dir,
        )

    assert (output_dir / "input.scs").read_text(encoding="utf-8") == "old netlist\n"


@pytest.mark.parametrize(
    ("source_file", "downloaded_name"),
    [
        (_SOURCE_FILE, "ade_e.scs"),
        ("/tmp/generated/netlist/other.scs", "other.scs"),
    ],
)
def test_export_schematic_netlist_requires_downloaded_input_scs(
    source_file: str,
    downloaded_name: str,
    tmp_path,
) -> None:
    client = _ExportClient(f'"{source_file}"', files={downloaded_name: _SPECTRE_TEXT})

    with pytest.raises(RuntimeError, match="downloaded netlist is missing input.scs"):
        export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")


def test_export_schematic_netlist_rejects_relative_source_path(tmp_path) -> None:
    client = _ExportClient('"input.scs"')

    with pytest.raises(RuntimeError, match="relative netlist path"):
        export_schematic_netlist(client, "demoLib", "tb_inv", tmp_path / "netlist")

    assert client.downloads == []


def test_export_schematic_netlist_rejects_local_output_nested_under_source_dir(tmp_path) -> None:
    from virtuoso_bridge import VirtuosoClient

    source_dir = tmp_path / "generated" / "netlist"
    source_dir.mkdir(parents=True)
    (source_dir / "input.scs").write_text(_SPECTRE_TEXT, encoding="utf-8")
    client = VirtuosoClient.local()

    def execute_skill(skill: str, *, timeout: int):
        return {"status": "success", "output": f'"{source_dir.as_posix()}/input.scs"'}

    client.execute_skill = execute_skill  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Refusing recursive copy with overlapping"):
        export_schematic_netlist(
            client,
            "demoLib",
            "tb_inv",
            source_dir / "nested" / "export",
            run_dir=str(tmp_path / "project"),
        )

    assert not (source_dir / "nested").exists()
    assert (source_dir / "input.scs").read_text(encoding="utf-8") == _SPECTRE_TEXT


def test_export_schematic_netlist_restores_existing_output_when_final_replace_fails(
    monkeypatch,
    tmp_path,
) -> None:
    original_rename = Path.rename

    def fail_tmp_install(self: Path, target: Path):
        if self.name.startswith(".netlist.tmp-"):
            raise OSError("install failed")
        return original_rename(self, target)

    output_dir = tmp_path / "netlist"
    output_dir.mkdir()
    (output_dir / "input.scs").write_text("old netlist\n", encoding="utf-8")
    monkeypatch.setattr(Path, "rename", fail_tmp_install)

    with pytest.raises(OSError, match="install failed"):
        export_schematic_netlist(
            _ExportClient(files={"input.scs": "new netlist\n"}),
            "demoLib",
            "tb_inv",
            output_dir,
        )

    assert output_dir.is_dir()
    assert (output_dir / "input.scs").read_text(encoding="utf-8") == "old netlist\n"


def _devmap_seen_by_spicein(monkeypatch, run_dir) -> dict:
    seen = {}

    def fake_run(*args, **kwargs):
        params = (run_dir / "spiceIn.il").read_text(encoding="utf-8")
        devmap = re.search(r"'devMapFile \"([^\"]*)\"", params).group(1)
        seen["path"] = devmap
        seen["text"] = Path(devmap).read_text(encoding="utf-8") if devmap else None
        return subprocess.CompletedProcess(args[0], 0, stdout="spiceIn ok\n", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)
    return seen


def test_import_without_a_device_map_uses_the_analoglib_defaults(local_import, tmp_path, monkeypatch) -> None:
    work_dir, netlist_file, _ = local_import
    run_dir = tmp_path / "import-nand2"
    seen = _devmap_seen_by_spicein(monkeypatch, run_dir)

    import_netlist_schematic(_ImportClient(work_dir), "demoLib", "nand2", netlist_file, run_dir=run_dir)

    assert seen["text"] == schematic_netlist_module.DEFAULT_DEVICE_MAP
    assert "devselect := resistor res" in seen["text"]
    assert not Path(seen["path"]).exists(), "the staged default map is removed afterwards"


def test_a_given_device_map_replaces_the_defaults(local_import, tmp_path, monkeypatch) -> None:
    work_dir, netlist_file, _ = local_import
    run_dir = tmp_path / "import-nand2"
    own = tmp_path / "pdk.devmap"
    own.write_text("devselect := resistor rppoly\n", encoding="utf-8")
    seen = _devmap_seen_by_spicein(monkeypatch, run_dir)

    import_netlist_schematic(
        _ImportClient(work_dir), "demoLib", "nand2", netlist_file, run_dir=run_dir, dev_map_file=own,
    )

    assert seen["text"] == "devselect := resistor rppoly\n"
