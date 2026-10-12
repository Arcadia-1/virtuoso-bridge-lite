"""layout_select_box drives geSelectArea/geDeselectArea with the cellview's window."""

from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.layout.ops import layout_select_box

BOX = (0.5, 0.0, 2.5, 2.0)
BOX_SKILL = "list(list(0.5 0) list(2.5 2))"


@pytest.mark.parametrize(("mode_name", "selection"), [
    ("replace", f"geDeselectAllFig(cv) geSelectArea(win {BOX_SKILL})"),
    ("add", f"geSelectArea(win {BOX_SKILL})"),
    ("sub", f"geDeselectArea(win {BOX_SKILL})"),
])
def test_selection_calls_take_the_window_first(mode_name, selection):
    skill = layout_select_box(BOX, mode_name=mode_name)
    window = "win = car(setof(w hiGetWindowList() car(errset(geGetWindowCellView(w))) == cv))"
    assert window in skill and selection in skill
    assert skill.index(window) < skill.index(selection)
    assert 'unless(win error("no window shows the layout cellview"))' in skill
    assert "length(geGetSelSet(cv))" in skill
    assert "geAddSelectBox" not in skill and "ERROR:" not in skill


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="Unsupported selection mode"):
        layout_select_box(BOX, mode_name="toggle")
