from __future__ import annotations

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.library import LibraryInfo, LibraryOps
from virtuoso_bridge.virtuoso.library.management import (
    LibraryPartialSuccessError,
    create_library,
    delete_library,
    get_library,
    library_create_skill,
    library_delete_skill,
    library_get_skill,
    library_list_skill,
    library_rename_skill,
    library_set_technology_skill,
    list_libraries,
    rename_library,
    set_technology_library,
)


class _Client:
    def __init__(self, output: str, *, status: object = None, errors=None) -> None:
        self.output = output
        self.status = status
        self.errors = errors or []
        self.skill: str | None = None
        self.timeout: int | None = None

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        return VirtuosoResult(
            status=self.status or ExecutionStatus.SUCCESS,
            output=self.output,
            errors=self.errors,
        )


def test_virtuoso_client_attaches_library_ops() -> None:
    client = VirtuosoClient.local()

    assert isinstance(client.library, LibraryOps)


def test_library_list_skill_uses_visible_library_database() -> None:
    skill = library_list_skill()

    assert "ddGetLibList()" in skill
    assert "ddUpdateLibList" not in skill
    assert "getDirFiles" not in skill


def test_library_get_skill_reads_path_and_technology_binding() -> None:
    skill = library_get_skill('demo"lib')

    assert 'ddGetObj("demo\\"lib")' in skill
    assert "vbLib~>readPath" in skill
    assert "techGetTechLibName(vbLib)" in skill
    assert 'list("error" "libraryNotFound")' in skill


def test_library_create_skill_requires_explicit_path_and_uses_supported_apis() -> None:
    skill = library_create_skill(
        "demoLib",
        "/tmp/demo lib",
        technology_library="gpdk045",
    )

    assert 'ddCreateLib("demoLib" "/tmp/demo lib")' in skill
    assert 'vbTechName = "gpdk045"' in skill
    assert "techBindTechFile(vbLib vbTechName)" in skill
    assert "techGetTechLibName(vbLib) == vbTechName" in skill
    assert 'list("partial" "technologyBindingFailed"' in skill
    assert "system(" not in skill
    assert "renameFile(" not in skill


def test_library_create_skill_without_technology_does_not_invent_binding() -> None:
    skill = library_create_skill("demoLib", "/tmp/demoLib")

    assert "vbTechName = nil" in skill
    assert "cdsDefTechLib" not in skill


def test_library_delete_skill_uses_dd_delete_obj_without_force_fallback() -> None:
    skill = library_delete_skill("demoLib")

    assert "vbDeleted = ddDeleteObj(vbLib)" in skill
    assert '!ddGetObj("demoLib")' in skill
    assert "deleteDir" not in skill
    assert "system(" not in skill


def test_library_rename_skill_uses_ccp_rename_without_overwrite() -> None:
    skill = library_rename_skill("demoLib", "renamedLib")

    assert 'gdmCreateSpec("demoLib" "" "" "" "CDBA")' in skill
    assert 'gdmCreateSpec("renamedLib" "" "" "" "CDBA")' in skill
    assert "ccpRename(vbSource vbDestination nil)" in skill
    assert 'ddGetObj("renamedLib")' in skill
    assert '!ddGetObj("demoLib")' in skill
    assert "renameFile(" not in skill


def test_library_set_technology_uses_bind_for_new_and_set_for_existing() -> None:
    skill = library_set_technology_skill("demoLib", "gpdk045")

    assert "vbCurrent = techGetTechLibName(vbLib)" in skill
    assert 'techSetTechLibName(vbLib "gpdk045")' in skill
    assert 'techBindTechFile(vbLib "gpdk045")' in skill
    assert 'techGetTechLibName(vbLib) == "gpdk045"' in skill


@pytest.mark.parametrize(
    ("builder", "args", "field"),
    [
        (library_get_skill, ("",), "name"),
        (library_create_skill, ("demo", ""), "path"),
        (library_delete_skill, (" ",), "name"),
        (library_rename_skill, ("demo", ""), "new_name"),
        (library_set_technology_skill, ("demo", ""), "technology_library"),
    ],
)
def test_library_skill_builders_reject_empty_required_values(builder, args, field) -> None:
    with pytest.raises(ValueError, match=field):
        builder(*args)


