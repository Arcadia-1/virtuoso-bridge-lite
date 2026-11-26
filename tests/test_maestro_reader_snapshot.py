from __future__ import annotations

import importlib
import io
import shlex
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro.reader import bundle, session

snapshot_mod = importlib.import_module("virtuoso_bridge.virtuoso.maestro.reader.snapshot")


class _SkillClient:
    def __init__(self, *outputs: str, runner=None) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []
        self.ssh_runner = runner
        self._tunnel = SimpleNamespace(_ssh_runner=runner) if runner is not None else None

    def execute_skill(self, expr: str, **_kwargs):
        self.calls.append(expr)
        output = self.outputs.pop(0) if self.outputs else "nil"
        return SimpleNamespace(ok=True, errors=[], output=output)


def test_sort_histories_by_mtime_ignores_orphan_logs_and_dotfiles() -> None:
    result = session.sort_histories_by_mtime([
        ("Interactive.5.rdb", 100),
        ("Job0.log", 200),
        (".log", 300),
    ])
    assert result == ["Interactive.5"]


def test_sort_histories_by_mtime_updates_known_rdb_histories_from_companions() -> None:
    result = session.sort_histories_by_mtime([
        ("Interactive.5.rdb", 100),
        ("Interactive.5.log", 250),
        ("Interactive.6.rdb", 200),
        ("Interactive.6.msg.db", 150),
        ("orphan.msg.db", 999),
        ("Job0.log", 888),
        (".rdb", 777),
        (".msg.db", 666),
    ])
    assert result == ["Interactive.5", "Interactive.6"]


@pytest.mark.parametrize(
    "bad_history",
    ["", ".", "..", "../escape", "sub/dir", "sub\\dir", "bad\x00name"],
)
def test_snapshot_and_dump_reject_invalid_history_segments(tmp_path: Path, bad_history: str) -> None:
    client = _SkillClient()
    with pytest.raises(ValueError, match="single safe path segment"):
        snapshot_mod.snapshot(client, output_root=str(tmp_path), history=bad_history)
    with pytest.raises(ValueError, match="single safe path segment"):
        snapshot_mod._dump_run_artifacts(
            client, tmp_path,
            history=bad_history, lib_path="/lib", scratch_root="/scratch",
            lib="LIB", cell="CELL", view="maestro",
        )
    with pytest.raises(ValueError, match="single safe path segment"):
        snapshot_mod._dump_run_artifacts_local(
            tmp_path,
            history=bad_history, lib_path="/lib", scratch_root="/scratch",
            lib="LIB", cell="CELL", view="maestro",
        )


def _add_tar_bytes(tf: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    tf.addfile(info, io.BytesIO(payload))


def test_dump_run_artifacts_quotes_paths_preserves_lib_precedence_and_handles_ancestor_name(
    tmp_path: Path,
) -> None:
    lib_path = "/proj/Interactive.0/my lib"
    scratch_root = "/scratch/Interactive.0/sim dir"
    history = "Interactive.0"
    lib, cell, view = "MY LIB", "amp cell", "maestro"

    commands: list[str] = []

    class _Runner:
        def run_command(self, cmd: str, timeout: int = 30):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    def _fake_download(remote: str, local: str):
        # Deliberately put authoritative lib_path FIRST and stale scratch_root
        # SECOND to verify extraction enforces lib_path precedence regardless
        # of archive member ordering.
        with tarfile.open(local, "w") as tf:
            _add_tar_bytes(
                tf,
                f"{lib_path}/{cell}/{view}/results/maestro/{history}.rdb",
                b"authoritative-lib-rdb",
            )
            _add_tar_bytes(
                tf,
                f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}.rdb",
                b"stale-scratch-rdb",
            )
            _add_tar_bytes(
                tf,
                f"{lib_path}/{cell}/{view}/results/maestro/{history}.log",
                b"authoritative-lib-log",
            )
            _add_tar_bytes(
                tf,
                f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}.log",
                b"stale-scratch-log",
            )
            _add_tar_bytes(
                tf,
                f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}.msg.db",
                b"scratch-only-msgdb",
            )
            _add_tar_bytes(
                tf,
                f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}/1/TEST/netlist/input.scs",
                b"// netlist contents\n",
            )

    client = _SkillClient(runner=_Runner())
    client.download_file = _fake_download

    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    snapshot_mod._dump_run_artifacts(
        client, snap_dir,
        history=history, lib_path=lib_path, scratch_root=scratch_root,
        lib=lib, cell=cell, view=view,
    )

    assert len(commands) == 2
    tar_cmd, rm_cmd = commands
    hist_remote = f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}"
    assert f"find {shlex.quote(hist_remote)} " in tar_cmd
    assert shlex.quote(f"{lib_path}/{cell}/{view}/results/maestro/{history}.rdb") in tar_cmd
    assert shlex.quote(f"{scratch_root}/{lib}/{cell}/{view}/results/maestro/{history}.rdb") in tar_cmd
    assert rm_cmd.startswith("rm -f ")

    hist_dir = snap_dir / history
    assert (hist_dir / f"{history}.rdb").read_bytes() == b"authoritative-lib-rdb"
    assert (hist_dir / f"{history}.log").read_bytes() == b"authoritative-lib-log"
    assert (hist_dir / f"{history}.msg.db").read_bytes() == b"scratch-only-msgdb"
    # Ancestor folder also named "Interactive.0" must NOT corrupt relative path
    assert (hist_dir / "1" / "TEST" / "netlist" / "input.scs").read_bytes() == b"// netlist contents\n"
    assert not (hist_dir / "sim dir").exists()


