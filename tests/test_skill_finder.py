from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.cli import main
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder


class _FakeSkillClient:
    def __init__(self) -> None:
        self.find_calls: list[tuple[str, str, int, bool]] = []
        self.missing_ok: list[bool] = []

    def find_skill(
        self,
        query: str,
        *,
        mode: str = "fuzzy",
        limit: int = 50,
        include_desc: bool = False,
        missing_ok: bool = True,
    ):
        self.find_calls.append((query, mode, limit, include_desc))
        self.missing_ok.append(missing_ok)
        return [
            {
                "name": "dbOpenCellViewByType",
                "syntax": "dbOpenCellViewByType(lib cell view)",
                "description": "Open a cellview.",
                "source_file": "database.fnd",
            }
        ]

    def get_skill_more_info(self, func_name: str, *, missing_ok: bool = True):
        self.missing_ok.append(missing_ok)
        return {
            "func_name": func_name,
            "file_path": "$database/db.html",
            "topic": func_name,
            "raw_html": "<h1>dbOpenCellViewByType</h1>",
            "plain_text": "# dbOpenCellViewByType",
        }


def _patch_cli_client(monkeypatch):
    fake = _FakeSkillClient()
    seen_profiles: list[str | None] = []

    class _FakeVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None):
            seen_profiles.append(profile)
            return fake

    # Documentation commands build their client without the daemon.
    monkeypatch.setattr(
        "virtuoso_bridge.cli._docs_client",
        lambda: _FakeVirtuosoClient.from_env(profile=cli._get_cli_profile()),
    )
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)
    return fake, seen_profiles


def test_skill_find_json_flag_emits_json(capsys, monkeypatch):
    fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "dbOpen", "--json", "--mode", "prefix", "--limit", "3"])

    assert rc == 0
    assert fake.find_calls == [("dbOpen", "prefix", 3, False)]
    # The CLI asks for an error, not [], when no Finder data exists.
    assert fake.missing_ok == [False]
    assert seen_profiles == [None]
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["name"] == "dbOpenCellViewByType"


def test_skill_find_passes_explicit_profile(capsys, monkeypatch):
    _fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "dbOpen", "-p", "worker1", "--json"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)[0]["source_file"] == "database.fnd"


def test_skill_find_passes_include_desc_with_explicit_profile(capsys, monkeypatch):
    fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "open.*cellview", "--mode", "regex", "-p", "worker1", "--json", "--include-desc"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)[0]["source_file"] == "database.fnd"


def test_skill_info_passes_explicit_profile(capsys, monkeypatch):
    _fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-info", "dbOpenCellViewByType", "-p", "worker1", "--json"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)["func_name"] == "dbOpenCellViewByType"


class _LocalTunnel:
    _ssh_runner = None
    _remote_host = "localhost"


def _write_finder_tree(tmp_path):
    doc_root = tmp_path / "ic" / "doc"
    skill_root = doc_root / "finder" / "SKILL" / "database"
    skill_root.mkdir(parents=True)
    (skill_root / "database.fnd").write_text(
        '("dbOpenCellViewByType"\n'
        '"dbOpenCellViewByType(lib cell view)"\n'
        '"Open a cellview.")\n',
        encoding="utf-8",
    )

    more_info_dir = doc_root / "api_more_info"
    more_info_dir.mkdir()
    (more_info_dir / "api_more_info.tgf").write_text(
        "dbOpenCellViewByType $database/db.html NULL HTML\n",
        encoding="utf-8",
    )
    html_dir = doc_root / "database"
    html_dir.mkdir()
    (html_dir / "db.html").write_text(
        "<html><body><h1>dbOpenCellViewByType</h1><p>Open a cellview.</p></body></html>",
        encoding="utf-8",
    )
    return skill_root.parent


def test_find_skill_uses_local_discovery_when_tunnel_has_no_ssh_runner(monkeypatch, tmp_path):
    skill_root = _write_finder_tree(tmp_path)

    def fake_discover(self, remote_runner=None, profile=None):
        assert remote_runner is None
        return skill_root

    monkeypatch.setattr(SKILLFinder, "discover", fake_discover)

    client = VirtuosoClient(tunnel=_LocalTunnel())
    results = client.find_skill("dbOpenCellViewByType", mode="exact")

    assert results == [
        {
            "name": "dbOpenCellViewByType",
            "syntax": "dbOpenCellViewByType(lib cell view)",
            "description": "Open a cellview.",
            "source_file": "database.fnd",
        }
    ]


def test_skill_more_info_uses_local_discovery_when_tunnel_has_no_ssh_runner(monkeypatch, tmp_path):
    skill_root = _write_finder_tree(tmp_path)

    def fake_discover(self, remote_runner=None, profile=None):
        assert remote_runner is None
        return skill_root

    monkeypatch.setattr(SKILLFinder, "discover", fake_discover)

    client = VirtuosoClient(tunnel=_LocalTunnel())
    result = client.get_skill_more_info("dbOpenCellViewByType", cache_dir=tmp_path / "cache")

    assert result is not None
    assert result["func_name"] == "dbOpenCellViewByType"
    assert "Open a cellview." in result["plain_text"]


