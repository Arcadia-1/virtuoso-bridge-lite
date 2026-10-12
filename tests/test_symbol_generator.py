from __future__ import annotations

import re
import subprocess
import sys

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.symbol import (
    SymbolGenerationResult,
    SymbolOps,
    symbol_generate_from_schematic_skill,
)

_CREATED_AY = '("generated" "created" (("A" "input" 1) ("Y" "output" 1)) ("A" "Y"))'


class _Client:
    def __init__(self, output: str = "", *, errors: list[str] | None = None) -> None:
        self.result = VirtuosoResult(
            status=ExecutionStatus.ERROR if errors else ExecutionStatus.SUCCESS,
            output=output,
            errors=errors or [],
        )
        self.calls: list[tuple[str, int]] = []

    def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
        self.calls.append((skill, timeout))
        return self.result


def _matching_parenthesis(text: str, open_index: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_index, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unbalanced generated SKILL")


def _assert_ordered(skill: str, *fragments: str) -> None:
    positions = [skill.index(fragment) for fragment in fragments]
    assert positions == sorted(positions), fragments


def _full_skill() -> str:
    return symbol_generate_from_schematic_skill(
        "demoLib",
        "nand2",
        sort_pins="geometric",
        overwrite=True,
    )


@pytest.mark.parametrize(
    ("output", "action", "terminal_names", "pin_order"),
    [
        (_CREATED_AY, "created", ("A", "Y"), ("A", "Y")),
        ('("generated" "replaced" (("A" "input" 1)) ("A"))', "replaced", ("A",), ("A",)),
        ('("generated" "created" nil nil)', "created", (), ()),
    ],
)
def test_generate_from_schematic_returns_result_from_single_verified_call(
    output: str,
    action: str,
    terminal_names: tuple[str, ...],
    pin_order: tuple[str, ...],
) -> None:
    client = _Client(output)

    result = SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert result == SymbolGenerationResult(
        lib="demoLib",
        cell="nand2",
        schematic_view="schematic",
        symbol_view="symbol",
        action=action,  # type: ignore[arg-type]
        terminal_names=terminal_names,
        pin_order=pin_order,
    )
    assert len(client.calls) == 1


def test_generate_from_schematic_forwards_views_and_options() -> None:
    client = _Client('("generated" "replaced" (("A" "input" 1)) ("A"))')

    result = SymbolOps(client).generate_from_schematic(
        "demoLib",
        "nand2",
        schematic_view="schematic_alt",
        symbol_view="symbol_alt",
        sort_pins="geometric",
        overwrite=True,
        timeout=17,
    )

    skill, timeout = client.calls[0]
    assert (result.schematic_view, result.symbol_view) == ("schematic_alt", "symbol_alt")
    assert timeout == 17
    assert 'schSchemToPinList("demoLib" "nand2" "schematic_alt")' in skill
    assert 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol_alt" nil nil t)' in skill
    assert 'schSetEnv("ssgSortPins" "geometric")' in skill


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol_view": "schematic"}, "schematic_view and symbol_view must differ"),
        ({"sort_pins": "alphabetic"}, "sort_pins must be one of: alphanumeric, geometric"),
        ({"sort_pins": "GEOMETRIC"}, "sort_pins must be one of: alphanumeric, geometric"),
        ({"sort_pins": ""}, "sort_pins must be one of: alphanumeric, geometric"),
    ],
)
def test_symbol_generation_skill_rejects_invalid_options(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        symbol_generate_from_schematic_skill("demoLib", "nand2", **kwargs)


def test_symbol_generation_skill_escapes_every_name() -> None:
    skill = symbol_generate_from_schematic_skill(
        'demo"\\Lib',
        'nand"\\2',
        schematic_view='schem"\\atic',
        symbol_view='sym"\\bol',
        sort_pins="geometric",
        overwrite=True,
    )
    lib_cell = '"demo\\"\\\\Lib" "nand\\"\\\\2"'

    assert f'ddGetObj({lib_cell} "sym\\"\\\\bol")' in skill
    assert f'dbOpenCellViewByType({lib_cell} "schem\\"\\\\atic" "schematic" "r")' in skill
    assert f'schSchemToPinList({lib_cell} "schem\\"\\\\atic")' in skill
    assert f'dbCopyCellView(vbTempCv {lib_cell} "sym\\"\\\\bol" nil nil t)' in skill
    assert _matching_parenthesis(skill, skill.index("(")) == len(skill) - 1


@pytest.mark.parametrize(("overwrite", "flag"), [(False, "nil"), (True, "t")])
def test_symbol_generation_skill_validates_temporary_view_before_install(
    overwrite: bool,
    flag: str,
) -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2", overwrite=overwrite)

    temp_match = re.search(
        r'schPinListToSymbol\("demoLib" "nand2" "(__vb_symbol_[0-9a-f]+)" vbPinList\)',
        skill,
    )
    assert temp_match is not None
    assert f'when(vbReplacing && !{flag} error("target symbol exists"))' in skill
    _assert_ordered(
        skill,
        f'"{temp_match.group(1)}" "schematicSymbol" "r")',
        'error("generated symbol terminals mismatch")',
        'error("generated symbol pin order mismatch")',
        f'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol" nil nil {flag})',
    )


def test_symbol_generation_skill_leaves_pin_sort_unchanged_when_not_requested() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    assert "schGetEnv" not in skill
    assert "schSetEnv" not in skill


def test_symbol_generation_skill_restores_pin_sort_before_modifying_destination() -> None:
    skill = _full_skill()

    _assert_ordered(
        skill,
        'vbOldSort = schGetEnv("ssgSortPins")',
        "unwindProtect(",
        'schSetEnv("ssgSortPins" "geometric")',
        "schSchemToPinList",
        'schSetEnv("ssgSortPins" vbOldSort)',
        'error("target symbol is open")',
        "dbCopyCellView(vbBackupSourceCv",
    )


def test_symbol_generation_skill_backs_up_before_replacing_and_rolls_back() -> None:
    skill = _full_skill()

    backup_match = re.search(r'"(__vb_symbol_backup_[0-9a-f]+)"', skill)
    assert backup_match is not None
    backup_view = backup_match.group(1)
    _assert_ordered(
        skill,
        'when(dbFindOpenCellViewByName("demoLib" "nand2" "symbol") error("target symbol is open"))',
        f'dbCopyCellView(vbBackupSourceCv "demoLib" "nand2" "{backup_view}" nil nil nil)',
        'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol" nil nil t)',
        "vbFinalOrder = schGetPinOrder(vbTargetCv)",
        "vbCommitOk = t",
        'dbCopyCellView(vbBackupCv "demoLib" "nand2" "symbol" nil nil t)',
    )
    assert f'"target symbol rollback failed; backup retained as %s" "{backup_view}"' in skill


def test_symbol_generation_skill_rolls_back_after_temporary_cleanup_failure() -> None:
    skill = _full_skill()

    rollback_start = skill.index("when(!vbCommitOk || vbCleanupFailures ")
    rollback_end = _matching_parenthesis(skill, skill.index("(", rollback_start))
    temp_cleanup = skill.rfind("vbTempObj = ddGetObj", 0, rollback_start)
    backup_cleanup = skill.index("when(vbBackupReady &&", rollback_end)

    assert 0 <= temp_cleanup < rollback_start < rollback_end < backup_cleanup
    assert "(vbCommitOk && !vbCleanupFailures)" in skill[backup_cleanup:]
    assert _matching_parenthesis(skill, skill.index("(")) == len(skill) - 1


@pytest.mark.parametrize(
    "fragments",
    [
        ("vbExpectedOrder = schGetPinOrder(vbSourceCv)", "dbClose(vbSourceCv)"),
        (
            "dbCopyCellView(vbTempCv",
            "vbFinalTerms = mapcar(",
            "vbFinalOrder = schGetPinOrder(vbTargetCv)",
            'error("installed symbol terminals mismatch")',
            "unless(equal(vbExpectedOrder vbFinalOrder) ",
            'error("installed symbol pin order mismatch")',
            'list("generated" vbAction vbFinalTerms vbFinalOrder)',
        ),
        (
            "vbBodyAttempt = errset(progn(",
            'vbBodyFailure = sprintf(nil "%L" errset.errset)',
            "vbCleanup = errset(dbClose(vbSourceCv) nil)",
        ),
    ],
    ids=["source_order_before_close", "installed_readback", "body_failure_before_cleanup"],
)
def test_symbol_generation_skill_verification_order(fragments: tuple[str, ...]) -> None:
    _assert_ordered(_full_skill(), *fragments)


@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        ('schSetEnv("ssgSortPins" vbOldSort)', "failed to restore ssgSortPins"),
        ("dbClose(vbSourceCv)", "source schematic cleanup close failed"),
        ("dbClose(vbTargetCv)", "target symbol cleanup close failed"),
        ("dbClose(vbTempCv)", "temporary symbol cleanup close failed"),
        ("dbClose(vbBackupSourceCv)", "symbol backup source cleanup close failed"),
        ("dbClose(vbBackupCv)", "symbol backup cleanup close failed"),
        ("ddDeleteObj(vbTempObj)", "temporary symbol cleanup failed"),
    ],
)
def test_symbol_generation_skill_reports_cleanup_failures(operation: str, failure: str) -> None:
    skill = _full_skill()

    assert f"vbCleanup = errset({operation} nil)" in skill
    assert f'vbCleanupFailures = cons("{failure}" vbCleanupFailures)' in skill
    assert "warn(" not in skill


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"errors": ["target symbol exists"]}, "target symbol exists"),
        (
            {"output": '("failed" nil ("temporary symbol cleanup failed"))'},
            "symbol generation cleanup failed: temporary symbol cleanup failed",
        ),
        (
            {"output": '("failed" "symbol generation failed" ("temporary symbol cleanup failed"))'},
            "symbol generation failed: symbol generation failed; "
            "cleanup failed: temporary symbol cleanup failed",
        ),
        (
            {"output": '("generated" "created" "not-a-terminal-list" nil)'},
            "unexpected final terminal payload",
        ),
        (
            {"output": '("generated" "created" (("A" "input" 1) ("A" "output" 1)) ("A"))'},
            "symbol generation response error for demoLib/nand2: duplicate final terminal: A",
        ),
        (
            {"output": '("generated" "created" (("A" "input" "wide")) ("A"))'},
            "symbol generation response error for demoLib/nand2: invalid final terminal width",
        ),
        (
            {"output": '("generated" "created" (("A" "input" 1)))'},
            "unexpected final pin order payload",
        ),
        (
            {"output": '("generated" "created" (("A" "input" 1)) "not-a-pin-order")'},
            "unexpected final pin order payload",
        ),
        (
            {"output": '("generated" "created" (("A" "input" 1) ("Y" "output" 1)) ("A"))'},
            "generated symbol pin order mismatch",
        ),
    ],
    ids=[
        "skill_error",
        "cleanup_failure",
        "body_and_cleanup_failure",
        "scalar_terminals",
        "duplicate_terminal",
        "invalid_width",
        "missing_pin_order",
        "scalar_pin_order",
        "pin_order_terminal_mismatch",
    ],
)
def test_generate_from_schematic_rejects_failed_or_malformed_output(
    response: dict,
    message: str,
) -> None:
    client = _Client(**response)

    with pytest.raises(RuntimeError, match=re.escape(message)):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert len(client.calls) == 1


def test_generate_from_schematic_rejects_trailing_protocol_data_without_hanging() -> None:
    code = """
import sys
sys.path.insert(0, "src")

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.symbol import SymbolOps

class Client:
    def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
        return VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output='("generated" "created" (("A" "input" 1)) ("A")) ("tail")',
        )

SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")
"""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("malformed generation response must not hang")

    assert completed.returncode != 0
    assert (
        "symbol generation response error for demoLib/nand2: "
        "symbol generation output must be a single complete SKILL list"
        in completed.stderr
    )
