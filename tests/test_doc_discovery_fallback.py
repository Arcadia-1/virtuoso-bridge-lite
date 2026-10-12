"""Documentation discovery on sites where Cadence tools sit behind wrappers.

Non-interactive SSH shells there have no ``virtuoso`` on PATH and no
CDS_INST_DIR/CDSHOME, so SSH discovery finds nothing. The commands must say
so (not "No results"), and may fall back to the install root an already
running Virtuoso reports from ``cdsGetInstPath()``, without changing the
behaviour of configurations whose discovery already works.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.cli import main
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.docs_search import doc_roots_from_install_root
from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder, SKILLFinderNotFoundError

INSTALL_ROOT = "/cad/ic/lnx86"
DOC_ROOT = f"{INSTALL_ROOT}/doc"
FINDER_ROOT = f"{DOC_ROOT}/finder/SKILL"
RECORDS_PATH = "/tmp/vb_doc_index_records.jsonl.gz"


def _doc_info_payload(doc_root: str) -> dict:
    return {
        "doc_root": doc_root,
        "install_root": doc_root[: -len("/doc")],
        "virtuoso_version": "23.1",
        "version_source": "sdp",
        "doc_set_count": 1,
        "doc_sets_sample": ["maeSKILLref"],
        "skill_finder": {"path": f"{doc_root}/finder/SKILL", "found": True, "fnd_count": 1},
        "api_more_info": {"tgf": f"{doc_root}/api_more_info/api_more_info.tgf", "found": True, "tgf_bytes": 1},
        "skdfref": {"path": f"{doc_root}/skdfref", "found": False, "html_count": 0, "style": None, "sample": []},
    }


class _DocsHostRunner:
    """Documentation host; ``virtuoso_on_path=False`` models a wrapper site.

    ``existing`` lists the remote directories that exist. ``events`` records
    the discovery steps in order, shared with the daemon probe.
    """

    host = "eda-host"

    def __init__(
        self,
        events: list[str],
        *,
        virtuoso_on_path: bool = False,
        existing: tuple[str, ...] = (DOC_ROOT, FINDER_ROOT),
    ) -> None:
        self.events = events
        self.virtuoso_on_path = virtuoso_on_path
        self.existing = set(existing)
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        if "vb_doc_info" in command:
            roots = [root for root in (DOC_ROOT,) if root in command]
            return CommandResult(0, json.dumps([_doc_info_payload(r) for r in roots]) + "\n", "")
        if "vb_doc_index" in command:
            return CommandResult(0, json.dumps({"path": RECORDS_PATH, "documents": 1, "topics": 0}) + "\n", "")
        if command.startswith("rm -f "):
            return CommandResult(0, "", "")
        if "which virtuoso" in command:
            self.events.append("ssh:which")
            if self.virtuoso_on_path:
                return CommandResult(0, f"{INSTALL_ROOT}/tools/dfII/bin/virtuoso\n", "")
            return CommandResult(0, "NOTFOUND\n", "")
        if "VB_DOC_ROOT_OK" in command:
            self.events.append("ssh:test-doc")
            if DOC_ROOT in self.existing and DOC_ROOT in command:
                return CommandResult(0, "VB_DOC_ROOT_OK\n", "")
            return CommandResult(1, "", "")
        if "doc/finder/SKILL" in command:
            self.events.append("ssh:walk")
            if FINDER_ROOT in self.existing and INSTALL_ROOT in command:
                return CommandResult(0, FINDER_ROOT + "\n", "")
            return CommandResult(1, "", "")
        if "CDS_INST_DIR" in command:
            self.events.append("ssh:env")
            return CommandResult(0, "DOC\t\nDOC\t\nINSTALL\t\nINSTALL\t\nINSTALL\t\n", "")
        return CommandResult(1, "", f"unexpected command: {command[:120]}")

    def download(self, remote_path, local_path, recursive=False, timeout=None) -> CommandResult:
        local_path = Path(local_path)
        remote_path = str(remote_path)
        if remote_path == RECORDS_PATH:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "kind": "document",
                "path": f"{DOC_ROOT}/maeSKILLref/maeRunSimulation.html",
                "relative_path": "maeSKILLref/maeRunSimulation.html",
                "suffix": ".html",
                "title": "maeRunSimulation",
                "text": "maeRunSimulation runs the simulation of a maestro session.",
            }
            with gzip.open(local_path, "wt", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            return CommandResult(0, "", "")
        if remote_path == FINDER_ROOT and recursive:
            local_path.mkdir(parents=True, exist_ok=True)
            (local_path / "database.fnd").write_text(
                '("dbCreateRect"\n"dbCreateRect(d_cellView tx_layer l_bBox)"\n"Creates a rectangle.")\n',
                encoding="utf-8",
            )
            return CommandResult(0, "", "")
        if remote_path == f"{DOC_ROOT}/api_more_info/api_more_info.tgf":
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text("dbCreateRect $skdfref/rect.html NULL HTML\n", encoding="utf-8")
            return CommandResult(0, "", "")
        if remote_path == f"{DOC_ROOT}/skdfref/rect.html":
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text("<html><body><h1>dbCreateRect</h1><p>Creates a rectangle.</p></body></html>", encoding="utf-8")
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected download: {remote_path}")


class _Tunnel:
    """One-host tunnel stub: docs and daemon on the same configured host."""

    port = 65000

    def __init__(self, runner) -> None:
        self._ssh_runner = runner
        self.gui_runner = runner
        self.remote_host = self._remote_host = runner.host
        self.gui_host = self._gui_host = runner.host
        self._profile = None

    def close(self) -> None:
        pass


class _Probe:
    def __init__(self, events: list[str], result: str | None = INSTALL_ROOT, exc: Exception | None = None) -> None:
        self.events = events
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        self.events.append("daemon:cdsGetInstPath")
        if self.exc is not None:
            raise self.exc
        return self.result


def _client(runner, probe=None) -> VirtuosoClient:
    client = VirtuosoClient(tunnel=_Tunnel(runner))
    client._install_root_probe = probe
    return client


# -- client-level fallback ---------------------------------------------------


def test_skill_finder_falls_back_to_running_virtuoso_install_root(tmp_path: Path) -> None:
    events: list[str] = []
    runner = _DocsHostRunner(events)
    probe = _Probe(events)
    client = _client(runner, probe)

    results = client.find_skill("dbCreateRect", mode="exact", cache_dir=tmp_path / "finder")
    more = client.get_skill_more_info("dbCreateRect", cache_dir=tmp_path / "more")

    assert [r["name"] for r in results] == ["dbCreateRect"]
    assert more is not None and "Creates a rectangle." in more["plain_text"]
    # SSH discovery runs first; the daemon is asked once, then the finder is
    # found by the same walk-up as discover(), starting at the install root.
    assert events[:3] == ["ssh:which", "daemon:cdsGetInstPath", "ssh:walk"]
    assert probe.calls == 1
    walk = next(command for command in runner.commands if "doc/finder/SKILL" in command)
    assert "p=/cad/ic/lnx86;" in walk


def test_doc_roots_fall_back_only_after_ssh_discovery_finds_nothing(tmp_path: Path) -> None:
    events: list[str] = []
    probe = _Probe(events)
    client = _client(_DocsHostRunner(events), probe)

    info = client.doc_info()
    payload = client.search_docs("maeRunSimulation", cache_dir=tmp_path / "cache")

    assert [item["doc_root"] for item in info["doc_roots"]] == [DOC_ROOT]
    assert payload["doc_roots"] == [DOC_ROOT]
    assert payload["results"][0]["title"] == "maeRunSimulation"
    # Existing discovery order is unchanged: finder anchor, then environment,
    # and only then the running Virtuoso, whose doc root is checked remotely.
    assert events[:4] == ["ssh:which", "ssh:env", "daemon:cdsGetInstPath", "ssh:test-doc"]
    assert probe.calls == 1  # the answer is reused for the second command


def test_fallback_is_not_consulted_when_discovery_succeeds(tmp_path: Path) -> None:
    events: list[str] = []
    probe = _Probe(events)
    client = _client(_DocsHostRunner(events, virtuoso_on_path=True), probe)

    assert client.find_skill("dbCreateRect", mode="exact", cache_dir=tmp_path / "f")
    assert client.get_skill_more_info("dbCreateRect", cache_dir=tmp_path / "m") is not None
    assert client.doc_info()["doc_roots"][0]["doc_root"] == DOC_ROOT
    assert client.search_docs("maeRunSimulation", cache_dir=tmp_path / "c")["doc_roots"] == [DOC_ROOT]

    assert probe.calls == 0
    assert "daemon:cdsGetInstPath" not in events


def test_fallback_adds_only_roots_that_exist_on_the_remote_host(tmp_path: Path) -> None:
    events: list[str] = []
    probe = _Probe(events)
    client = _client(_DocsHostRunner(events, existing=()), probe)

    assert client.doc_info() == {"ok": True, "doc_roots": []}
    assert client.search_docs("maeRunSimulation", cache_dir=tmp_path / "c") == {"doc_roots": [], "results": []}
    assert client.find_skill("dbCreateRect", cache_dir=tmp_path / "f") == []
    with pytest.raises(SKILLFinderNotFoundError, match="no SKILL Finder data .* on eda-host"):
        client.find_skill("dbCreateRect", cache_dir=tmp_path / "f", missing_ok=False)
    with pytest.raises(SKILLFinderNotFoundError):
        client.get_skill_more_info("dbCreateRect", cache_dir=tmp_path / "m", missing_ok=False)


@pytest.mark.parametrize(
    "probe_kwargs",
    [
        {"result": None},
        {"exc": ConnectionRefusedError("no daemon")},
        {"exc": RuntimeError("Virtuoso daemon identity check failed: busy")},
    ],
)
def test_unreachable_daemon_means_no_fallback_and_no_exception(tmp_path: Path, probe_kwargs) -> None:
    events: list[str] = []
    probe = _Probe(events, **probe_kwargs)
    runner = _DocsHostRunner(events)
    client = _client(runner, probe)

    assert client.find_skill("dbCreateRect", cache_dir=tmp_path / "f") == []
    assert client.get_skill_more_info("dbCreateRect", cache_dir=tmp_path / "m") is None
    assert client.doc_info() == {"ok": True, "doc_roots": []}
    # Asked once per client; without an answer nothing else is looked up.
    assert probe.calls == 1
    assert "ssh:walk" not in events and "ssh:test-doc" not in events


def test_client_without_probe_or_token_never_contacts_the_daemon(tmp_path: Path, monkeypatch) -> None:
    def _daemon(*_args, **_kwargs):
        raise AssertionError("documentation lookup contacted the daemon")

    monkeypatch.setattr(VirtuosoClient, "execute_skill", _daemon)
    monkeypatch.setattr(VirtuosoClient, "_ensure_daemon_capabilities", _daemon)
    client = _client(_DocsHostRunner([]))

    assert client.find_skill("dbCreateRect", cache_dir=tmp_path / "f") == []
    assert client.doc_info() == {"ok": True, "doc_roots": []}


def test_connected_client_asks_its_own_daemon(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def _query(self, timeout=5.0):
        calls.append("query")
        return INSTALL_ROOT

    monkeypatch.setattr(VirtuosoClient, "_query_install_root", _query)
    client = VirtuosoClient(tunnel=_Tunnel(_DocsHostRunner([])), daemon_token="ab" * 32)

    assert [r["name"] for r in client.find_skill("dbCreateRect", cache_dir=tmp_path / "f")] == ["dbCreateRect"]
    assert calls == ["query"]


# -- the daemon query itself ----------------------------------------------------


def _daemon_client(monkeypatch, *, caps, output="\"/cad/ic/lnx86\"", status=ExecutionStatus.SUCCESS):
    sent: list[tuple[str, dict]] = []
    client = VirtuosoClient(daemon_token="ab" * 32)

    def _caps(deadline, *, refresh=False):
        if isinstance(caps, Exception):
            raise caps
        return caps

    def _execute(skill, timeout=None, **kwargs):
        sent.append((skill, kwargs))
        return VirtuosoResult(status=status, output=output)

    monkeypatch.setattr(client, "_ensure_daemon_capabilities", _caps)
    monkeypatch.setattr(client, "execute_skill", _execute)
    return client, sent


def test_query_install_root_reads_cds_get_inst_path(monkeypatch) -> None:
    client, sent = _daemon_client(monkeypatch, caps={"auth": "on", "lane": {"state": "idle"}})

    assert client._query_install_root() == INSTALL_ROOT
    assert sent == [("cdsGetInstPath()", {"retry_connect": False})]


@pytest.mark.parametrize("state", ["busy", "poisoned", "resyncing"])
def test_query_install_root_skips_a_busy_or_stuck_lane(monkeypatch, state) -> None:
    client, sent = _daemon_client(monkeypatch, caps={"auth": "on", "lane": {"state": state}})

    assert client._query_install_root() is None
    assert sent == []


def test_query_install_root_swallows_handshake_failures(monkeypatch) -> None:
    client, sent = _daemon_client(monkeypatch, caps=ConnectionRefusedError("refused"))

    assert client._query_install_root() is None
    assert sent == []


@pytest.mark.parametrize("output", ["nil", "\"cad/ic\"", "\"\"", "(\"/a\" \"/b\")"])
def test_query_install_root_rejects_unusable_answers(monkeypatch, output) -> None:
    client, _sent = _daemon_client(monkeypatch, caps={"auth": "on"}, output=output)

    assert client._query_install_root() is None


def test_query_install_root_ignores_skill_errors(monkeypatch) -> None:
    client, _sent = _daemon_client(monkeypatch, caps={"auth": "on"}, status=ExecutionStatus.ERROR)

    assert client._query_install_root() is None


# -- remote helpers --------------------------------------------------------------


class _RecordingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        return self.result


def test_install_root_finder_walk_matches_binary_walk() -> None:
    from_binary = _RecordingRunner(CommandResult(0, f"{FINDER_ROOT}\n", ""))
    from_install = _RecordingRunner(CommandResult(0, f"{FINDER_ROOT}\n", ""))

    SKILLFinder._walk_up_remote(from_binary, f"{INSTALL_ROOT}/tools/dfII/bin/virtuoso")
    found = SKILLFinder().discover_from_install_root(from_install, INSTALL_ROOT)

    assert found == Path(FINDER_ROOT)
    assert from_install.commands[0] == from_binary.commands[0].replace(
        f"{INSTALL_ROOT}/tools/dfII/bin/virtuoso", INSTALL_ROOT
    )


def test_doc_roots_from_install_root_checks_the_remote_directory() -> None:
    present = _RecordingRunner(CommandResult(0, "VB_DOC_ROOT_OK\n", ""))
    absent = _RecordingRunner(CommandResult(1, "", ""))

    assert doc_roots_from_install_root(present, "/cad/ic dir/") == ["/cad/ic dir/doc"]
    assert doc_roots_from_install_root(absent, INSTALL_ROOT) == []
    assert "'/cad/ic dir/doc'" in present.commands[0]


# -- CLI through the registered entrypoint function -------------------------------


class _FakeDaemonClient:
    def __init__(self, install_root: str | None) -> None:
        self.install_root = install_root
        self.closed = False

    def _query_install_root(self) -> str | None:
        return self.install_root

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cli_site(monkeypatch, tmp_path):
    """Wire the CLI to a fake docs host and a controllable bridge daemon."""
    events: list[str] = []
    state = {
        "runner": _DocsHostRunner(events),
        "tunnel_running": False,
        "daemon": _FakeDaemonClient(INSTALL_ROOT),
        "from_env_error": None,
        "is_running_profiles": [],
        "events": events,
    }

    monkeypatch.setenv("VB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VB_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)
    monkeypatch.setattr(SSHClient, "from_env", classmethod(lambda cls, **_kw: _Tunnel(state["runner"])))

    def _is_running(cls, profile=None):
        state["is_running_profiles"].append(profile)
        return state["tunnel_running"]

    def _from_env(cls, **_kw):
        events.append("daemon:connect")
        if state["from_env_error"] is not None:
            raise state["from_env_error"]
        return state["daemon"]

    monkeypatch.setattr(SSHClient, "is_running", classmethod(_is_running))
    monkeypatch.setattr(VirtuosoClient, "from_env", classmethod(_from_env))
    return state


def test_cli_skill_commands_explain_missing_finder_data(cli_site, capsys) -> None:
    assert main(["skill-find", "dbCreateRect"]) == 1
    out, err = capsys.readouterr()
    assert "No results" not in out
    assert "Error: no SKILL Finder data (doc/finder/SKILL) found on eda-host; cannot search for 'dbCreateRect'." in err
    assert "VB_CADENCE_CSHRC to a script that exports the Cadence environment" in err
    assert "VB_CADENCE_ENV_SHELL=sh" in err
    assert "--doc-root" not in err

    assert main(["skill-info", "dbCreateRect", "--json"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no SKILL Finder data" in err and "No More Info found" not in err


def test_cli_doc_commands_explain_missing_docs_and_local_doc_root(cli_site, capsys) -> None:
    for argv in (["doc-search", "maeRunSimulation"], ["doc-info"]):
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert "Error: no Cadence documentation found on eda-host." in err
        assert "VB_CADENCE_CSHRC to a script" in err
        assert "--doc-root takes directories on this machine" in err
        assert "Pass --doc-root" not in err


def test_cli_hint_names_the_profile_variables(cli_site, capsys) -> None:
    assert main(["skill-find", "dbCreateRect", "-p", "worker1"]) == 1
    err = capsys.readouterr().err
    assert "VB_CADENCE_CSHRC_worker1" in err and "VB_CADENCE_ENV_SHELL_worker1=sh" in err
    assert cli_site["is_running_profiles"] == ["worker1"]


def test_cli_keeps_no_results_when_finder_data_exists(cli_site, capsys) -> None:
    cli_site["runner"] = _DocsHostRunner([], virtuoso_on_path=True)

    assert main(["skill-find", "noSuchFunctionXyz"]) == 0
    assert capsys.readouterr().out.strip() == "No results for: noSuchFunctionXyz"

    assert main(["skill-info", "noSuchFunctionXyz"]) == 1
    assert capsys.readouterr().out.strip() == "No More Info found for: noSuchFunctionXyz"


def test_cli_uses_running_daemon_when_discovery_finds_nothing(cli_site, capsys) -> None:
    cli_site["tunnel_running"] = True

    assert main(["skill-find", "dbCreateRect", "--mode", "exact", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "dbCreateRect"
    assert cli_site["daemon"].closed is True

    assert main(["doc-search", "maeRunSimulation", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["doc_roots"] == [DOC_ROOT]
    assert payload["results"][0]["path"] == f"{DOC_ROOT}/maeSKILLref/maeRunSimulation.html"

    events = cli_site["events"]
    assert events.index("ssh:which") < events.index("daemon:connect")


@pytest.mark.parametrize("running, error", [(False, None), (True, ConnectionRefusedError("refused"))])
def test_cli_unreachable_daemon_gives_the_clear_error(cli_site, capsys, running, error) -> None:
    cli_site["tunnel_running"] = running
    cli_site["from_env_error"] = error

    assert main(["skill-find", "dbCreateRect"]) == 1
    err = capsys.readouterr().err
    assert "no SKILL Finder data" in err and "Traceback" not in err
    assert ("daemon:connect" in cli_site["events"]) is running


def test_cli_never_contacts_the_daemon_when_discovery_succeeds(cli_site, capsys) -> None:
    cli_site["runner"] = _DocsHostRunner(cli_site["events"], virtuoso_on_path=True)
    cli_site["tunnel_running"] = True

    assert main(["skill-find", "dbCreateRect", "--json"]) == 0
    assert main(["skill-info", "dbCreateRect", "--json"]) == 0
    assert main(["doc-info", "--json"]) == 0
    assert main(["doc-search", "maeRunSimulation", "--json"]) == 0
    capsys.readouterr()

    assert cli_site["is_running_profiles"] == []
    assert "daemon:connect" not in cli_site["events"]


def test_cli_missing_local_doc_root_says_it_is_local(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "remote-only" / "doc"
    for argv in (
        ["doc-search", "maeRunSimulation", "--doc-root", str(missing)],
        ["doc-info", "--doc-root", str(missing)],
    ):
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert "no --doc-root directory exists on this machine" in err
        assert "omit it to discover the docs on the remote host" in err


def test_cli_doc_root_help_says_local_directory(capsys) -> None:
    for command in ("doc-search", "doc-info"):
        with pytest.raises(SystemExit):
            main([command, "--help"])
        help_text = " ".join(capsys.readouterr().out.split())
        assert "Cadence doc directory on this machine" in help_text


def test_registered_entrypoint_reports_local_doc_root_error(tmp_path: Path) -> None:
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the virtuoso-bridge console script is not installed in this environment")
    if Path(DOC_ROOT).is_dir():
        pytest.skip("this machine has a real directory at the placeholder path")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("VB_") and key not in {"HOME", "USERPROFILE"}
    }
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        VB_LOG_DIR=str(tmp_path / "logs"),
        VB_STATE_DIR=str(tmp_path / "state"),
        VB_CACHE_DIR=str(tmp_path / "cache"),
    )

    result = subprocess.run(
        [script, "doc-search", "maeRunSimulation", "--doc-root", DOC_ROOT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, result.stderr
    assert "no --doc-root directory exists on this machine" in result.stderr
    assert "Traceback" not in result.stderr
