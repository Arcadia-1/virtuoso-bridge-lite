from __future__ import annotations

from pathlib import Path

import pytest

from virtuoso_bridge.virtuoso.schematic import (
    SchematicOps,
    export_schematic_netlist,
    schematic_export_netlist_skill,
)


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
