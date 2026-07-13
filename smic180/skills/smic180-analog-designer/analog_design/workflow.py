"""Resumable offline analog design workflow."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
from typing import Any

from .artifacts import ArtifactError, ArtifactStore, file_sha256
from .builder import build_circuit_ir
from .ir import CircuitIr, canonical_ir_digest, load_circuit_ir
from .netlist.spectre_writer import SpectreWriter
from .sizing.base import SizingResult
from .sizing.square_law import size_two_stage_miller
from .spec import DesignSpec, load_design_spec
from .technology.base import TechnologyProfile, technology_profile_to_dict
from .technology.smic180 import create_offline_smic180_profile
from .topology.registry import default_registry
from .validation import validate_circuit_ir


class WorkflowError(ValueError):
    """Raised when a workflow gate or resume invariant fails."""


_STATES = (
    "initialized", "spec_validated", "topology_selected",
    "initial_sizing_complete", "ir_validated", "windows_nominal_passed",
    "candidate_frozen", "schematic_created", "cdf_roundtrip_passed",
    "schematic_checked", "equivalence_passed", "simulator_validated",
    "optimization_complete", "pvt_passed", "published",
    "final_validation_passed",
)


class WorkflowState:
    def __init__(self, path: Path, current: str, transitions: list[dict[str, Any]]) -> None:
        self.path = path
        self.current = current
        self.transitions = transitions
        self.store = ArtifactStore(path.parent)

    @classmethod
    def create(cls, path: str | Path) -> "WorkflowState":
        state = cls(Path(path), "initialized", [])
        state._save()
        return state

    @classmethod
    def load(cls, path: str | Path) -> "WorkflowState":
        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            state = cls(target, data["current"], list(data["transitions"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise WorkflowError(f"invalid workflow state: {exc}") from exc
        if state.current not in _STATES:
            raise WorkflowError(f"unknown workflow state: {state.current}")
        return state

    def _save(self) -> None:
        self.store.write_json(self.path, {"current": self.current, "transitions": self.transitions})

    def advance(self, target: str, evidence: dict[str, str]) -> None:
        expected_index = _STATES.index(self.current) + 1
        expected = _STATES[expected_index] if expected_index < len(_STATES) else None
        if target != expected:
            raise WorkflowError(f"expected {expected}, cannot advance to {target}")
        self.transitions.append({"from": self.current, "to": target, "evidence": dict(evidence)})
        self.current = target
        self._save()


class DesignWorkflow:
    def __init__(self, run_dir: Path, state: WorkflowState) -> None:
        self.run_dir = run_dir
        self.state = state
        self.store = ArtifactStore(run_dir)

    @classmethod
    def initialize(cls, spec_path: str | Path, run_dir: str | Path) -> "DesignWorkflow":
        source = Path(spec_path)
        target = Path(run_dir)
        if target.exists():
            raise WorkflowError(f"run directory already exists: {target}")
        target.mkdir(parents=True)
        for relative in ArtifactStore.LAYOUT:
            (target / relative).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target / "inputs" / "design_spec.json")
        state = WorkflowState.create(target / "workflow_state.json")
        workflow = cls(target, state)
        workflow.store.write_json(target / "manifest.json", {"version": 1, "spec": "inputs/design_spec.json"})
        return workflow

    @classmethod
    def resume(cls, run_dir: str | Path) -> "DesignWorkflow":
        target = Path(run_dir)
        state = WorkflowState.load(target / "workflow_state.json")
        workflow = cls(target, state)
        for transition in state.transitions:
            for marker in transition.get("evidence", {}).values():
                try:
                    workflow.store.verify_confirmation(target / marker)
                except ArtifactError as exc:
                    raise WorkflowError(str(exc)) from exc
        return workflow

    def _record_failure(self, stage: str, exc: Exception) -> None:
        path = self.run_dir / "failed_attempts.json"
        if path.is_file():
            try:
                failures = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                failures = []
        else:
            failures = []
        failures.append({"stage": stage, "error": str(exc), "type": type(exc).__name__})
        self.store.write_json(path, failures)

    def _guard(self, stage: str, operation):
        try:
            return operation()
        except Exception as exc:
            self._record_failure(stage, exc)
            if isinstance(exc, WorkflowError):
                raise
            raise WorkflowError(str(exc)) from exc

    @property
    def spec_path(self) -> Path:
        return self.run_dir / "inputs" / "design_spec.json"

    def validate_spec(self) -> DesignSpec:
        def operation():
            if self.state.current != "initialized":
                raise WorkflowError("validate_spec requires initialized state")
            spec = load_design_spec(self.spec_path)
            marker = self.store.confirm(self.run_dir / "inputs" / "spec_validated.confirmed.json", "spec_validated", [self.spec_path])
            self.state.advance("spec_validated", {"confirmation": str(marker.relative_to(self.run_dir))})
            return spec
        return self._guard("spec_validated", operation)

    def select_topology(self):
        def operation():
            if self.state.current != "spec_validated":
                raise WorkflowError("select_topology requires spec_validated state")
            spec = load_design_spec(self.spec_path)
            plan = default_registry().create(str(spec.circuit["topology"]), spec.interfaces)
            path = self.run_dir / "topology" / "topology_plan.json"
            self.store.write_json(path, {
                "id": plan.id, "ports": list(plan.ports), "nets": list(plan.nets),
                "instances": [{"id": item.id, "role": item.role, "device_class": item.device_class, "terminals": dict(item.terminals), "enabled": item.enabled} for item in plan.instances],
                "matching_groups": {key: list(value) for key, value in plan.matching_groups.items()},
                "selection_basis": list(plan.selection_basis), "known_limits": list(plan.known_limits),
            })
            marker = self.store.confirm(self.run_dir / "topology" / "topology_selected.confirmed.json", "topology_selected", [path])
            self.state.advance("topology_selected", {"confirmation": str(marker.relative_to(self.run_dir))})
            return plan
        return self._guard("topology_selected", operation)

    def calculate_initial_sizing(self) -> SizingResult:
        def operation():
            if self.state.current != "topology_selected":
                raise WorkflowError("calculate_initial_sizing requires topology_selected state")
            spec = load_design_spec(self.spec_path)
            topology = default_registry().create(str(spec.circuit["topology"]), spec.interfaces)
            result = size_two_stage_miller(spec, topology)
            path = self.run_dir / "sizing" / "initial_sizing.json"
            self.store.write_json(path, {
                "records": {name: {"formula_id": item.formula_id, "inputs": dict(item.inputs), "assumptions": list(item.assumptions), "dimension": item.dimension, "value": item.value, "status": item.status, "confidence": item.confidence} for name, item in result.records.items()},
                "confirmed_values": dict(result.confirmed_values),
            })
            marker = self.store.confirm(self.run_dir / "sizing" / "initial_sizing_complete.confirmed.json", "initial_sizing_complete", [path])
            self.state.advance("initial_sizing_complete", {"confirmation": str(marker.relative_to(self.run_dir))})
            return result
        return self._guard("initial_sizing_complete", operation)

    def _recompute_design(self) -> tuple[DesignSpec, Any, SizingResult]:
        spec = load_design_spec(self.spec_path)
        topology = default_registry().create(str(spec.circuit["topology"]), spec.interfaces)
        sizing = size_two_stage_miller(spec, topology)
        return spec, topology, sizing

    def build_ir(self, *, technology: TechnologyProfile | None = None) -> CircuitIr:
        def operation():
            if self.state.current != "initial_sizing_complete":
                raise WorkflowError("build_ir requires initial_sizing_complete state")
            spec, topology, sizing = self._recompute_design()
            profile = technology or create_offline_smic180_profile()
            ir = build_circuit_ir(spec, topology, sizing, profile)
            validate_circuit_ir(ir)
            path = self.run_dir / "ir" / "circuit_ir.json"
            self.store.write_json(path, dict(ir.source_data))
            dependencies = [path]
            if profile.state == "confirmed":
                profile.require_live_ready()
                profile_path = self.run_dir / "ir" / "technology_profile.json"
                self.store.write_json(profile_path, technology_profile_to_dict(profile))
                dependencies.append(profile_path)
            marker = self.store.confirm(self.run_dir / "ir" / "ir_validated.confirmed.json", "ir_validated", dependencies)
            self.state.advance("ir_validated", {"confirmation": str(marker.relative_to(self.run_dir))})
            return ir
        return self._guard("ir_validated", operation)

    def render_netlist(
        self,
        model_includes: tuple[tuple[str, str | None], ...] = (),
        *,
        technology: TechnologyProfile | None = None,
    ) -> Path:
        if self.state.current != "ir_validated":
            raise WorkflowError("render_netlist requires ir_validated state")
        ir = load_circuit_ir(self.run_dir / "ir" / "circuit_ir.json")
        if ir.technology.get("profile_state") == "confirmed":
            if technology is None:
                raise WorkflowError("confirmed IR netlisting requires its confirmed technology profile")
            technology.require_live_ready()
            if technology.name != ir.technology.get("profile"):
                raise WorkflowError("technology profile does not match the Circuit IR")
        path = self.run_dir / "windows_sim" / "generated" / "design.scs"
        self.store.write_text(path, SpectreWriter(model_includes, technology=technology).render(ir))
        self.store.confirm(self.run_dir / "windows_sim" / "generated" / "netlist_generated.confirmed.json", "netlist_generated", [self.run_dir / "ir" / "circuit_ir.json", path])
        return path

    def simulate(self, backend, *, iteration: int):
        def operation():
            if self.state.current != "ir_validated":
                raise WorkflowError("simulate requires ir_validated state")
            deck = self.run_dir / "windows_sim" / "generated" / "design.scs"
            if not deck.is_file():
                raise WorkflowError("generated Spectre deck is missing")
            result = backend.run(deck, self.run_dir / "windows_sim" / "iterations", iteration)
            measurements_path = self.run_dir / "windows_sim" / "measurements.json"
            scopes_path = self.run_dir / "windows_sim" / "measurement_scopes.json"
            diagnosis_path = self.run_dir / "windows_sim" / "diagnosis.json"
            self.store.write_json(measurements_path, result.measurements)
            self.store.write_json(scopes_path, result.measurement_scopes)
            self.store.write_json(diagnosis_path, result.diagnostics)
            marker = self.store.confirm(
                self.run_dir / "windows_sim" / "windows_nominal_passed.confirmed.json",
                "windows_nominal_passed",
                [deck, result.run_dir / "measurements.json", result.run_dir / "measurement_scopes.json", result.run_dir / "operating_points.json", result.run_dir / "diagnosis.json"],
            )
            self.state.advance("windows_nominal_passed", {"confirmation": str(marker.relative_to(self.run_dir))})
            return result
        return self._guard("windows_nominal_passed", operation)

    def _hard_spec_results(self) -> dict[str, dict[str, object]]:
        spec = load_design_spec(self.spec_path)
        measurements = json.loads((self.run_dir / "windows_sim" / "measurements.json").read_text(encoding="utf-8"))
        results: dict[str, dict[str, object]] = {}
        operators = {
            ">=": lambda actual, target: actual >= target,
            "<=": lambda actual, target: actual <= target,
            ">": lambda actual, target: actual > target,
            "<": lambda actual, target: actual < target,
            "==": lambda actual, target: actual == target,
        }
        for metric in spec.metrics:
            if metric.kind != "hard":
                continue
            actual = measurements.get(metric.id)
            passed = isinstance(actual, (int, float)) and not isinstance(actual, bool)
            if passed and metric.operator:
                passed = operators[metric.operator](float(actual), metric.value)
            results[metric.id] = {"actual": actual, "target": metric.value, "operator": metric.operator, "passed": bool(passed)}
        return results

    def freeze(self, *, allow_near_feasible: bool = False, reason: str = "") -> Path:
        def operation():
            if self.state.current != "windows_nominal_passed":
                raise WorkflowError("freeze requires windows_nominal_passed state")
            hard_results = self._hard_spec_results()
            failures = [name for name, item in hard_results.items() if not item["passed"]]
            near = bool(failures)
            if failures and not allow_near_feasible:
                raise WorkflowError("hard specification failures block freeze: " + ", ".join(failures))
            if near and not reason.strip():
                raise WorkflowError("near-feasible freeze requires a reason")
            source_ir = self.run_dir / "ir" / "circuit_ir.json"
            source_deck = self.run_dir / "windows_sim" / "generated" / "design.scs"
            frozen_ir = self.run_dir / "frozen" / "circuit_ir.json"
            frozen_deck = self.run_dir / "frozen" / "design.scs"
            shutil.copyfile(source_ir, frozen_ir)
            shutil.copyfile(source_deck, frozen_deck)
            manifest = self.run_dir / "frozen" / "candidate_manifest.json"
            self.store.write_json(manifest, {"near_feasible": near, "reason": reason, "hard_specs": hard_results, "ir_sha256": file_sha256(frozen_ir), "deck_sha256": file_sha256(frozen_deck)})
            marker = self.store.confirm(self.run_dir / "frozen" / "candidate_frozen.confirmed.json", "candidate_frozen", [frozen_ir, frozen_deck, manifest])
            self.state.advance("candidate_frozen", {"confirmation": str(marker.relative_to(self.run_dir))})
            return manifest
        return self._guard("candidate_frozen", operation)

    def record_materialization(self, plan: str | Path, cdf_readback: str | Path, schcheck: str | Path, exported_netlist: str | Path) -> None:
        if self.state.current != "candidate_frozen":
            raise WorkflowError("materialization evidence requires candidate_frozen state")
        plan_path = Path(plan)
        cdf_path = Path(cdf_readback)
        check_path = Path(schcheck)
        netlist_path = Path(exported_netlist)
        created = self.store.confirm(self.run_dir / "virtuoso" / "schematic_created.confirmed.json", "schematic_created", [plan_path])
        self.state.advance("schematic_created", {"confirmation": str(created.relative_to(self.run_dir))})
        roundtrip = self.store.confirm(self.run_dir / "virtuoso" / "cdf_roundtrip.confirmed.json", "cdf_roundtrip_passed", [plan_path, cdf_path])
        self.state.advance("cdf_roundtrip_passed", {"confirmation": str(roundtrip.relative_to(self.run_dir))})
        checked = self.store.confirm(self.run_dir / "virtuoso" / "schematic_checked.confirmed.json", "schematic_checked", [check_path, netlist_path])
        self.state.advance("schematic_checked", {"confirmation": str(checked.relative_to(self.run_dir))})

    def record_equivalence(self, structural: dict[str, Any], simulation: dict[str, Any]) -> Path:
        if self.state.current != "schematic_checked":
            raise WorkflowError("equivalence evidence requires schematic_checked state")
        from .netlist.equivalence import EquivalenceError, write_equivalence_confirmation
        try:
            marker = write_equivalence_confirmation(self.run_dir / "equivalence", structural, simulation)
        except EquivalenceError as exc:
            raise WorkflowError(str(exc)) from exc
        self.state.advance("equivalence_passed", {"confirmation": str(marker.relative_to(self.run_dir))})
        return marker

    def record_simulator_handoff(self, pins: str | Path, config: str | Path, review: str | Path) -> Path:
        if self.state.current != "equivalence_passed":
            raise WorkflowError("simulator handoff requires equivalence_passed state")
        marker = self.store.confirm(self.run_dir / "simulator" / "simulator_validated.confirmed.json", "simulator_validated", [pins, config, review])
        self.state.advance("simulator_validated", {"confirmation": str(marker.relative_to(self.run_dir))})
        return marker

    def record_optimizer_preparation(self, config: str | Path, baseline: str | Path, evidence: str | Path) -> Path:
        if self.state.current != "simulator_validated":
            raise WorkflowError("optimizer preparation requires simulator_validated state")
        return self.store.confirm(self.run_dir / "optimizer" / "prepared.confirmed.json", "optimizer_prepared", [config, baseline, evidence])

    def record_optimizer_completion(self, external_confirmation: str | Path) -> Path:
        if self.state.current != "simulator_validated":
            raise WorkflowError("optimizer completion requires simulator_validated state")
        source = Path(external_confirmation)
        if not source.is_file() or "confirmed" not in source.name:
            raise WorkflowError("optimizer completion requires an external confirmed run artifact")
        marker = self.store.confirm(self.run_dir / "optimizer" / "optimization_complete.confirmed.json", "optimization_complete", [source])
        self.state.advance("optimization_complete", {"confirmation": str(marker.relative_to(self.run_dir))})
        return marker
