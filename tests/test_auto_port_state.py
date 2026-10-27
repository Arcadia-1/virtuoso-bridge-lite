"""Automatic ports retain runtime identity and limit stop to the tracked child."""
import os
from pathlib import Path
import re


RESOURCES = Path(os.environ.get("VB_TEST_DAEMON_RESOURCES",
    Path(__file__).parents[1] / "src/virtuoso_bridge/virtuoso/basic/resources"))
SOURCE = RESOURCES / "ramic_bridge.il"


def procedure(source, name):
    start = source.index("procedure(" + name + "()")
    end = source.find("\nprocedure(", start + 1)
    return source[start:end if end >= 0 else len(source)]


def test_stop_all_cannot_discover_or_signal_untracked_processes():
    source = SOURCE.read_text(encoding="utf-8")
    body = procedure(source, "RBStopAll")
    # The function ends before monitor creation, which contains other callbacks.
    body = body.split("; ============================================================================", 1)[0]
    executable = "\n".join(line.split(";", 1)[0] for line in body.splitlines())
    calls = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", executable)
    assert calls == ["procedure", "RBStopAll", "RBStop"]
    assert "RBPort" not in executable
    assert "RBLastPid" not in executable
    # No clearing of the handle before stop success/exit can be reconciled.
    assert "RBIpc =" not in executable
    assert "fuser -k" not in source
    assert "pgrep" not in source



def test_monitor_describes_current_ciw_scope():
    source = SOURCE.read_text(encoding="utf-8")
    assert '"Stop this bridge (force)"' in source
    assert "Kill MY daemons" not in source


def test_runtime_port_survives_reload_and_monitor_edits_configuration():
    source = SOURCE.read_text(encoding="utf-8")
    config = source[:source.index('RBIdentityPath =')]
    assert "unless(boundp('RBPort) && boundp('RBIpc) && RBIpc && RBIpc != 'unbound" in config
    assert '?value RBConfiguredPort' in source
    assert 'RBMIntPort->value = RBConfiguredPort' in procedure(source, "RBMRefresh")

