"""Live GDS export check, kept apart because it can leave the CIW blocked.

Runs after ``test_bridge_live.py`` with the same environment variables (see
that module) plus ``VB_TEST_LAYER`` and ``VB_TEST_STREAM_MAP``. On IC6.1.8
XStream Out shows a modal "Stream out translation complete" box after every
run: the box follows the ``xstShowCompletionMsgBox`` SKILL variable, while the
bridge only clears the ``showCompletionMsgBox`` form field. Until that is
fixed the box stays open, so this module never sends SKILL after a blocked
inspection and leaves its cell in place for manual cleanup.
"""

from __future__ import annotations

import os
import uuid
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_LIVE_TESTS") != "1",
    reason="set VB_RUN_LIVE_TESTS=1 to run against a live Virtuoso session",
)

if os.getenv("VB_RUN_LIVE_TESTS") == "1" and os.getenv("VB_TEST_ENV_FILE"):
    from virtuoso_bridge.env import set_runtime_env_file

    set_runtime_env_file(os.path.expanduser(os.environ["VB_TEST_ENV_FILE"]))

from virtuoso_bridge import VirtuosoClient  # noqa: E402
from virtuoso_bridge.virtuoso.layout import layout_create_rect as rect  # noqa: E402


@pytest.mark.xfail(strict=True, reason=(
    "open bug: XStream's completion message box is controlled by xstShowCompletionMsgBox "
    "on IC6.1.8, so every export leaves a modal box that blocks the next request"))
def test_gds_export_succeeds_and_leaves_ciw_unblocked(tmp_path):
    lib = os.getenv("VB_TEST_LIB")
    layer = os.getenv("VB_TEST_LAYER")
    stream_map = os.getenv("VB_TEST_STREAM_MAP")
    if not (lib and layer and stream_map):
        pytest.skip("set VB_TEST_LIB, VB_TEST_LAYER and VB_TEST_STREAM_MAP")
    client = VirtuosoClient.from_env(timeout=120)
    cell = f"VB_LIVE_gds_{uuid.uuid4().hex[:8]}"
    with client.layout.create(lib, cell) as lay:
        lay.add(rect(layer, "drawing", 0, 0, 2, 1))
    gds = tmp_path / "out.gds"
    result = client.layout.export_gds(lib, cell, gds, stream_map=Path(stream_map).expanduser(), timeout=240)
    assert result.ok, (result.reason, result.errors)
    assert gds.read_bytes()[:4] == bytes.fromhex("00060002")

    report = client.dialogs.inspect(pid=client.get_virtuoso_pid())
    if report.status == "clear":
        client.execute_skill(f'ddDeleteObj(ddGetObj("{lib}" "{cell}"))')
    else:
        warnings.warn(f"CIW blocked after export; left {lib}/{cell} in place: {report.dialogs}")
    assert report.status == "clear", [d.title for d in report.dialogs]