class _RemoteMoreInfoTunnel:
    _remote_host = "eda-host"
    remote_host = "eda-host"

    def __init__(self, runner) -> None:
        self._ssh_runner = runner


class _RemoteMoreInfoRunner:
    """Fake SSH runner that serves a remote doc root's More Info files."""

    host = "eda-host"

    def __init__(self, remote_doc_root: str) -> None:
        self.remote_doc_root = remote_doc_root
        self.download_calls: list[tuple[str, Path]] = []

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        if "which virtuoso" in command:
            return CommandResult(0, f"{self.remote_doc_root.rstrip('/')}/../tools/dfII/bin/virtuoso\n", "")
        if "doc/finder/SKILL" in command:
            return CommandResult(0, f"{self.remote_doc_root}/finder/SKILL\n", "")
        return CommandResult(1, "", "unexpected command")

    def download(
        self,
        remote_path: str,
        local_path: Path,
        recursive: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        self.download_calls.append((remote_path, Path(local_path)))
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if remote_path.endswith(".tgf"):
            local_path.write_text(
                'dbOpenCellViewByType $skdfref/cvio.html "pgfId-5447242" HTML\n',
                encoding="utf-8",
            )
        else:
            local_path.write_text(
                "<html><body>\n"
                "<!-- [TOPIC_START_OPEN] [TOPIC_START_ATTR]text=pgfId-5447242 -->\n"
                "<h1>dbOpenCellViewByType</h1><p>Opens a cellview.</p>\n"
                "<!-- [TOPIC_END] -->\n"
                "</body></html>",
                encoding="utf-8",
            )
        return CommandResult(0, "", "")


def test_skill_more_info_remote_downloads_tgf_into_cache_file(monkeypatch, tmp_path):
    # Regression: the remote .tgf index must be downloaded to a file inside
    # the more_info cache directory.  Targeting the directory itself replaced
    # the directory with a file and broke every later More Info lookup.
    remote_root = Path("/opt/cadence/IC618/doc")
    runner = _RemoteMoreInfoRunner(remote_root.as_posix())
    client = VirtuosoClient(tunnel=_RemoteMoreInfoTunnel(runner))

    result = client.get_skill_more_info("dbOpenCellViewByType", cache_dir=tmp_path / "cache")

    assert result is not None
    assert result["func_name"] == "dbOpenCellViewByType"
    assert "Opens a cellview." in result["plain_text"]

    tgf_calls = [
        (remote_path, local_path)
        for remote_path, local_path in runner.download_calls
        if remote_path.endswith(".tgf")
    ]
    assert len(tgf_calls) == 1
    remote_path, local_path = tgf_calls[0]
    # Remote path stays POSIX (Windows client safety).
    assert remote_path == "/opt/cadence/IC618/doc/api_more_info/api_more_info.tgf"
    # Local target is a file inside the cache dir, never the dir itself.
    assert local_path.name == "api_more_info.tgf"
    assert local_path.parent == tmp_path / "cache" / "more_info"
    assert local_path.parent.is_dir()


def test_find_skill_writes_remote_doc_root_not_local_cache_ancestor(tmp_path):
    class _RemoteFinderRunner(_RemoteMoreInfoRunner):
        def download(self, remote_path, local_path, recursive=False, timeout=None):
            self.download_calls.append((remote_path, Path(local_path)))
            local_path = Path(local_path)
            if recursive:
                db_dir = local_path / "database"
                db_dir.mkdir(parents=True, exist_ok=True)
                (db_dir / "database.fnd").write_text(
                    '("dbOpenCellViewByType"\n"dbOpenCellViewByType(l c v)"\n"Open.")\n',
                    encoding="utf-8",
                )
                return CommandResult(0, "", "")
            return super().download(remote_path, local_path, recursive=recursive, timeout=timeout)

    runner = _RemoteFinderRunner("/opt/cadence/IC231/doc")
    client = VirtuosoClient(tunnel=_RemoteMoreInfoTunnel(runner))
    cache_dir = tmp_path / "local_parent" / "skill_cache"

    results = client.find_skill("dbOpenCellViewByType", mode="exact", cache_dir=cache_dir)

    assert len(results) == 1
    doc_root_marker = cache_dir / ".doc_root"
    assert doc_root_marker.read_text().strip() == "/opt/cadence/IC231/doc"
    assert str(tmp_path) not in doc_root_marker.read_text()


def test_skill_more_info_invalidates_stale_html_on_doc_root_switch_and_missing_marker(tmp_path):
    class _VersionedMoreInfoRunner(_RemoteMoreInfoRunner):
        def __init__(self, remote_doc_root: str, tag: str) -> None:
            super().__init__(remote_doc_root)
            self.tag = tag

        def download(self, remote_path, local_path, recursive=False, timeout=None):
            self.download_calls.append((remote_path, Path(local_path)))
            local_path = Path(local_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if remote_path.endswith(".tgf"):
                local_path.write_text(
                    'dbOpenCellViewByType $skdfref/cvio.html "pgfId-5447242" HTML\n',
                    encoding="utf-8",
                )
            else:
                local_path.write_text(
                    "<html><body>\n"
                    "<!-- [TOPIC_START_OPEN] [TOPIC_START_ATTR]text=pgfId-5447242 -->\n"
                    f"<h1>dbOpenCellViewByType</h1><p>Release {self.tag} doc.</p>\n"
                    "<!-- [TOPIC_END] -->\n"
                    "</body></html>",
                    encoding="utf-8",
                )
            return CommandResult(0, "", "")

    cache_dir = tmp_path / "cache"
    runner_old = _VersionedMoreInfoRunner("/opt/cadence/IC618/doc", "IC618")
    client_old = VirtuosoClient(tunnel=_RemoteMoreInfoTunnel(runner_old))
    res_old = client_old.get_skill_more_info("dbOpenCellViewByType", cache_dir=cache_dir)
    assert res_old is not None and "Release IC618 doc." in res_old["plain_text"]

    # Switching remote doc_root to IC231 must invalidate stale IC618 HTML files.
    runner_new = _VersionedMoreInfoRunner("/opt/cadence/IC231/doc", "IC231")
    client_new = VirtuosoClient(tunnel=_RemoteMoreInfoTunnel(runner_new))
    res_new = client_new.get_skill_more_info("dbOpenCellViewByType", cache_dir=cache_dir)
    assert res_new is not None
    assert "Release IC231 doc." in res_new["plain_text"]
    assert "IC618" not in res_new["plain_text"]

    # Missing .source_dir marker with stale cached HTML must also invalidate & re-download.
    (cache_dir / "more_info" / ".source_dir").unlink()
    (cache_dir / "more_info" / "skdfref" / "cvio.html").write_text(
        "<html><body><!-- [TOPIC_START_OPEN] [TOPIC_START_ATTR]text=pgfId-5447242 -->"
        "<p>Stale untagged HTML</p><!-- [TOPIC_END] --></body></html>",
        encoding="utf-8",
    )
    runner_new.download_calls.clear()
    res_refreshed = client_new.get_skill_more_info("dbOpenCellViewByType", cache_dir=cache_dir)
    assert res_refreshed is not None
    assert "Release IC231 doc." in res_refreshed["plain_text"]
    assert "Stale untagged HTML" not in res_refreshed["plain_text"]
    assert len(runner_new.download_calls) == 2


class _LocalBashRunner:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        proc = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout or 15,
            env=self.env,
            check=False,
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires POSIX bash")
def test_walk_up_remote_resolves_symlinked_virtuoso_binary(tmp_path: Path) -> None:
    real_ic = tmp_path / "opt" / "cadence" / "IC231"
    real_bin = real_ic / "tools" / "dfII" / "bin" / "virtuoso"
    real_bin.parent.mkdir(parents=True)
    real_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    real_bin.chmod(0o755)
    skill_dir = real_ic / "doc" / "finder" / "SKILL"
    skill_dir.mkdir(parents=True)

    wrapper_bin = tmp_path / "site_bin" / "virtuoso"
    wrapper_bin.parent.mkdir(parents=True)
    try:
        wrapper_bin.symlink_to(real_bin)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    found = SKILLFinder._walk_up_remote(_LocalBashRunner(), str(wrapper_bin))
    assert found == skill_dir.resolve()


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires POSIX bash")
def test_walk_up_remote_falls_back_when_readlink_f_unavailable(tmp_path: Path) -> None:
    real_ic = tmp_path / "opt" / "cadence" / "IC231"
    real_bin = real_ic / "tools" / "dfII" / "bin" / "virtuoso"
    real_bin.parent.mkdir(parents=True)
    real_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    real_bin.chmod(0o755)
    skill_dir = real_ic / "doc" / "finder" / "SKILL"
    skill_dir.mkdir(parents=True)

    wrapper_bin = tmp_path / "site_bin" / "virtuoso"
    wrapper_bin.parent.mkdir(parents=True)
    try:
        wrapper_bin.symlink_to(real_bin)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    real_readlink = shutil.which("readlink")
    if real_readlink is None:
        pytest.skip("readlink not found")

    # Simulate an older BSD/Solaris host where `readlink -f` and `realpath` fail
    # while plain `readlink <symlink>` works.
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    readlink_shim = shim_dir / "readlink"
    readlink_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-f" ]; then exit 1; fi\n'
        f'exec {shlex.quote(real_readlink)} "$@"\n',
        encoding="utf-8",
    )
    readlink_shim.chmod(0o755)
    realpath_shim = shim_dir / "realpath"
    realpath_shim.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    realpath_shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"

    found = SKILLFinder._walk_up_remote(_LocalBashRunner(env=env), str(wrapper_bin))
    assert found == skill_dir.resolve()
