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
                    output=(
                        '("generated" "created" '
                        '(("A" "input" 1) ("Y" "output" 1)) ("A" "Y"))'
                    ),
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
    assert result.pin_order == ("A", "Y")
    assert len(client.calls) == 1
    assert client.calls[0][1] == 17


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
    assert skill.index('schSetEnv("ssgSortPins" vbOldSort)') < skill.index(
        "dbCopyCellView(vbTempCv"
    )
    assert skill.index('schSetEnv("ssgSortPins" vbOldSort)') < skill.index("when(vbSourceCv")
    assert (
        'list("failed" if(vbBodyResult nil vbBodyFailure) reverse(vbCleanupFailures))'
        in skill
    )

    temp_match = re.search(r'schPinListToSymbol\([^)]*"(__vb_symbol_[0-9a-f]+)" vbPinList\)', skill)
    assert temp_match is not None
    temp_view = temp_match.group(1)
    assert f'"{temp_view}" "schematicSymbol" "r")' in skill
    assert f'ddGetObj("demo\\"\\\\Lib" "nand\\"\\\\2" "{temp_view}")' in skill
    assert (
        'dbCopyCellView(vbTempCv "demo\\"\\\\Lib" "nand\\"\\\\2" '
        '"sym\\"\\\\bol" nil nil nil)'
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
    assert 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol" nil nil t)' in skill
    assert 'unless(dbClose(vbTargetCv) error("installed symbol close failed"))' in skill
    assert 'dbFindOpenCellViewByName("demoLib" "nand2" "symbol")' in skill


def test_symbol_generation_skill_rolls_back_failed_overwrite_from_backup() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2", overwrite=True)

    backup_match = re.search(r'"(__vb_symbol_backup_[0-9a-f]+)"', skill)
    assert backup_match is not None
    backup_view = backup_match.group(1)
    open_guard = 'error("target symbol is open")'
    backup_copy = (
        f'dbCopyCellView(vbBackupSourceCv "demoLib" "nand2" "{backup_view}" nil nil nil)'
    )
    target_copy = 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol" nil nil t)'
    final_order = "vbFinalOrder = schGetPinOrder(vbTargetCv)"
    rollback_copy = 'dbCopyCellView(vbBackupCv "demoLib" "nand2" "symbol" nil nil t)'

    assert 'dbFindOpenCellViewByName("demoLib" "nand2" "symbol")' in skill
    assert open_guard in skill
    assert backup_copy in skill
    assert target_copy in skill
    assert final_order in skill
    assert rollback_copy in skill
    assert "target symbol rollback failed" in skill
    assert skill.index(open_guard) < skill.index(backup_copy) < skill.index(target_copy)
    assert skill.index(target_copy) < skill.index(final_order) < skill.index("vbCommitOk = t")


def test_symbol_generation_skill_rolls_back_after_temporary_cleanup_failure() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2", overwrite=True)

    rollback_start = skill.index("when(!vbCommitOk || vbCleanupFailures ")
    rollback_open = skill.index("(", rollback_start)
    rollback_end = _matching_parenthesis(skill, rollback_open)
    temp_cleanup = skill.rfind("vbTempObj = ddGetObj", 0, rollback_start)
    backup_cleanup = skill.index("when(vbBackupReady &&", rollback_end)

    assert temp_cleanup >= 0
    assert temp_cleanup < rollback_start < rollback_end < backup_cleanup
    assert "(vbCommitOk && !vbCleanupFailures)" in skill[backup_cleanup:]
    assert _matching_parenthesis(skill, skill.index("(")) == len(skill) - 1


def test_generate_from_schematic_does_not_depend_on_post_commit_readback() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("committed symbol must be verified in the generation call")
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '("generated" "replaced" '
                    '(("A" "input" 1) ("Y" "output" 1)) ("A" "Y"))'
                ),
            )

    client = Client()

    result = SymbolOps(client).generate_from_schematic(
        "demoLib",
        "nand2",
        overwrite=True,
    )

    assert result.action == "replaced"
    assert result.terminal_names == ("A", "Y")
    assert result.pin_order == ("A", "Y")
    assert client.calls == 1


