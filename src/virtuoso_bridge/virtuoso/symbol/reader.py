"""Read-only helpers for symbol cellviews."""

from __future__ import annotations

from typing import Any

from virtuoso_bridge.virtuoso.ops import open_cell_view
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr


def symbol_read_ports_skill(
    lib: str,
    cell: str,
    *,
    view: str = "symbol",
    view_type: str = "schematicSymbol",
) -> str:
    """Build SKILL to report symbol terminals, labels, and term order."""
    open_expr = open_cell_view(lib, cell, view=view, view_type=view_type, mode="r")
    return (
        "let((cv term pin fig label bbox xy result) "
        f"{open_expr} "
        'unless(cv error("open symbol failed")) '
        "result = nil "
        "foreach(term cv~>terminals "
        "pin = car(term~>pins) "
        "fig = nil "
        "when(pin "
        "fig = car(errset(pin~>fig nil)) "
        "unless(fig fig = car(errset(car(pin~>figs) nil)))) "
        "bbox = nil "
        "when(fig && fig~>bBox "
        "bbox = list(list(xCoord(car(fig~>bBox)) yCoord(car(fig~>bBox))) "
        "list(xCoord(cadr(fig~>bBox)) yCoord(cadr(fig~>bBox))))) "
        'result = cons(list("term" term~>name if(term~>direction term~>direction "") '
        "if(term~>numBits term~>numBits 1) bbox) result)) "
        "foreach(label cv~>shapes "
        'when(label~>objType == "label" '
        "xy = nil "
        "when(label~>xy xy = list(xCoord(label~>xy) yCoord(label~>xy))) "
        'result = cons(list("label" if(label~>theLabel label~>theLabel "") '
        'if(label~>labelType label~>labelType "") xy) result))) '
        'result = cons(list("termOrder" cv~>termOrder) result) '
        "dbClose(cv) "
        "reverse(result))"
    )


def parse_symbol_ports_output(output: str) -> dict[str, Any]:
    """Parse ``symbol_read_ports_skill`` text output."""
    text = (output or "").strip()
    if not text.startswith("("):
        raise ValueError("symbol readback output must be a structured SKILL list")
    return _parse_symbol_ports_records(text)


def _parse_symbol_ports_records(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {"terms": [], "labels": [], "termOrder": []}
    parsed = parse_sexpr(output)
    if not isinstance(parsed, list):
        return result

    for record in parsed:
        if not isinstance(record, list) or not record:
            continue
        kind = _string_value(record[0])
        if kind == "term" and len(record) >= 5:
            result["terms"].append(
                {
                    "name": _string_value(record[1]),
                    "direction": _string_value(record[2]),
                    "numBits": _int_value(record[3], default=1),
                    "bbox": _bbox_value(record[4]),
                }
            )
        elif kind == "label" and len(record) >= 4:
            result["labels"].append(
                {
                    "text": _string_value(record[1]),
                    "labelType": _string_value(record[2]),
                    "xy": _point_value(record[3]),
                }
            )
        elif kind == "termOrder" and len(record) >= 2:
            order = record[1] if isinstance(record[1], list) else []
            result["termOrder"] = [_string_value(item) for item in order]
    return result


def read_symbol_ports(
    client: Any,
    lib: str,
    cell: str,
    *,
    view: str = "symbol",
    view_type: str = "schematicSymbol",
    timeout: int = 30,
) -> dict[str, Any]:
    """Read symbol terminals, labels, and term order."""
    response = client.execute_skill(
        symbol_read_ports_skill(lib, cell, view=view, view_type=view_type),
        timeout=timeout,
    )
    errors, status, output = _response_fields(response)
    _raise_for_symbol_read_error(errors, status, output, lib=lib, cell=cell)
    raw = (output or "").strip()
    if raw.strip() == "ERROR":
        raise RuntimeError(f"read_symbol_ports could not open symbol {lib}/{cell}")
    if not raw.strip():
        raise RuntimeError(f"read_symbol_ports returned empty output for {lib}/{cell}")
    return parse_symbol_ports_output(raw)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "t"
    return str(value)


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _point_value(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [_float_value(item) for item in value[:2]] if len(value) >= 2 else None
    return None


def _bbox_value(value: Any) -> list[list[float]] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    points = [_point_value(point) for point in value[:2]]
    if any(point is None for point in points):
        return None
    return [point for point in points if point is not None]


def _response_fields(response: Any) -> tuple[Any, Any, str]:
    if isinstance(response, dict):
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        errors = response.get("errors") or result.get("errors")
        status = response.get("status") or result.get("status")
        output = response.get("output")
        if output is None:
            output = result.get("output", "")
        if response.get("ok") is False and not errors:
            errors = [response.get("error") or result.get("error") or "request failed"]
        return errors, status, output or ""

    return (
        getattr(response, "errors", None),
        getattr(response, "status", None),
        getattr(response, "output", "") or "",
    )


def _raise_for_symbol_read_error(
    errors: Any,
    status: Any,
    output: Any,
    *,
    lib: str,
    cell: str,
) -> None:
    if errors:
        raise RuntimeError(f"read_symbol_ports SKILL error: {errors[0]}")

    status_value = getattr(status, "value", status)
    if status_value is not None and str(status_value).lower() not in {"success", "ok"}:
        detail = output or f"status={status_value}"
        raise RuntimeError(f"read_symbol_ports SKILL error for {lib}/{cell}: {detail}")
