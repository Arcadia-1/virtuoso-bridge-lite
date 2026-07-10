"""Native schematic-to-symbol generation helpers."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, cast

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr
from virtuoso_bridge.virtuoso.symbol.reader import _response_fields

SymbolPinSort = Literal["alphanumeric", "geometric"]
SymbolGenerationAction = Literal["created", "replaced"]

_PIN_SORT_MODES = {"alphanumeric", "geometric"}


@dataclass(frozen=True)
class SymbolGenerationResult:
    """Verified source, destination, action, and terminal readback."""

    lib: str
    cell: str
    schematic_view: str
    symbol_view: str
    action: SymbolGenerationAction
    terminal_names: tuple[str, ...]
    term_order: tuple[str, ...]


def symbol_generate_from_schematic_skill(
    lib: str,
    cell: str,
    *,
    schematic_view: str = "schematic",
    symbol_view: str = "symbol",
    sort_pins: SymbolPinSort | None = None,
    overwrite: bool = False,
) -> str:
    """Build SKILL for Cadence's native schematic-to-symbol pipeline."""
    _validate_sort_pins(sort_pins)
    if schematic_view == symbol_view:
        raise ValueError("schematic_view and symbol_view must differ")
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_schematic_view = escape_skill_string(schematic_view)
    escaped_symbol_view = escape_skill_string(symbol_view)
    escaped_temp_view = escape_skill_string(f"__vb_symbol_{uuid.uuid4().hex}")
    escaped_backup_view = escape_skill_string(f"__vb_symbol_backup_{uuid.uuid4().hex}")
    overwrite_expr = "t" if overwrite else "nil"

    sort_capture = ""
    sort_setup = ""
    sort_finalize = ""
    sort_restore = ""
    if sort_pins is not None:
        escaped_sort = escape_skill_string(sort_pins)
        sort_capture = 'vbOldSort = schGetEnv("ssgSortPins") '
        sort_setup = (
            f'vbSortChanged = schSetEnv("ssgSortPins" "{escaped_sort}") '
            'unless(vbSortChanged error("failed to set ssgSortPins")) '
        )
        sort_finalize = (
            "when(vbSortChanged "
            'vbCleanup = errset(schSetEnv("ssgSortPins" vbOldSort) nil) '
            'unless(vbCleanup && car(vbCleanup) error("failed to restore ssgSortPins")) '
            "vbSortChanged = nil) "
        )
        sort_restore = (
            "when(vbSortChanged "
            'vbCleanup = errset(schSetEnv("ssgSortPins" vbOldSort) nil) '
            "unless(vbCleanup && car(vbCleanup) "
            'vbCleanupFailures = cons("failed to restore ssgSortPins" '
            "vbCleanupFailures)) "
            "vbSortChanged = nil) "
        )

    return (
        "let((vbSourceCv vbTargetObj vbTempObj vbTempCv vbTargetCv vbPinList vbGenerated "
        "vbReplacing vbAction vbExpectedTerms vbActualTerms vbExpectedTerm vbOldSort "
        "vbExpectedOrder vbActualOrder vbFinalTerms vbFinalOrder vbBackupSourceCv "
        "vbOriginalTerms vbOriginalOrder vbBackupCv vbBackupObj vbSortChanged vbCleanup "
        "vbCleanupFailures vbBodyResult vbBodyAttempt vbBodyFailure vbResult vbBackupReady "
        "vbInstallAttempted vbInstalled vbCommitOk vbRollback vbRollbackSucceeded) "
        f'vbTargetObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_symbol_view}") '
        "vbReplacing = if(vbTargetObj t nil) "
        'vbAction = if(vbReplacing "replaced" "created") '
        "when(vbTargetObj ddReleaseObj(vbTargetObj) vbTargetObj = nil) "
        f'when(vbReplacing && !{overwrite_expr} error("target symbol exists")) '
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_temp_view}") '
        'when(vbTempObj unless(ddDeleteObj(vbTempObj) error("temporary symbol delete failed"))) '
        f'vbBackupObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_backup_view}") '
        'when(vbBackupObj unless(ddDeleteObj(vbBackupObj) error("symbol backup delete failed"))) '
        f"{sort_capture}"
        'vbBodyFailure = "symbol generation failed" '
        "vbBodyResult = unwindProtect(progn("
        "vbBodyAttempt = errset(progn("
        f'vbSourceCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_schematic_view}" "schematic" "r") '
        'unless(vbSourceCv error("source schematic not found")) '
        "vbExpectedTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbSourceCv~>terminals) "
        "vbExpectedOrder = schGetPinOrder(vbSourceCv) "
        'unless(dbClose(vbSourceCv) error("source schematic close failed")) '
        "vbSourceCv = nil "
        f"{sort_setup}"
        f'vbPinList = schSchemToPinList("{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}") '
        'unless(vbPinList error("schematic to pin list failed")) '
        f'vbGenerated = schPinListToSymbol("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_temp_view}" vbPinList) '
        'unless(vbGenerated error("symbol generation failed")) '
        f'vbTempCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_temp_view}" "schematicSymbol" "r") '
        'unless(vbTempCv error("temporary symbol open failed")) '
        "vbActualTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbTempCv~>terminals) "
        "vbActualOrder = schGetPinOrder(vbTempCv) "
        "unless(length(vbExpectedTerms) == length(vbActualTerms) "
        'error("generated symbol terminals mismatch")) '
        "foreach(vbExpectedTerm vbExpectedTerms "
        "unless(member(vbExpectedTerm vbActualTerms) "
        'error("generated symbol terminals mismatch"))) '
        'unless(equal(vbExpectedOrder vbActualOrder) error("generated symbol pin order mismatch")) '
        f"{sort_finalize}"
        "unless(isCallable('dbCopyCellView) error(\"dbCopyCellView API unavailable\")) "
        f'when(dbFindOpenCellViewByName("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_symbol_view}") error("target symbol is open")) '
        "when(vbReplacing "
        f'vbBackupSourceCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_symbol_view}" "schematicSymbol" "r") '
        'unless(vbBackupSourceCv error("target symbol backup source open failed")) '
        "vbOriginalTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbBackupSourceCv~>terminals) "
        "vbOriginalOrder = schGetPinOrder(vbBackupSourceCv) "
        f'vbBackupCv = dbCopyCellView(vbBackupSourceCv "{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_backup_view}" nil nil nil) '
        'unless(vbBackupCv error("target symbol backup failed")) '
        "vbBackupReady = t "
        'unless(dbClose(vbBackupCv) error("target symbol backup close failed")) '
        "vbBackupCv = nil "
        'unless(dbClose(vbBackupSourceCv) error("target symbol backup source close failed")) '
        "vbBackupSourceCv = nil) "
        "vbInstallAttempted = t "
        f'vbTargetCv = dbCopyCellView(vbTempCv "{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_symbol_view}" nil nil {overwrite_expr}) '
        'unless(vbTargetCv error("target symbol copy failed")) '
        "vbInstalled = t "
        "vbFinalTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbTargetCv~>terminals) "
        "vbFinalOrder = schGetPinOrder(vbTargetCv) "
        "unless(length(vbExpectedTerms) == length(vbFinalTerms) "
        'error("installed symbol terminals mismatch")) '
        "foreach(vbExpectedTerm vbExpectedTerms "
        "unless(member(vbExpectedTerm vbFinalTerms) "
        'error("installed symbol terminals mismatch"))) '
        'unless(equal(vbExpectedOrder vbFinalOrder) error("installed symbol pin order mismatch")) '
        'unless(dbClose(vbTargetCv) error("installed symbol close failed")) '
        "vbTargetCv = nil "
        'unless(dbClose(vbTempCv) error("temporary symbol close failed")) '
        "vbTempCv = nil "
        'vbResult = list("generated" vbAction vbFinalTerms vbFinalOrder) '
        "vbCommitOk = t "
        "vbResult) nil) "
        'unless(vbBodyAttempt vbBodyFailure = sprintf(nil "%L" errset.errset)) '
        "vbBodyAttempt) "
        "progn("
        f"{sort_restore}"
        "when(vbSourceCv errset(dbClose(vbSourceCv) nil) vbSourceCv = nil) "
        "when(vbTargetCv errset(dbClose(vbTargetCv) nil) vbTargetCv = nil) "
        "when(vbTempCv errset(dbClose(vbTempCv) nil) vbTempCv = nil) "
        "when(vbBackupSourceCv errset(dbClose(vbBackupSourceCv) nil) "
        "vbBackupSourceCv = nil) "
        "when(vbBackupCv errset(dbClose(vbBackupCv) nil) vbBackupCv = nil) "
        "unless(vbCommitOk "
        "when(vbInstallAttempted "
        "if(vbReplacing "
        "then if(vbBackupReady "
        "then vbRollback = errset(progn("
        f'vbBackupCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_backup_view}" "schematicSymbol" "r") '
        'unless(vbBackupCv error("symbol backup open failed")) '
        f'vbTargetCv = dbCopyCellView(vbBackupCv "{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_symbol_view}" nil nil t) '
        'unless(vbTargetCv error("target symbol rollback copy failed")) '
        "vbActualTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbTargetCv~>terminals) "
        "vbActualOrder = schGetPinOrder(vbTargetCv) "
        "unless(length(vbOriginalTerms) == length(vbActualTerms) "
        'error("target symbol rollback terminals mismatch")) '
        "foreach(vbExpectedTerm vbOriginalTerms "
        "unless(member(vbExpectedTerm vbActualTerms) "
        'error("target symbol rollback terminals mismatch"))) '
        'unless(equal(vbOriginalOrder vbActualOrder) '
        'error("target symbol rollback pin order mismatch")) '
        'unless(dbClose(vbTargetCv) error("target symbol rollback close failed")) '
        "vbTargetCv = nil "
        'unless(dbClose(vbBackupCv) error("symbol backup close failed")) '
        "vbBackupCv = nil t) nil) "
        "if(vbRollback && car(vbRollback) "
        "then vbRollbackSucceeded = t "
        "else vbCleanupFailures = cons("
        f'sprintf(nil "target symbol rollback failed; backup retained as %s" '
        f'"{escaped_backup_view}") vbCleanupFailures)) '
        "else vbCleanupFailures = cons("
        '"target symbol rollback failed; backup unavailable" vbCleanupFailures)) '
        "else when(vbInstalled "
        f'vbTargetObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_symbol_view}") '
        "if(vbTargetObj "
        "then vbRollback = errset(ddDeleteObj(vbTargetObj) nil) "
        "if(vbRollback && car(vbRollback) "
        "then vbRollbackSucceeded = t "
        "else vbCleanupFailures = cons("
        '"created symbol rollback failed" vbCleanupFailures)) '
        "else vbRollbackSucceeded = t)))) "
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_temp_view}") '
        "when(vbTempObj "
        "vbCleanup = errset(ddDeleteObj(vbTempObj) nil) "
        "unless(vbCleanup && car(vbCleanup) "
        'vbCleanupFailures = cons("temporary symbol cleanup failed" vbCleanupFailures)))) '
        "when(vbBackupReady && "
        "(!vbInstallAttempted || vbCommitOk || vbRollbackSucceeded) "
        f'vbBackupObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_backup_view}") '
        "when(vbBackupObj "
        "vbCleanup = errset(ddDeleteObj(vbBackupObj) nil) "
        "unless(vbCleanup && car(vbCleanup) "
        "vbCleanupFailures = cons("
        f'sprintf(nil "symbol backup cleanup failed; backup retained as %s" '
        f'"{escaped_backup_view}") vbCleanupFailures)))) '
        ") "
        ") "
        "if(vbBodyResult && vbCommitOk && !vbCleanupFailures "
        "then car(vbBodyResult) "
        "else list(\"failed\" if(vbBodyResult nil vbBodyFailure) "
        "reverse(vbCleanupFailures))))"
    )


