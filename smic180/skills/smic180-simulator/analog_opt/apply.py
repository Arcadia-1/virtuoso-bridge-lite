"""Safe Virtuoso cell copying and CDF parameter application."""
from __future__ import annotations

import math
import re
from numbers import Real
from typing import Any, Mapping, Sequence

from .parameters import ParameterSpec
from .units import UnitError, format_quantity


class ApplyError(RuntimeError):
    """Raised when an application request is unsafe or Virtuoso rejects it."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ApplyError("invalid %s identifier" % label)
    return value


def _skill_string(value: str) -> str:
    # Identifiers are validated before reaching this boundary. Keep escaping
    # here as a second line of defence for future non-identifier strings.
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ApplyError("candidate %s must be a finite number" % name)
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ApplyError("candidate %s must be a finite number" % name) from exc
    if not math.isfinite(result):
        raise ApplyError("candidate %s must be a finite number" % name)
    return result


class VirtuosoApplier:
    """Apply validated physical candidates through checked SKILL calls."""

    def __init__(self, client: Any, timeout: int = 30) -> None:
        self.client = client
        self.timeout = timeout

    def _execute(self, skill: str) -> Any:
        try:
            result = self.client.execute_skill(skill, timeout=self.timeout)
        except Exception as exc:
            raise ApplyError("bridge execution failed: %s" % exc) from exc
        errors = getattr(result, "errors", ()) or ()
        output = getattr(result, "output", "") or ""
        if errors or "error" in output.lower():
            detail = errors or output
            raise ApplyError("bridge rejected SKILL: %s" % detail)
        return result

    def _copy_cell(self, library: str, source: str, destination: str, replace: bool) -> None:
        library = _identifier(library, "library")
        source = _identifier(source, "source cell")
        destination = _identifier(destination, "destination cell")
        if source == destination:
            raise ApplyError("source and destination cells must be distinct")
        lib, src, dst = map(_skill_string, (library, source, destination))
        if not replace:
            exists = self._execute("if(ddGetObj(%s %s) then \"EXISTS\" else \"MISSING\")" % (lib, dst))
            if "EXISTS" in (getattr(exists, "output", "") or ""):
                raise ApplyError("destination cell already exists; set replace=true")
        delete = "when(ddGetObj(%s %s) ddDeleteCell(%s %s)) " % (lib, dst, lib, dst) if replace else ""
        skill = (
            "let((srcCv dstCv) %s"
            "srcCv=dbOpenCellViewByType(%s %s \"schematic\" \"schematic\" \"r\") "
            "unless(srcCv error(\"source schematic missing\")) "
            "dstCv=dbCopyCellView(srcCv %s %s \"schematic\") "
            "unless(dstCv error(\"dbCopyCellView failed\")) dbSave(dstCv) dbClose(srcCv) t)"
        ) % (delete, lib, src, lib, dst)
        self._execute(skill)

    def create_work_cell(self, library: str, source_cell: str, work_cell: str, replace: bool) -> None:
        self._copy_cell(library, source_cell, work_cell, bool(replace))

    def apply_cdf(self, library: str, cell: str, specs: Sequence[ParameterSpec], candidate: Mapping[str, Any]) -> None:
        library = _identifier(library, "library")
        cell = _identifier(cell, "cell")
        if not isinstance(candidate, Mapping):
            raise ApplyError("candidate must be a mapping")
        selected = list(specs)
        expected = {spec.name for spec in selected}
        if len(expected) != len(selected) or set(candidate) != expected:
            raise ApplyError("candidate names must exactly match CDF parameter specs")
        updates = []
        for spec in selected:
            if not isinstance(spec, ParameterSpec) or spec.target != "virtuoso_cdf":
                raise ApplyError("apply_cdf accepts only virtuoso_cdf ParameterSpec values")
            instance = _identifier(spec.instance, "instance")
            prop = _identifier(spec.property, "property")
            value = _finite(candidate[spec.name], spec.name)
            try:
                text = format_quantity(value, spec.unit) if spec.unit else "%.12g" % value
            except UnitError as exc:
                raise ApplyError("invalid unit for %s: %s" % (spec.name, exc)) from exc
            assignments = [(prop, text)]
            if prop == "w":
                assignments.append(("fw", text))
            body = " ".join(
                "dbReplaceProp(inst %s \"string\" %s)" % (_skill_string(name), _skill_string(val))
                for name, val in assignments
            )
            updates.append(
                "found=nil foreach(inst cv~>instances when(inst~>name==%s found=t %s)) unless(found error(\"instance not found: %s\"))"
                % (_skill_string(instance), body, instance)
            )
        skill = (
            "let((cv inst found) cv=dbOpenCellViewByType(%s %s \"schematic\" \"schematic\" \"a\") "
            "unless(cv error(\"work schematic missing\")) %s schCheck(cv) dbSave(cv) t)"
        ) % (_skill_string(library), _skill_string(cell), " ".join(updates))
        self._execute(skill)

    def read_cdf(self, library: str, cell: str, specs: Sequence[ParameterSpec]) -> str:
        library = _identifier(library, "library")
        cell = _identifier(cell, "cell")
        requests = []
        for spec in specs:
            if not isinstance(spec, ParameterSpec) or spec.target != "virtuoso_cdf":
                raise ApplyError("read_cdf accepts only virtuoso_cdf ParameterSpec values")
            instance = _identifier(spec.instance, "instance")
            prop = _identifier(spec.property, "property")
            requests.append("list(%s %s)" % (_skill_string(instance), _skill_string(prop)))
        skill = (
            "let((cv result inst) cv=dbOpenCellViewByType(%s %s \"schematic\" \"schematic\" \"r\") "
            "unless(cv error(\"schematic missing\")) result=list() foreach(pair list(%s) "
            "inst=car(setof(x cv~>instances x~>name==car(pair))) "
            "unless(inst error(\"instance not found\")) result=cons(list(car(pair) cadr(pair) dbGetq(inst stringToSymbol(cadr(pair)))) result)) reverse(result))"
        ) % (_skill_string(library), _skill_string(cell), " ".join(requests))
        result = self._execute(skill)
        return getattr(result, "output", "") or ""

    def publish_result_cell(self, library: str, work_cell: str, result_cell: str, source_cell: str, replace: bool) -> None:
        source_cell = _identifier(source_cell, "source cell")
        result_cell = _identifier(result_cell, "result cell")
        if result_cell == source_cell:
            raise ApplyError("result cell cannot overwrite source cell")
        self._copy_cell(library, work_cell, result_cell, bool(replace))
