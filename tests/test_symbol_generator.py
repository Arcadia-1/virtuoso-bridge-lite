from __future__ import annotations

import re

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.symbol import (
    SymbolGenerationResult,
    SymbolOps,
    symbol_generate_from_schematic_skill,
)


def test_symbol_ops_exposes_generate_from_schematic() -> None:
    ops = SymbolOps(object())

    assert callable(ops.generate_from_schematic)


def test_generate_from_schematic_returns_verified_created_symbol() -> None:
    class Client:
        calls: list[tuple[str, int]]

        def __init__(self) -> None:
            self.calls = []

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls.append((skill, timeout))
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" (("A" "input" 1) ("Y" "output" 1)))',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '(("term" "A" "input" 1 nil) '
                    '("term" "Y" "output" 1 nil) '
                    '("pinOrder" ("A" "Y")) '
                    '("termOrder" ("A" "Y")))'
                ),
            )

    client = Client()

    result = SymbolOps(client).generate_from_schematic(
        "demoLib",
        "nand2",
        sort_pins="geometric",
        timeout=17,
    )

    assert result.lib == "demoLib"
    assert result.cell == "nand2"
    assert result.schematic_view == "schematic"
    assert result.symbol_view == "symbol"
    assert result.action == "created"
    assert result.terminal_names == ("A", "Y")
    assert result.term_order == ("A", "Y")
    assert len(client.calls) == 2
    assert client.calls[0][1] == 17
    assert client.calls[1][1] == 17


def test_generate_from_schematic_rejects_same_source_and_target_view() -> None:
    with pytest.raises(ValueError, match="schematic_view and symbol_view must differ"):
        symbol_generate_from_schematic_skill(
            "demoLib",
            "nand2",
            schematic_view="schematic",
            symbol_view="schematic",
        )


def test_symbol_generation_skill_escapes_names_and_restores_pin_sort() -> None:
    skill = symbol_generate_from_schematic_skill(
        'demo"\\Lib',
        'nand"\\2',
        schematic_view='schem"\\atic',
        symbol_view='sym"\\bol',
        sort_pins="geometric",
    )

    assert 'dbOpenCellViewByType("demo\\"\\\\Lib" "nand\\"\\\\2"' in skill
    assert '"schem\\"\\\\atic" "schematic" "r")' in skill
    assert 'ddGetObj("demo\\"\\\\Lib" "nand\\"\\\\2" "sym\\"\\\\bol")' in skill
    assert 'schSchemToPinList("demo\\"\\\\Lib" "nand\\"\\\\2" "schem\\"\\\\atic")' in skill
    assert 'schGetEnv("ssgSortPins")' in skill
    assert 'vbSortChanged = schSetEnv("ssgSortPins" "geometric")' in skill
    assert "unwindProtect(" in skill
    assert 'schSetEnv("ssgSortPins" vbOldSort)' in skill
    assert skill.index("unwindProtect(") < skill.index('schSetEnv("ssgSortPins" "geometric")')
    assert skill.index('schSetEnv("ssgSortPins" "geometric")') < skill.index("schSchemToPinList")
    assert skill.index("schSchemToPinList") < skill.index('schSetEnv("ssgSortPins" vbOldSort)')
    assert skill.index('schSetEnv("ssgSortPins" vbOldSort)') < skill.index("when(vbSourceCv")
    assert 'list("cleanupFailed" "temporary symbol cleanup failed")' in skill

    temp_match = re.search(r'schPinListToSymbol\([^)]*"(__vb_symbol_[0-9a-f]+)" vbPinList\)', skill)
    assert temp_match is not None
    temp_view = temp_match.group(1)
    assert f'"{temp_view}" "schematicSymbol" "r")' in skill
    assert f'ddGetObj("demo\\"\\\\Lib" "nand\\"\\\\2" "{temp_view}")' in skill
    assert (
        'dbCopyCellView(vbTempCv "demo\\"\\\\Lib" "nand\\"\\\\2" '
        '"sym\\"\\\\bol" nil nil vbReplacing)'
    ) in skill


def test_symbol_generation_skill_leaves_pin_sort_unchanged_when_not_requested() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    assert "schGetEnv" not in skill
    assert "schSetEnv" not in skill
    assert "unwindProtect(" in skill


