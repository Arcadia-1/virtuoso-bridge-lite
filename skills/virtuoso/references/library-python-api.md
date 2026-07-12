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

## Return and error contract

- `list()` returns `list[str]`.
- `get()`, `create()`, and `rename()` return `LibraryInfo`.
- `get_technology_library()` returns `str | None`.
- `set_technology_library()` returns the verified technology library name.
- `delete()` returns `None` after verified success.
- Empty required strings raise `ValueError`.
- Missing objects, name conflicts, transport failures, Cadence failures, and
  verification failures raise `RuntimeError`.
