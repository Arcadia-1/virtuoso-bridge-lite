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


def test_category_list_filters_stale_entries_by_opening_each_category() -> None:
    skill = category_list_skill("demoLib")

    assert "ddCatGetLibCats(vbLib)" in skill
    assert 'ddCatOpen(vbLib vbName "r")' in skill
    assert "when(vbCat" in skill
    assert "ddCatClose(vbCat)" in skill
    assert "Everything" not in skill
    assert "Uncategorized" not in skill


def test_category_create_keeps_empty_category_and_verifies_it() -> None:
    skill = category_create_skill("demoLib", "ADC")

    assert 'ddCatOpenEx(vbLib "ADC" "w" 1)' in skill
    assert "vbSaved = ddCatSave(vbCat)" in skill
    assert 'vbVerify = ddCatOpen(vbLib "ADC" "r")' in skill
    assert "ddCatAddItem" not in skill


def test_category_delete_uses_ddcat_remove_and_never_deletes_cells() -> None:
    skill = category_delete_skill("demoLib", "ADC")

    assert 'ddCatOpen(vbLib "ADC" "r")' in skill
    assert 'ddCatOpen(vbLib "ADC" "a")' in skill
    assert "vbRemoved = ddCatRemove(vbCat)" in skill
    assert 'vbVerify = ddCatOpen(vbLib "ADC" "r")' in skill
    assert "ddDeleteObj" not in skill
    assert "deleteFile" not in skill
    assert "system(" not in skill


def test_category_list_cells_filters_member_type() -> None:
    skill = category_list_cells_skill("demoLib", "ADC")

    assert "ddCatGetCatMembers(vbCat)" in skill
    assert 'cadr(vbMember) == "cell"' in skill
    assert "reverse(vbCells)" in skill


def test_category_add_cell_uses_ddcat_add_save_close_and_verification() -> None:
    skill = category_add_cell_skill("demoLib", "ADC", "comparator")

    assert 'member("comparator" vbLib~>cells~>name)' in skill
    assert 'ddGetObj("demoLib" "comparator")' not in skill
    assert 'ddCatOpen(vbLib "ADC" "r")' in skill
    assert 'ddCatOpen(vbLib "ADC" "a")' in skill
    assert 'ddCatAddItem(vbCat "comparator" "cell")' in skill
    assert "ddCatSave(vbCat)" in skill
    assert 'member(list("comparator" "cell") vbMembers)' in skill


def test_category_remove_cell_uses_ddcat_subitem_and_verification() -> None:
    skill = category_remove_cell_skill("demoLib", "ADC", "comparator")

    assert 'ddCatSubItem(vbCat "comparator")' in skill
    assert 'ddCatOpen(vbLib "ADC" "r")' in skill
    assert "ddCatSave(vbCat)" in skill
    assert 'member(list("comparator" "cell") vbMembers)' in skill
    assert "ddDeleteObj" not in skill


def test_category_rename_preserves_cells_without_merge_or_overwrite() -> None:
    skill = category_rename_skill("demoLib", "ADC", "Comparators")

    assert 'ddCatOpen(vbLib "ADC" "r")' in skill
    assert 'ddCatOpen(vbLib "Comparators" "r")' in skill
    assert 'ddCatOpenEx(vbLib "Comparators" "w" 1)' in skill
    assert "ddCatGetCatMembers(vbSource)" in skill
    assert "ddCatAddItem(" in skill
    assert "vbDestination car(vbMember) cadr(vbMember))" in skill
    assert "ddCatRemove(vbSource)" in skill
    assert 'categoryContainsSubcategories' in skill
    assert "ccpRename" not in skill


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


def test_list_categories_returns_only_verified_names() -> None:
    client = _Client('("ok" ("ADC" "DAC"))')

    result = list_categories(client, "demoLib", timeout=19)

    assert result == ["ADC", "DAC"]
    assert client.timeout == 19


def test_create_category_returns_verified_name() -> None:
    client = _Client('("ok" "ADC")')

    assert create_category(client, "demoLib", "ADC") == "ADC"


def test_delete_category_returns_none() -> None:
    client = _Client('("ok")')

    assert delete_category(client, "demoLib", "ADC") is None


def test_list_category_cells_returns_cell_names() -> None:
    client = _Client('("ok" ("comparator" "strongarm"))')

    result = list_category_cells(client, "demoLib", "ADC")

    assert result == ["comparator", "strongarm"]


def test_add_and_remove_cell_return_none() -> None:
    assert add_cell_to_category(_Client('("ok")'), "demoLib", "ADC", "comparator") is None
    assert (
        remove_cell_from_category(
            _Client('("ok")'),
            "demoLib",
            "ADC",
            "comparator",
        )
        is None
    )


def test_rename_category_returns_destination_name() -> None:
    client = _Client('("ok" "Comparators")')

    assert rename_category(client, "demoLib", "ADC", "Comparators") == "Comparators"


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
    assert client.skill is not None
    assert "ddCatGetLibCats" in client.skill
