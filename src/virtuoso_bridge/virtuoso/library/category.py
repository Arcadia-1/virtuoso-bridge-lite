"""Flat library category management through the supported ``ddCat`` API."""

from __future__ import annotations

from typing import Any

from virtuoso_bridge.virtuoso.library.management import (
    _execute_record,
    _record_value,
    _require_text,
)
from virtuoso_bridge.virtuoso.ops import q


class CategoryPartialSuccessError(RuntimeError):
    """Raised when a category mutation may have changed persisted state."""

    def __init__(self, message: str, *, library: str, category: str) -> None:
        self.library = library
        self.category = category
        super().__init__(message)


def category_list_skill(library: str) -> str:
    """Build SKILL that returns existing top-level category names."""
    library = _require_text("library", library)
    return (
        "let((vbLib vbName vbCat vbClosed vbResult) "
        f"vbLib = ddGetObj({q(library)}) "
        "if(!vbLib "
        'then list("error" "libraryNotFound") '
        "else progn("
        "vbResult = nil "
        "foreach(vbName ddCatGetLibCats(vbLib) "
        'vbCat = ddCatOpen(vbLib vbName "r") '
        "when(vbCat "
        "vbResult = cons(vbName vbResult) "
        "vbClosed = ddCatClose(vbCat) "
        'unless(vbClosed error("category close failed")))) '
        'list("ok" reverse(vbResult)))))'
    )


def category_create_skill(library: str, category: str) -> str:
    """Build SKILL that creates and verifies an empty persistent category."""
    library = _require_text("library", library)
    category = _require_text("category", category)
    return f"""
let((vbLib vbExisting vbCat vbSaved vbClosed vbVerify)
  vbLib = ddGetObj({q(library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else progn(
      vbExisting = ddCatOpen(vbLib {q(category)} "r")
      if(vbExisting
        then progn(
          vbClosed = ddCatClose(vbExisting)
          if(vbClosed
            then list("error" "categoryExists")
            else list("error" "categoryCloseFailed")
          )
        )
        else progn(
          vbCat = ddCatOpenEx(vbLib {q(category)} "w" 1)
          if(!vbCat
            then list("error" "categoryCreateFailed")
            else progn(
              vbSaved = ddCatSave(vbCat)
              vbClosed = ddCatClose(vbCat)
              if(!vbSaved || !vbClosed
                then list("partial" "categoryCreateFailed")
                else progn(
                  vbVerify = ddCatOpen(vbLib {q(category)} "r")
                  if(!vbVerify
                    then list("partial" "categoryCreateVerificationFailed")
                    else progn(
                      vbClosed = ddCatClose(vbVerify)
                      if(vbClosed
                        then list("ok" {q(category)})
                        else list("partial" "categoryCloseFailed")
                      )
                    )
                  )
                )
              )
            )
          )
        )
      )
    )
  )
)
""".strip()


def category_delete_skill(library: str, category: str) -> str:
    """Build SKILL that removes only a category and its memberships."""
    library = _require_text("library", library)
    category = _require_text("category", category)
    return f"""
let((vbLib vbExisting vbCat vbRemoved vbClosed vbVerify)
  vbLib = ddGetObj({q(library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else progn(
      vbExisting = ddCatOpen(vbLib {q(category)} "r")
      if(!vbExisting
        then list("error" "categoryNotFound")
        else progn(
          vbClosed = ddCatClose(vbExisting)
          if(!vbClosed
            then list("error" "categoryCloseFailed")
            else progn(
              vbCat = ddCatOpen(vbLib {q(category)} "a")
              if(!vbCat
                then list("error" "categoryReopenFailed")
                else progn(
                  vbRemoved = ddCatRemove(vbCat)
                  if(!vbRemoved
                    then progn(
                      vbClosed = ddCatClose(vbCat)
                      if(vbClosed
                        then list("error" "categoryDeleteFailed")
                        else list("error" "categoryCloseFailed")
                      )
                    )
                    else progn(
                      vbVerify = ddCatOpen(vbLib {q(category)} "r")
                      if(vbVerify
                        then progn(
                          vbClosed = ddCatClose(vbVerify)
                          if(vbClosed
                            then list("partial" "categoryDeleteVerificationFailed")
                            else list("partial" "categoryCloseFailed")
                          )
                        )
                        else list("ok")
                      )
                    )
                  )
              )
            )
          )
        )
      )
    )
  )
  )
)
""".strip()


