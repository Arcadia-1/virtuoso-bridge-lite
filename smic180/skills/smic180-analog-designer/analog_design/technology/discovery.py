"""Evidence-driven live technology discovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .base import DeviceAdapter, TechnologyError, TechnologyProfile


class DiscoveryError(ValueError):
    """Raised when live evidence cannot prove a unique complete profile."""


@dataclass(frozen=True)
class DiscoveryRequest:
    pdk_roots: tuple[str, ...]
    cds_lib_candidates: tuple[str, ...]
    device_candidates: Mapping[str, tuple[tuple[str, str, str], ...]]
    model_sections: Mapping[str, tuple[str, ...]]


class DiscoveryClient(Protocol):
    def existing_paths(self, paths: tuple[str, ...]) -> list[str]: ...
    def probe_device(self, master_ref: str, candidates: tuple[tuple[str, str, str], ...]) -> dict[str, Any] | None: ...


_DEVICE_CONTRACTS: dict[str, tuple[str, dict[str, str], dict[str, str], dict[str, str]]] = {
    "smic180.core_nmos": (
        "mos.nmos",
        {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"},
        {"width": "length", "finger_width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        {"D": "D", "G": "G", "S": "S", "B": "B"},
    ),
    "smic180.core_pmos": (
        "mos.pmos",
        {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"},
        {"width": "length", "finger_width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        {"D": "D", "G": "G", "S": "S", "B": "B"},
    ),
    "smic180.miller_capacitor": (
        "passive.capacitor",
        {"width": "w", "length": "l", "multiplier": "m", "capacitance": "c"},
        {"width": "length", "length": "length", "multiplier": "integer", "capacitance": "capacitance"},
        {"P": "PLUS", "N": "MINUS"},
    ),
}


def _skill_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_output(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
    return text


class VirtuosoDiscoveryClient:
    """Read live Virtuoso metadata and combine it with explicit round-trip evidence."""

    def __init__(
        self,
        client: Any,
        evidence_dir: str | Path,
        roundtrip_evidence: Mapping[str, Mapping[str, Any]],
        *,
        timeout: int = 30,
    ) -> None:
        self.client = client
        self.evidence_dir = Path(evidence_dir)
        self.roundtrip_evidence = dict(roundtrip_evidence)
        self.timeout = timeout

    def _execute(self, expression: str) -> str:
        result = self.client.execute_skill(expression, timeout=self.timeout)
        errors = getattr(result, "errors", ())
        if errors:
            raise DiscoveryError(f"Virtuoso discovery probe failed: {errors}")
        return _decode_output(getattr(result, "output", ""))

    def existing_paths(self, paths: tuple[str, ...]) -> list[str]:
        existing = []
        for path in paths:
            quoted = _skill_quote(path)
            if self._execute(f"if(isDir({quoted}) || isFile({quoted}) then \"EXISTS\" else \"\")") == "EXISTS":
                existing.append(path)
        return existing

    def _probe_candidate(self, library: str, cell: str, view: str) -> dict[str, Any] | None:
        expression = f'''let((obj cv cdf sim out p term modelParam first)
  obj=ddGetObj({_skill_quote(library)} {_skill_quote(cell)})
  out=""
  when(obj
    cv=dbOpenCellViewByType({_skill_quote(library)} {_skill_quote(cell)} {_skill_quote(view)} nil "r")
    when(cv
      out=strcat(out sprintf(nil "MASTER|%s|%s|%s\\n" {_skill_quote(library)} {_skill_quote(cell)} {_skill_quote(view)}))
      out=strcat(out "TERMINALS|") first=t
      foreach(term cv~>terminals unless(first out=strcat(out ",")) out=strcat(out term~>name) first=nil)
      out=strcat(out "\\n")
      cdf=cdfGetBaseCellCDF(obj)
      when(cdf
        sim=get(cdf~>simInfo 'spectre)
        when(sim
          out=strcat(out "SPECTRE_TERMINALS|") first=t
          foreach(term getq(sim termOrder) unless(first out=strcat(out ",")) out=strcat(out sprintf(nil "%L" term)) first=nil)
          out=strcat(out "\\n"))
        modelParam=nil
        foreach(p cdf~>parameters
          when(p~>name=="model" modelParam=p)
          out=strcat(out sprintf(nil "CDF|%s|%L|%L\\n" p~>name p~>defValue p~>callback)))
        when(modelParam out=strcat(out sprintf(nil "MODEL|%s\\n" modelParam~>defValue))))
      dbClose(cv)))
  out)'''
        text = self._execute(expression)
        if not text:
            return None
        record: dict[str, Any] = {"cdf": {}}
        for line in text.splitlines():
            parts = line.split("|", 3)
            if parts[0] == "MASTER" and len(parts) == 4:
                record.update({"library": parts[1], "cell": parts[2], "view": parts[3]})
            elif parts[0] == "TERMINALS" and len(parts) >= 2:
                record["terminals"] = [item for item in parts[1].split(",") if item]
            elif parts[0] == "SPECTRE_TERMINALS" and len(parts) >= 2:
                record["spectre_terminals"] = [item.strip('"') for item in parts[1].split(",") if item]
            elif parts[0] == "MODEL" and len(parts) >= 2:
                record["model"] = parts[1].strip('"')
            elif parts[0] == "CDF" and len(parts) == 4:
                record["cdf"][parts[1]] = {"default": parts[2], "callback": parts[3]}
        required = {"library", "cell", "view", "terminals", "spectre_terminals", "model"}
        return record if required.issubset(record) else None

    def probe_device(self, master_ref: str, candidates: tuple[tuple[str, str, str], ...]) -> dict[str, Any] | None:
        contract = _DEVICE_CONTRACTS.get(master_ref)
        roundtrip = self.roundtrip_evidence.get(master_ref)
        if contract is None or not isinstance(roundtrip, Mapping):
            return None
        matches = [record for candidate in candidates if (record := self._probe_candidate(*candidate)) is not None]
        if len(matches) != 1:
            return None
        record = matches[0]
        device_class, parameter_map, parameter_dimensions, terminal_map = contract
        if not set(parameter_map.values()).issubset(record["cdf"]):
            return None
        if roundtrip.get("netlist_model") != record["model"]:
            return None
        evidence_path = self.evidence_dir / f"{master_ref}.master.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return {
            "device_class": device_class,
            "library": record["library"],
            "cell": record["cell"],
            "view": record["view"],
            "terminals": record["terminals"],
            "parameter_map": parameter_map,
            "parameter_dimensions": parameter_dimensions,
            "terminal_map": terminal_map,
            "evidence": {
                "master": str(evidence_path),
                "terminals": str(evidence_path),
                "cdf": str(roundtrip["evidence_file"]),
            },
            "netlist_model": roundtrip["netlist_model"],
            "netlist_terminals": list(roundtrip["netlist_terminals"]),
            "netlist_parameter_map": dict(roundtrip.get("netlist_parameter_map", {})),
            "parameter_relations": dict(roundtrip.get("parameter_relations", {})),
            "limits": dict(roundtrip.get("limits", {})),
        }


def discover_technology(client: DiscoveryClient, request: DiscoveryRequest, *, plan_only: bool = False) -> TechnologyProfile | dict[str, Any]:
    if plan_only:
        return {
            "pdk_roots": list(request.pdk_roots),
            "cds_lib_candidates": list(request.cds_lib_candidates),
            "device_candidates": {key: [list(item) for item in value] for key, value in request.device_candidates.items()},
            "model_sections": {corner: list(sections) for corner, sections in request.model_sections.items()},
        }
    roots = client.existing_paths(request.pdk_roots)
    if not roots:
        raise DiscoveryError("no requested PDK root exists")
    if len(roots) != 1:
        raise DiscoveryError("multiple PDK roots exist; resolve the conflict explicitly")
    cds_libs = client.existing_paths(request.cds_lib_candidates)
    matching_libs = [path for path in cds_libs if path.startswith(roots[0].rstrip("/") + "/")]
    if len(matching_libs) != 1:
        raise DiscoveryError("exactly one cds.lib under the selected PDK root is required")
    adapters: dict[str, DeviceAdapter] = {}
    for master_ref, candidates in request.device_candidates.items():
        probe = client.probe_device(master_ref, candidates)
        if not isinstance(probe, dict):
            raise DiscoveryError(f"device discovery is incomplete for {master_ref}")
        required = {"device_class", "library", "cell", "view", "terminals", "parameter_map", "parameter_dimensions", "evidence"}
        missing = sorted(required - set(probe))
        if missing:
            raise DiscoveryError(f"device discovery is incomplete for {master_ref}: {', '.join(missing)}")
        try:
            adapter = DeviceAdapter(
                master_ref=master_ref,
                device_class=str(probe["device_class"]),
                library=str(probe["library"]),
                cell=str(probe["cell"]),
                view=str(probe["view"]),
                terminals=tuple(probe["terminals"]),
                parameter_map=dict(probe["parameter_map"]),
                parameter_dimensions=dict(probe["parameter_dimensions"]),
                evidence=dict(probe["evidence"]),
                terminal_map=dict(probe.get("terminal_map", {})),
                netlist_model=probe.get("netlist_model"),
                netlist_terminals=tuple(probe.get("netlist_terminals", ())),
                netlist_parameter_map=dict(probe.get("netlist_parameter_map", {})),
                parameter_relations=dict(probe.get("parameter_relations", {})),
                limits=dict(probe.get("limits", {})),
            )
        except (TechnologyError, TypeError, ValueError) as exc:
            raise DiscoveryError(f"invalid live evidence for {master_ref}: {exc}") from exc
        adapters[master_ref] = adapter
    try:
        return TechnologyProfile(
            "smic180",
            "confirmed",
            adapters,
            {"pdk_root": roots[0], "cds_lib": matching_libs[0]},
            request.model_sections,
        )
    except TechnologyError as exc:
        raise DiscoveryError(str(exc)) from exc