def generate_symbol_from_schematic(
    client: Any,
    lib: str,
    cell: str,
    *,
    schematic_view: str = "schematic",
    symbol_view: str = "symbol",
    sort_pins: SymbolPinSort | None = None,
    overwrite: bool = False,
    timeout: int = 60,
) -> SymbolGenerationResult:
    """Generate and verify a symbol view using Cadence's native generator.

    ``sort_pins`` temporarily overrides ``ssgSortPins`` for this operation and
    is restored even when generation fails. Existing symbols are rejected by
    default; ``overwrite=True`` rejects open targets, backs up the existing
    view, and rolls it back if installation or final validation fails.
    """
    response = client.execute_skill(
        symbol_generate_from_schematic_skill(
            lib,
            cell,
            schematic_view=schematic_view,
            symbol_view=symbol_view,
            sort_pins=sort_pins,
            overwrite=overwrite,
        ),
        timeout=timeout,
    )
    output = _require_generation_success(response, lib=lib, cell=cell)
    action, final_terms, term_order = _parse_generation_output(output)
    terminal_names = tuple(final_terms)
    if Counter(term_order) != Counter(terminal_names):
        raise RuntimeError(
            f"generated symbol term order mismatch for {lib}/{cell}: "
            f"terminals {terminal_names}, order {term_order}"
        )
    return SymbolGenerationResult(
        lib=lib,
        cell=cell,
        schematic_view=schematic_view,
        symbol_view=symbol_view,
        action=action,
        terminal_names=terminal_names,
        term_order=term_order,
    )


