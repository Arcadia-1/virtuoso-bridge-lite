from __future__ import annotations

import json
from pathlib import Path

import virtuoso_bridge
from virtuoso_bridge.cli import main
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder


class _FakeSkillClient:
    def __init__(self) -> None:
        self.find_calls: list[tuple[str, str, int, bool]] = []

    def find_skill(self, query: str, *, mode: str = "fuzzy", limit: int = 50, include_desc: bool = False):
        self.find_calls.append((query, mode, limit, include_desc))
        return [
            {
                "name": "dbOpenCellViewByType",
                "syntax": "dbOpenCellViewByType(lib cell view)",
                "description": "Open a cellview.",
                "source_file": "database.fnd",
            }
        ]

    def get_skill_more_info(self, func_name: str):
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

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)
    return fake, seen_profiles


def test_skill_find_json_flag_emits_json(capsys, monkeypatch):
    fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "dbOpen", "--json", "--mode", "prefix", "--limit", "3"])

    assert rc == 0
    assert fake.find_calls == [("dbOpen", "prefix", 3, False)]
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
