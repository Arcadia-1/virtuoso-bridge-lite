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


def test_schematic_export_netlist_skill_uses_ocean_netlister() -> None:
    skill = schematic_export_netlist_skill(
        "demoLib",
        "tb_inv",
        simulator="spectre",
        recreate_all=False,
    )

    assert 'isCallable(\'createNetlist)' in skill
    assert "simulator('spectre)" in skill
    assert 'design("demoLib" "tb_inv" "schematic" "r")' in skill
    assert "createNetlist(?recreateAll nil ?display nil)" in skill
    assert "simplifyFilename(vbSourceFile)" in skill
    assert "vbSourceFile" in skill


def test_schematic_export_netlist_skill_escapes_skill_strings() -> None:
    skill = schematic_export_netlist_skill(
        'demo"Lib',
        "tb\\inv",
    )

    assert 'design("demo\\"Lib" "tb\\\\inv" "schematic" "r")' in skill


def test_schematic_export_netlist_skill_rejects_unsafe_simulator_symbol() -> None:
    with pytest.raises(ValueError, match="simulator"):
        schematic_export_netlist_skill(
            "demoLib",
            "tb_inv",
            simulator='spectre") system("rm -rf /")',
        )


def test_export_schematic_netlist_downloads_generated_netlist_directory(tmp_path) -> None:
    class Client:
        skill: str | None = None
        timeout: int | None = None
        downloads: list[tuple[str, Path, int | None, bool]] = []

        def execute_skill(self, skill: str, *, timeout: int):
            self.skill = skill
            self.timeout = timeout
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            self.downloads.append((remote_path, local_path, timeout, recursive))
            local_path.mkdir()
            (local_path / "input.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

    client = Client()
    output_dir = tmp_path / "tb_inv_netlist"
    result = export_schematic_netlist(
        client,
        "demoLib",
        "tb_inv",
        output_dir,
        timeout=45,
    )

    assert result == {
        "source_file": "/tmp/generated/netlist/input.scs",
        "source_dir": "/tmp/generated/netlist",
        "output_dir": str(output_dir),
        "input_file": str(output_dir / "input.scs"),
        "skill_result": {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'},
        "download_result": {"status": "success", "output": str(output_dir)},
    }
    assert client.timeout == 45
    assert client.skill is not None
    assert 'design("demoLib" "tb_inv" "schematic" "r")' in client.skill
    assert len(client.downloads) == 1
    remote_path, local_path, timeout, recursive = client.downloads[0]
    assert remote_path == "/tmp/generated/netlist"
    assert local_path.parent == tmp_path
    assert local_path.name.startswith(".tb_inv_netlist.tmp-")
    assert timeout == 45
    assert recursive is True


def test_schematic_ops_export_netlist_delegates_to_client(tmp_path) -> None:
    class Client:
        skill: str | None = None
        timeout: int | None = None
        downloads: list[tuple[str, Path, int | None, bool]] = []

        def execute_skill(self, skill: str, *, timeout: int):
            self.skill = skill
            self.timeout = timeout
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            self.downloads.append((remote_path, local_path, timeout, recursive))
            local_path.mkdir()
            (local_path / "input.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

    client = Client()
    output_dir = tmp_path / "tb_inv_netlist"
    result = SchematicOps(client).export_netlist(
        "demoLib",
        "tb_inv",
        output_dir,
        timeout=75,
    )

    assert result["source_file"] == "/tmp/generated/netlist/input.scs"
    assert result["input_file"] == str(output_dir / "input.scs")
    assert client.timeout == 75
    assert client.skill is not None
    assert 'design("demoLib" "tb_inv" "schematic" "r")' in client.skill
    assert len(client.downloads) == 1
    remote_path, local_path, timeout, recursive = client.downloads[0]
    assert remote_path == "/tmp/generated/netlist"
    assert local_path.parent == tmp_path
    assert local_path.name.startswith(".tb_inv_netlist.tmp-")
    assert timeout == 75
    assert recursive is True


def test_schematic_import_netlist_skill_converts_imported_netlist_view_only() -> None:
    skill = schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        overwrite=True,
        param_file="/tmp/import-nand2/spiceIn.il",
        spicein_log_file="/tmp/import-nand2/spiceIn.log",
    )

    assert "spiceIn -param" not in skill
    assert "system(" not in skill
    assert "conn2sch -" not in skill
    assert "ddDeleteObj(vbSchematicObj)" not in skill
    assert "dbCopyCellView" in skill
    assert "unwindProtect" in skill
    assert 'vbDestViewName = "__vb_import_' in skill
    assert 'conn2Sch("demoLib" "nand2" "netlist" ?destLibName "demoLib"' in skill
    assert '?destViewName vbDestViewName ?block t) nil)' in skill
    assert 'list("imported" "demoLib" "nand2" "/tmp/import-nand2/spiceIn.il"' in skill
    assert "conn2sch.stdout" not in skill
    assert "smic12sf" not in skill
    assert "sinomos" not in skill


def test_schematic_import_netlist_skill_rejects_same_target_views() -> None:
    skill = schematic_import_netlist_skill(
        "demoLib",
        "nand2",
        netlist_view="schematic",
        schematic_view="schematic",
    )

    assert 'when("netlist" == "schematic"' not in skill
    assert 'when("schematic" == "schematic" error("netlist and schematic views must differ"))' in skill


def test_import_netlist_schematic_runs_spicein_outside_skill(monkeypatch, tmp_path) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()
    (work_dir / "cds.lib").write_text("DEFINE demoLib ./demoLib\n", encoding="utf-8")
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text("simulator lang=spectre\n", encoding="utf-8")
    run_dir = tmp_path / "import-nand2"

    class Client:
        ssh_runner = None
        skills: list[str] = []
        timeouts: list[int] = []
        responses = [
            {
                "status": "success",
                "output": (
                    f'("{work_dir}" "/cad/IC/bin:/usr/bin" "/cad/lib" '
                    '"27080@lic" "/lic.dat" "/cad/IC")'
                ),
            },
            {"status": "success", "output": "t"},
            {"status": "success", "output": '("imported" "demoLib" "nand2")'},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            self.skills.append(skill)
            self.timeouts.append(timeout)
            return self.responses.pop(0)

    subprocess_calls = []

    def fake_run(*args, **kwargs):
        subprocess_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="spiceIn ok\n", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)

    client = Client()
    result = import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        netlist_file,
        language="Spectre",
        run_dir=run_dir,
        timeout=90,
    )

    assert result == {"status": "success", "output": '("imported" "demoLib" "nand2")'}
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
    assert all("spiceIn -param" not in skill and "system(" not in skill for skill in client.skills)
    assert 'conn2Sch("demoLib" "nand2" "netlist"' in client.skills[-1]


def test_import_netlist_schematic_rejects_local_control_file_collision(
    monkeypatch,
    tmp_path,
) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()
    (work_dir / "cds.lib").write_text("DEFINE demoLib ./demoLib\n", encoding="utf-8")
    run_dir = tmp_path / "import-nand2"
    run_dir.mkdir()
    netlist_file = run_dir / "spiceIn.il"
    netlist_file.write_text("original netlist\n", encoding="utf-8")

    class Client:
        ssh_runner = None
        responses = [
            {
                "status": "success",
                "output": f'("{work_dir}" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
            {"status": "success", "output": '("imported" "demoLib" "nand2")'},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

    def fake_run(*args, **kwargs):
        raise AssertionError("spiceIn must not run when the input would be overwritten")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="local input file conflicts"):
        import_netlist_schematic(
            Client(),
            "demoLib",
            "nand2",
            netlist_file,
            run_dir=run_dir,
            timeout=30,
        )

    assert netlist_file.read_text(encoding="utf-8") == "original netlist\n"


def test_import_netlist_schematic_rejects_missing_local_input(tmp_path) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()

    class Client:
        ssh_runner = None
        responses = [
            {
                "status": "success",
                "output": f'("{work_dir}" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

    with pytest.raises(RuntimeError, match="local input file not found"):
        import_netlist_schematic(
            Client(),
            "demoLib",
            "nand2",
            tmp_path / "missing.scs",
            run_dir=tmp_path / "import-nand2",
            timeout=30,
        )


def test_import_netlist_schematic_uses_unique_default_run_dirs(monkeypatch, tmp_path) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()
    (work_dir / "cds.lib").write_text("DEFINE demoLib ./demoLib\n", encoding="utf-8")
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text("simulator lang=spectre\n", encoding="utf-8")

    class Client:
        ssh_runner = None
        skills: list[str] = []

        def execute_skill(self, skill: str, *, timeout: int):
            self.skills.append(skill)
            if "getWorkingDir()" in skill:
                return {
                    "status": "success",
                    "output": f'("{work_dir}" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
                }
            if "vbSchematicObj" in skill and "vbNetlistObj" in skill and "conn2Sch" not in skill:
                return {"status": "success", "output": "t"}
            return {"status": "success", "output": '("imported" "demoLib" "nand2")'}

    subprocess_calls = []

    def fake_run(*args, **kwargs):
        subprocess_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)

    client = Client()
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
    netlist_file.write_text("simulator lang=spectre\n", encoding="utf-8")
    dev_map_file = tmp_path / "devmap.txt"
    dev_map_file.write_text("devselect := resistor res\n", encoding="utf-8")

    class Runner:
        commands: list[tuple[str, int | None]] = []

        def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
            self.commands.append((command, timeout))
            return CommandResult(0, "", "")

    class Client:
        ssh_runner = Runner()
        skills: list[str] = []
        uploads: list[tuple[Path, str, int | None]] = []
        responses = [
            {
                "status": "success",
                "output": (
                    '("/remote/work" "/cad/IC/bin:/usr/bin" "/cad/lib" '
                    '"27080@lic" "/lic.dat" "/cad/IC")'
                ),
            },
            {"status": "success", "output": "t"},
            {"status": "success", "output": '("imported" "demoLib" "nand2")'},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            self.skills.append(skill)
            return self.responses.pop(0)

        def upload_file(self, local_path: Path, remote_path: str, *, timeout: int | None = None):
            self.uploads.append((Path(local_path), remote_path, timeout))
            return {"status": "success", "output": remote_path}

    client = Client()
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
    commands = [command for command, _ in client.ssh_runner.commands]
    assert any(command.startswith("mkdir -p ") for command in commands)
    spicein_commands = [command for command in commands if "spiceIn" in command]
    assert len(spicein_commands) == 1
    assert "bash -lc" in spicein_commands[0]
    assert "/cad/IC/bin/spiceIn -param /remote/import-nand2/spiceIn.il" in spicein_commands[0]
    assert all("spiceIn -param" not in skill and "system(" not in skill for skill in client.skills)
    assert 'conn2Sch("demoLib" "nand2" "netlist"' in client.skills[-1]


def test_import_netlist_schematic_validates_assumed_remote_inputs() -> None:
    class Runner:
        commands: list[tuple[str, int | None]] = []

        def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
            self.commands.append((command, timeout))
            if command.startswith("test -f "):
                return CommandResult(0, "", "")
            return CommandResult(0, "", "")

    class Client:
        ssh_runner = Runner()
        uploads: list[tuple[Path, str, int | None]] = []
        responses = [
            {
                "status": "success",
                "output": '("/remote/work" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
            {"status": "success", "output": '("imported" "demoLib" "nand2")'},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

        def upload_file(self, local_path: Path, remote_path: str, *, timeout: int | None = None):
            self.uploads.append((Path(local_path), remote_path, timeout))
            return {"status": "success", "output": remote_path}

    client = Client()
    import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        "/already/remote/nand2.scs",
        dev_map_file="/already/remote/devmap.txt",
        run_dir="/remote/import-nand2",
        timeout=120,
    )

    commands = [command for command, _ in client.ssh_runner.commands]
    assert "test -f /already/remote/nand2.scs" in commands
    assert "test -f /already/remote/devmap.txt" in commands
    uploaded_names = [path.name for path, _, _ in client.uploads]
    assert "nand2.scs" not in uploaded_names
    assert "devmap.txt" not in uploaded_names
    assert {"spiceIn.il", "cds.lib"}.issubset(set(uploaded_names))


def test_import_netlist_schematic_rejects_relative_remote_input() -> None:
    class Runner:
        commands: list[tuple[str, int | None]] = []

        def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
            self.commands.append((command, timeout))
            return CommandResult(0, "", "")

    class Client:
        ssh_runner = Runner()
        responses = [
            {
                "status": "success",
                "output": '("/remote/work" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

    with pytest.raises(RuntimeError, match="remote input file must be absolute"):
        import_netlist_schematic(
            Client(),
            "demoLib",
            "nand2",
            "relative/nand2.scs",
            run_dir="/remote/import-nand2",
            timeout=120,
        )


def test_import_netlist_schematic_quotes_assumed_remote_input_checks() -> None:
    netlist_file = "/already remote/nand'2.scs"
    dev_map_file = "/already remote/dev map's.txt"
    run_dir = "/remote/import dir/nand'2"

    class Runner:
        commands: list[tuple[str, int | None]] = []

        def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
            self.commands.append((command, timeout))
            return CommandResult(0, "", "")

    class Client:
        ssh_runner = Runner()
        uploads: list[tuple[Path, str, int | None]] = []
        responses = [
            {
                "status": "success",
                "output": '("/remote/work" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
            {"status": "success", "output": '("imported" "demoLib" "nand2")'},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

        def upload_file(self, local_path: Path, remote_path: str, *, timeout: int | None = None):
            self.uploads.append((Path(local_path), remote_path, timeout))
            return {"status": "success", "output": remote_path}

    client = Client()
    import_netlist_schematic(
        client,
        "demoLib",
        "nand2",
        netlist_file,
        dev_map_file=dev_map_file,
        run_dir=run_dir,
        timeout=120,
    )

    commands = [command for command, _ in client.ssh_runner.commands]
    assert f"mkdir -p {shlex.quote(run_dir)}" in commands
    assert f"test -f {shlex.quote(netlist_file)}" in commands
    assert f"test -f {shlex.quote(dev_map_file)}" in commands


def test_import_netlist_schematic_rejects_missing_remote_input() -> None:
    class Runner:
        def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
            if command.startswith("test -f "):
                return CommandResult(1, "", "missing")
            return CommandResult(0, "", "")

    class Client:
        ssh_runner = Runner()
        responses = [
            {
                "status": "success",
                "output": '("/remote/work" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

    with pytest.raises(RuntimeError, match="remote input file not found"):
        import_netlist_schematic(
            Client(),
            "demoLib",
            "nand2",
            "/missing/remote/nand2.scs",
            run_dir="/remote/import-nand2",
            timeout=120,
        )


def test_import_netlist_schematic_raises_on_conn2sch_error(monkeypatch, tmp_path) -> None:
    work_dir = tmp_path / "virtuoso"
    work_dir.mkdir()
    (work_dir / "cds.lib").write_text("DEFINE demoLib ./demoLib\n", encoding="utf-8")
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text("simulator lang=spectre\n", encoding="utf-8")

    class Client:
        ssh_runner = None
        responses = [
            {
                "status": "success",
                "output": f'("{work_dir}" "/cad/IC/bin:/usr/bin" "" "" "" "/cad/IC")',
            },
            {"status": "success", "output": "t"},
            {"status": "error", "errors": ["conn2Sch failed"], "output": ""},
        ]

        def execute_skill(self, skill: str, *, timeout: int):
            return self.responses.pop(0)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(schematic_netlist_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="conn2Sch failed"):
        import_netlist_schematic(
            Client(),
            "demoLib",
            "nand2",
            netlist_file,
            run_dir=tmp_path / "import-nand2",
            timeout=30,
        )


def test_schematic_ops_import_netlist_delegates_to_client(monkeypatch, tmp_path) -> None:
    calls = []
    netlist_file = tmp_path / "nand2.scs"
    netlist_file.write_text("simulator lang=spectre\n", encoding="utf-8")

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
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            assert recursive is True
            assert not local_path.exists()
            local_path.mkdir()
            (local_path / "input.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

    output_dir = tmp_path / "netlist"
    output_dir.mkdir()
    (output_dir / "stale.scs").write_text("old\n", encoding="utf-8")

    result = export_schematic_netlist(Client(), "demoLib", "tb_inv", output_dir)

    assert result["input_file"] == str(output_dir / "input.scs")
    assert not (output_dir / "stale.scs").exists()
    assert (output_dir / "input.scs").read_text(encoding="utf-8") == "simulator lang=spectre\n"


def test_export_schematic_netlist_preserves_existing_output_on_download_failure(tmp_path) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            return {"status": "error", "errors": ["network failed"], "output": ""}

    output_dir = tmp_path / "netlist"
    output_dir.mkdir()
    (output_dir / "input.scs").write_text("old netlist\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="network failed"):
        export_schematic_netlist(Client(), "demoLib", "tb_inv", output_dir)

    assert (output_dir / "input.scs").read_text(encoding="utf-8") == "old netlist\n"


def test_export_schematic_netlist_requires_downloaded_input_file(tmp_path) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            local_path.mkdir()
            (local_path / "ade_e.scs").write_text("simulatorOptions options\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

    with pytest.raises(RuntimeError, match="downloaded netlist is missing input.scs"):
        export_schematic_netlist(Client(), "demoLib", "tb_inv", tmp_path / "netlist")


def test_export_schematic_netlist_requires_input_scs_even_for_other_returned_file(tmp_path) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"/tmp/generated/netlist/other.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            local_path.mkdir()
            (local_path / "other.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

    with pytest.raises(RuntimeError, match="downloaded netlist is missing input.scs"):
        export_schematic_netlist(Client(), "demoLib", "tb_inv", tmp_path / "netlist")


def test_export_schematic_netlist_rejects_relative_source_path(tmp_path) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"input.scs"'}

        def download_file(self, *args, **kwargs):
            raise AssertionError("relative netlist path must not be downloaded")

    with pytest.raises(RuntimeError, match="relative netlist path"):
        export_schematic_netlist(Client(), "demoLib", "tb_inv", tmp_path / "netlist")


def test_export_schematic_netlist_rejects_local_output_nested_under_source_dir(tmp_path) -> None:
    from virtuoso_bridge import VirtuosoClient

    source_dir = tmp_path / "generated" / "netlist"
    source_dir.mkdir(parents=True)
    (source_dir / "input.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
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
    assert (source_dir / "input.scs").read_text(encoding="utf-8") == "simulator lang=spectre\n"


def test_export_schematic_netlist_restores_existing_output_when_final_replace_fails(
    monkeypatch,
    tmp_path,
) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"status": "success", "output": '"/tmp/generated/netlist/input.scs"'}

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: int | None = None,
            recursive: bool = False,
        ):
            local_path.mkdir()
            (local_path / "input.scs").write_text("new netlist\n", encoding="utf-8")
            return {"status": "success", "output": str(local_path)}

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
        export_schematic_netlist(Client(), "demoLib", "tb_inv", output_dir)

    assert output_dir.is_dir()
    assert (output_dir / "input.scs").read_text(encoding="utf-8") == "old netlist\n"
