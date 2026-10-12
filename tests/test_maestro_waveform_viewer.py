from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.maestro import (
    close_waveform_viewer,
    maestro_close_waveform_viewer_skill,
    maestro_open_waveform_viewer_skill,
    open_waveform_viewer,
)


class _Client:
    def __init__(self, output: str) -> None:
        self.output = output
        self.skill: str | None = None
        self.timeout: int | None = None

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        return {"status": "success", "output": self.output}


def _open_skill(**kwargs) -> str:
    kwargs.setdefault("signals", ["/OUT"])
    return maestro_open_waveform_viewer_skill("demoLib", "tb_inv", "Interactive.1", **kwargs)


def test_maestro_open_waveform_viewer_skill_plots_each_explicit_signal() -> None:
    skill = _open_skill(signals=["/IN", "/OUT"], results_dir="/tmp/psf/tran/psf", result="tran")

    assert 'v("/IN" ?result "tran" ?resultsDir vbResultsDir)' in skill
    assert 'v("/OUT" ?result "tran" ?resultsDir vbResultsDir)' in skill
    assert '?expr list("/IN" "/OUT"))' in skill


def test_maestro_open_waveform_viewer_keeps_session_open_unless_open_fails() -> None:
    skill = _open_skill(results_dir="/tmp/psf/tran/psf")

    cleanup = skill.index("unless(vbOpenOk ")
    assert skill.index("vbOpenOk = t ") < cleanup
    assert skill.count("maeCloseSession(") == 1
    assert skill.index("hiCloseWindow(vbWindowId)") > cleanup
    assert skill.index("maeCloseSession(?session vbSession ?forceClose t)") > cleanup
    assert 'error("open waveform viewer failed")' in skill[cleanup:]


def test_maestro_open_waveform_viewer_escapes_string_inputs() -> None:
    skill = maestro_open_waveform_viewer_skill(
        'demo"Lib\\1',
        'tb"inv\\2',
        'Interactive."3\\4',
        signals=['/A"NET\\1'],
        test='tr"an\\5',
        result='res"ult\\6',
        results_dir='/tmp/a"b\\c',
    )

    assert 'demo\\"Lib\\\\1' in skill
    assert 'tb\\"inv\\\\2' in skill
    assert 'Interactive.\\"3\\\\4' in skill
    assert '/A\\"NET\\\\1' in skill
    assert 'tr\\"an\\\\5' in skill
    assert 'res\\"ult\\\\6' in skill
    assert '/tmp/a\\"b\\\\c' in skill


def test_maestro_open_waveform_viewer_requires_explicit_results_dir_to_open() -> None:
    skill = _open_skill(results_dir="/tmp/psf/tran/psf")

    assert 'when(vbResultsDir && !vbRawResultsOpen error("open raw results failed"))' in skill


def test_maestro_open_waveform_viewer_skill_can_fallback_to_maestro_outputs() -> None:
    skill = _open_skill(signals=["vout"], test="tran_test")

    assert 'maeGetOutputValue("vout" "tran_test")' in skill


def test_maestro_open_waveform_viewer_requires_signals() -> None:
    with pytest.raises(ValueError, match="signals must not be empty"):
        _open_skill(signals=[])


@pytest.mark.parametrize(
    ("window", "expected"),
    [(7, "window(7)"), ("window:7", "window(7)"), ("window(8)", "window(8)")],
)
def test_maestro_close_waveform_viewer_parses_window_refs(window, expected: str) -> None:
    skill = maestro_close_waveform_viewer_skill(window=window)

    assert f"vbWindow = {expected} " in skill
    assert "vbSession = nil " in skill


def test_maestro_close_waveform_viewer_verifies_session_close() -> None:
    skill = maestro_close_waveform_viewer_skill(session='fnx"Session\\2')

    assert 'vbSession = "fnx\\"Session\\\\2"' in skill
    assert 'when(member(vbSession vbSessionsAfter) error("close waveform session failed"))' in skill


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "window or session must be provided"),
        ({"session": "  "}, "session must not be blank"),
        ({"window": "window(7) hiCloseWindow(window(1))"}, "window must be a window number"),
        ({"window": 0}, "positive window number"),
    ],
)
def test_maestro_close_waveform_viewer_rejects_invalid_targets(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        maestro_close_waveform_viewer_skill(**kwargs)


def test_open_waveform_viewer_returns_raw_result_and_forwards_options() -> None:
    client = _Client('("opened" "demoLib" "tb_inv")')

    result = open_waveform_viewer(
        client,
        "demoLib",
        "tb_inv",
        "Interactive.1",
        signals=["/OUT"],
        timeout=30,
    )

    assert result == {"status": "success", "output": '("opened" "demoLib" "tb_inv")'}
    assert client.timeout == 30
    assert client.skill == _open_skill()


def test_close_waveform_viewer_returns_raw_result_and_forwards_options() -> None:
    client = _Client('("closed" "fnxSession2" window:7)')

    result = close_waveform_viewer(client, window=7, session="fnxSession2", timeout=10)

    assert result == {"status": "success", "output": '("closed" "fnxSession2" window:7)'}
    assert client.timeout == 10
    assert client.skill == maestro_close_waveform_viewer_skill(window=7, session="fnxSession2")
