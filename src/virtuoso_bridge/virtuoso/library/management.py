"""Library management through supported Cadence SKILL APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from virtuoso_bridge.virtuoso.ops import q
from virtuoso_bridge.virtuoso.response import response_fields
from virtuoso_bridge.virtuoso.skill_output import (
    is_single_complete_skill_list,
    parse_sexpr,
)


@dataclass(frozen=True)
class LibraryInfo:
    """Verified Virtuoso library identity and technology binding."""

    name: str
    path: str
    technology_library: str | None


class LibraryPartialSuccessError(RuntimeError):
    """Raised when a library was created but a requested binding failed."""

    def __init__(self, message: str, library: LibraryInfo) -> None:
        self.library = library
        super().__init__(message)


def library_list_skill() -> str:
    """Build SKILL that lists libraries visible in the current session."""
    return 'list("ok" mapcar(lambda((vbLib) vbLib~>name) ddGetLibList()))'


def library_get_skill(name: str) -> str:
    """Build SKILL that returns one library's path and technology binding."""
    name = _require_text("name", name)
    return (
        "let((vbLib) "
        f"vbLib = ddGetObj({q(name)}) "
        "if(vbLib "
        f"then list(\"ok\" {_library_info_expr('vbLib')}) "
        'else list("error" "libraryNotFound")))'
    )


def library_create_skill(
    name: str,
    path: str,
    *,
    technology_library: str | None = None,
) -> str:
    """Build SKILL that creates a library and optionally binds technology."""
    name = _require_text("name", name)
    path = _require_text("path", path)
    technology = (
        "nil"
        if technology_library is None
        else q(_require_text("technology_library", technology_library))
    )
    info = _library_info_expr("vbLib")
    return f"""
let((vbLib vbTechName vbBound)
  vbTechName = {technology}
  if(ddGetObj({q(name)})
    then list("error" "libraryExists")
    else if(vbTechName && !ddGetObj(vbTechName)
      then list("error" "technologyLibraryNotFound")
      else progn(
        vbLib = ddCreateLib({q(name)} {q(path)})
        if(!vbLib
          then list("error" "createFailed")
          else if(!vbTechName
            then list("ok" {info})
            else progn(
              vbBound = techBindTechFile(vbLib vbTechName)
              if(vbBound && techGetTechLibName(vbLib) == vbTechName
                then list("ok" {info})
                else list("partial" "technologyBindingFailed" {info})
              )
            )
          )
        )
      )
    )
  )
)
""".strip()


def library_delete_skill(name: str) -> str:
    """Build SKILL that deletes a library through ``ddDeleteObj``."""
    name = _require_text("name", name)
    return (
        "let((vbLib vbDeleted) "
        f"vbLib = ddGetObj({q(name)}) "
        "if(!vbLib "
        'then list("error" "libraryNotFound") '
        "else progn("
        "vbDeleted = ddDeleteObj(vbLib) "
        f"if(vbDeleted && !ddGetObj({q(name)}) "
        'then list("ok") '
        'else list("error" "deleteFailed")))))'
    )


def library_rename_skill(name: str, new_name: str) -> str:
    """Build SKILL that renames a library through ``ccpRename``."""
    name = _require_text("name", name)
    new_name = _require_text("new_name", new_name)
    return f"""
let((vbSource vbDestination vbRenamed vbLib)
  if(!ddGetObj({q(name)})
    then list("error" "libraryNotFound")
    else if(ddGetObj({q(new_name)})
      then list("error" "destinationExists")
      else progn(
        vbSource = gdmCreateSpec({q(name)} "" "" "" "CDBA")
        vbDestination = gdmCreateSpec({q(new_name)} "" "" "" "CDBA")
        if(!vbSource || !vbDestination
          then list("error" "renameSpecFailed")
          else progn(
            vbRenamed = ccpRename(vbSource vbDestination nil)
            vbLib = ddGetObj({q(new_name)})
            if(vbRenamed && vbLib && !ddGetObj({q(name)})
              then list("ok" {_library_info_expr("vbLib")})
              else list("error" "renameFailed")
            )
          )
        )
      )
    )
  )
)
""".strip()


def library_set_technology_skill(name: str, technology_library: str) -> str:
    """Build SKILL that binds or changes a library's technology library."""
    name = _require_text("name", name)
    technology_library = _require_text("technology_library", technology_library)
    return f"""
let((vbLib vbTechLib vbCurrent vbChanged)
  vbLib = ddGetObj({q(name)})
  vbTechLib = ddGetObj({q(technology_library)})
  if(!vbLib
    then list("error" "libraryNotFound")
    else if(!vbTechLib
      then list("error" "technologyLibraryNotFound")
      else progn(
        vbCurrent = techGetTechLibName(vbLib)
        vbChanged = if(vbCurrent
          then techSetTechLibName(vbLib {q(technology_library)})
          else techBindTechFile(vbLib {q(technology_library)})
        )
        if(vbChanged && techGetTechLibName(vbLib) == {q(technology_library)}
          then list("ok" {_library_info_expr("vbLib")})
          else list("error" "technologyBindingFailed")
        )
      )
    )
  )
)
""".strip()


def list_libraries(client: Any, *, timeout: int = 30) -> list[str]:
    """Return library names visible in the current Virtuoso session."""
    record = _execute_record(client, library_list_skill(), timeout, "list libraries")
    _raise_record_error(record, "list libraries")
    values = record[1] if len(record) > 1 else []
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise RuntimeError("list libraries returned malformed library names")
    return values