def test_symbol_generation_skill_preserves_effective_source_pin_order() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    source_order = "vbExpectedOrder = schGetPinOrder(vbSourceCv)"
    temp_order = "vbActualOrder = schGetPinOrder(vbTempCv)"
    order_check = 'unless(equal(vbExpectedOrder vbActualOrder) error("generated symbol pin order mismatch"))'
    target_copy = 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol"'

    assert source_order in skill
    assert temp_order in skill
    assert order_check in skill
    assert 'unless(dbClose(vbSourceCv) error("source schematic close failed"))' in skill
    assert skill.index(source_order) < skill.index("dbClose(vbSourceCv)")
    assert skill.index(temp_order) < skill.index(order_check) < skill.index(target_copy)
    assert 'list("generated" vbAction vbFinalTerms vbFinalOrder)' in skill


def test_symbol_generation_skill_captures_body_failure_before_cleanup_reporting() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    body_attempt = "vbBodyAttempt = errset(progn("
    failure_capture = 'vbBodyFailure = sprintf(nil "%L" errset.errset)'
    cleanup_start = "progn(when(vbSourceCv"

    assert "vbBodyResult = unwindProtect(progn(" in skill
    assert body_attempt in skill
    assert 'vbBodyFailure = "symbol generation failed"' in skill
    assert failure_capture in skill
    assert skill.index(body_attempt) < skill.index(failure_capture) < skill.index(cleanup_start)
    assert (
        'vbCleanupFailures = cons("temporary symbol cleanup failed" vbCleanupFailures)'
        in skill
    )
    assert (
        'list("failed" if(vbBodyResult nil vbBodyFailure) reverse(vbCleanupFailures))'
        in skill
    )


def test_symbol_generation_skill_reports_pin_sort_restore_failure() -> None:
    skill = symbol_generate_from_schematic_skill(
        "demoLib",
        "nand2",
        sort_pins="geometric",
    )

    assert 'vbCleanup = errset(schSetEnv("ssgSortPins" vbOldSort) nil)' in skill
    assert (
        'vbCleanupFailures = cons("failed to restore ssgSortPins" vbCleanupFailures)'
        in skill
    )
    assert 'warn("failed to restore ssgSortPins")' not in skill


@pytest.mark.parametrize(
    ("variable", "failure"),
    [
        ("vbSourceCv", "source schematic cleanup close failed"),
        ("vbTargetCv", "target symbol cleanup close failed"),
        ("vbTempCv", "temporary symbol cleanup close failed"),
        ("vbBackupSourceCv", "symbol backup source cleanup close failed"),
        ("vbBackupCv", "symbol backup cleanup close failed"),
    ],
)
def test_symbol_generation_skill_reports_cleanup_close_failure(
    variable: str,
    failure: str,
) -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2", overwrite=True)

    assert f"vbCleanup = errset(dbClose({variable}) nil)" in skill
    assert f'vbCleanupFailures = cons("{failure}" vbCleanupFailures)' in skill


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
                    output='("generated" "replaced" (("A" "input" 1)) ("A"))',
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
    assert (
        'dbOpenCellViewByType("demoLib" "nand2" "symbol_alt" "schematicSymbol" "r")'
        in client.skills[0]
    )


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


def test_symbol_generation_skill_validates_installed_terminal_readback() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    target_copy = 'dbCopyCellView(vbTempCv "demoLib" "nand2" "symbol"'
    final_terms = "vbFinalTerms = mapcar("
    terminal_check = 'error("installed symbol terminals mismatch")'

    assert target_copy in skill
    assert final_terms in skill
    assert terminal_check in skill
    assert skill.index(target_copy) < skill.index(final_terms) < skill.index(terminal_check)


