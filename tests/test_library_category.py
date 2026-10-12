from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.library import LibraryOps
from virtuoso_bridge.virtuoso.library.category import (
    CategoryPartialSuccessError,
    add_cell_to_category,
    category_add_cell_skill,
    category_create_skill,
    category_delete_skill,
    category_list_cells_skill,
    category_list_skill,
    category_remove_cell_skill,
    category_rename_skill,
    create_category,
    delete_category,
    list_categories,
    list_category_cells,
    remove_cell_from_category,
    rename_category,
)


class _Client:
    def __init__(self, output: str, *, errors=None) -> None:
        self.output = output
        self.errors = errors or []
        self.skill: str | None = None
        self.timeout: int | None = None

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        return VirtuosoResult(
            status=(ExecutionStatus.ERROR if self.errors else ExecutionStatus.SUCCESS),
            output=self.output,
            errors=self.errors,
        )


def _assert_ordered(skill: str, *fragments: str) -> None:
    positions = [skill.index(fragment) for fragment in fragments]
    assert positions == sorted(positions), fragments


_SKILLS = {
    "list": lambda: category_list_skill("demoLib"),
    "create": lambda: category_create_skill("demoLib", "ADC"),
    "delete": lambda: category_delete_skill("demoLib", "ADC"),
    "list_cells": lambda: category_list_cells_skill("demoLib", "ADC"),
    "add_cell": lambda: category_add_cell_skill("demoLib", "ADC", "comparator"),
    "remove_cell": lambda: category_remove_cell_skill("demoLib", "ADC", "comparator"),
    "rename": lambda: category_rename_skill("demoLib", "ADC", "Comparators"),
}

# Documented library-python-api.md category contracts, as steps that must
# appear in this order in the generated SKILL.
_ORDERED_STEPS = {
    # Stale list entries are filtered by opening each category.
    "list": ('vbCat = ddCatOpen(vbLib vbName "r")', "when(vbCat vbResult = cons(vbName"),
    # Empty categories are kept (keepEmpty flag) and verified by reopening.
    "create": ('ddCatOpenEx(vbLib "ADC" "w" 1)', 'vbVerify = ddCatOpen(vbLib "ADC" "r")'),
    "delete": ("vbRemoved = ddCatRemove(vbCat)", 'vbVerify = ddCatOpen(vbLib "ADC" "r")'),
    # Only members whose type is cell are returned.
    "list_cells": ("ddCatGetCatMembers(vbCat)", 'when(cadr(vbMember) == "cell"'),
    # Existing cell and membership are prechecked; the change is verified.
    "add_cell": (
        'member("comparator" vbLib~>cells~>name)',
        'list("error" "cellAlreadyInCategory")',
        'ddCatAddItem(vbCat "comparator" "cell")',
        'vbVerify = ddCatOpen(vbLib "ADC" "r")',
    ),
    "remove_cell": (
        'member("comparator" vbLib~>cells~>name)',
        'list("error" "cellNotInCategory")',
        'ddCatSubItem(vbCat "comparator")',
        'vbVerify = ddCatOpen(vbLib "ADC" "r")',
    ),
    # Subcategories and existing destinations are refused; members are copied
    # and verified before the source category is removed.
    "rename": (
        '"categoryContainsSubcategories"',
        '"destinationCategoryExists"',
        'ddCatOpenEx(vbLib "Comparators" "w" 1)',
        "ddCatAddItem(",
        "vbDestinationMembers = ddCatGetCatMembers(vbVerify)",
        "vbRemoved = ddCatRemove(vbSource)",
    ),
}


@pytest.mark.parametrize("name", list(_ORDERED_STEPS))
def test_category_skill_steps_are_ordered(name: str) -> None:
    _assert_ordered(_SKILLS[name](), *_ORDERED_STEPS[name])


@pytest.mark.parametrize("name", list(_SKILLS))
def test_category_builders_never_touch_cells_or_category_files(name: str) -> None:
    skill = _SKILLS[name]()

    for forbidden in ("ddDeleteObj", "deleteFile", "system(", ".Cat", "ccpRename"):
        assert forbidden not in skill


@pytest.mark.parametrize(
    ("builder", "args", "field"),
    [
        (category_list_skill, ("",), "library"),
        (category_create_skill, ("demoLib", ""), "category"),
        (category_delete_skill, ("demoLib", " "), "category"),
        (category_list_cells_skill, ("", "ADC"), "library"),
        (category_add_cell_skill, ("demoLib", "ADC", ""), "cell"),
        (category_rename_skill, ("demoLib", "ADC", ""), "new_name"),
    ],
)
def test_category_builders_reject_empty_required_values(builder, args, field) -> None:
    with pytest.raises(ValueError, match=field):
        builder(*args)


@pytest.mark.parametrize(
    ("call", "output", "expected"),
    [
        (lambda c: list_categories(c, "demoLib", timeout=19), '("ok" ("ADC" "DAC"))', ["ADC", "DAC"]),
        (lambda c: create_category(c, "demoLib", "ADC", timeout=19), '("ok" "ADC")', "ADC"),
        (lambda c: delete_category(c, "demoLib", "ADC", timeout=19), '("ok")', None),
        (
            lambda c: list_category_cells(c, "demoLib", "ADC", timeout=19),
            '("ok" ("comparator" "strongarm"))',
            ["comparator", "strongarm"],
        ),
        (lambda c: add_cell_to_category(c, "demoLib", "ADC", "comparator", timeout=19), '("ok")', None),
        (
            lambda c: remove_cell_from_category(c, "demoLib", "ADC", "comparator", timeout=19),
            '("ok")',
            None,
        ),
        (
            lambda c: rename_category(c, "demoLib", "ADC", "Comparators", timeout=19),
            '("ok" "Comparators")',
            "Comparators",
        ),
    ],
    ids=["list", "create", "delete", "list_cells", "add_cell", "remove_cell", "rename"],
)
def test_category_operations_return_verified_results(call, output: str, expected) -> None:
    client = _Client(output)

    assert call(client) == expected
    assert client.timeout == 19


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("libraryNotFound", "library does not exist"),
        ("categoryNotFound", "category does not exist"),
        ("categoryExists", "category already exists"),
        ("destinationCategoryExists", "destination category already exists"),
        ("categoryContainsSubcategories", "unsupported subcategories"),
        ("cellNotFound", "cell does not exist"),
        ("cellAlreadyInCategory", "already in category"),
        ("cellNotInCategory", "not in category"),
    ],
)
def test_category_operations_raise_explicit_errors(code: str, message: str) -> None:
    client = _Client(f'("error" "{code}")')

    with pytest.raises(RuntimeError, match=message):
        list_category_cells(client, "demoLib", "ADC")


def test_category_partial_success_error_preserves_context() -> None:
    client = _Client('("partial" "categoryRenameSourceRemovalFailed")')

    with pytest.raises(CategoryPartialSuccessError) as exc_info:
        rename_category(client, "demoLib", "ADC", "Comparators")

    assert exc_info.value.library == "demoLib"
    assert exc_info.value.category == "ADC"
    assert "partially succeeded" in str(exc_info.value)


def test_category_operations_raise_transport_errors() -> None:
    client = _Client("", errors=["category lock failed"])

    with pytest.raises(RuntimeError, match="SKILL error: category lock failed"):
        list_categories(client, "demoLib")


def test_library_ops_exposes_category_methods() -> None:
    client = _Client('("ok" ("ADC"))')
    ops = LibraryOps(client)  # type: ignore[arg-type]

    assert ops.list_categories("demoLib") == ["ADC"]
    assert client.skill == category_list_skill("demoLib")