def category_list_cells_skill(library: str, category: str) -> str:
    """Build SKILL that returns existing cell members of one category."""
    library = _require_text("library", library)
    category = _require_text("category", category)
    return f"""
let((vbLib vbCat vbMember vbCells vbClosed)
  vbLib = ddGetObj({q(library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else progn(
      vbCat = ddCatOpen(vbLib {q(category)} "r")
      if(!vbCat
        then list("error" "categoryNotFound")
        else progn(
          vbCells = nil
          foreach(vbMember ddCatGetCatMembers(vbCat)
            when(cadr(vbMember) == "cell"
              vbCells = cons(car(vbMember) vbCells)
            )
          )
          vbClosed = ddCatClose(vbCat)
          if(vbClosed
            then list("ok" reverse(vbCells))
            else list("error" "categoryCloseFailed")
          )
        )
      )
    )
  )
)
""".strip()


def category_add_cell_skill(library: str, category: str, cell: str) -> str:
    """Build SKILL that adds and verifies one cell membership."""
    return _category_change_cell_skill(library, category, cell, add=True)


def category_remove_cell_skill(library: str, category: str, cell: str) -> str:
    """Build SKILL that removes and verifies one cell membership."""
    return _category_change_cell_skill(library, category, cell, add=False)


def category_rename_skill(library: str, category: str, new_name: str) -> str:
    """Build SKILL that renames a flat category while preserving cells."""
    library = _require_text("library", library)
    category = _require_text("category", category)
    new_name = _require_text("new_name", new_name)
    return f"""
let((vbLib vbSource vbDestination vbExisting vbMember vbMembers vbDestinationMembers
     vbUnsupported vbAdded vbSaved vbClosed vbMatch vbRemoved vbVerify)
  vbLib = ddGetObj({q(library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else progn(
      vbSource = ddCatOpen(vbLib {q(category)} "r")
      if(!vbSource
        then list("error" "categoryNotFound")
        else progn(
          vbMembers = ddCatGetCatMembers(vbSource)
          vbUnsupported = nil
          foreach(vbMember vbMembers
            unless(cadr(vbMember) == "cell" vbUnsupported = t)
          )
          vbClosed = ddCatClose(vbSource)
          if(!vbClosed
            then list("error" "categoryCloseFailed")
            else if(vbUnsupported
              then list("error" "categoryContainsSubcategories")
              else progn(
                vbExisting = ddCatOpen(vbLib {q(new_name)} "r")
                if(vbExisting
                  then progn(
                    vbClosed = ddCatClose(vbExisting)
                    if(vbClosed
                      then list("error" "destinationCategoryExists")
                      else list("error" "categoryCloseFailed")
                    )
                  )
                  else progn(
                    vbDestination = ddCatOpenEx(vbLib {q(new_name)} "w" 1)
                    if(!vbDestination
                      then list("error" "categoryRenameCreateFailed")
                      else progn(
                        vbAdded = t
                        foreach(vbMember vbMembers
                          unless(ddCatAddItem(
                              vbDestination car(vbMember) cadr(vbMember))
                            vbAdded = nil
                          )
                        )
                        vbSaved = if(vbAdded
                          then ddCatSave(vbDestination)
                          else nil
                        )
                        vbClosed = ddCatClose(vbDestination)
                        if(!vbAdded || !vbSaved || !vbClosed
                          then list("partial" "categoryRenameDestinationFailed")
                          else progn(
                            vbVerify = ddCatOpen(vbLib {q(new_name)} "r")
                            if(!vbVerify
                              then list("partial" "categoryRenameVerificationFailed")
                              else progn(
                                vbDestinationMembers = ddCatGetCatMembers(vbVerify)
                                vbMatch = length(vbMembers) == length(vbDestinationMembers)
                                foreach(vbMember vbMembers
                                  unless(member(vbMember vbDestinationMembers)
                                    vbMatch = nil
                                  )
                                )
                                vbClosed = ddCatClose(vbVerify)
                                if(!vbMatch || !vbClosed
                                  then list("partial" "categoryRenameVerificationFailed")
                                  else progn(
                                    vbSource = ddCatOpen(vbLib {q(category)} "a")
                                    if(!vbSource
                                      then list("partial" "categoryRenameSourceReopenFailed")
                                      else progn(
                                        vbRemoved = ddCatRemove(vbSource)
                                        if(!vbRemoved
                                          then progn(
                                            vbClosed = ddCatClose(vbSource)
                                            list("partial" "categoryRenameSourceRemovalFailed")
                                          )
                                          else progn(
                                            vbVerify = ddCatOpen(vbLib {q(category)} "r")
                                            if(vbVerify
                                              then progn(
                                                vbClosed = ddCatClose(vbVerify)
                                                list("partial" "categoryRenameSourceVerificationFailed")
                                              )
                                              else list("ok" {q(new_name)})
                                            )
                                          )
                                        )
                                      )
                                    )
                                  )
                                )
                              )
                            )
                          )
                        )
                      )
                    )
                  )
                )
              )
            )
          )
        )
      )
    )
  )
)
""".strip()