def test_generate_from_schematic_rejects_pin_order_terminal_set_mismatch() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '("generated" "created" '
                    '(("A" "input" 1) ("Y" "output" 1)) ("A"))'
                ),
            )

    with pytest.raises(RuntimeError, match="generated symbol pin order mismatch"):
        SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")


def test_symbol_generation_skill_rejects_reordered_installed_pin_order() -> None:
    skill = symbol_generate_from_schematic_skill("demoLib", "nand2")

    final_order = "vbFinalOrder = schGetPinOrder(vbTargetCv)"
    order_check = 'unless(equal(vbExpectedOrder vbFinalOrder) '
    order_error = 'error("installed symbol pin order mismatch"))'

    assert final_order in skill
    assert order_check in skill
    assert order_error in skill
    assert skill.index(final_order) < skill.index(order_check) < skill.index(order_error)


def test_generate_from_schematic_uses_sch_get_pin_order() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            if "schSchemToPinList" in skill:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output=(
                        '("generated" "created" '
                        '(("A" "input" 1) ("Y" "output" 1)) ("A" "Y"))'
                    ),
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
    assert result.pin_order == ("A", "Y")


def test_generate_from_schematic_reports_temporary_view_cleanup_failure() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='("failed" nil ("temporary symbol cleanup failed"))',
            )

    client = Client()

    with pytest.raises(
        RuntimeError,
        match="symbol generation cleanup failed: temporary symbol cleanup failed",
    ):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert client.calls == 1


def test_generate_from_schematic_combines_body_and_cleanup_failures() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '("failed" "symbol generation failed" '
                    '("temporary symbol cleanup failed"))'
                ),
            )

    client = Client()

    with pytest.raises(
        RuntimeError,
        match=(
            "symbol generation failed: symbol generation failed; "
            "cleanup failed: temporary symbol cleanup failed"
        ),
    ):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert client.calls == 1


def test_generate_from_schematic_rejects_non_list_terminal_payload() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            if self.calls == 1:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" "not-a-terminal-list" nil)',
                )
            raise AssertionError("malformed generation output must fail before readback")

    client = Client()

    with pytest.raises(RuntimeError, match="unexpected final terminal payload"):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert client.calls == 1


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


@pytest.mark.parametrize(
    ("output", "error"),
    [
        (
            '("generated" "created" '
            '(("A" "input" 1) ("A" "output" 1)) ("A"))',
            "duplicate final terminal: A",
        ),
        (
            '("generated" "created" (("A" "input" "wide")) ("A"))',
            "invalid final terminal width",
        ),
    ],
)
def test_generate_from_schematic_rejects_invalid_terminal_records(
    output: str,
    error: str,
) -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)

    with pytest.raises(
        RuntimeError,
        match=rf"symbol generation response error for demoLib/nand2: {error}",
    ):
        SymbolOps(Client()).generate_from_schematic("demoLib", "nand2")


@pytest.mark.parametrize(
    "output",
    [
        '("generated" "created" (("A" "input" 1)))',
        '("generated" "created" (("A" "input" 1)) "not-a-pin-order")',
    ],
)
def test_generate_from_schematic_rejects_missing_or_scalar_pin_order(output: str) -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            if self.calls == 1:
                return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)
            raise AssertionError("malformed generation output must fail before readback")

    client = Client()

    with pytest.raises(RuntimeError, match="unexpected final pin order payload"):
        SymbolOps(client).generate_from_schematic("demoLib", "nand2")

    assert client.calls == 1


def test_generate_from_schematic_accepts_nil_terminal_payload() -> None:
    class Client:
        calls = 0

        def execute_skill(self, skill: str, *, timeout: int) -> VirtuosoResult:
            self.calls += 1
            if self.calls == 1:
                return VirtuosoResult(
                    status=ExecutionStatus.SUCCESS,
                    output='("generated" "created" nil nil)',
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '(("pinOrder" nil) ("portOrder" nil) ("termOrder" nil))'
                ),
            )

    result = SymbolOps(Client()).generate_from_schematic("demoLib", "empty")

    assert result.terminal_names == ()
    assert result.pin_order == ()
