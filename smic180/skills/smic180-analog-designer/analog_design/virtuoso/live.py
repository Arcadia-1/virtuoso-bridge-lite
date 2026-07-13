"""Native headless Virtuoso materialization client."""

from __future__ import annotations

from collections.abc import Callable
import json
import math
from pathlib import Path
import re
from typing import Any

from .plan import SchematicPlan


class NativeVirtuosoError(ValueError):
    """Raised when a native Virtuoso database operation is rejected."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NATIVE_VALUE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([fpnumkKMG]?)$")
_FACTORS = {"": 1.0, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise NativeVirtuosoError(f"invalid {label}: {value!r}")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _native_cdf_value(value: Any, dimension: str) -> str:
    if dimension == "string":
        if not isinstance(value, str):
            raise NativeVirtuosoError("string CDF value must be text")
        return value
    if dimension in {"integer", "dimensionless"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise NativeVirtuosoError("integer CDF value must be integral")
        return str(int(value))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise NativeVirtuosoError("numeric CDF value must be finite")
    number = float(value)
    if dimension == "length":
        suffix, factor = ("u", 1e-6) if abs(number) >= 1e-6 else ("n", 1e-9)
    elif dimension == "capacitance":
        suffix, factor = "f", 1e-15
    elif dimension == "current":
        suffix, factor = "u", 1e-6
    elif dimension == "resistance":
        suffix, factor = "k", 1e3
    else:
        suffix, factor = "", 1.0
    return f"{number / factor:.12g}{suffix}"


def _parse_native_cdf_value(text: str, dimension: str) -> dict[str, Any]:
    raw = text.strip().strip('"')
    if dimension == "string":
        return {"value": raw, "raw": raw, "resolution": 0.0}
    match = _NATIVE_VALUE.fullmatch(raw)
    if not match:
        raise NativeVirtuosoError(f"unparseable CDF readback value: {raw!r}")
    number_text, suffix = match.groups()
    number = float(number_text) * _FACTORS[suffix]
    if dimension in {"integer", "dimensionless"}:
        if int(number) != number:
            raise NativeVirtuosoError(f"non-integral CDF readback value: {raw!r}")
        return {"value": int(number), "raw": raw, "resolution": 0.0}
    mantissa, _, exponent_text = number_text.lower().partition("e")
    decimals = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    exponent = int(exponent_text) if exponent_text else 0
    resolution = (10.0 ** (exponent - decimals) * _FACTORS[suffix]) if decimals else 0.0
    return {"value": number, "raw": raw, "resolution": resolution}


class NativeVirtuosoMaterializationClient:
    """Use database connectivity and native PasCdf callbacks in headless CIW."""

    def __init__(self, client: Any, *, exporter: Callable[[Any, str, str, Path], Path | None], timeout: int = 120) -> None:
        self.client = client
        self.exporter = exporter
        self.timeout = timeout

    def _execute(self, expression: str, marker: str | None = None) -> str:
        result = self.client.execute_skill(expression, timeout=self.timeout)
        errors = list(getattr(result, "errors", ()))
        if errors:
            raise NativeVirtuosoError(f"Virtuoso rejected operation: {errors}")
        output = str(getattr(result, "output", "") or "").strip()
        if output.startswith('"') and output.endswith('"'):
            try:
                decoded = json.loads(output)
                if isinstance(decoded, str):
                    output = decoded
            except json.JSONDecodeError:
                output = output.strip('"')
        output = output.replace("\\n", "\n")
        if marker is not None and marker not in output:
            raise NativeVirtuosoError(f"Virtuoso did not confirm {marker}")
        return output

    def cell_exists(self, library: str, cell: str, view: str) -> bool:
        library = _identifier(library, "library")
        cell = _identifier(cell, "cell")
        view = _identifier(view, "view")
        output = self._execute(f'if(ddGetObj({_quote(library)} {_quote(cell)} {_quote(view)}) then "EXISTS" else "MISSING")')
        return "EXISTS" in output

    def preflight_master(self, library: str, cell: str, view: str, terminals: tuple[str, ...]) -> bool:
        library = _identifier(library, "master library")
        cell = _identifier(cell, "master cell")
        view = _identifier(view, "master view")
        expected = tuple(_identifier(name, "terminal") for name in terminals)
        expression = (
            f'let((cv out) cv=dbOpenCellViewByType({_quote(library)} {_quote(cell)} {_quote(view)} nil "r") '
            'unless(cv error("master missing")) out="" '
            'foreach(term cv~>terminals out=strcat(out term~>name "\\n")) dbClose(cv) out)'
        )
        actual = tuple(line.strip() for line in self._execute(expression).splitlines() if line.strip())
        return set(actual) == set(expected)

    def create_schematic(self, plan: SchematicPlan) -> None:
        library = _identifier(plan.library, "library")
        cell = _identifier(plan.target_cell, "target cell")
        create_parts = [
            'let((cv master inst)',
            f'cv=dbOpenCellViewByType({_quote(library)} {_quote(cell)} "schematic" "schematic" "w")',
            'unless(cv error("target schematic create failed"))',
        ]
        for index, item in enumerate(plan.instances):
            x = (index % 4) * 2.0
            y = -(index // 4) * 2.0
            create_parts.extend([
                f'master=dbOpenCellViewByType({_quote(item.library)} {_quote(item.cell)} {_quote(item.view)} nil "r")',
                'unless(master error("master open failed"))',
                f'inst=dbCreateInst(cv master {_quote(_identifier(item.id, "instance"))} list({x:g} {y:g}) "R0")',
                'unless(inst error("instance creation failed"))',
                'dbClose(master)',
            ])
        create_parts.extend(['unless(dbSave(cv) error("initial schematic save failed"))', 'dbClose(cv)', '"NATIVE_INSTANCES_OK")'])
        self._execute(" ".join(create_parts), "NATIVE_INSTANCES_OK")

        from virtuoso_bridge.virtuoso.schematic.ops import schematic_label_instance_term

        connect_parts = [
            'let((cv net master pinFig term pinObj)',
            f'cv=dbOpenCellViewByType({_quote(library)} {_quote(cell)} "schematic" "schematic" "a")',
            'unless(cv error("target schematic reopen failed"))',
        ]
        for item in plan.instances:
            for terminal, net_name in item.terminals.items():
                connect_parts.append(
                    schematic_label_instance_term(
                        item.id,
                        terminal,
                        net_name,
                        cv_expr="cv",
                        cosmetic="clean",
                        auto_rotation=True,
                    )
                )
        connect_parts.append('unless(schCheck(cv) error("labeled connectivity check failed"))')
        for index, (port_name, direction) in enumerate(plan.ports.items()):
            port_name = _identifier(port_name, "port")
            direction = _identifier(direction, "port direction")
            pin_cell = "opin" if direction == "output" else "iopin" if direction == "inputOutput" else "ipin"
            pin_view = "symbolr" if pin_cell == "iopin" else "symbol"
            x = 10.0 if direction == "output" else -4.0
            y = 3.0 - index
            orientation = "R180" if direction == "output" else "R0"
            connect_parts.extend([
                f'net=nil foreach(candidate cv~>nets when(candidate~>name=={_quote(port_name)} net=candidate))',
                f'unless(net error({_quote("port net missing: " + port_name)}))',
                f'master=dbOpenCellViewByType("basic" {_quote(pin_cell)} {_quote(pin_view)} nil "r")',
                'unless(master error("basic pin master missing"))',
                f'pinFig=dbCreateInst(cv master {_quote("PIN_" + port_name)} list({x:g} {y:g}) {_quote(orientation)})',
                'unless(pinFig error("pin figure creation failed"))',
                f'term=dbCreateTerm(net {_quote(port_name)} {_quote(direction)})',
                'unless(term error("port term creation failed"))',
                'pinObj=dbCreatePin(net pinFig)',
                'unless(pinObj error("database pin creation failed"))',
                'dbClose(master)',
            ])
        connect_parts.extend([
            'unless(schCheck(cv) error("final connectivity check failed"))',
            'unless(dbSave(cv) error("connected schematic save failed"))',
            'dbClose(cv)',
            '"NATIVE_CREATE_OK")',
        ])
        self._execute(" ".join(connect_parts), "NATIVE_CREATE_OK")

    def apply_cdf(self, plan: SchematicPlan) -> None:
        parts = [
            'let((cv inst iCDF callbacks done)',
            f'cv=dbOpenCellViewByType({_quote(plan.library)} {_quote(plan.target_cell)} "schematic" "schematic" "a")',
            'unless(cv error("target schematic missing"))',
        ]
        for item in plan.instances:
            parts.extend([
                f'inst=nil foreach(candidate cv~>instances when(candidate~>name=={_quote(item.id)} inst=candidate))',
                'unless(inst error("instance missing"))',
                'iCDF=cdfGetInstCDF(inst)',
                'unless(iCDF error("instance CDF missing"))',
                'PasCdfFormInit(iCDF)',
            ])
            callback_names = []
            for name, value in item.cdf_values.items():
                name = _identifier(name, "CDF parameter")
                native = _native_cdf_value(value, item.cdf_dimensions[name])
                parts.extend([
                    f'unless(get(iCDF {_quote(name)}) error("CDF parameter missing"))',
                    f'PasCdfSetValue(get(iCDF {_quote(name)}) {_quote(native)})',
                ])
                callback_names.append(_quote(name))
            parts.extend([
                f'callbacks=PasCdfCallCallbacks(inst list({" ".join(callback_names)}))',
                'unless(callbacks error("CDF callback failed"))',
                'done=PasCdfDone(inst)',
                'unless(done error("CDF finalization failed"))',
            ])
        parts.extend(['unless(dbSave(cv) error("CDF save failed"))', 'dbClose(cv)', '"NATIVE_CDF_OK")'])
        self._execute(" ".join(parts), "NATIVE_CDF_OK")

    def save_close(self, library: str, cell: str) -> None:
        expression = (
            f'let((cv) cv=dbOpenCellViewByType({_quote(library)} {_quote(cell)} "schematic" "schematic" "a") '
            'unless(cv error("target schematic missing")) unless(dbSave(cv) error("save failed")) dbClose(cv) "NATIVE_CLOSE_OK")'
        )
        self._execute(expression, "NATIVE_CLOSE_OK")

    def reopen_readback(self, plan: SchematicPlan) -> dict[str, dict[str, Any]]:
        parts = [
            'let((cv inst iCDF param out) out=""',
            f'cv=dbOpenCellViewByType({_quote(plan.library)} {_quote(plan.target_cell)} "schematic" "schematic" "r")',
            'unless(cv error("target schematic reopen failed"))',
        ]
        for item in plan.instances:
            parts.extend([
                f'inst=nil foreach(candidate cv~>instances when(candidate~>name=={_quote(item.id)} inst=candidate))',
                'unless(inst error("readback instance missing"))',
                'iCDF=cdfGetInstCDF(inst)',
            ])
            for name in item.cdf_values:
                parts.extend([
                    f'param=get(iCDF {_quote(name)})',
                    'unless(param error("readback CDF parameter missing"))',
                    f'out=strcat(out sprintf(nil {_quote(item.id + "|" + name + "|%L\\n")} param~>value))',
                ])
        parts.extend(['dbClose(cv)', 'out)'])
        output = self._execute(" ".join(parts))
        result: dict[str, dict[str, Any]] = {item.id: {} for item in plan.instances}
        dimensions = {item.id: dict(item.cdf_dimensions) for item in plan.instances}
        for line in output.splitlines():
            pieces = line.strip().split("|", 2)
            if len(pieces) != 3 or pieces[0] not in result or pieces[1] not in dimensions[pieces[0]]:
                continue
            result[pieces[0]][pieces[1]] = _parse_native_cdf_value(pieces[2], dimensions[pieces[0]][pieces[1]])
        return result

    def schcheck_save(self, library: str, cell: str) -> bool:
        expression = (
            f'let((cv ok) cv=dbOpenCellViewByType({_quote(library)} {_quote(cell)} "schematic" "schematic" "a") '
            'unless(cv error("target schematic missing")) ok=schCheck(cv) '
            'unless(ok error("schCheck failed")) unless(dbSave(cv) error("schCheck save failed")) dbClose(cv) "NATIVE_SCHCHECK_OK")'
        )
        return "NATIVE_SCHCHECK_OK" in self._execute(expression, "NATIVE_SCHCHECK_OK")

    def export_si(self, library: str, cell: str, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.exporter(self.client, library, cell, output)
        if result is None:
            raise NativeVirtuosoError("si export failed")
        return Path(result)