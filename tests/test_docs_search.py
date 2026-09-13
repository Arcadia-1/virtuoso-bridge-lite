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
    _remote_doc_info_script,
    doc_root_info_local,
    doc_root_info_remote,
    parse_tgf_line,
    parse_virtuoso_version,
    resolve_doc_roots,
    search_docs,
    to_remote_posix,
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

        def search_docs(
            self,
            query: str,
            *,
            limit: int = 10,
            rebuild_index: bool = False,
            cache_dir: str | Path | None = None,
        ):
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

    assert payload["doc_roots"] == [remote_root.as_posix()]
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

    assert payload["doc_roots"] == [remote_root.as_posix()]
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

        def search_docs(
            self,
            query: str,
            *,
            limit: int = 10,
            rebuild_index: bool = False,
            cache_dir: str | Path | None = None,
        ):
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


def _run_remote_script(
    script: str,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run a generated remote script through local bash."""
    script_path = tmp_path / "vb_remote_script.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


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

    result = _run_remote_script(_remote_doc_index_command(str(doc_root)), tmp_path)

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


def test_remote_doc_index_command_orders_records_deterministically(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    for relative_path, title in (
        ("zref/zeta.html", "Zeta"),
        ("aref/alpha.html", "Alpha"),
    ):
        html_path = doc_root / relative_path
        html_path.parent.mkdir(parents=True)
        html_path.write_text(
            f"<html><title>{title}</title><body>{title} docs.</body></html>",
            encoding="utf-8",
        )

    tgf_path = doc_root / "api_more_info" / "api_more_info.tgf"
    tgf_path.parent.mkdir()
    tgf_path.write_text(
        "alpha $aref/alpha.html NULL HTML\n",
        encoding="utf-8",
    )

    result = _run_remote_script(_remote_doc_index_command(str(doc_root)), tmp_path)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    records_path = Path(summary["path"])
    try:
        with gzip.open(records_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    finally:
        records_path.unlink(missing_ok=True)

    assert [record["relative_path"] for record in records] == [
        "aref/alpha.html",
        "zref/zeta.html",
        "api_more_info/api_more_info.tgf",
    ]


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
    result = _run_remote_script(_remote_doc_index_command(str(doc_root)), tmp_path, env=env)

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


# ---------------------------------------------------------------------------
# doc-info: version + structure facts
# ---------------------------------------------------------------------------


def test_parse_virtuoso_version_from_install_dir_name() -> None:
    assert parse_virtuoso_version("IC618") == "6.1.8"
    assert parse_virtuoso_version("IC614") == "6.1.4"
    assert parse_virtuoso_version("IC617") == "6.1.7"
    assert parse_virtuoso_version("IC231") == "23.1"
    assert parse_virtuoso_version("IC618", sdp_names=()) == "6.1.8"
    assert parse_virtuoso_version("") == ""
    assert parse_virtuoso_version("tools") == ""


def test_parse_virtuoso_version_prefers_sdp_names() -> None:
    # sdp names are unambiguous and win over the directory fallback.
    assert parse_virtuoso_version(
        "IC618", sdp_names=["Base_IC06.18.000_lnx86.sdp"]
    ) == "6.1.8"
    assert parse_virtuoso_version(
        "IC618", sdp_names=["Hotfix_IC06.18.130_lnx86.sdp"]
    ) == "6.1.8"
    assert parse_virtuoso_version(
        "IC231", sdp_names=["Hotfix_IC23.10.030_lnx86.sdp"]
    ) == "23.1"
    # No sdp match -> fall back to the directory name.
    assert parse_virtuoso_version(
        "IC231", sdp_names=["unrelated.sdp"]
    ) == "23.1"


def _make_fake_doc_root(tmp_path: Path, *, sdp: str | None = None, per_function: bool = False) -> Path:
    install_root = tmp_path / ("IC618" if sdp and "06.18" in sdp else "IC231")
    doc_root = install_root / "doc"
    (doc_root / "skdfref").mkdir(parents=True)
    (doc_root / "api_more_info").mkdir()
    (doc_root / "finder" / "SKILL" / "Core_SKILL").mkdir(parents=True)
    (doc_root / "DFII").mkdir()
    (doc_root / "Schematics").mkdir()
    (doc_root / "api_more_info" / "api_more_info.tgf").write_text(
        "dbOpenCellViewByType $skdfref/cvio.html dbOpenCellViewByType HTML\n",
        encoding="utf-8",
    )
    (doc_root / "finder" / "SKILL" / "Core_SKILL" / "sklangref.fnd").write_text(
        "name\tsyntax\tdescription\n", encoding="utf-8"
    )
    (doc_root / "finder" / "SKILL" / "Core_SKILL" / "skoopref.fnd").write_text(
        "name\tsyntax\tdescription\n", encoding="utf-8"
    )
    if per_function:
        for i in range(150):
            (doc_root / "skdfref" / f"cvio_re_fn{i}.html").write_text("<html></html>\n")
    else:
        (doc_root / "skdfref" / "cvio.html").write_text("<html></html>\n")
        (doc_root / "skdfref" / "chap1.html").write_text("<html></html>\n")
    if sdp:
        (install_root / sdp).write_text("", encoding="utf-8")
    return doc_root


def test_doc_root_info_local_reports_version_and_structure(tmp_path: Path) -> None:
    doc_root = _make_fake_doc_root(tmp_path, sdp="Base_IC06.18.000_lnx86.sdp")

    infos = doc_root_info_local([doc_root])

    assert len(infos) == 1
    info = infos[0]
    assert info["doc_root"] == doc_root.resolve().as_posix()
    assert info["install_root"] == doc_root.resolve().parent.as_posix()
    assert info["virtuoso_version"] == "6.1.8"
    assert info["version_source"] == "sdp"
    assert info["doc_set_count"] >= 3
    assert "DFII" in info["doc_sets_sample"]
    assert info["skill_finder"]["found"] is True
    assert info["skill_finder"]["fnd_count"] == 2
    assert info["api_more_info"]["found"] is True
    assert info["api_more_info"]["tgf_bytes"] > 0
    assert info["skdfref"]["style"] == "chapter"
    assert info["skdfref"]["html_count"] == 2


def test_doc_root_info_local_detects_per_function_skdfref(tmp_path: Path) -> None:
    doc_root = _make_fake_doc_root(tmp_path, per_function=True)

    info = doc_root_info_local([doc_root])[0]

    # No .sdp files -> version falls back to the install directory name (IC231).
    assert info["virtuoso_version"] == "23.1"
    assert info["version_source"] == "install_dir"
    assert info["skdfref"]["style"] == "per-function"
    assert info["skdfref"]["html_count"] == 150


def test_doc_root_info_local_skips_missing_roots(tmp_path: Path) -> None:
    assert doc_root_info_local([tmp_path / "nope"]) == []


class _RemoteDocInfoRunner:
    host = "eda-host"

    def __init__(self, info_payload: list[dict] | None, fail: bool = False) -> None:
        self.info_payload = info_payload
        self.fail = fail
        self.commands: list[str] = []

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        if "which virtuoso" in command:
            return CommandResult(0, "/cad/ic/bin/virtuoso\n", "")
        if "doc/finder/SKILL" in command:
            return CommandResult(0, "/cad/ic/doc/finder/SKILL\n", "")
        if "vb_doc_info" in command:
            if self.fail:
                return CommandResult(1, "", "remote doc-info failed")
            return CommandResult(0, json.dumps(self.info_payload or []) + "\n", "")
        return CommandResult(1, "", "unexpected command")


def test_doc_root_info_remote_parses_json_payload() -> None:
    payload = [
        {
            "doc_root": "/opt/cadence/IC231/doc",
            "install_root": "/opt/cadence/IC231",
            "virtuoso_version": "23.1",
            "version_source": "sdp",
            "doc_set_count": 252,
            "doc_sets_sample": ["DFII", "Schematics"],
            "skill_finder": {"path": "/opt/cadence/IC231/doc/finder/SKILL", "found": True, "fnd_count": 39},
            "api_more_info": {"tgf": "/opt/cadence/IC231/doc/api_more_info/api_more_info.tgf", "found": True, "tgf_bytes": 986632},
            "skdfref": {"path": "/opt/cadence/IC231/doc/skdfref", "found": True, "html_count": 1925, "style": "per-function", "sample": []},
        }
    ]
    runner = _RemoteDocInfoRunner(payload)

    infos = doc_root_info_remote(runner, ["/opt/cadence/IC231/doc"])

    assert infos == payload
    assert any("vb_doc_info" in command for command in runner.commands)


def test_doc_root_info_remote_empty_roots_and_failure() -> None:
    assert doc_root_info_remote(_RemoteDocInfoRunner([]), []) == []

    failing = _RemoteDocInfoRunner(None, fail=True)
    try:
        doc_root_info_remote(failing, ["/cad/ic/doc"])
    except RuntimeError as exc:
        assert "remote doc-info failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_remote_doc_info_script_selects_python_candidates(tmp_path: Path) -> None:
    script = _remote_doc_info_script(["/opt/cadence/IC231/doc", "/opt/cadence/IC618/doc"])

    assert "vb_doc_info" in script
    assert "/opt/cadence/IC231/tools.lnx86/python/64bit/bin/python3" in script
    assert "/opt/cadence/IC618/tools.lnx86/python/64bit/bin/python3" in script
    assert "tools.lnx86/python/64bit/bin/python3" in script


def test_doc_info_cli_local_json_output(tmp_path: Path, capsys) -> None:
    doc_root = _make_fake_doc_root(tmp_path, sdp="Base_IC06.18.000_lnx86.sdp")

    rc = main(["doc-info", "--doc-root", str(doc_root), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["doc_roots"][0]["doc_root"] == doc_root.resolve().as_posix()
    assert payload["doc_roots"][0]["virtuoso_version"] == "6.1.8"
    assert payload["doc_roots"][0]["skdfref"]["style"] == "chapter"


def test_client_doc_info_uses_remote_runner(tmp_path: Path) -> None:
    remote_root = Path("/opt/cadence/IC231/doc")
    runner = _RemoteDocInfoRunner(
        [
            {
                "doc_root": remote_root.as_posix(),
                "virtuoso_version": "23.1",
                "version_source": "sdp",
                "doc_set_count": 252,
            }
        ]
    )
    client = VirtuosoClient(tunnel=_RemoteDocsTunnel(runner))

    payload = client.doc_info()

    assert payload["ok"] is True
    assert payload["doc_roots"][0]["doc_root"] == remote_root.as_posix()
    assert payload["doc_roots"][0]["virtuoso_version"] == "23.1"


def test_to_remote_posix_normalizes_locally_constructed_paths() -> None:
    # On a Windows client, Path("/opt/...") stringifies with backslashes;
    # remote path handling requires POSIX separators.
    finder_root = Path("/opt/cadence/IC231/doc/finder/SKILL")
    assert to_remote_posix(finder_root.parent.parent) == "/opt/cadence/IC231/doc"
    assert to_remote_posix("/opt/cadence/IC618/doc") == "/opt/cadence/IC618/doc"
    assert to_remote_posix(Path("/opt/cadence/IC618/doc")) == "/opt/cadence/IC618/doc"


def test_remote_doc_info_script_embeds_roots_without_argv() -> None:
    script = _remote_doc_info_script(["/opt/cadence/IC231/doc"])

    # Roots must be embedded in the Python body: some remote shell/ssh
    # chains mangle double quotes in command arguments, which would break
    # JSON parsing of an argv-passed root list.
    assert 'ROOTS = ["/opt/cadence/IC231/doc"]' in script
    assert "sys.argv" not in script


def test_discover_remote_doc_roots_yields_posix_root_on_any_client(monkeypatch) -> None:
    # The SKILL Finder anchor comes back as a locally-constructed Path;
    # the derived doc root must stay POSIX even when the client is Windows.
    import virtuoso_bridge.virtuoso.skill_finder as sf
    from virtuoso_bridge.virtuoso.docs_search import discover_remote_doc_roots

    class _FinderStub:
        def discover(self, remote_runner=None, profile=None):
            return Path("/opt/cadence/IC231/doc/finder/SKILL")

    monkeypatch.setattr(sf, "SKILLFinder", _FinderStub)
    runner = _RemoteDocsRunner(Path("/opt/cadence/IC231/doc"), Path("/tmp/downloads"))

    roots = discover_remote_doc_roots(runner)

    assert roots == ["/opt/cadence/IC231/doc"]
    for root in roots:
        assert "\\" not in root


# ---------------------------------------------------------------------------
# Full-text indexing: no 64 KiB preview truncation
# ---------------------------------------------------------------------------


def _write_chapter_style_document(doc_root: Path, *, filler_paragraphs: int = 4000) -> Path:
    """IC618-style chapter page: one big HTML file holding many topics.

    The needle topic sits after more than 64 KiB of preceding content, the
    point where the old preview-truncated indexer stopped reading.
    """
    chapter = doc_root / "skdfref" / "cvio.html"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    filler = "".join(
        f"<p>Cellview chapter filler {index}: routine reference text.</p>\n"
        for index in range(filler_paragraphs)
    )
    chapter.write_text(
        "<html><head><title>Cellview Database Functions</title></head><body>\n"
        + filler
        + "<h4>dbZebraOpenCellView</h4>\n"
        "<p>Opens a zebra-striped cellview for offline debugging.</p>\n"
        "</body></html>",
        encoding="utf-8",
    )
    return chapter


def test_indexed_search_finds_match_after_64kib(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    chapter = _write_chapter_style_document(doc_root)
    assert chapter.stat().st_size > 128 * 1024

    payload = VirtuosoClient.local().search_docs(
        "dbZebraOpenCellView",
        doc_roots=[doc_root],
        cache_dir=tmp_path / "cache",
    )

    assert payload["results"], "match after 64 KiB must be indexed"
    result = payload["results"][0]
    assert result["relative_path"] == "skdfref/cvio.html"
    assert "zebra-striped" in result["snippet"]


def test_remote_doc_index_command_indexes_beyond_64kib(tmp_path: Path) -> None:
    doc_root = tmp_path / "doc"
    _write_chapter_style_document(doc_root)

    result = _run_remote_script(_remote_doc_index_command(str(doc_root)), tmp_path)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    records_path = Path(summary["path"])
    try:
        with gzip.open(records_path, "rt", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    finally:
        records_path.unlink(missing_ok=True)

    assert summary["documents"] == 1
    text = records[0]["text"]
    assert len(text) > 64 * 1024, "remote index must not truncate document text"
    assert "dbZebraOpenCellView" in text
    assert "zebra-striped" in text


def test_search_rebuilds_index_from_older_schema_version(tmp_path: Path) -> None:
    from virtuoso_bridge.virtuoso.docs_search import SCHEMA_VERSION, _index_dir_for_root

    doc_root = tmp_path / "doc"
    doc_root.mkdir()
    (doc_root / "guide.html").write_text(
        "<html><title>Net Expression Guide</title><body>Use net expression labels.</body></html>",
        encoding="utf-8",
    )
    cache_root = tmp_path / "cache"
    index_dir = _index_dir_for_root(cache_root, doc_root.resolve())
    index_dir.mkdir(parents=True)
    # A pre-existing index at the previous schema version (truncated text)
    # plus a placeholder db must be rejected and rebuilt.
    (index_dir / "index.sqlite").write_bytes(b"")
    (index_dir / "manifest.json").write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION - 1, "doc_root": doc_root.resolve().as_posix()}
        ),
        encoding="utf-8",
    )

    results = search_docs("net expression", [doc_root], cache_root=cache_root)

    assert results and results[0]["relative_path"] == "guide.html"
    manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# doc-info --json schema parity between local and remote modes
# ---------------------------------------------------------------------------


def test_remote_doc_info_script_payload_keys_match_local(tmp_path: Path) -> None:
    doc_root = _make_fake_doc_root(tmp_path, sdp="Base_IC06.18.000_lnx86.sdp")
    local_info = doc_root_info_local([doc_root])[0]

    good_bin = tmp_path / "bin"
    good_bin.mkdir()
    good_python = good_bin / "python3"
    good_python.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    good_python.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{good_bin}{os.pathsep}{env.get('PATH', '')}"

    result = _run_remote_script(
        _remote_doc_info_script([doc_root.resolve().as_posix()]), tmp_path, env=env
    )

    assert result.returncode == 0, result.stderr
    remote_info = json.loads(result.stdout.strip().splitlines()[-1])[0]

    assert list(remote_info.keys()) == list(local_info.keys())
    assert remote_info["virtuoso_version"] == local_info["virtuoso_version"]
    assert remote_info["version_source"] == local_info["version_source"]
    assert remote_info["skdfref"]["style"] == local_info["skdfref"]["style"]
    for section in ("skill_finder", "api_more_info", "skdfref"):
        assert list(remote_info[section].keys()) == list(local_info[section].keys())


def test_client_doc_info_includes_ok_key_in_every_mode(tmp_path: Path, monkeypatch) -> None:
    doc_root = _make_fake_doc_root(tmp_path, sdp="Base_IC06.18.000_lnx86.sdp")

    # Explicit doc roots
    explicit = VirtuosoClient.local().doc_info(doc_roots=[doc_root])
    assert explicit["ok"] is True
    assert explicit["doc_roots"]

    # Remote runner
    remote = VirtuosoClient(
        tunnel=_RemoteDocsTunnel(
            _RemoteDocInfoRunner(
                [
                    {
                        "doc_root": "/opt/cadence/IC231/doc",
                        "virtuoso_version": "23.1",
                        "version_source": "sdp",
                        "doc_set_count": 252,
                    }
                ]
            )
        )
    ).doc_info()
    assert remote["ok"] is True
    assert remote["doc_roots"]

    # Local fallback without configured docs
    monkeypatch.delenv("CADENCE_DOC_ROOT", raising=False)
    monkeypatch.delenv("CADENCE_DOC_ROOTS", raising=False)
    monkeypatch.delenv("CDS_INST_DIR", raising=False)
    monkeypatch.delenv("CDSHOME", raising=False)
    monkeypatch.delenv("CDS_HOME", raising=False)
    fallback = VirtuosoClient.local().doc_info()
    assert fallback["ok"] is True
    assert fallback["doc_roots"] == []

    assert list(explicit.keys()) == list(remote.keys()) == list(fallback.keys())


def test_doc_info_cli_bridge_mode_json_schema_matches_local(tmp_path: Path, capsys, monkeypatch) -> None:
    doc_root = _make_fake_doc_root(tmp_path, sdp="Base_IC06.18.000_lnx86.sdp")
    local_infos = doc_root_info_local([doc_root])

    class _FakeDocsClient:
        @classmethod
        def from_env(cls, profile=None):
            return cls()

        def doc_info(self):
            return {"ok": True, "doc_roots": local_infos}

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeDocsClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)

    rc = main(["doc-info", "--json"])
    assert rc == 0
    bridge_payload = json.loads(capsys.readouterr().out)

    rc = main(["doc-info", "--doc-root", str(doc_root), "--json"])
    assert rc == 0
    local_payload = json.loads(capsys.readouterr().out)

    assert list(bridge_payload.keys()) == list(local_payload.keys()) == ["ok", "doc_roots"]
    assert bridge_payload["ok"] is local_payload["ok"] is True
    assert list(bridge_payload["doc_roots"][0].keys()) == list(local_payload["doc_roots"][0].keys())
