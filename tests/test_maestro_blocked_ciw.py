"""A CIW held by a modal dialog: seen live on IC23.1 (#297, #298).

* run_and_wait waited silently for 25 minutes on an ASSEMBLER-1610 question
  because blockers were only checked with the opt-in dialog guard.
* read_results queued an export behind an ASSEMBLER-2225 disk-error dialog;
  it ran once the dialog closed and crashed Virtuoso.
* Removing history entries while a run was in progress broke the session,
  and closing its window then crashed Virtuoso.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError, DialogInspection
from virtuoso_bridge.virtuoso.maestro import writer
from virtuoso_bridge.virtuoso.maestro.history import MaestroHistoryError, delete_history
from virtuoso_bridge.virtuoso.maestro.reader.runs import read_results


def _report(status: str, *titles: str) -> DialogInspection:
    return DialogInspection(status=status, target={"pid": 4242},
                            dialogs=[{"title": t} for t in titles])


class _Dialogs:
    enabled = False

    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    def probe(self, timeout):
        self.calls += 1
        return self.reports.pop(0) if self.reports else _report("clear")


class _Client:
    ssh_runner = None

    def __init__(self, outputs=(), dialogs=None, errors=None, metadata=None):
        self.outputs = list(outputs)
        self.errors = errors or {}
        self.metadata = metadata or {}
        self.calls: list[str] = []
        self.call_kwargs: list[dict] = []
        if dialogs is not None:
            self.dialogs = dialogs

    def execute_skill(self, code, timeout=None, **kwargs):
        self.calls.append(code)
        self.call_kwargs.append({"timeout": timeout, **kwargs})
        n = len(self.calls)
        if n in self.errors:
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[self.errors[n]],
                metadata=dict(self.metadata.get(n, {})),
            )
        out = self.outputs.pop(0) if self.outputs else "t"
        return VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=out,
            metadata=dict(self.metadata.get(n, {})),
        )


@pytest.fixture
def fast_probe(monkeypatch):
    monkeypatch.setattr(writer, "_UNGUARDED_PROBE_INTERVAL", 0.0)
    monkeypatch.setattr(writer.time, "sleep", lambda s: None)


def test_wait_reports_a_dialog_seen_twice_without_the_guard(fast_probe, tmp_path):
    question = "ADE Assembler Message 1610"
    client = _Client(dialogs=_Dialogs([_report("blocked", question)] * 2))
    with pytest.raises(DialogBlockedError) as caught:
        writer._wait_until_done(client, str(tmp_path / "never"), timeout=60)
    assert question in str(caught.value)
    assert caught.value.outcome == "unknown"
    assert caught.value.result.metadata["phase"] == "completion_wait"
    assert client.calls == []          # read-only: no SKILL was sent


def test_wait_ignores_a_dialog_seen_once(fast_probe, tmp_path):
    marker = tmp_path / "marker"

    class Dialogs(_Dialogs):
        def probe(self, timeout):
            report = super().probe(timeout)
            if self.calls == 3:
                marker.write_text("done\n")
            return report

    client = _Client(dialogs=Dialogs([_report("blocked", "Loading"), _report("clear")]))
    assert writer._wait_until_done(client, str(marker), timeout=60) == "done"


def test_wait_without_dialog_check_does_not_probe(fast_probe, tmp_path, monkeypatch):
    dialogs = _Dialogs([_report("blocked", "Q")] * 10)
    client = _Client(dialogs=dialogs)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(writer.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(TimeoutError):
        writer._wait_until_done(client, str(tmp_path / "never"), timeout=60, dialog_check=False)
    assert dialogs.calls == 0


def test_read_results_sends_nothing_to_a_blocked_ciw():
    client = _Client(dialogs=_Dialogs([_report("blocked", "ADE Assembler Message 2225")]))
    out = read_results(client, "s1", lib="LIB", cell="cell", history="Interactive.7")
    assert out == {} and "2225" in out.reason
    assert client.calls == []


def test_read_results_adds_no_calls_after_an_export_timeout():
    # 1 maeGetSetup, 2 export (times out): no deleteFile, no spec/yield after it.
    client = _Client(outputs=['("TEST")'], errors={2: "Socket timeout after 30s"})
    client.download_file = lambda remote, local: pytest.fail("no download")
    out = read_results(client, "s1", lib="LIB", cell="cell", history="Interactive.7")
    assert out == {}
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "meta",
    [
        {"outcome": "unknown"},
        {"request_handle": {"request_id": "abc123"}},
    ],
)
def test_read_results_treats_recoverable_unknown_metadata_as_channel_lost(meta):
    client = _Client(
        outputs=['("TEST")'],
        errors={2: "request outcome uncertain"},
        metadata={2: meta},
    )
    client.download_file = lambda remote, local: pytest.fail("no download")
    out = read_results(client, "s1", lib="LIB", cell="cell", history="Interactive.7")
    assert out == {}
    assert len(client.calls) == 2


def _results(tmp_path):
    lib = tmp_path / "lib"
    res = lib / "cell" / "maestro" / "results" / "maestro"
    (res / "Interactive.3" / "1").mkdir(parents=True)
    for suffix in (".rdb", ".log", ".msg.db"):
        (res / f"Interactive.3{suffix}").write_bytes(b"x")
    (res / "Interactive.4.rdb").write_bytes(b"keep")
    return lib, res


def test_delete_history_refuses_while_a_run_is_in_progress(tmp_path):
    lib, res = _results(tmp_path)
    client = _Client(outputs=["(15 16)"])
    with pytest.raises(MaestroHistoryError, match="running: 15 of 16"):
        delete_history(client, "Interactive.3", session="s1", lib="LIB", cell="cell")
    assert (res / "Interactive.3.rdb").exists()


def test_delete_history_removes_the_entry_and_its_files(tmp_path):
    lib, res = _results(tmp_path)
    client = _Client(outputs=["(16 16)", f'"{lib}"', "nil",
                              '("VB_MAESTRO_HISTORY_DELETE_V1" "ok" "Interactive.3")'])
    removed = delete_history(client, "Interactive.3", session="s1", lib="LIB", cell="cell")
    assert sorted(p.rsplit("/", 1)[1] for p in removed) == [
        "Interactive.3", "Interactive.3.log", "Interactive.3.msg.db", "Interactive.3.rdb"]
    assert (res / "Interactive.4.rdb").exists()
    assert "axlGetRunStatus" in client.calls[-1]


def test_delete_history_rejects_a_path_like_name():
    with pytest.raises(ValueError):
        delete_history(_Client(), "../x", session="s1", lib="LIB", cell="cell")


def test_probe_uses_the_pid_learned_before_the_dialog(monkeypatch):
    # Live on IC23.1: once a modal dialog held the CIW the identity handshake
    # timed out, while inspecting the known PID still found the dialog.
    from virtuoso_bridge.virtuoso.dialogs import DialogOps

    ops = DialogOps(SimpleNamespace())
    handshake = iter([4242])

    def connected_pid(timeout):
        try:
            return next(handshake)
        except StopIteration:
            raise TimeoutError("timed out") from None

    monkeypatch.setattr(ops, "connected_pid", connected_pid)
    monkeypatch.setattr(ops, "inspect", lambda pid, timeout: _report("blocked", "VB probe test"))
    assert ops.remember_pid() is True
    report = ops.probe(timeout=5)
    assert report.status == "blocked" and report.dialogs[0]["title"] == "VB probe test"


def test_probe_without_a_known_pid_is_unknown_when_the_handshake_hangs(monkeypatch):
    from virtuoso_bridge.virtuoso.dialogs import DialogOps

    ops = DialogOps(SimpleNamespace())
    monkeypatch.setattr(ops, "connected_pid", lambda timeout: (_ for _ in ()).throw(TimeoutError()))
    assert ops.probe(timeout=5) is None


def test_run_and_wait_learns_the_pid_before_starting(monkeypatch):
    order = []

    class Dialogs(_Dialogs):
        def remember_pid(self, timeout):
            order.append("pid")
            return True

    client = _Client(dialogs=Dialogs([]))
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **k: order.append("run") or '"Interactive.1"')
    monkeypatch.setattr(writer, "_wait_until_done", lambda *a, **k: "done")
    assert writer.run_and_wait(client, session="s1")[1] == "done"
    assert order == ["pid", "run"]


def test_set_analysis_drops_the_cached_expression_of_the_fields_it_sets():
    client = _Client(outputs=["t"])
    assert writer.set_analysis(client, "NOISE", "tran",
                               options='(("stop" "906*VAR(\\"tclk\\")"))', session="s1") == "t"
    code = client.calls[0]
    assert code.startswith('let((vbR vbS vbT vbK) vbR = maeSetAnalysis("NOISE" "tran"')
    assert "expressionTable" in code and '"tran_stop"' in code and "remove(k vbT)" in code


def test_set_analysis_without_options_is_unchanged():
    client = _Client(outputs=["t"])
    writer.set_analysis(client, "NOISE", "tran", enable=False, session="s1")
    assert client.calls == ['maeSetAnalysis("NOISE" "tran" ?enable nil ?session "s1")']


def test_delete_history_finds_lib_and_cell_from_the_session(tmp_path):
    lib, res = _results(tmp_path)
    client = _Client(outputs=["(16 16)", '"LIB"', '"cell"', f'"{lib}"', "nil",
                              '("VB_MAESTRO_HISTORY_DELETE_V1" "ok" "Interactive.3")'])
    assert len(delete_history(client, "Interactive.3", session="s1")) == 4
    assert "axlGetSessionLibName" in client.calls[1]