def list_categories(client: Any, library: str, *, timeout: int = 30) -> list[str]:
    """Return existing top-level categories, filtering stale list entries."""
    operation = f"list categories in {library}"
    record = _execute_record(client, category_list_skill(library), timeout, operation)
    _raise_category_error(record, operation, library=library)
    return _string_list(_record_value(record, operation), operation)


def create_category(
    client: Any,
    library: str,
    category: str,
    *,
    timeout: int = 30,
) -> str:
    """Create and verify an empty persistent top-level category."""
    operation = f"create category {library}/{category}"
    record = _execute_record(client, category_create_skill(library, category), timeout, operation)
    _raise_category_error(record, operation, library=library, category=category)
    value = _record_value(record, operation)
    if not isinstance(value, str):
        raise RuntimeError(f"{operation} returned malformed category name")
    return value


def delete_category(
    client: Any,
    library: str,
    category: str,
    *,
    timeout: int = 30,
) -> None:
    """Delete a category and memberships without touching member cells."""
    operation = f"delete category {library}/{category}"
    record = _execute_record(client, category_delete_skill(library, category), timeout, operation)
    _raise_category_error(record, operation, library=library, category=category)


def list_category_cells(
    client: Any,
    library: str,
    category: str,
    *,
    timeout: int = 30,
) -> list[str]:
    """Return existing cell members of a top-level category."""
    operation = f"list category cells for {library}/{category}"
    record = _execute_record(
        client,
        category_list_cells_skill(library, category),
        timeout,
        operation,
    )
    _raise_category_error(record, operation, library=library, category=category)
    return _string_list(_record_value(record, operation), operation)


def add_cell_to_category(
    client: Any,
    library: str,
    category: str,
    cell: str,
    *,
    timeout: int = 30,
) -> None:
    """Add and verify one existing cell's category membership."""
    operation = f"add cell {library}/{cell} to category {category}"
    record = _execute_record(
        client,
        category_add_cell_skill(library, category, cell),
        timeout,
        operation,
    )
    _raise_category_error(record, operation, library=library, category=category)


def remove_cell_from_category(
    client: Any,
    library: str,
    category: str,
    cell: str,
    *,
    timeout: int = 30,
) -> None:
    """Remove and verify one existing cell's category membership."""
    operation = f"remove cell {library}/{cell} from category {category}"
    record = _execute_record(
        client,
        category_remove_cell_skill(library, category, cell),
        timeout,
        operation,
    )
    _raise_category_error(record, operation, library=library, category=category)


def rename_category(
    client: Any,
    library: str,
    category: str,
    new_name: str,
    *,
    timeout: int = 60,
) -> str:
    """Rename a flat category without merging or overwriting."""
    operation = f"rename category {library}/{category} to {new_name}"
    record = _execute_record(
        client,
        category_rename_skill(library, category, new_name),
        timeout,
        operation,
    )
    _raise_category_error(record, operation, library=library, category=category)
    value = _record_value(record, operation)
    if not isinstance(value, str):
        raise RuntimeError(f"{operation} returned malformed category name")
    return value


