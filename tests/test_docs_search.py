from __future__ import annotations

import gzip
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import virtuoso_bridge
from virtuoso_bridge.cli import main
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.docs_search import (
    _remote_doc_index_command,
    parse_tgf_line,
    resolve_doc_roots,
    search_docs,
)
from virtuoso_bridge.virtuoso import docs_search as docs_search_module


def test_resolve_doc_roots_uses_explicit_paths_before_environment(tmp_path: Path) -> None:
    explicit_root = tmp_path / "explicit-doc"
    explicit_root.mkdir()
    env_root = tmp_path / "env-doc"
    env_root.mkdir()

    roots = resolve_doc_roots([explicit_root], env={"CADENCE_DOC_ROOT": str(env_root)})

    assert roots == [explicit_root.resolve()]


def test_resolve_doc_roots_supports_doc_and_install_root_environment(tmp_path: Path) -> None:
    direct_root = tmp_path / "direct-doc"
    direct_root.mkdir()
    install_root = tmp_path / "IC"
    install_doc = install_root / "doc"
    install_doc.mkdir(parents=True)
    missing = tmp_path / "missing"

    roots = resolve_doc_roots(
        env={
            "CADENCE_DOC_ROOTS": os.pathsep.join([str(direct_root), str(missing)]),
            "CDS_INST_DIR": str(install_root),
        }
    )

    assert roots == [direct_root.resolve(), install_doc.resolve()]


