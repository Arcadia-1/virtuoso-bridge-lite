"""Safe Virtuoso cell copying and CDF parameter application."""
from __future__ import annotations

import math
import re
import uuid
from numbers import Real
from typing import Any, Dict, Mapping, Sequence

from .parameters import ParameterSpec
from .units import UnitError, format_quantity, parse_quantity


class ApplyError(RuntimeError):
    """Raised when validation or a structured bridge operation fails."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")
_UNIT_DIMENSIONS = {
    "V": "voltage", "mV": "voltage", "uV": "voltage",
    "A": "current", "mA": "current", "uA": "current", "nA": "current",
    "F": "capacitance", "nF": "capacitance", "pF": "capacitance",
    "Ohm": "resistance", "kOhm": "resistance", "MOhm": "resistance",
    "Hz": "frequency", "kHz": "frequency", "MHz": "frequency", "GHz": "frequency",
    "s": "time", "ms": "time", "us": "time", "ns": "time",
    "m": "length", "mm": "length", "um": "length", "nm": "length",
    "W": "power", "mW": "power", "uW": "power",
}


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ApplyError("invalid %s identifier" % label)
    return value


def _quote(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _number(value: Any, spec: ParameterSpec) -> Real:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ApplyError("candidate %s must be a finite number" % spec.name)
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ApplyError("candidate %s must be a finite number" % spec.name) from exc
    if not math.isfinite(result):
        raise ApplyError("candidate %s must be a finite number" % spec.name)
    if spec.dtype == "int":
        if not result.is_integer():
            raise ApplyError("candidate %s must be an integer" % spec.name)
        return int(result)
    return result


class VirtuosoApplier:
    """Apply validated physical candidates through machine-readable SKILL calls."""

    def __init__(self, client: Any, timeout: int = 30) -> None:
        self.client = client
        self.timeout = timeout

    def _execute(self, skill: str, sentinel: str) -> str:
        try:
            result = self.client.execute_skill(skill, timeout=self.timeout)
        except Exception as exc:
            raise ApplyError("bridge execution failed: %s" % exc) from exc
        errors = getattr(result, "errors", ()) or ()
        if errors:
            raise ApplyError("bridge rejected SKILL: %s" % (errors,))
        output = getattr(result, "output", "") or ""
        if not any(line.strip().startswith(sentinel) for line in output.splitlines()):
            raise ApplyError("bridge response missing sentinel %s" % sentinel)
        return output

    @staticmethod
    def _cdf_specs(specs: Sequence[ParameterSpec]) -> Sequence[ParameterSpec]:
        result = list(specs)
        names = set()
        for spec in result:
            if not isinstance(spec, ParameterSpec) or spec.target != "virtuoso_cdf":
                raise ApplyError("operation accepts only virtuoso_cdf ParameterSpec values")
            if spec.name in names:
                raise ApplyError("parameter names must be unique")
            names.add(spec.name)
            _identifier(spec.name, "parameter")
            _identifier(spec.instance, "instance")
            _identifier(spec.property, "property")
        return result

    def _copy_new(self, library: str, source: str, destination: str, operation: str, replace: bool) -> None:
        library = _identifier(library, "library")
        source = _identifier(source, "source cell")
        destination = _identifier(destination, "destination cell")
        if source == destination:
            raise ApplyError("source and destination cells must be distinct")
        temp = _identifier(destination + "__analog_opt_tmp", "temporary cell")
        backup = _identifier(destination + "__analog_opt_backup_" + uuid.uuid4().hex[:12], "backup cell")
        lib, src, dst, tmp, bak = map(_quote, (library, source, destination, temp, backup))
        prefix = "ANALOG_OPT_OK:%s" % operation
        replace_flag = "t" if replace else "nil"
        skill = (
            f'let((srcCv tmpCv oldCv backupCv dstCv restoreCv status replacing) status="FAILED" replacing=nil '
            f'unwindProtect(progn('
            f'when(ddGetObj({lib} {tmp}) dbDeleteCellView({lib} {tmp} "schematic")) '
            f'when(ddGetObj({lib} {bak}) dbDeleteCellView({lib} {bak} "schematic")) '
            f'srcCv=dbOpenCellViewByType({lib} {src} "schematic" "schematic" "r") '
            f'unless(srcCv error("source schematic missing")) '
            f'tmpCv=dbCopyCellView(srcCv {lib} {tmp} "schematic") '
            f'unless(tmpCv error("temporary copy failed")) dbSave(tmpCv) '
            f'if(ddGetObj({lib} {dst}) then '
            f'if({replace_flag} then progn('
            f'oldCv=dbOpenCellViewByType({lib} {dst} "schematic" "schematic" "r") '
            f'unless(oldCv error("existing target open failed")) '
            f'backupCv=dbCopyCellView(oldCv {lib} {bak} "schematic") '
            f'unless(backupCv error("backup copy failed")) dbSave(backupCv) replacing=t when(oldCv dbClose(oldCv)) oldCv=nil '
            f'unless(dbDeleteCellView({lib} {dst} "schematic") error("target delete failed")) '
            f'dstCv=dbCopyCellView(tmpCv {lib} {dst} "schematic") '
            f'unless(dstCv progn('
            f'when(ddGetObj({lib} {dst}) dbDeleteCellView({lib} {dst} "schematic")) '
            f'restoreCv=dbCopyCellView(backupCv {lib} {dst} "schematic") '
            f'unless(restoreCv error("rollback restore failed")) dbSave(restoreCv) error("replacement publish failed"))) '
            f'dbSave(dstCv) status="REPLACED") status="EXISTS") '
            f'else progn(dstCv=dbCopyCellView(tmpCv {lib} {dst} "schematic") '
            f'unless(dstCv error("publish copy failed")) dbSave(dstCv) status="CREATED")) '
            f'unless(status=="FAILED" printf("{prefix}:%s" status))) '
            f'when(srcCv dbClose(srcCv)) when(tmpCv dbClose(tmpCv)) when(oldCv dbClose(oldCv)) '
            f'when(backupCv dbClose(backupCv)) when(dstCv dbClose(dstCv)) when(restoreCv dbClose(restoreCv)) '
            f'when(ddGetObj({lib} {tmp}) dbDeleteCellView({lib} {tmp} "schematic")) '
            f'when(ddGetObj({lib} {bak}) dbDeleteCellView({lib} {bak} "schematic"))))'
        )
        output = self._execute(skill, prefix + ":")
        if any(line.strip() == prefix + ":EXISTS" for line in output.splitlines()):
            raise ApplyError("destination cell already exists")
        expected = prefix + (":REPLACED" if replace else ":CREATED")
        if not any(line.strip() == expected for line in output.splitlines()):
            raise ApplyError("bridge did not confirm destination publication")
    def create_work_cell(self, library: str, source_cell: str, work_cell: str, replace: bool) -> None:
        # replace is accepted for API compatibility, but an existing destination
        # is never destroyed because this bridge has no atomic cell swap.
        self._copy_new(library, source_cell, work_cell, "create", bool(replace))

    def apply_cdf(self, library: str, cell: str, specs: Sequence[ParameterSpec], candidate: Mapping[str, Any]) -> None:
        library = _identifier(library, "library")
        cell = _identifier(cell, "cell")
        selected = self._cdf_specs(specs)
        if not isinstance(candidate, Mapping) or set(candidate) != {spec.name for spec in selected}:
            raise ApplyError("candidate names must exactly match CDF parameter specs")
        prepared = []
        for index, spec in enumerate(selected):
            value = _number(candidate[spec.name], spec)
            try:
                text = format_quantity(value, spec.unit) if spec.unit else (str(value) if spec.dtype == "int" else "%.12g" % value)
            except UnitError as exc:
                raise ApplyError("invalid unit for %s: %s" % (spec.name, exc)) from exc
            sync_property = _identifier(spec.sync_property, "sync property") if spec.sync_property is not None else None
            prepared.append((index, spec, text, sync_property))
        declarations = "param " + " ".join("inst%d cdf%d param%d syncProp%d" % (i, i, i, i) for i, _, _, _ in prepared)
        locate_instances = []
        locate_params = []
        writes = []
        verifies = []
        for index, spec, text, sync_property in prepared:
            locate_instances.append(
                "foreach(inst cv~>instances when(inst~>name==%s inst%d=inst)) unless(inst%d error(\"instance not found: %s\"))"
                % (_quote(spec.instance), index, index, spec.instance)
            )
            locate_params.append(
                "cdf%d=cdfGetInstCDF(inst%d) unless(cdf%d error(\"CDF unavailable: %s\")) "
                "foreach(param cdf%d~>parameters when(param~>name==%s param%d=param)) "
                "unless(param%d error(\"CDF parameter missing: %s.%s\"))"
                % (index, index, index, spec.instance, index, _quote(spec.property), index, index, spec.instance, spec.property)
            )
            writes.append("param%d~>value=%s" % (index, _quote(text)))
            if sync_property is not None:
                writes.append("syncProp%d=dbReplaceProp(inst%d %s \"string\" %s) unless(syncProp%d error(\"sync property failed: %s.%s\"))" % (index, index, _quote(sync_property), _quote(text), index, spec.instance, sync_property))
                verifies.append("unless(syncProp%d~>value==%s error(\"sync verification failed: %s.%s\"))" % (index, _quote(text), spec.instance, sync_property))
            verifies.append(
                "unless(param%d~>value==%s error(\"CDF verification failed: %s.%s\"))"
                % (index, _quote(text), spec.instance, spec.property)
            )
        skill = (
            "let((cv inst %s ok) ok=nil cv=dbOpenCellViewByType(%s %s \"schematic\" \"schematic\" \"a\") "
            "unless(cv error(\"work schematic missing\")) unwindProtect(progn(%s %s %s %s "
            "when(schCheck(cv) dbSave(cv) ok=t) unless(ok error(\"schCheck failed\")) printf(\"ANALOG_OPT_OK:apply\")) "
            "when(cv dbClose(cv))))"
        ) % (declarations, _quote(library), _quote(cell), " ".join(locate_instances), " ".join(locate_params), " ".join(writes), " ".join(verifies))
        self._execute(skill, "ANALOG_OPT_OK:apply")

    def read_cdf(self, library: str, cell: str, specs: Sequence[ParameterSpec]) -> Dict[str, Real]:
        library = _identifier(library, "library")
        cell = _identifier(cell, "cell")
        selected = self._cdf_specs(specs)
        rows = []
        for spec in selected:
            rows.append(
                "inst=nil param=nil foreach(x cv~>instances when(x~>name==%s inst=x)) unless(inst error(\"instance not found\")) "
                "foreach(p cdfGetInstCDF(inst)~>parameters when(p~>name==%s param=p)) "
                "unless(param error(\"CDF parameter missing\")) printf(\"\\n%s\\t%%s\" param~>value)"
                % (_quote(spec.instance), _quote(spec.property), spec.name)
            )
        skill = (
            "let((cv inst x p param) cv=dbOpenCellViewByType(%s %s \"schematic\" \"schematic\" \"r\") "
            "unless(cv error(\"schematic missing\")) unwindProtect(progn(printf(\"ANALOG_OPT_OK:read\") %s) when(cv dbClose(cv))))"
        ) % (_quote(library), _quote(cell), " ".join(rows))
        output = self._execute(skill, "ANALOG_OPT_OK:read")
        parsed: Dict[str, Real] = {}
        expected = {spec.name: spec for spec in selected}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line == "ANALOG_OPT_OK:read":
                continue
            parts = line.split("\t")
            if len(parts) != 2 or parts[0] not in expected:
                continue
            name, text = parts
            if name in parsed:
                raise ApplyError("duplicate read value for %s" % name)
            spec = expected[name]
            try:
                if spec.dtype == "int":
                    numeric = float(text)
                    if not math.isfinite(numeric):
                        raise ApplyError("read value must be finite")
                    if not numeric.is_integer():
                        raise ApplyError("read value for %s must be an integer" % name)
                    parsed[name] = int(numeric)
                elif spec.unit:
                    parsed[name] = parse_quantity(text, _UNIT_DIMENSIONS[spec.unit])
                else:
                    numeric = float(text)
                    if not math.isfinite(numeric):
                        raise ApplyError("read value must be finite")
                    parsed[name] = numeric
            except (ValueError, UnitError, KeyError) as exc:
                raise ApplyError("read value for %s must be finite and valid" % name) from exc
        missing = set(expected) - set(parsed)
        if missing:
            raise ApplyError("missing read values: %s" % ", ".join(sorted(missing)))
        return parsed
    def publish_result_cell(self, library: str, work_cell: str, result_cell: str, source_cell: str, replace: bool) -> None:
        work_cell = _identifier(work_cell, "work cell")
        result_cell = _identifier(result_cell, "result cell")
        source_cell = _identifier(source_cell, "source cell")
        if len({work_cell, result_cell, source_cell}) != 3:
            raise ApplyError("source, work, and result cells must be distinct")
        self._copy_new(library, work_cell, result_cell, "publish", bool(replace))