def test_brief_bundle_guards_empty_maestro_and_escapes_strings() -> None:
    client = _SkillClient('("/proj/lib" nil nil nil)')

    out = bundle.brief_bundle(
        client, sess='fnx"1\\a', lib='my"lib', cell="tb", view="maestro",
    )

    skill = client.calls[0]
    assert 'errset(maeGetSetup(?session "fnx\\"1\\\\a"))' in skill
    assert 'when(stringp(vbTest) car(errset(maeGetEnabledAnalysis(' in skill
    assert 'ddGetObj("my\\"lib")~>readPath' in skill
    assert skill.count("(") == skill.count(")")
    assert len(out["raw_sections"]) == 2
    assert out["raw_sections"][1][1] == "nil"


def test_full_bundle_guards_empty_maestro_preserves_newer_scratch_mtimes_and_quotes_find() -> None:
    find_cmds: list[str] = []

    class _Runner:
        def run_command(self, cmd: str, timeout: int = 15):
            find_cmds.append(cmd)
            if "/proj/my lib/" in cmd:
                return SimpleNamespace(
                    returncode=0,
                    stdout="100.0 Interactive.5.rdb\n100.0 Interactive.4.rdb\n",
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="500.0 Interactive.5.rdb\n400.0 Interactive.6.RO.rdb\n",
                stderr="",
            )

    # Round 1: empty Maestro (no tests)
    # Round 2: 2 head probes + 9 tail probes = 11 probes
    round2_output = (
        '("/proj/my lib" '
        '(nil) '
        '("Single Run") '
        '("LSCS") '
        '(nil) '
        '(nil) '
        '(nil) '
        '(nil) '
        '(nil) '
        '(("Interactive.5.rdb" "Interactive.4.rdb")) '
        '("/scratch/dir/MY LIB/tb cell/maestro/results/maestro/Interactive.5"))'
    )
    client = _SkillClient("(nil nil)", round2_output, runner=_Runner())

    out = bundle.full_bundle(
        client,
        sess='sess"1',
        lib="MY LIB",
        cell="tb cell",
        view="maestro",
    )

    assert len(client.calls) == 2
    assert 'errset(maeGetSetup(?session "sess\\"1"))' in client.calls[0]
    assert client.calls[0].count("(") == client.calls[0].count(")")
    assert "maeGetEnvOption" not in client.calls[1]
    assert "maeGetSimOption" not in client.calls[1]
    assert "maeGetTestOutputs" not in client.calls[1]
    assert client.calls[1].count("(") == client.calls[1].count(")")

    assert out["test"] == ""
    assert out["lib_path"] == "/proj/my lib"
    assert out["scratch_root"] == "/scratch/dir"

    assert len(find_cmds) == 2
    assert shlex.quote("/proj/my lib/tb cell/maestro/results/maestro") in find_cmds[0]
    assert shlex.quote("/scratch/dir/MY LIB/tb cell/maestro/results/maestro") in find_cmds[1]

    mtimes = dict(out["hist_files_mtime"])
    assert mtimes["Interactive.5.rdb"] == 500
    assert mtimes["Interactive.4.rdb"] == 100
    assert mtimes["Interactive.6.RO.rdb"] == 400
    assert session.sort_histories_by_mtime(out["hist_files_mtime"]) == [
        "Interactive.5",
        "Interactive.6.RO",
        "Interactive.4",
    ]
