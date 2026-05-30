from __future__ import annotations

import json

import virtuoso_bridge
from virtuoso_bridge.cli import main
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
