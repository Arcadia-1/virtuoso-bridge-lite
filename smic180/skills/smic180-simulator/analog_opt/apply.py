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
            transmitted = "progn(\n%s\n)" % skill
            result = self.client.execute_skill(transmitted, timeout=self.timeout)
        except Exception as exc:
            raise ApplyError("bridge execution failed: %s" % exc) from exc
        errors = getattr(result, "errors", ()) or ()
        if errors:
            raise ApplyError("bridge rejected SKILL: %s" % (errors,))
        output = getattr(result, "output", "") or ""
        normalized_lines = [line.strip().strip("\"") for line in output.splitlines()]
        if not any(line.startswith(sentinel) for line in normalized_lines):
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
        if not replace:
            lib, src, dst = map(_quote, (library, source, destination))
            prefix = "ANALOG_OPT_OK:%s" % operation
            skill = (
                f'let((srcCv srcSym dstCv dstSym) '
                f'when(ddGetObj({lib} {dst}) error("destination cell already exists")) '
                f'srcCv=dbOpenCellViewByType({lib} {src} "schematic" "schematic" "r") '
                f'unless(srcCv error("source schematic missing")) '
                f'srcSym=dbOpenCellViewByType({lib} {src} "symbol" nil "r") '
                f'unless(srcSym error("source symbol missing")) '
                f'dstCv=dbCopyCellView(srcCv {lib} {dst} "schematic") '
                f'unless(dstCv error("schematic copy failed")) '
                f'unless(dbSave(dstCv) error("schematic save failed")) '
                f'dstSym=dbCopyCellView(srcSym {lib} {dst} "symbol") '
                f'unless(dstSym error("symbol copy failed")) '
                f'unless(dbSave(dstSym) error("symbol save failed")) '
                f'unless(ddGetObj({lib} {dst} "schematic") error("destination schematic missing")) '
                f'unless(ddGetObj({lib} {dst} "symbol") error("destination symbol missing")) '
                f'when(srcCv dbClose(srcCv)) when(srcSym dbClose(srcSym)) '
                f'when(dstCv dbClose(dstCv)) when(dstSym dbClose(dstSym)) '
                f'"{prefix}:CREATED")'
            )
            output = self._execute(skill, prefix + ":")
            normalized = [line.strip().strip("\"") for line in output.splitlines()]
            if prefix + ":EXISTS" in normalized:
                raise ApplyError("destination cell already exists")
            if prefix + ":CREATED" not in normalized:
                raise ApplyError("bridge did not confirm destination publication")
            return
        nonce = uuid.uuid4().hex[:12]
        temp = _identifier(destination + "__analog_opt_tmp_" + nonce, "temporary cell")
        backup = _identifier(destination + "__analog_opt_backup_" + nonce, "backup cell")
        if len({source, destination, temp, backup}) != 4:
            raise ApplyError("source, destination, temporary, and backup cells must be distinct")
        lib, src, dst, tmp, bak = map(_quote, (library, source, destination, temp, backup))
        prefix = "ANALOG_OPT_OK:%s" % operation
        replace_flag = "t" if replace else "nil"
        recovery = "ANALOG_OPT_RECOVERY_REQUIRED:%s" % backup
        skill = (
            f'prog((srcCv tmpCv oldCv backupCv dstCv restoreCv srcSym oldSym backupSym dstSym restoreSym status tempCreated backupSafe cleanupBackup publishOk symbolPublishOk symbolTxn symbolDeleted) '
            f'status="FAILED" tempCreated=nil backupSafe=nil cleanupBackup=nil publishOk=nil symbolPublishOk=nil symbolDeleted=t '
            f'unwindProtect(progn('
            f'when(ddGetObj({lib} {tmp}) error("temporary cell already exists")) '
            f'when(ddGetObj({lib} {bak}) error("backup cell already exists")) '
            f'srcCv=dbOpenCellViewByType({lib} {src} "schematic" "schematic" "r") '
            f'unless(srcCv error("source schematic missing")) '
            f'srcSym=dbOpenCellViewByType({lib} {src} "symbol" nil "r") '
            f'unless(srcSym error("source symbol missing")) '
            f'tmpCv=dbCopyCellView(srcCv {lib} {tmp} "schematic") '
            f'unless(tmpCv error("temporary copy failed")) tempCreated=t '
            f'unless(dbSave(tmpCv) error("temporary save failed")) '
            f'if(ddGetObj({lib} {dst}) then '
            f'if({replace_flag} then progn('
            f'oldCv=dbOpenCellViewByType({lib} {dst} "schematic" "schematic" "r") '
            f'unless(oldCv error("existing target open failed")) '
            f'oldSym=dbOpenCellViewByType({lib} {dst} "symbol" nil "r") '
            f'unless(oldSym error("existing target symbol open failed")) '
            f'backupCv=dbCopyCellView(oldCv {lib} {bak} "schematic") '
            f'unless(backupCv error("backup copy failed")) '
            f'unless(dbSave(backupCv) error("backup save failed")) '
            f'backupSym=dbCopyCellView(oldSym {lib} {bak} "symbol") '
            f'unless(backupSym error("symbol backup copy failed")) '
            f'unless(dbSave(backupSym) error("symbol backup save failed")) backupSafe=t '
            f'when(oldCv dbClose(oldCv)) oldCv=nil when(oldSym dbClose(oldSym)) oldSym=nil '
            f'unless(dbDeleteCellView({lib} {dst} "schematic") error("target delete failed")) '
            f'dstCv=dbCopyCellView(tmpCv {lib} {dst} "schematic") '
            f'when(dstCv when(dbSave(dstCv) publishOk=t)) '
            f'unless(publishOk progn('
            f'when(dstCv dbClose(dstCv)) dstCv=nil '
            f'when(ddGetObj({lib} {dst}) unless(dbDeleteCellView({lib} {dst} "schematic") error("failed target cleanup failed"))) '
            f'restoreCv=dbCopyCellView(backupCv {lib} {dst} "schematic") '
            f'unless(restoreCv progn(printf("{recovery}") error("rollback restore failed; backup={backup}"))) '
            f'unless(dbSave(restoreCv) progn(printf("{recovery}") error("rollback save failed; backup={backup}"))) '
            f'cleanupBackup=t error("replacement publish failed; rollback restored"))) '
            f'when(publishOk status="REPLACED") '
            f'status="EXISTS") '
            f'else progn(dstCv=dbCopyCellView(tmpCv {lib} {dst} "schematic") '
            f'unless(dstCv error("publish copy failed")) '
            f'unless(dbSave(dstCv) error("destination save failed")) status="CREATED")) '
            f'when(status=="CREATED"||status=="REPLACED" progn('
            f'symbolTxn=errset(progn(when(ddGetObj({lib} {dst} "symbol") symbolDeleted=nil if(dbDeleteCellView({lib} {dst} "symbol") then symbolDeleted=t else error("target symbol delete failed"))) '
            f'dstSym=dbCopyCellView(srcSym {lib} {dst} "symbol") '
            f'unless(dstSym error("symbol publish copy failed")) unless(dbSave(dstSym) error("symbol publish save failed")) t) t) '
            f'if(symbolTxn then symbolPublishOk=t else symbolPublishOk=nil) '
            f'unless(symbolTxn symbolPublishOk=nil) '
            f'unless(symbolPublishOk progn('
            f'when(dstCv dbClose(dstCv)) dstCv=nil when(dstSym dbClose(dstSym)) dstSym=nil '
            f'when(ddGetObj({lib} {dst} "schematic") errset(dbDeleteCellView({lib} {dst} "schematic") t)) '
            f'when(symbolDeleted errset(when(ddGetObj({lib} {dst} "symbol") dbDeleteCellView({lib} {dst} "symbol")) t)) '
            f'if(status=="REPLACED" then progn('
            f'restoreCv=dbCopyCellView(backupCv {lib} {dst} "schematic") '
            f'if(symbolDeleted then restoreSym=dbCopyCellView(backupSym {lib} {dst} "symbol") else restoreSym=t) '
            f'unless(restoreCv&&restoreSym progn(cleanupBackup=nil printf("{recovery}") error("rollback restore failed; backup={backup}"))) '
            f'unless(dbSave(restoreCv)&&(!symbolDeleted||dbSave(restoreSym)) progn(cleanupBackup=nil printf("{recovery}") error("rollback save failed; backup={backup}"))) '
            f'cleanupBackup=t printf("{recovery}") error("symbol publish failed; rollback restored; backup={backup}")) '
            f'error("symbol publish failed; new destination removed"))) '
            f'unless(ddGetObj({lib} {dst} "symbol") error("destination symbol missing")) '
            f'when(status=="REPLACED" cleanupBackup=t) printf("{prefix}:%s" status)))) '
            f'when(srcCv dbClose(srcCv)) when(tmpCv dbClose(tmpCv)) when(oldCv dbClose(oldCv)) when(srcSym dbClose(srcSym)) when(oldSym dbClose(oldSym)) when(dstSym dbClose(dstSym)) '
            f'when(backupCv dbClose(backupCv)) when(backupSym dbClose(backupSym)) when(dstCv dbClose(dstCv)) when(restoreCv dbClose(restoreCv)) when(restoreSym dbClose(restoreSym)) '
            f'when(tempCreated unless(dbDeleteCellView({lib} {tmp} "schematic") error("temporary cleanup failed"))) '
            f'when(backupSafe&&cleanupBackup progn(unless(dbDeleteCellView({lib} {bak} "schematic") error("backup cleanup failed")) unless(dbDeleteCellView({lib} {bak} "symbol") error("symbol backup cleanup failed"))))))'
        )
        output = self._execute(skill, prefix + ":")
        if any(line.strip() == prefix + ":EXISTS" for line in output.splitlines()):
            raise ApplyError("destination cell already exists")
        accepted = {prefix + ":CREATED"}
        if replace:
            accepted.add(prefix + ":REPLACED")
        if not any(line.strip() in accepted for line in output.splitlines()):
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
            "unless(schCheck(cv) error(\"schCheck failed\")) unless(dbSave(cv) error(\"schematic save failed\")) ok=t printf(\"ANALOG_OPT_OK:apply\")) "
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
