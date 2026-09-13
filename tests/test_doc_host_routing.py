"""Split-host routing tests for the doc-first tooling.

The daemon host only launches ``ipcBeginProcess`` and may have no Cadence
installation at all; doc-info, doc-search, skill-find, and skill-info must
run on the GUI/documentation host and key their caches by it — including
the local-daemon + remote-GUI split where no daemon SSH runner exists.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

GUI_DOC_ROOT = "/opt/cadence/IC231/doc"


def _doc_info_payload(doc_root: str) -> dict:
    install_root = doc_root[:-4] if doc_root.endswith("/doc") else doc_root
    return {
        "doc_root": doc_root,
        "install_root": install_root,
        "virtuoso_version": "23.1",
        "version_source": "sdp",
        "doc_set_count": 2,
        "doc_sets_sample": ["DFII", "Schematics"],
        "skill_finder": {
            "path": f"{doc_root}/finder/SKILL",
            "found": True,
            "fnd_count": 1,
        },
        "api_more_info": {
            "tgf": f"{doc_root}/api_more_info/api_more_info.tgf",
            "found": True,
            "tgf_bytes": 1234,
        },
        "skdfref": {
            "path": f"{doc_root}/skdfref",
            "found": True,
            "html_count": 200,
            "style": "per-function",
            "sample": [],
        },
    }


class _StrictDaemonRunner:
    """Fails the test if any doc command reaches the daemon host."""

    host = "daemon-host"

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.downloads: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        raise AssertionError(f"doc command reached the daemon host: {command[:120]}")

    def download(self, remote_path, local_path, recursive=False, timeout=None) -> CommandResult:
        self.downloads.append(str(remote_path))
        raise AssertionError(f"doc download reached the daemon host: {remote_path}")


class _FakeGuiHostRunner:
    """Serves finder discovery, doc-info, doc-index, and More Info files."""

    host = "gui-host"

    def __init__(self, doc_root: str = GUI_DOC_ROOT) -> None:
        self.doc_root = doc_root
        self.commands: list[str] = []
        self.download_calls: list[tuple[str, Path]] = []
        self.records_path = "/tmp/vb_doc_index_records.jsonl.gz"

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        if "which virtuoso" in command:
            return CommandResult(0, f"{self.doc_root}/../tools/dfII/bin/virtuoso\n", "")
        if "doc/finder/SKILL" in command:
            return CommandResult(0, f"{self.doc_root}/finder/SKILL\n", "")
        if "vb_doc_info" in command:
            return CommandResult(0, json.dumps([_doc_info_payload(self.doc_root)]) + "\n", "")
        if "vb_doc_index" in command:
            return CommandResult(
                0,
                json.dumps({"path": self.records_path, "documents": 1, "topics": 0}) + "\n",
                "",
            )
        if command.startswith("rm -f "):
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected command: {command[:120]}")

    def download(self, remote_path, local_path, recursive=False, timeout=None) -> CommandResult:
        self.download_calls.append((str(remote_path), Path(local_path)))
        local_path = Path(local_path)
        if remote_path == self.records_path:
            records = [
                {
                    "kind": "document",
                    "path": f"{self.doc_root}/guide.html",
                    "relative_path": "guide.html",
                    "suffix": ".html",
                    "title": "Net Expression Guide",
                    "text": "Use net expression labels.",
                }
            ]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(local_path, "wt", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            return CommandResult(0, "", "")
        if remote_path.endswith(".tgf"):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                'dbOpenCellViewByType $skdfref/cvio.html "pgfId-5447242" HTML\n',
                encoding="utf-8",
            )
            return CommandResult(0, "", "")
        if remote_path.endswith(".html"):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                "<html><body>\n"
                "<!-- [TOPIC_START_OPEN] [TOPIC_START_ATTR]text=pgfId-5447242 -->\n"
                "<h1>dbOpenCellViewByType</h1><p>Opens a cellview.</p>\n"
                "<!-- [TOPIC_END] -->\n"
                "</body></html>",
                encoding="utf-8",
            )
            return CommandResult(0, "", "")
        if recursive:
            # SKILL Finder .fnd tree download.
            local_path.mkdir(parents=True, exist_ok=True)
            (local_path / "database.fnd").write_text(
                '("dbOpenCellViewByType"\n'
                '"dbOpenCellViewByType(lib cell view)"\n'
                '"Open a cellview.")\n',
                encoding="utf-8",
            )
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected download: {remote_path}")


class _SplitHostTunnel:
    """Role-aware tunnel stub with distinct daemon and GUI hosts."""

    def __init__(
        self,
        daemon_runner,
        gui_runner,
        *,
        daemon_host: str = "daemon-host",
        gui_host: str = "gui-host",
    ) -> None:
        self._ssh_runner = daemon_runner
        self.remote_host = daemon_host
        self._remote_host = daemon_host
        self.gui_runner = gui_runner
        self.gui_host = gui_host
        self._gui_host = gui_host
        self._profile = None


class _LegacyTunnel:
    """Pre-role tunnel stub: only a daemon runner and its host name."""

    def __init__(self, host: str) -> None:
        self.remote_host = host
        self._remote_host = host
        self._ssh_runner = object()


def test_doc_tools_route_through_gui_runner_not_daemon(tmp_path: Path) -> None:
    gui = _FakeGuiHostRunner()
    daemon = _StrictDaemonRunner()
    client = VirtuosoClient(tunnel=_SplitHostTunnel(daemon, gui))

    assert client.docs_runner is gui

    info = client.doc_info()
    assert info["ok"] is True
    assert info["doc_roots"][0]["doc_root"] == GUI_DOC_ROOT

    payload = client.search_docs("net expression", cache_dir=tmp_path / "cache")
    assert payload["results"][0]["relative_path"] == "guide.html"

    results = client.find_skill(
        "dbOpenCellViewByType", mode="exact", cache_dir=tmp_path / "finder-cache"
    )
    assert results[0]["name"] == "dbOpenCellViewByType"

    more = client.get_skill_more_info(
        "dbOpenCellViewByType", cache_dir=tmp_path / "mi-cache"
    )
    assert more is not None
    assert "Opens a cellview." in more["plain_text"]

    assert daemon.commands == []
    assert daemon.downloads == []
    assert any("vb_doc_info" in command for command in gui.commands)


def test_search_cache_is_keyed_by_gui_host(tmp_path: Path) -> None:
    gui = _FakeGuiHostRunner()
    client = VirtuosoClient(tunnel=_SplitHostTunnel(_StrictDaemonRunner(), gui))

    client.search_docs("net expression", cache_dir=tmp_path / "cache")

    assert list((tmp_path / "cache" / "gui-host").rglob("index.sqlite"))
    assert not (tmp_path / "cache" / "daemon-host").exists()


def test_local_daemon_remote_gui_doc_tools_use_gui_runner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VB_CACHE_DIR", str(tmp_path / "runcache"))
    gui = _FakeGuiHostRunner()
    client = VirtuosoClient(tunnel=_SplitHostTunnel(None, gui))

    # The daemon host is local: there is no daemon SSH runner at all, yet
    # every doc tool must still work through the remote GUI host runner.
    assert client.ssh_runner is None
    assert client.docs_runner is gui

    info = client.doc_info()
    assert info["ok"] is True
    assert info["doc_roots"][0]["doc_root"] == GUI_DOC_ROOT

    payload = client.search_docs("net expression", cache_dir=tmp_path / "cache")
    assert payload["results"][0]["relative_path"] == "guide.html"
    assert list((tmp_path / "cache" / "gui-host").rglob("index.sqlite"))

    results = client.find_skill("dbOpenCellViewByType", mode="exact")
    assert results[0]["name"] == "dbOpenCellViewByType"
    # Default cache key (runtime cache dir) is the GUI host, not "local".
    assert (tmp_path / "runcache" / "skill_finder" / "gui-host").is_dir()

    more = client.get_skill_more_info("dbOpenCellViewByType")
    assert more is not None
    assert "Opens a cellview." in more["plain_text"]


def test_cache_segments_keyed_by_gui_host_with_backward_compatible_fallbacks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VB_CACHE_DIR", str(tmp_path / "runcache"))

    split = VirtuosoClient(tunnel=_SplitHostTunnel(None, _FakeGuiHostRunner()))
    assert split._skill_finder_cache_host() == "gui-host"

    # Legacy stub tunnels (no role attributes) keep keying by the daemon
    # host so existing cache directories remain valid.
    legacy = VirtuosoClient(tunnel=_LegacyTunnel("eda-host"))
    assert legacy._skill_finder_cache_host() == "eda-host"

    local = VirtuosoClient.local()
    assert local._skill_finder_cache_host() == "local"

    # An explicit localhost GUI host means documentation is read locally.
    local_gui = VirtuosoClient(tunnel=_SplitHostTunnel(None, None, gui_host="localhost"))
    assert local_gui._skill_finder_cache_host() == "local"


class _RecordingRunner:
    def __init__(self, **kwargs) -> None:
        self.host = str(kwargs["host"])


def test_ssh_client_split_hosts_docs_target_gui_host(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner", _RecordingRunner)

    ssh = SSHClient(remote_host="compute-b", gui_host="gui-a", remote_user="designer")
    client = VirtuosoClient(tunnel=ssh)

    assert client.docs_runner is ssh.gui_runner
    assert client.docs_runner is not client.ssh_runner
    assert client.docs_runner.host == "gui-a"
    assert client._skill_finder_cache_host() == "gui-a"


def test_ssh_client_local_daemon_remote_gui_docs_target_gui_host(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner", _RecordingRunner)

    ssh = SSHClient(remote_host="localhost", gui_host="gui-a", remote_user="designer")
    assert ssh.ssh_runner is None

    client = VirtuosoClient(tunnel=ssh)

    assert client.docs_runner is ssh.gui_runner
    assert client.docs_runner.host == "gui-a"
    assert client._skill_finder_cache_host() == "gui-a"


def test_ssh_client_one_host_docs_keep_shared_runner_and_cache_key(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner", _RecordingRunner)

    ssh = SSHClient(remote_host="compute-b", remote_user="designer")
    client = VirtuosoClient(tunnel=ssh)

    assert client.docs_runner is ssh.ssh_runner
    assert client.docs_runner.host == "compute-b"
    assert client._skill_finder_cache_host() == "compute-b"
