"""Native schematic-to-symbol generation helpers."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, cast

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr
from virtuoso_bridge.virtuoso.symbol.reader import _response_fields, read_symbol_ports

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
    overwrite_expr = "t" if overwrite else "nil"

    sort_capture = ""
    sort_setup = ""
    sort_restore = ""
    if sort_pins is not None:
        escaped_sort = escape_skill_string(sort_pins)
        sort_capture = 'vbOldSort = schGetEnv("ssgSortPins") '
        sort_setup = (
            f'vbSortChanged = schSetEnv("ssgSortPins" "{escaped_sort}") '
            'unless(vbSortChanged error("failed to set ssgSortPins")) '
        )
        sort_restore = (
            "when(vbSortChanged "
            'unless(schSetEnv("ssgSortPins" vbOldSort) '
            'warn("failed to restore ssgSortPins")) '
            "vbSortChanged = nil) "
        )

    return (
        "let((vbSourceCv vbTargetObj vbTempObj vbTempCv vbTargetCv vbPinList vbGenerated "
        "vbReplacing vbAction vbExpectedTerms vbActualTerms vbExpectedTerm vbOldSort "
        "vbSortChanged vbCleanup vbCleanupFailed vbResult) "
        f'vbTargetObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_symbol_view}") '
        "vbReplacing = if(vbTargetObj t nil) "
        'vbAction = if(vbReplacing "replaced" "created") '
        "when(vbTargetObj ddReleaseObj(vbTargetObj) vbTargetObj = nil) "
        f'when(vbReplacing && !{overwrite_expr} error("target symbol exists")) '
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_temp_view}") '
        'when(vbTempObj unless(ddDeleteObj(vbTempObj) error("temporary symbol delete failed"))) '
        f"{sort_capture}"
        "vbResult = unwindProtect("
        "progn("
        f'vbSourceCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_schematic_view}" "schematic" "r") '
        'unless(vbSourceCv error("source schematic not found")) '
        "vbExpectedTerms = mapcar(lambda((vbTerm) "
        'list(vbTerm~>name if(vbTerm~>direction vbTerm~>direction "inputOutput") '
        "if(vbTerm~>numBits vbTerm~>numBits 1))) vbSourceCv~>terminals) "
        "dbClose(vbSourceCv) vbSourceCv = nil "
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
        "unless(length(vbExpectedTerms) == length(vbActualTerms) "
        'error("generated symbol terminals mismatch")) '
        "foreach(vbExpectedTerm vbExpectedTerms "
        "unless(member(vbExpectedTerm vbActualTerms) "
        'error("generated symbol terminals mismatch"))) '
        "unless(isCallable('dbCopyCellView) error(\"dbCopyCellView API unavailable\")) "
        f'vbTargetCv = dbCopyCellView(vbTempCv "{escaped_lib}" "{escaped_cell}" '
        f'"{escaped_symbol_view}" nil nil vbReplacing) '
        'unless(vbTargetCv error("target symbol copy failed")) '
        "dbClose(vbTargetCv) vbTargetCv = nil "
        "dbClose(vbTempCv) vbTempCv = nil "
        'list("generated" vbAction vbExpectedTerms)) '
        "progn("
        f"{sort_restore}"
        "when(vbSourceCv errset(dbClose(vbSourceCv) nil) vbSourceCv = nil) "
        "when(vbTargetCv errset(dbClose(vbTargetCv) nil) vbTargetCv = nil) "
        "when(vbTempCv errset(dbClose(vbTempCv) nil) vbTempCv = nil) "
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_temp_view}") '
        "when(vbTempObj "
        "vbCleanup = errset(ddDeleteObj(vbTempObj) nil) "
        "unless(vbCleanup && car(vbCleanup) "
        "vbCleanupFailed = t))) "
        ") "
        "if(vbCleanupFailed "
        'then list("cleanupFailed" "temporary symbol cleanup failed") '
        "else vbResult))"
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
    default; ``overwrite=True`` generates and validates a temporary view before
    copying it over the destination.
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
    action, expected_terms = _parse_generation_output(output)
    ports = read_symbol_ports(
        client,
        lib,
        cell,
        view=symbol_view,
        timeout=timeout,
    )
    generated_terms = {
        str(term.get("name", "")): (
            str(term.get("direction", "")),
            int(term.get("numBits", 1)),
        )
        for term in ports["terms"]
    }
    if generated_terms != expected_terms:
        raise RuntimeError(
            f"generated symbol readback mismatch for {lib}/{cell}: "
            f"expected {expected_terms}, got {generated_terms}"
        )

    terminal_names = tuple(term["name"] for term in ports["terms"])
    term_order = tuple(ports["pinOrder"])
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
) -> tuple[SymbolGenerationAction, dict[str, tuple[str, int]]]:
    parsed = parse_sexpr(output)
    if isinstance(parsed, list) and len(parsed) >= 2 and parsed[0] == "cleanupFailed":
        raise RuntimeError(f"symbol generation cleanup failed: {parsed[1]}")
    if not isinstance(parsed, list) or len(parsed) < 3 or parsed[0] != "generated":
        raise RuntimeError(f"unexpected symbol generation output: {output}")
    action = str(parsed[1])
    if action not in {"created", "replaced"}:
        raise RuntimeError(f"unexpected symbol generation action: {action}")
    records = parsed[2] if isinstance(parsed[2], list) else []
    expected_terms: dict[str, tuple[str, int]] = {}
    for record in records:
        if not isinstance(record, list) or len(record) < 3:
            raise RuntimeError(f"unexpected source terminal record: {record}")
        expected_terms[str(record[0])] = (str(record[1]), int(record[2]))
    return cast(SymbolGenerationAction, action), expected_terms


__all__ = [
    "SymbolGenerationAction",
    "SymbolGenerationResult",
    "SymbolPinSort",
    "generate_symbol_from_schematic",
    "symbol_generate_from_schematic_skill",
]
