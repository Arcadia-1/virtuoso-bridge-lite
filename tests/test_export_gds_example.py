from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult


_EXAMPLE_PATH = (
    Path(__file__).parents[1]
    / "examples"
    / "01_virtuoso"
    / "layout"
    / "15_export_gds.py"
)


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_export_gds_example_module",
        _EXAMPLE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load example: {_EXAMPLE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_gds_example_reports_preflight_error_as_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = _load_example()
    export_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    error = "stream_map is not an existing regular file: /missing/stream.map"

    class FakeLayout:
        def export_gds(self, *args: object, **kwargs: object) -> object:
            export_calls.append((args, kwargs))
            raise FileNotFoundError(error)

    class FakeClient:
        layout = FakeLayout()

        def execute_skill(self, code: str, *, timeout: float) -> VirtuosoResult:
            assert code == "1+1"
            assert timeout == 10.0
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output="2",
            )

    fake_client = FakeClient()

    class FakeVirtuosoClient:
        @classmethod
        def from_env(cls) -> FakeClient:
            return fake_client

    monkeypatch.setattr(example, "VirtuosoClient", FakeVirtuosoClient)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "15_export_gds.py",
            "--library",
            "worklib",
            "--cell",
            "top",
            "--stream-map",
            "/missing/stream.map",
            "--output",
            "top.gds",
        ],
    )

    exit_code = example.main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out) == {
        "status": "error",
        "reason": "invalid_arguments",
        "errors": [error],
    }
    assert captured.err == ""
    assert len(export_calls) == 1