@pytest.mark.parametrize("sort_pins", ["alphabetic", "GEOMETRIC", ""])
def test_symbol_generation_rejects_unknown_pin_sort(sort_pins: str) -> None:
    with pytest.raises(ValueError, match="sort_pins must be one of: alphanumeric, geometric"):
        symbol_generate_from_schematic_skill("demoLib", "nand2", sort_pins=sort_pins)  # type: ignore[arg-type]


def test_symbol_generation_skill_disables_existing_target_by_default() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    assert 'when(vbReplacing && !nil error("target symbol exists"))' in skill
    assert 'schPinListToSymbol("demoLib" "nand2" "__vb_symbol_' in skill
    assert 'schPinListToSymbol("demoLib" "nand2" "symbol"' not in skill


def test_symbol_generation_skill_allows_verified_temporary_copy_when_overwriting() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2", overwrite=True)

    assert 'when(vbReplacing && !t error("target symbol exists"))' in skill
    assert 'error("generated symbol terminals mismatch")' in skill
    assert 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol" nil nil vbReplacing)' in skill
    assert "dbClose(vbTargetCv) vbTargetCv = nil" in skill
    assert 'ddDeleteObj(vbTargetObj)' not in skill


def test_symbol_generation_skill_returns_cleanup_status_inside_let() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    assert "vbCleanupFailed = t))) ) if(vbCleanupFailed" in skill
    assert "vbCleanupFailed = t))) )) if(vbCleanupFailed" not in skill


def test_generate_from_schematic_reports_replaced_custom_view() -> None:
    class Client:
        skills: list[str]

        def __init__(self) -> None:
            self.skills = []

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.skills.append(skill)
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "replaced" (("A" "input" 1)))',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '(("term" "A" "input" 1 nil) '
                    '("pinOrder" ("A")) ("termOrder" ("A")))'
                ),
            )

    client = Client()
    result = SymbolOps(client).generate_from_schematic(
        "demoLib",
        "nand2",
        schematic_view="schematic_alt",
        symbol_view="symbol_alt",
        overwrite=True,
    )

    assert isinstance(result, SymbolGenerationResult)
    assert result.action == "replaced"
    assert result.schematic_view == "schematic_alt"
    assert result.symbol_view == "symbol_alt"
    assert 'schSchemToPinList("demoLib" "nand2" "schematic_alt")' in client.skills[0]
    assert 'dbOpenCellViewByType("demoLib" "nand2" "symbol_alt" "schematicSymbol" "r")' in client.skills[1]


@pytest.mark.parametrize(
    "error",
    [
        "source schematic not found",
        "target symbol exists",
        "schematic to pin list failed",
        "symbol generation failed",
    ],
)
def test_generate_from_schematic_raises_for_skill_failure(error: str) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[error])

    with pytest.raises(RuntimeError, match=re.escape(error)):
        SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")


def test_generate_from_schematic_rejects_terminal_readback_mismatch() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" (("A" "input" 1)))',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='(("term" "B" "input" 1 nil) ("termOrder" ("B")))',
            )

    with pytest.raises(RuntimeError, match="generated symbol readback mismatch"):
        SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")


def test_generate_from_schematic_rejects_term_order_mismatch() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" (("A" "input" 1) ("Y" "output" 1)))',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '(("term" "A" "input" 1 nil) '
                    '("term" "Y" "output" 1 nil) '
                    '("pinOrder" ("A")) ("termOrder" ("A" "Y")))'
                ),
            )

    with pytest.raises(RuntimeError, match="generated symbol term order mismatch"):
        SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")


def test_generate_from_schematic_uses_sch_get_pin_order() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" (("A" "input" 1) ("Y" "output" 1)))',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '(("term" "A" "input" 1 nil) '
                    '("term" "Y" "output" 1 nil) '
                    '("pinOrder" ("A" "Y")) '
                    '("portOrder" nil) ("termOrder" nil))'
                ),
            )

    result = SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")

    assert result.terminal_names == ("A", "Y")
    assert result.term_order == ("A", "Y")


def test_generate_from_schematic_reports_temporary_view_cleanup_failure() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='("cleanupFailed" "temporary symbol cleanup failed")',
            )

    client = Client()

    with pytest.raises(
        RuntimeError,
        match="symbol generation cleanup failed: temporary symbol cleanup failed",
    ):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert client.calls == 1