def test_list_libraries_returns_names_and_forwards_timeout() -> None:
    client = _Client('("ok" ("analogLib" "basic" "demoLib"))')

    result = list_libraries(client, timeout=17)

    assert result == ["analogLib", "basic", "demoLib"]
    assert client.timeout == 17


def test_get_library_returns_structured_info() -> None:
    client = _Client('("ok" ("library" "demoLib" "/work/demoLib" "gpdk045"))')

    info = get_library(client, "demoLib")

    assert info == LibraryInfo("demoLib", "/work/demoLib", "gpdk045")


def test_get_library_preserves_unbound_technology_as_none() -> None:
    client = _Client('("ok" ("library" "demoLib" "/work/demoLib" nil))')

    info = get_library(client, "demoLib")

    assert info.technology_library is None


def test_create_library_returns_verified_info() -> None:
    client = _Client('("ok" ("library" "demoLib" "/tmp/demoLib" "gpdk045"))')

    info = create_library(
        client,
        "demoLib",
        "/tmp/demoLib",
        technology_library="gpdk045",
    )

    assert info == LibraryInfo("demoLib", "/tmp/demoLib", "gpdk045")
    assert client.skill is not None
    assert 'ddCreateLib("demoLib" "/tmp/demoLib")' in client.skill


def test_create_library_reports_partial_technology_binding() -> None:
    client = _Client(
        '("partial" "technologyBindingFailed" '
        '("library" "demoLib" "/tmp/demoLib" nil))'
    )

    with pytest.raises(LibraryPartialSuccessError) as exc_info:
        create_library(
            client,
            "demoLib",
            "/tmp/demoLib",
            technology_library="gpdk045",
        )

    assert exc_info.value.library == LibraryInfo("demoLib", "/tmp/demoLib", None)
    assert "was created" in str(exc_info.value)


def test_delete_library_returns_none_on_verified_success() -> None:
    client = _Client('("ok")')

    assert delete_library(client, "demoLib") is None


def test_rename_library_returns_destination_info() -> None:
    client = _Client('("ok" ("library" "newLib" "/work/newLib" "gpdk045"))')

    info = rename_library(client, "oldLib", "newLib")

    assert info.name == "newLib"
    assert info.path == "/work/newLib"


def test_set_technology_library_returns_verified_name() -> None:
    client = _Client('("ok" ("library" "demoLib" "/work/demoLib" "gpdk045"))')

    result = set_technology_library(client, "demoLib", "gpdk045")

    assert result == "gpdk045"


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ('("error" "libraryNotFound")', "does not exist"),
        ('("error" "libraryExists")', "already exists"),
        ('("error" "destinationExists")', "destination library already exists"),
        ('("error" "technologyLibraryNotFound")', "technology library does not exist"),
        ('("error" "renameFailed")', "renameFailed"),
    ],
)
def test_library_operations_raise_explicit_operation_errors(output: str, message: str) -> None:
    client = _Client(output)

    with pytest.raises(RuntimeError, match=message):
        get_library(client, "demoLib")


@pytest.mark.parametrize("output", ["", "t", '("ok"', '("ok") trailing'])
def test_library_operations_reject_malformed_structured_output(output: str) -> None:
    with pytest.raises(RuntimeError, match="malformed structured output"):
        list_libraries(_Client(output))


def test_library_operations_raise_transport_errors() -> None:
    client = _Client("", status=ExecutionStatus.ERROR, errors=["daemon failed"])

    with pytest.raises(RuntimeError, match="SKILL error: daemon failed"):
        list_libraries(client)


def test_library_ops_methods_delegate_to_owner() -> None:
    client = _Client('("ok" ("library" "demoLib" "/work/demoLib" "gpdk045"))')
    ops = LibraryOps(client)  # type: ignore[arg-type]

    assert ops.get_technology_library("demoLib") == "gpdk045"
    assert client.skill is not None
    assert 'ddGetObj("demoLib")' in client.skill
