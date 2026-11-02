"""Opt-in configuration-only checks in an isolated headless Virtuoso.

No PDK, simulation, user CIW or design variants; one V1 cell is reused.
"""

import json
import os
import sys

import pytest

from virtuoso_bridge.virtuoso.maestro import MaestroEngineError

pytest_plugins = ["tests.test_daemon_lifecycle_live"]
pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_MAESTRO_ENGINE_TESTS") != "1" or sys.platform != "linux",
    reason="set VB_RUN_MAESTRO_ENGINE_TESTS=1 and VB_TEST_VIRTUOSO on a Cadence host",
)


def exercise_engine(client, evaluate, library_path):
    lib, cell, test = "VB_ENGINE_TEST", "V1", "TRAN"
    assert evaluate(f'ddCreateLib("{lib}" {json.dumps(str(library_path))})') != "nil"
    evaluate(f'let((cv) cv=dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "w") dbSave(cv) dbClose(cv))')
    session = client.maestro.open_session(lib, cell)
    try:
        client.maestro.create_test(test, lib=lib, cell=cell, session=session)
        client.maestro.set_sim_option(test, '(("temp" "37"))', session=session)
        unrelated = evaluate(f'maeGetSimOption("{test}" ?option "temp" ?session "{session}")')
        initial = client.maestro.get_engine(test, session=session)
        for engine in ("spectre", "aps"):
            result = client.maestro.set_engine(test, engine=engine, session=session)
            assert result.after.engine == engine and result.after.preset is None
            assert result.saved is False
        for preset in ("CX", "AX", "MX", "LX", "VX"):
            result = client.maestro.set_engine(test, engine="spectre_x", preset=preset, session=session)
            assert result.after.preset == preset and result.after.engine == "spectre_x"
        repeated = client.maestro.set_engine(test, engine="spectre_x", preset="VX", session=session)
        assert repeated.changed is False
        client.maestro.set_engine(test, engine="aps", session=session)
        inactive = client.maestro.get_engine(test, session=session)
        assert inactive.preset is None and inactive.stored_spectre_x_preset == "VX"
        assert evaluate(f'maeGetSimOption("{test}" ?option "temp" ?session "{session}")') == unrelated
        try:
            client.maestro.set_engine("MISSING", engine="aps", session=session)
        except MaestroEngineError as exc:
            assert exc.result is not None
        else:
            raise AssertionError("missing test unexpectedly accepted")
        client.maestro.set_engine(test, engine="spectre_x", preset="AX", session=session)
        # Persistence is a separate explicit action, not part of set_engine.
        client.maestro.save_setup(lib, cell, session=session)
    finally:
        client.maestro.close_session(session)
    session = client.maestro.open_session(lib, cell)
    try:
        persisted = client.maestro.get_engine(test, session=session)
        assert persisted.engine == "spectre_x" and persisted.preset == "AX"
        assert evaluate(f'maeGetSimOption("{test}" ?option "temp" ?session "{session}")') == unrelated
        assert evaluate(f'length(maeGetSetup(?typeName "tests" ?session "{session}"))') == "1"
    finally:
        client.maestro.close_session(session)
    return dict(initial=initial.model_dump(), persisted=persisted.model_dump())


def test_native_engine_configuration(sessions, tmp_path):
    session = sessions("engine")
    exercise_engine(session["client"], session["evaluate"], tmp_path / "library")
