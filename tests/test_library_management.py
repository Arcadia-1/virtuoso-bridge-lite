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

_DEMO_INFO = '("ok" ("library" "demoLib" "/work/demoLib" "gpdk045"))'


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


def test_library_get_skill_escapes_library_name() -> None:
    assert 'ddGetObj("demo\\"lib")' in library_get_skill('demo"lib')


@pytest.mark.parametrize(
    ("technology", "tech_expr"),
    [(None, "vbTechName = nil"), ("gpdk045", 'vbTechName = "gpdk045"')],
)
def test_library_create_skill_uses_explicit_path_and_verifies_binding(
    technology: str | None,
    tech_expr: str,
) -> None:
    skill = library_create_skill("demoLib", "/tmp/demo lib", technology_library=technology)

    assert 'ddCreateLib("demoLib" "/tmp/demo lib")' in skill
    assert tech_expr in skill
    assert "techGetTechLibName(vbLib) == vbTechName" in skill


@pytest.mark.parametrize(
    "build",
    [
        lambda: library_create_skill("demoLib", "/tmp/demoLib", technology_library="gpdk045"),
        lambda: library_delete_skill("demoLib"),
        lambda: library_rename_skill("demoLib", "renamedLib"),
        lambda: library_set_technology_skill("demoLib", "gpdk045"),
    ],
    ids=["create", "delete", "rename", "set_technology"],
)
def test_library_mutations_have_no_filesystem_fallback(build) -> None:
    skill = build()

    for forbidden in ("system(", "renameFile(", "deleteDir", "deleteFile"):
        assert forbidden not in skill


def test_library_delete_skill_verifies_library_is_gone() -> None:
    skill = library_delete_skill("demoLib")

    assert "vbDeleted = ddDeleteObj(vbLib)" in skill
    assert 'if(vbDeleted && !ddGetObj("demoLib")' in skill


def test_library_rename_skill_uses_ccp_rename_without_overwrite() -> None:
    skill = library_rename_skill("demoLib", "renamedLib")

    assert "ccpRename(vbSource vbDestination nil)" in skill
    assert '(vbRenamed && vbLib && !ddGetObj("demoLib")' in skill


def test_library_rename_skill_removes_only_an_empty_destination_it_created() -> None:
    skill = library_rename_skill("demoLib", "renamedLib")

    # The destination is checked to be absent before ccpRename runs, so a
    # destination present afterwards was created by this failed rename.
    assert skill.index('if(ddGetObj("renamedLib")') < skill.index("ccpRename(")
    assert "car(errset(ccpRename(vbSource vbDestination nil) t))" in skill
    rollback = skill[skill.index("(vbLib && ddGetObj"):skill.index('list("error" "renameFailed"))')]
    assert '!vbLib~>cells' in rollback
    assert "vbLib~>readPath != vbSourcePath" in rollback
    assert rollback.index("!vbLib~>cells") < rollback.index("ddDeleteObj(vbLib)")
    assert '!ddGetObj("renamedLib")' in rollback
    # Any other state with both names defined is reported, not repaired.
    assert skill.count('(vbLib && ddGetObj("demoLib")') == 2
    assert 'list("error" "renameIncomplete")' in skill


def test_library_set_technology_uses_bind_for_new_and_set_for_existing() -> None:
    skill = library_set_technology_skill("demoLib", "gpdk045")

    assert 'then techSetTechLibName(vbLib "gpdk045")' in skill
    assert 'else techBindTechFile(vbLib "gpdk045")' in skill
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


@pytest.mark.parametrize(
    ("call", "output", "expected"),
    [
        (
            lambda client: list_libraries(client, timeout=17),
            '("ok" ("analogLib" "basic" "demoLib"))',
            ["analogLib", "basic", "demoLib"],
        ),
        (
            lambda client: get_library(client, "demoLib", timeout=17),
            _DEMO_INFO,
            LibraryInfo("demoLib", "/work/demoLib", "gpdk045"),
        ),
        (
            lambda client: get_library(client, "demoLib", timeout=17),
            '("ok" ("library" "demoLib" "/work/demoLib" nil))',
            LibraryInfo("demoLib", "/work/demoLib", None),
        ),
        (
            lambda client: create_library(
                client,
                "demoLib",
                "/work/demoLib",
                technology_library="gpdk045",
                timeout=17,
            ),
            _DEMO_INFO,
            LibraryInfo("demoLib", "/work/demoLib", "gpdk045"),
        ),
        (
            lambda client: delete_library(client, "demoLib", timeout=17),
            '("ok")',
            None,
        ),
        (
            lambda client: rename_library(client, "oldLib", "newLib", timeout=17),
            '("ok" ("library" "newLib" "/work/newLib" "gpdk045"))',
            LibraryInfo("newLib", "/work/newLib", "gpdk045"),
        ),
        (
            lambda client: set_technology_library(client, "demoLib", "gpdk045", timeout=17),
            _DEMO_INFO,
            "gpdk045",
        ),
    ],
    ids=["list", "get", "get_unbound", "create", "delete", "rename", "set_technology"],
)
def test_library_operations_return_verified_results(call, output: str, expected) -> None:
    client = _Client(output)

    assert call(client) == expected
    assert client.timeout == 17


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


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ('("error" "libraryNotFound")', "does not exist"),
        ('("error" "libraryExists")', "already exists"),
        ('("error" "destinationExists")', "destination library already exists"),
        ('("error" "technologyLibraryNotFound")', "technology library does not exist"),
        ('("error" "renameFailed")', "renameFailed"),
        ('("error" "renameIncomplete")', "left both libraries defined"),
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
    client = _Client(_DEMO_INFO)
    ops = LibraryOps(client)  # type: ignore[arg-type]

    assert ops.get_technology_library("demoLib") == "gpdk045"
    assert client.skill == library_get_skill("demoLib")