def _category_change_cell_skill(
    library: str,
    category: str,
    cell: str,
    *,
    add: bool,
) -> str:
    library = _require_text("library", library)
    category = _require_text("category", category)
    cell = _require_text("cell", cell)
    present_error = "cellAlreadyInCategory" if add else "cellNotInCategory"
    change = (
        f'ddCatAddItem(vbCat {q(cell)} "cell")'
        if add
        else f"ddCatSubItem(vbCat {q(cell)})"
    )
    expected = "t" if add else "nil"
    return f"""
let((vbLib vbCell vbCat vbMembers vbPresent vbChanged vbSaved vbClosed vbVerify)
  vbLib = ddGetObj({q(library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else progn(
      vbCell = member({q(cell)} vbLib~>cells~>name)
      if(!vbCell
        then list("error" "cellNotFound")
        else progn(
          vbCat = ddCatOpen(vbLib {q(category)} "r")
          if(!vbCat
            then list("error" "categoryNotFound")
            else progn(
              vbMembers = ddCatGetCatMembers(vbCat)
              vbPresent = if(member(list({q(cell)} "cell") vbMembers) t nil)
              vbClosed = ddCatClose(vbCat)
              if(!vbClosed
                then list("error" "categoryCloseFailed")
                else if(vbPresent == {expected}
                  then list("error" "{present_error}")
                  else progn(
                    vbCat = ddCatOpen(vbLib {q(category)} "a")
                    if(!vbCat
                      then list("error" "categoryReopenFailed")
                      else progn(
                        vbChanged = {change}
                        vbSaved = if(vbChanged then ddCatSave(vbCat) else nil)
                        vbClosed = ddCatClose(vbCat)
                        if(!vbChanged || !vbSaved || !vbClosed
                          then list("partial" "categoryMembershipChangeFailed")
                          else progn(
                            vbVerify = ddCatOpen(vbLib {q(category)} "r")
                            if(!vbVerify
                              then list("partial" "categoryMembershipVerificationFailed")
                              else progn(
                                vbMembers = ddCatGetCatMembers(vbVerify)
                                vbPresent = if(member(list({q(cell)} "cell") vbMembers) t nil)
                                vbClosed = ddCatClose(vbVerify)
                                if(vbClosed && vbPresent == {expected}
                                  then list("ok")
                                  else list("partial" "categoryMembershipVerificationFailed")
                                )
                              )
                            )
                          )
                        )
                      )
                    )
                  )
                )
              )
            )
          )
        )
      )
    )
  )
)
""".strip()


def _raise_category_error(
    record: list[Any],
    operation: str,
    *,
    library: str,
    category: str = "",
) -> None:
    if record[0] == "ok":
        return
    if record[0] == "partial":
        code = record[1] if len(record) > 1 else "unknownPartialFailure"
        raise CategoryPartialSuccessError(
            f"{operation} partially succeeded: {code}",
            library=library,
            category=category,
        )
    if record[0] != "error" or len(record) < 2 or not isinstance(record[1], str):
        raise RuntimeError(f"{operation} returned malformed operation status")
    code = record[1]
    messages = {
        "libraryNotFound": "library does not exist",
        "categoryNotFound": "category does not exist",
        "categoryExists": "category already exists",
        "destinationCategoryExists": "destination category already exists",
        "categoryContainsSubcategories": "category contains unsupported subcategories",
        "cellNotFound": "cell does not exist",
        "cellAlreadyInCategory": "cell is already in category",
        "cellNotInCategory": "cell is not in category",
    }
    raise RuntimeError(f"{operation} failed: {messages.get(code, code)}")


def _string_list(value: Any, operation: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"{operation} returned malformed names")
    return value


__all__ = [
    "CategoryPartialSuccessError",
    "add_cell_to_category",
    "category_add_cell_skill",
    "category_create_skill",
    "category_delete_skill",
    "category_list_cells_skill",
    "category_list_skill",
    "category_remove_cell_skill",
    "category_rename_skill",
    "create_category",
    "delete_category",
    "list_categories",
    "list_category_cells",
    "remove_cell_from_category",
    "rename_category",
]