def test_parse_tgf_line_resolves_cadence_doc_variables(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    tgf_path = doc_root / "api_more_info" / "api_more_info.tgf"
    tgf_path.parent.mkdir(parents=True)

    entry = parse_tgf_line(
        "schCreateNetExpression $schematic/schCreateNetExpression.html schCreateNetExpression HTML",
        tgf_path=tgf_path,
        doc_root=doc_root,
        line_no=3,
    )

    assert entry is not None
    assert entry.topic_id == "schCreateNetExpression"
    assert entry.target_path == doc_root / "schematic" / "schCreateNetExpression.html"
    assert entry.anchor == "schCreateNetExpression"
    assert entry.line == 3


def test_search_docs_finds_html_content_and_tgf_topics(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "schematic" / "schCreateNetExpression.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><head><title>Net Expression</title><script>ignore this</script></head>"
        "<body><h1>Net Expression</h1><p>Create an inherited net expression label.</p></body></html>",
        encoding="utf-8",
    )
    tgf_path = doc_root / "api_more_info" / "api_more_info.tgf"
    tgf_path.parent.mkdir()
    tgf_path.write_text(
        "netExpression $schematic/schCreateNetExpression.html netExpression HTML\n",
        encoding="utf-8",
    )

    results = search_docs("net expression", [doc_root], limit=5)

    kinds = {result["kind"] for result in results}
    assert {"document", "topic"} <= kinds
    document = next(result for result in results if result["kind"] == "document")
    assert document["relative_path"] == "schematic/schCreateNetExpression.html"
    assert "inherited net expression" in document["snippet"]
    topic = next(result for result in results if result["kind"] == "topic")
    assert topic["target_relative_path"] == "schematic/schCreateNetExpression.html"


def test_doc_search_cli_outputs_json_without_virtuoso_connection(tmp_path: Path, capsys, monkeypatch) -> None:
    doc_root = tmp_path / "doc"
    doc_root.mkdir()
    (doc_root / "guide.html").write_text(
        "<html><title>Net Expression Guide</title><body>Use net expression labels.</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setenv("VB_CACHE_DIR", str(tmp_path / "cache"))

    rc = main(["doc-search", "net expression", "--doc-root", str(doc_root), "--json", "--limit", "1"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "net expression"
    assert payload["doc_roots"] == [str(doc_root.resolve())]
    assert payload["results"][0]["relative_path"] == "guide.html"


def test_doc_search_cli_uses_bridge_when_no_doc_root(capsys, monkeypatch) -> None:
    class _FakeDocsClient:
        @classmethod
        def from_env(cls, profile=None):
            seen_profiles.append(profile)
            return cls()

        def search_docs(self, query: str, *, limit: int = 10, rebuild_index: bool = False):
            seen_calls.append((query, limit, rebuild_index))
            return {
                "doc_roots": ["/cad/ic/doc"],
                "results": [
                    {
                        "kind": "document",
                        "path": "/cad/ic/doc/guide.html",
                        "relative_path": "guide.html",
                        "title": "Net Expression Guide",
                        "line": 1,
                        "snippet": "Use net expression labels.",
                    }
                ],
            }

    seen_profiles: list[str | None] = []
    seen_calls: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeDocsClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)

    rc = main(["doc-search", "net expression", "-p", "worker1", "--json", "--limit", "3"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert seen_calls == [("net expression", 3, False)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["doc_roots"] == ["/cad/ic/doc"]
    assert payload["results"][0]["path"] == "/cad/ic/doc/guide.html"


class _RemoteDocsRunner:
    host = "eda-host"

    def __init__(self, remote_root: Path, downloads: Path) -> None:
        self.remote_root = remote_root
        self.downloads = downloads
        self.commands: list[str] = []
        self.download_calls: list[tuple[str, Path, bool]] = []
        self.remote_records_path = "/tmp/vb_doc_index_records.jsonl.gz"

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        if "which virtuoso" in command:
            return CommandResult(0, "/cad/ic/bin/virtuoso\n", "")
        if "doc/finder/SKILL" in command:
            return CommandResult(0, f"{self.remote_root}/finder/SKILL\n", "")
        if "vb_doc_index" in command:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "path": self.remote_records_path,
                        "documents": 1,
                        "topics": 1,
                    }
                )
                + "\n",
                "",
            )
        if "vb_doc_search" in command:
            return CommandResult(
                0,
                f"{self.remote_root}\t{self.remote_root}/schematic/guide.html\n",
                "",
            )
        if command.startswith("rm -f "):
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unexpected command")

    def download(
        self,
        remote_path: str,
        local_path: Path,
        recursive: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        self.download_calls.append((remote_path, local_path, recursive))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if remote_path == self.remote_records_path:
            records = [
                {
                    "kind": "document",
                    "path": f"{self.remote_root}/schematic/guide.html",
                    "relative_path": "schematic/guide.html",
                    "suffix": ".html",
                    "title": "Net Expression Guide",
                    "text": "Create an inherited net expression label.",
                },
                {
                    "kind": "topic",
                    "path": f"{self.remote_root}/api_more_info/api_more_info.tgf",
                    "relative_path": "api_more_info/api_more_info.tgf",
                    "line": 7,
                    "topic_id": "netExpression",
                    "anchor": "netExpression",
                    "target_path": f"{self.remote_root}/schematic/guide.html",
                    "target_relative_path": "schematic/guide.html",
                    "title": "netExpression",
                    "text": "netExpression netExpression schematic/guide.html",
                },
            ]
            with gzip.open(local_path, "wt", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            return CommandResult(0, "", "")
        source = self.downloads / Path(remote_path).relative_to(self.remote_root)
        local_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return CommandResult(0, "", "")


class _RemoteDocsRunnerIndexFails(_RemoteDocsRunner):
    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        if "vb_doc_index" in command:
            self.commands.append(command)
            return CommandResult(1, "", "index failed")
        return super().run_command(command, timeout=timeout)


class _RemoteDocsTunnel:
    _remote_host = "eda-host"
    remote_host = "eda-host"

    def __init__(self, runner: _RemoteDocsRunner) -> None:
        self._ssh_runner = runner


def test_client_search_docs_builds_remote_index_from_metadata(tmp_path: Path) -> None:
    remote_root = Path("/cad/ic/doc")
    downloaded_tree = tmp_path / "remote-docs"
    html_path = downloaded_tree / "schematic" / "guide.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>Net Expression Guide</title>"
        "<body>Create an inherited net expression label.</body></html>",
        encoding="utf-8",
    )
    runner = _RemoteDocsRunner(remote_root, downloaded_tree)
    client = VirtuosoClient(tunnel=_RemoteDocsTunnel(runner))

    payload = client.search_docs("net expression", limit=2, cache_dir=tmp_path / "cache")

    assert payload["doc_roots"] == [str(remote_root)]
    assert payload["results"][0]["path"] == f"{remote_root}/schematic/guide.html"
    assert payload["results"][0]["relative_path"] == "schematic/guide.html"
    assert "inherited net expression" in payload["results"][0]["snippet"]
    assert len(runner.download_calls) == 1
    remote_path, local_path, recursive = runner.download_calls[0]
    assert remote_path == runner.remote_records_path
    assert local_path.name == "remote_records.jsonl.gz"
    assert local_path.parent.parent == tmp_path / "cache" / "eda-host"
    assert recursive is False
    assert list((tmp_path / "cache" / "eda-host").rglob("index.sqlite"))

    runner.download_calls.clear()
    second = client.search_docs("net expression", limit=2, cache_dir=tmp_path / "cache")
    assert second["results"][0]["relative_path"] == "schematic/guide.html"
    assert runner.download_calls == []
    assert any(command.startswith("rm -f ") for command in runner.commands)


def test_client_search_docs_falls_back_to_candidate_download_when_remote_index_fails(tmp_path: Path) -> None:
    remote_root = Path("/cad/ic/doc")
    downloaded_tree = tmp_path / "remote-docs"
    html_path = downloaded_tree / "schematic" / "guide.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>Net Expression Guide</title>"
        "<body>Create an inherited net expression label.</body></html>",
        encoding="utf-8",
    )
    runner = _RemoteDocsRunnerIndexFails(remote_root, downloaded_tree)
    client = VirtuosoClient(tunnel=_RemoteDocsTunnel(runner))

    payload = client.search_docs("net expression", limit=2, cache_dir=tmp_path / "cache")

    assert payload["doc_roots"] == [str(remote_root)]
    assert payload["results"][0]["path"] == f"{remote_root}/schematic/guide.html"
    assert payload["results"][0]["relative_path"] == "schematic/guide.html"
    assert "inherited net expression" in payload["results"][0]["snippet"]
    assert any("vb_doc_index" in command for command in runner.commands)
    assert any("vb_doc_search" in command for command in runner.commands)
    assert runner.download_calls == [
        (
            f"{remote_root}/schematic/guide.html",
            tmp_path / "cache" / "eda-host" / "cad_ic_doc" / "schematic" / "guide.html",
            False,
        )
    ]
    assert not list((tmp_path / "cache" / "eda-host").rglob("remote_records.jsonl.gz"))


def test_client_search_docs_builds_and_reuses_local_index(tmp_path: Path, monkeypatch) -> None:
    doc_root = tmp_path / "doc"
    doc_root.mkdir()
    (doc_root / "guide.html").write_text(
        "<html><title>Net Expression Guide</title><body>Use net expression labels.</body></html>",
        encoding="utf-8",
    )
    client = VirtuosoClient.local()
    cache_dir = tmp_path / "cache"

    first = client.search_docs("net expression", doc_roots=[doc_root], cache_dir=cache_dir)

    assert first["results"][0]["relative_path"] == "guide.html"
    assert list(cache_dir.rglob("index.sqlite"))

    def fail_iter_doc_files(_roots):
        raise AssertionError("cached search should not rescan doc files")

    monkeypatch.setattr(docs_search_module, "iter_doc_files", fail_iter_doc_files)

    second = client.search_docs("net expression", doc_roots=[doc_root], cache_dir=cache_dir)

    assert second["results"][0]["relative_path"] == "guide.html"


def test_doc_search_cli_passes_rebuild_index(capsys, monkeypatch) -> None:
    class _FakeDocsClient:
        @classmethod
        def from_env(cls, profile=None):
            return cls()

        def search_docs(self, query: str, *, limit: int = 10, rebuild_index: bool = False):
            seen_calls.append((query, limit, rebuild_index))
            return {"doc_roots": ["/cad/ic/doc"], "results": []}

    seen_calls: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeDocsClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)

    rc = main(["doc-search", "net expression", "--rebuild-index", "--json"])

    assert rc == 0
    assert seen_calls == [("net expression", 10, True)]
    assert json.loads(capsys.readouterr().out)["doc_roots"] == ["/cad/ic/doc"]


def test_client_search_docs_ranks_identifier_like_title_for_concept_query(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    api_path = doc_root / "skcompref" / "schCreateNetExpression.html"
    faq_path = doc_root / "faq" / "How_to_use_regular_expressions_for_netlisting.html"
    api_path.parent.mkdir(parents=True)
    faq_path.parent.mkdir(parents=True)
    faq_path.write_text(
        "<html><title>How to use regular expressions for netlisting</title>"
        "<body>These examples discuss netlisting properties and expression evaluation.</body></html>",
        encoding="utf-8",
    )
    api_path.write_text(
        "<html><title>schCreateNetExpression</title>"
        "<body>Creates an inherited connection and the corresponding net expression label.</body></html>",
        encoding="utf-8",
    )

    payload = VirtuosoClient.local().search_docs(
        "net expression",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    assert payload["results"][0]["relative_path"] == "skcompref/schCreateNetExpression.html"


def test_client_search_docs_ignores_common_question_words(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "skdfref" / "Inherited_Connections_Functions.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>Inherited Connections Functions</title>"
        "<body>Inherited connections are used for connectivity across hierarchy.</body></html>",
        encoding="utf-8",
    )

    payload = VirtuosoClient.local().search_docs(
        "what is inherited connection",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    assert payload["results"][0]["relative_path"] == "skdfref/Inherited_Connections_Functions.html"


def test_client_search_docs_keeps_all_terms_when_query_is_only_stopwords(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "guide.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>Guide</title><body>This page explains how to run a check.</body></html>",
        encoding="utf-8",
    )

    payload = VirtuosoClient.local().search_docs(
        "how to",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    assert payload["results"][0]["relative_path"] == "guide.html"


def test_client_search_docs_matches_simple_plural_query_words(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "vivaxlskill" / "awvCloseWindow.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>awvCloseWindow</title>"
        "<body>Closes a waveform window from SKILL.</body></html>",
        encoding="utf-8",
    )

    payload = VirtuosoClient.local().search_docs(
        "Close All Waveform Windows",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    assert payload["results"][0]["relative_path"] == "vivaxlskill/awvCloseWindow.html"


def test_client_search_docs_deduplicates_topic_and_document_hits(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "skdfref" / "dbOpenCellViewByType.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>dbOpenCellViewByType</title><body>Open a cellview by type.</body></html>",
        encoding="utf-8",
    )
    tgf_path = doc_root / "api_more_info" / "api_more_info.tgf"
    tgf_path.parent.mkdir()
    tgf_path.write_text(
        "dbOpenCellViewByType $skdfref/dbOpenCellViewByType.html NULL HTML\n",
        encoding="utf-8",
    )

    payload = VirtuosoClient.local().search_docs(
        "dbOpenCellViewByType",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    locations = [
        result.get("target_relative_path") or result.get("relative_path")
        for result in payload["results"]
    ]
    assert locations == ["skdfref/dbOpenCellViewByType.html"]


def test_remote_doc_index_command_extracts_records(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    html_path = doc_root / "skdfref" / "dbOpenCellViewByType.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>dbOpenCellViewByType</title><body>Open a cellview by type.</body></html>",
        encoding="utf-8",
    )
    tgf_path = doc_root / "api_more_info" / "api_more_info.tgf"
    tgf_path.parent.mkdir()
    tgf_path.write_text(
        "dbOpenCellViewByType $skdfref/dbOpenCellViewByType.html NULL HTML\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", "-lc", _remote_doc_index_command(str(doc_root))],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    records_path = Path(summary["path"])
    try:
        with gzip.open(records_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    finally:
        records_path.unlink(missing_ok=True)

    assert summary["documents"] == 1
    assert summary["topics"] == 1
    assert {record["kind"] for record in records} == {"document", "topic"}
    assert records[0]["relative_path"] == "skdfref/dbOpenCellViewByType.html"


def test_remote_doc_index_command_skips_broken_cadence_python(tmp_path: Path) -> None:
    install_root = tmp_path / "IC618"
    doc_root = install_root / "doc"
    html_path = doc_root / "skdfref" / "dbOpenCellViewByType.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text(
        "<html><title>dbOpenCellViewByType</title><body>Open a cellview by type.</body></html>",
        encoding="utf-8",
    )

    broken_python = install_root / "tools.lnx86" / "python" / "64bit" / "bin" / "python3"
    broken_python.parent.mkdir(parents=True)
    broken_python.write_text("#!/bin/sh\necho broken cadence python >&2\nexit 127\n", encoding="utf-8")
    broken_python.chmod(0o755)

    good_bin = tmp_path / "bin"
    good_bin.mkdir()
    good_python = good_bin / "python3"
    good_python.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    good_python.chmod(0o755)

    env = os.environ.copy()
    env["CDSHOME"] = str(install_root)
    env["PATH"] = f"{good_bin}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-lc", _remote_doc_index_command(str(doc_root))],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "broken cadence python" not in result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    records_path = Path(summary["path"])
    try:
        with gzip.open(records_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    finally:
        records_path.unlink(missing_ok=True)

    assert summary["documents"] == 1
    assert records[0]["relative_path"] == "skdfref/dbOpenCellViewByType.html"
