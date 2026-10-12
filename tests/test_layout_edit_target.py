"""Layout content operations act on the edited cellview inside an editor batch."""

from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.layout.editor import LayoutEditor
from virtuoso_bridge.virtuoso.layout.ops import (
    LAYOUT_EDIT_CV_MARKER,
    layout_clear_routing,
    layout_delete_shapes_on_layer,
    layout_list_shapes,
)

BUILDERS = [
    lambda **kw: layout_list_shapes(**kw),
    lambda **kw: layout_delete_shapes_on_layer("M3", "drawing", **kw),
    lambda **kw: layout_clear_routing(**kw),
]


def _balanced(text: str) -> bool:
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            escaped = char == "\\" and not escaped
            in_string = not (char == '"' and not escaped)
            continue
        if char == '"':
            in_string = True
        elif char in "()":
            depth += 1 if char == "(" else -1
            if depth < 0:
                return False
    return depth == 0


@pytest.mark.parametrize("build", BUILDERS)
def test_default_target_prefers_editor_cellview_then_current_window(build):
    skill = build()
    marker = f"if(boundp('{LAYOUT_EDIT_CV_MARKER}) && {LAYOUT_EDIT_CV_MARKER} then {LAYOUT_EDIT_CV_MARKER} else "
    assert f"cv = {marker}" in skill
    assert skill.index(marker) < skill.index("geGetEditCellView()")
    assert 'unless(cv error("no layout cellview' in skill
    assert "ERROR:" not in skill
    assert _balanced(skill)


@pytest.mark.parametrize("build", BUILDERS)
def test_explicit_cv_expr_is_used_verbatim(build):
    skill = build(cv_expr="targetCv")
    assert "cv = targetCv " in skill
    assert LAYOUT_EDIT_CV_MARKER not in skill and "geGetEditCellView" not in skill


class _Client:
    def __init__(self, status=ExecutionStatus.SUCCESS):
        self.status = status
        self.calls = []

    def execute_operations(self, commands, *, timeout=None, wrap_in_progn=True):
        self.calls.append((list(commands), wrap_in_progn))
        errors = [] if self.status == ExecutionStatus.SUCCESS else ["boom"]
        return VirtuosoResult(status=self.status, output="t", errors=errors)


@pytest.mark.parametrize("mode", ["a", "w"])
def test_editor_marks_its_cellview_and_always_clears_the_mark(mode):
    client = _Client()
    with LayoutEditor(client, "L", "C", mode=mode) as lay:
        lay.add(layout_delete_shapes_on_layer("M3"))

    (commands, wrap), = client.calls
    assert wrap is False and len(commands) == 1
    script = commands[0]
    assert script.startswith("unwindProtect(progn(")
    assert script.endswith(f" {LAYOUT_EDIT_CV_MARKER} = nil)")
    opened = script.index('cv = ')
    marked = script.index(f"{LAYOUT_EDIT_CV_MARKER} = cv")
    deleted = script.index('"M3"')
    saved = script.index("dbSave(")
    assert opened < marked < deleted < saved
    assert _balanced(script)


def test_editor_failure_still_raises():
    with pytest.raises(RuntimeError, match="layout edit failed: boom"):
        with LayoutEditor(_Client(ExecutionStatus.ERROR), "L", "C") as lay:
            lay.add(layout_list_shapes())
