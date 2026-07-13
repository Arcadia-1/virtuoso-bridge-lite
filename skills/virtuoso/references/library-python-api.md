# Library Python API

Library management is attached to `VirtuosoClient` as `client.library`.
These methods use supported Cadence SKILL APIs and verify the resulting
Virtuoso library state before returning.

## Read libraries

```python
names = client.library.list()
info = client.library.get("MY_LIB")

print(info.name)
print(info.path)
print(info.technology_library)  # str or None
```

`list()` reads the libraries already visible in the current Virtuoso session.
It does not call `ddUpdateLibList()` or scan the filesystem.

## Create a library

The remote library path is required. The API never chooses a path from the
local working directory.

```python
info = client.library.create(
    "MY_LIB",
    "/remote/work/MY_LIB",
    technology_library="TECH_LIB",
)
```

Creation uses `ddCreateLib`. When `technology_library` is supplied, the new
library is bound with `techBindTechFile` and the binding is read back with
`techGetTechLibName`.

If creation succeeds but technology binding fails,
`LibraryPartialSuccessError` is raised. Its `library` attribute contains the
created library's verified state. The API does not silently delete that
library.

## Change technology binding

```python
current = client.library.get_technology_library("MY_LIB")
bound = client.library.set_technology_library("MY_LIB", "OTHER_TECH_LIB")
```

An unbound library is attached with `techBindTechFile`. An existing binding is
changed with `techSetTechLibName`. The target technology library must already
exist; this API does not create or copy technology data.

## Rename and delete

```python
renamed = client.library.rename("MY_LIB", "MY_RENAMED_LIB")
client.library.delete("MY_RENAMED_LIB")
```

Rename uses `ccpRename` with overwrite disabled. Delete uses `ddDeleteObj`.
There is no `force` option and no Python-side filesystem fallback. Both
operations raise `RuntimeError` when Cadence rejects the operation or the
post-operation state does not match.

## Manage top-level categories

Categories are flat in this API. `Everything` and `Uncategorized` are Library
Manager views, not persisted categories, and are not returned.

```python
categories = client.library.list_categories("MY_LIB")
created = client.library.create_category("MY_LIB", "ADC")
cells = client.library.list_category_cells("MY_LIB", "ADC")

client.library.add_cell_to_category("MY_LIB", "ADC", "comparator")
client.library.remove_cell_from_category("MY_LIB", "ADC", "comparator")

renamed = client.library.rename_category("MY_LIB", "ADC", "Comparators")
client.library.delete_category("MY_LIB", "Comparators")
```

Category operations use `ddCatOpenEx`, `ddCatOpen`, `ddCatGetLibCats`,
`ddCatGetCatMembers`, `ddCatAddItem`, `ddCatSubItem`, `ddCatSave`,
`ddCatRemove`, and `ddCatClose`.

- Empty categories are created with the `keepEmpty` flag and may remain hidden
  in some Library Manager views until a cell is added.
- `list_categories()` opens every returned name and filters stale entries that
  no longer have a category file.
- `list_category_cells()` returns only existing members whose type is `cell`.
- Adding a cell that is already a member, or removing a cell that is not a
  member, raises `RuntimeError` without changing the category.
- Category deletion removes category membership data only; it never deletes
  member cells.
- Category rename refuses a destination that already exists. Because the
  supported `ddCat` API has no direct rename call, the implementation copies
  verified cell memberships to the new category and then removes the old one.
- Categories containing subcategories cannot be renamed by this flat API.
- No category operation edits `.Cat` or `.TopCat` files directly.

If a multi-step category mutation may have persisted only part of its change,
`CategoryPartialSuccessError` reports the affected library and category. No
automatic rollback or filesystem fallback is attempted.

## Return and error contract

- `list()` returns `list[str]`.
- `get()`, `create()`, and `rename()` return `LibraryInfo`.
- `get_technology_library()` returns `str | None`.
- `set_technology_library()` returns the verified technology library name.
- Category list methods return `list[str]`.
- `create_category()` and `rename_category()` return the verified category name.
- Category membership mutations and `delete_category()` return `None`.
- `delete()` returns `None` after verified success.
- Empty required strings raise `ValueError`.
- Missing objects, name conflicts, transport failures, Cadence failures, and
  verification failures raise `RuntimeError`.