def get_library(client: Any, name: str, *, timeout: int = 30) -> LibraryInfo:
    """Return verified information for one Virtuoso library."""
    record = _execute_record(client, library_get_skill(name), timeout, f"get library {name}")
    _raise_record_error(record, f"get library {name}", name=name)
    return _library_info_from_record(_record_value(record, f"get library {name}"))


def create_library(
    client: Any,
    name: str,
    path: str,
    *,
    technology_library: str | None = None,
    timeout: int = 60,
) -> LibraryInfo:
    """Create and verify a library, optionally binding existing technology."""
    record = _execute_record(
        client,
        library_create_skill(name, path, technology_library=technology_library),
        timeout,
        f"create library {name}",
    )
    if record and record[0] == "partial":
        info = _library_info_from_record(_record_value(record, f"create library {name}", 2))
        raise LibraryPartialSuccessError(
            f"library {name!r} was created but technology binding failed",
            info,
        )
    _raise_record_error(record, f"create library {name}", name=name)
    return _library_info_from_record(_record_value(record, f"create library {name}"))


def delete_library(client: Any, name: str, *, timeout: int = 60) -> None:
    """Delete and verify a library through Cadence's supported API."""
    record = _execute_record(client, library_delete_skill(name), timeout, f"delete library {name}")
    _raise_record_error(record, f"delete library {name}", name=name)


def rename_library(
    client: Any,
    name: str,
    new_name: str,
    *,
    timeout: int = 120,
) -> LibraryInfo:
    """Rename and verify a library without overwrite semantics."""
    record = _execute_record(
        client,
        library_rename_skill(name, new_name),
        timeout,
        f"rename library {name} to {new_name}",
    )
    _raise_record_error(record, f"rename library {name} to {new_name}", name=name)
    return _library_info_from_record(
        _record_value(record, f"rename library {name} to {new_name}")
    )


def set_technology_library(
    client: Any,
    name: str,
    technology_library: str,
    *,
    timeout: int = 60,
) -> str:
    """Bind or change a library's technology library and verify the result."""
    record = _execute_record(
        client,
        library_set_technology_skill(name, technology_library),
        timeout,
        f"set technology library for {name}",
    )
    _raise_record_error(record, f"set technology library for {name}", name=name)
    info = _library_info_from_record(
        _record_value(record, f"set technology library for {name}")
    )
    if info.technology_library is None:
        raise RuntimeError(f"set technology library for {name} returned no binding")
    return info.technology_library


def _execute_record(client: Any, skill: str, timeout: int, operation: str) -> list[Any]:
    response = client.execute_skill(skill, timeout=timeout)
    errors, status, output = response_fields(response)
    if errors:
        raise RuntimeError(f"{operation} SKILL error: {errors[0]}")
    status_value = getattr(status, "value", status)
    if status_value is not None and str(status_value).lower() not in {"success", "ok"}:
        detail = output or f"status={status_value}"
        raise RuntimeError(f"{operation} SKILL error: {detail}")
    text = (output or "").strip()
    if not text or not is_single_complete_skill_list(text):
        raise RuntimeError(f"{operation} returned malformed structured output")
    try:
        parsed = parse_sexpr(text)
    except ValueError as exc:
        raise RuntimeError(f"{operation} returned malformed structured output") from exc
    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], str):
        raise RuntimeError(f"{operation} returned malformed structured output")
    return parsed


def _raise_record_error(record: list[Any], operation: str, *, name: str = "") -> None:
    if record[0] == "ok":
        return
    if record[0] != "error" or len(record) < 2 or not isinstance(record[1], str):
        raise RuntimeError(f"{operation} returned malformed operation status")
    code = record[1]
    if code == "libraryNotFound":
        raise RuntimeError(f"library {name!r} does not exist")
    if code == "libraryExists":
        raise RuntimeError(f"library {name!r} already exists")
    if code == "destinationExists":
        raise RuntimeError(f"{operation} failed: destination library already exists")
    if code == "technologyLibraryNotFound":
        raise RuntimeError(f"{operation} failed: technology library does not exist")
    raise RuntimeError(f"{operation} failed: {code}")


def _record_value(record: list[Any], operation: str, index: int = 1) -> Any:
    if len(record) <= index:
        raise RuntimeError(f"{operation} returned no result value")
    return record[index]


def _library_info_from_record(record: Any) -> LibraryInfo:
    if (
        not isinstance(record, list)
        or len(record) != 4
        or record[0] != "library"
        or not isinstance(record[1], str)
        or not isinstance(record[2], str)
        or (record[3] is not None and not isinstance(record[3], str))
    ):
        raise RuntimeError("library operation returned malformed library information")
    return LibraryInfo(record[1], record[2], record[3])


def _library_info_expr(variable: str) -> str:
    return (
        f'list("library" {variable}~>name {variable}~>readPath '
        f"techGetTechLibName({variable}))"
    )


def _require_text(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


__all__ = [
    "LibraryInfo",
    "LibraryPartialSuccessError",
    "create_library",
    "delete_library",
    "get_library",
    "library_create_skill",
    "library_delete_skill",
    "library_get_skill",
    "library_list_skill",
    "library_rename_skill",
    "library_set_technology_skill",
    "list_libraries",
    "rename_library",
    "set_technology_library",
]
