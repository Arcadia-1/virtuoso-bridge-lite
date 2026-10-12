from __future__ import annotations

import shlex
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


class _ExportClient:
    def __init__(
        self,
        output: str = f'"{_SOURCE_FILE}"',
        *,
        files: dict[str, str] | None = None,
        download_errors: list[str] | None = None,
    ) -> None:
        self.output = output
        self.files = {"input.scs": _SPECTRE_TEXT} if files is None else files
        self.download_errors = download_errors
        self.skill: str | None = None
        self.timeout: int | None = None
        self.downloads: list[tuple[str, Path, int | None, bool]] = []

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        return {"status": "success", "output": self.output}

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        *,
        timeout: int | None = None,
        recursive: bool = False,
    ):
        self.downloads.append((remote_path, local_path, timeout, recursive))
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

    assert result == {
        "source_file": _SOURCE_FILE,
        "source_dir": "/tmp/generated/netlist",
        "output_dir": str(output_dir),
        "input_file": str(output_dir / "input.scs"),
        "skill_result": {"status": "success", "output": f'"{_SOURCE_FILE}"'},
        "download_result": {"status": "success", "output": str(output_dir)},
    }
    assert client.timeout == 45
    assert client.skill == schematic_export_netlist_skill("demoLib", "tb_inv")
    assert len(client.downloads) == 1
    remote_path, local_path, timeout, recursive = client.downloads[0]
    assert remote_path == "/tmp/generated/netlist"
    assert local_path.parent == tmp_path
    assert local_path.name.startswith(".tb_inv_netlist.tmp-")
    assert timeout == 45
    assert recursive is True


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
    assert any(command.startswith("mkdir -p ") for command in commands)
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
    uploaded_names = [path.name for path, _, _ in client.uploads]
    assert sorted(uploaded_names) == ["cds.lib", "spiceIn.il"]


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