def _validate_sort_pins(sort_pins: str | None) -> None:
    if sort_pins is not None and sort_pins not in _PIN_SORT_MODES:
        choices = ", ".join(sorted(_PIN_SORT_MODES))
        raise ValueError(f"sort_pins must be one of: {choices}")


def _require_generation_success(response: Any, *, lib: str, cell: str) -> str:
    errors, status, output = _response_fields(response)
    if errors:
        raise RuntimeError(f"symbol generation failed for {lib}/{cell}: {errors[0]}")
    status_value = getattr(status, "value", status)
    if status_value is not None and str(status_value).lower() not in {"success", "ok"}:
        detail = output or f"status={status_value}"
        raise RuntimeError(f"symbol generation failed for {lib}/{cell}: {detail}")
    if not output.strip():
        raise RuntimeError(f"symbol generation returned empty output for {lib}/{cell}")
    return output.strip()


def _parse_generation_output(
    output: str,
) -> tuple[SymbolGenerationAction, dict[str, tuple[str, int]], tuple[str, ...]]:
    parsed = parse_sexpr(output)
    if isinstance(parsed, list) and len(parsed) >= 3 and parsed[0] == "failed":
        body_failure = parsed[1]
        cleanup_failures = parsed[2]
        if cleanup_failures is None:
            cleanup_failures = []
        if not isinstance(cleanup_failures, list):
            raise RuntimeError(f"unexpected symbol generation failure output: {output}")
        cleanup_detail = ", ".join(str(item) for item in cleanup_failures)
        if body_failure is not None:
            detail = f"symbol generation failed: {body_failure}"
            if cleanup_detail:
                detail += f"; cleanup failed: {cleanup_detail}"
            raise RuntimeError(detail)
        if cleanup_detail:
            raise RuntimeError(f"symbol generation cleanup failed: {cleanup_detail}")
        raise RuntimeError(f"symbol generation failed without details: {output}")
    if not isinstance(parsed, list) or len(parsed) < 3 or parsed[0] != "generated":
        raise RuntimeError(f"unexpected symbol generation output: {output}")
    action = str(parsed[1])
    if action not in {"created", "replaced"}:
        raise RuntimeError(f"unexpected symbol generation action: {action}")
    raw_records = parsed[2]
    if raw_records is None:
        records = []
    elif isinstance(raw_records, list):
        records = raw_records
    else:
        raise RuntimeError(f"unexpected final terminal payload: {raw_records}")
    expected_terms: dict[str, tuple[str, int]] = {}
    for record in records:
        if not isinstance(record, list) or len(record) < 3:
            raise RuntimeError(f"unexpected final terminal record: {record}")
        expected_terms[str(record[0])] = (str(record[1]), int(record[2]))
    if len(parsed) < 4:
        raise RuntimeError("unexpected final pin order payload: missing")
    raw_order = parsed[3]
    if raw_order is None:
        expected_order: tuple[str, ...] = ()
    elif isinstance(raw_order, list):
        expected_order = tuple(str(item) for item in raw_order)
    else:
        raise RuntimeError(f"unexpected final pin order payload: {raw_order}")
    return cast(SymbolGenerationAction, action), expected_terms, expected_order


__all__ = [
    "SymbolGenerationAction",
    "SymbolGenerationResult",
    "SymbolPinSort",
    "generate_symbol_from_schematic",
    "symbol_generate_from_schematic_skill",
]
