"""Command line interface for the SMIC180 analog designer."""

from __future__ import annotations

import argparse
import json
import importlib
import os
from pathlib import Path
import sys

from .adapters.optimizer_v2 import prepare_optimizer_v2_handoff
from .adapters.simulator import prepare_simulator_handoff
from .audit import write_audit_addendum
from .ir import load_circuit_ir
from .jsonio import load_strict_json
from .netlist.equivalence import compare_metrics, compare_netlists
from .virtuoso.materialize import materialize_schematic
from .virtuoso.plan import build_schematic_plan
from .report import write_report
from .site import DesignSite
from .spec import SpecError, load_design_spec
from .technology.base import load_technology_profile, write_technology_profile
from .technology.discovery import discover_technology
from .technology.smic180 import create_smic180_discovery_request
from .workflow import DesignWorkflow, WorkflowError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analog_design.py")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-spec")
    validate.add_argument("--spec", type=Path, required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--run-dir", type=Path, required=True)
    build_ir = commands.add_parser("build-ir")
    build_ir.add_argument("--run-dir", type=Path, required=True)
    build_ir.add_argument("--technology-profile", type=Path)
    render = commands.add_parser("render-netlist")
    render.add_argument("--run-dir", type=Path, required=True)
    render.add_argument("--technology-profile", type=Path)
    render.add_argument("--corner", default="tt")
    for name in ("resume", "report", "audit-run"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
    simulate = commands.add_parser("simulate")
    simulate.add_argument("--run-dir", type=Path, required=True)
    simulate.add_argument("--iteration", type=int, required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--run-dir", type=Path, required=True)
    freeze.add_argument("--allow-near-feasible", action="store_true")
    freeze.add_argument("--reason", default="")
    simulator_validation = commands.add_parser("validate-simulator")
    simulator_validation.add_argument("--run-dir", type=Path, required=True)
    simulator_validation.add_argument("--evidence", type=Path, required=True)
    optimizer_binding = commands.add_parser("bind-optimizer-run")
    optimizer_binding.add_argument("--run-dir", type=Path, required=True)
    optimizer_binding.add_argument("--optimizer-run-dir", type=Path, required=True)
    optimizer_binding.add_argument("--expected-pvt-points", type=int, default=45)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--run-dir", type=Path, required=True)
    materialize.add_argument("--technology-profile", type=Path, required=True)
    materialize.add_argument("--library", required=True)
    materialize.add_argument("--source-cell", required=True)
    materialize.add_argument("--target-cell", required=True)
    materialize.add_argument("--plan-only", action="store_true")
    materialize.add_argument("--replace", action="store_true")
    equivalence = commands.add_parser("verify-equivalence")
    equivalence.add_argument("--run-dir", type=Path, required=True)
    equivalence.add_argument("--direct-metrics", type=Path, required=True)
    equivalence.add_argument("--exported-metrics", type=Path, required=True)
    equivalence.add_argument("--tolerances", type=Path, required=True)
    simulator = commands.add_parser("prepare-simulator")
    simulator.add_argument("--run-dir", type=Path, required=True)
    simulator.add_argument("--library", required=True)
    simulator.add_argument("--cell", required=True)
    simulator.add_argument("--technology-profile", type=Path)
    simulator.add_argument("--corner", default="tt")
    optimizer = commands.add_parser("prepare-optimizer")
    optimizer.add_argument("--run-dir", type=Path, required=True)
    optimizer.add_argument("--library", required=True)
    optimizer.add_argument("--source-cell", required=True)
    optimizer.add_argument("--work-cell", required=True)
    optimizer.add_argument("--result-cell", required=True)
    optimizer.add_argument("--testbench-cell", required=True)
    optimizer.add_argument("--cdf-evidence", type=Path, required=True)
    optimizer.add_argument("--bias-mapping", type=Path)
    optimizer.add_argument("--technology-profile", type=Path)
    optimizer.add_argument("--corner", default="tt")
    discovery = commands.add_parser("discover-technology")
    discovery.add_argument("--output", type=Path, required=True)
    discovery.add_argument("--plan-only", action="store_true")
    discovery.add_argument("--evidence-dir", type=Path)
    discovery.add_argument("--roundtrip-evidence", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "discover-technology":
            request = create_smic180_discovery_request()
            if args.plan_only:
                plan = discover_technology(object(), request, plan_only=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
            else:
                if args.evidence_dir is None or args.roundtrip_evidence is None:
                    raise WorkflowError("live discovery requires --evidence-dir and --roundtrip-evidence")
                roundtrip = load_strict_json(args.roundtrip_evidence)
                if not isinstance(roundtrip, dict):
                    raise WorkflowError("roundtrip evidence must be a JSON object keyed by master_ref")
                module = importlib.import_module(os.getenv("ANALOG_DESIGN_DISCOVERY_MODULE", "analog_design.technology.live_bridge"))
                client = module.create_client(args.evidence_dir, roundtrip)
                write_technology_profile(args.output, discover_technology(client, request))
            print(args.output)
            return 0
        if args.command == "validate-spec":
            load_design_spec(args.spec)
            print(f"Valid analog design specification: {args.spec}")
            return 0
        if args.command == "plan":
            workflow = DesignWorkflow.initialize(args.spec, args.run_dir)
            workflow.validate_spec()
            workflow.select_topology()
            workflow.calculate_initial_sizing()
            print(args.run_dir)
            return 0
        if args.command == "build-ir":
            technology = load_technology_profile(args.technology_profile) if args.technology_profile else None
            DesignWorkflow.resume(args.run_dir).build_ir(technology=technology)
            return 0
        if args.command == "render-netlist":
            technology = load_technology_profile(args.technology_profile) if args.technology_profile else None
            model_includes = ()
            if technology is not None:
                site = DesignSite.from_environment()
                model_includes = technology.model_includes(site.model_include, args.corner)
            DesignWorkflow.resume(args.run_dir).render_netlist(model_includes=model_includes, technology=technology)
            return 0
        if args.command == "simulate":
            module = importlib.import_module(os.getenv("ANALOG_DESIGN_LIVE_MODULE", "analog_design.live"))
            workflow = DesignWorkflow.resume(args.run_dir)
            workflow.simulate(module.create_backend(args.run_dir), iteration=args.iteration)
            return 0
        if args.command == "freeze":
            DesignWorkflow.resume(args.run_dir).freeze(allow_near_feasible=args.allow_near_feasible, reason=args.reason)
            return 0
        if args.command == "materialize":
            workflow = DesignWorkflow.resume(args.run_dir)
            technology = load_technology_profile(args.technology_profile)
            ir = load_circuit_ir(args.run_dir / "frozen" / "circuit_ir.json")
            plan = build_schematic_plan(ir, technology, args.library, args.target_cell, source_cell=args.source_cell)
            if args.plan_only:
                result = materialize_schematic(object(), plan, args.run_dir / "virtuoso", plan_only=True)
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            module = importlib.import_module(os.getenv("ANALOG_DESIGN_MATERIALIZATION_MODULE", "analog_design.virtuoso.live_bridge"))
            result = materialize_schematic(module.create_client(args.run_dir), plan, args.run_dir / "virtuoso", replace=args.replace)
            workflow.record_materialization(
                args.run_dir / "virtuoso" / "schematic_plan.json",
                args.run_dir / "virtuoso" / "cdf_readback.json",
                args.run_dir / "virtuoso" / "schcheck.json",
                args.run_dir / "virtuoso" / "exported_netlist.scs",
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "verify-equivalence":
            workflow = DesignWorkflow.resume(args.run_dir)
            direct = (args.run_dir / "frozen" / "design.scs").read_text(encoding="utf-8")
            exported = (args.run_dir / "virtuoso" / "exported_netlist.scs").read_text(encoding="utf-8")
            structural = compare_netlists(direct, exported)
            left = load_strict_json(args.direct_metrics)
            right = load_strict_json(args.exported_metrics)
            tolerances = load_strict_json(args.tolerances)
            if not all(isinstance(item, dict) for item in (left, right, tolerances)):
                raise WorkflowError("equivalence metrics and tolerances must be JSON objects")
            workflow.record_equivalence(structural, compare_metrics(left, right, tolerances, fresh=True))
            return 0
        if args.command == "prepare-simulator":
            workflow = DesignWorkflow.resume(args.run_dir)
            ir = load_circuit_ir(args.run_dir / "frozen" / "circuit_ir.json")
            model_includes = ()
            if args.technology_profile:
                technology = load_technology_profile(args.technology_profile)
                model_includes = technology.model_includes(DesignSite.from_environment().model_include, args.corner)
            outputs = prepare_simulator_handoff(
                ir, args.run_dir / "simulator", library=args.library, cell=args.cell,
                equivalence_confirmed=workflow.state.current == "equivalence_passed", model_includes=model_includes,
            )
            workflow.record_simulator_preparation(outputs.pin_classifications, outputs.sim_config, outputs.review_required)
            return 0
        if args.command == "prepare-optimizer":
            workflow = DesignWorkflow.resume(args.run_dir)
            ir = load_circuit_ir(args.run_dir / "frozen" / "circuit_ir.json")
            cdf = load_strict_json(args.cdf_evidence)
            biases = load_strict_json(args.bias_mapping) if args.bias_mapping else {}
            if not isinstance(cdf, dict) or not isinstance(biases, dict):
                raise WorkflowError("CDF evidence and bias mapping must be JSON objects")
            model_includes = ()
            if args.technology_profile:
                technology = load_technology_profile(args.technology_profile)
                model_includes = technology.model_includes(DesignSite.from_environment().model_include, args.corner)
            outputs = prepare_optimizer_v2_handoff(
                ir, args.run_dir / "optimizer", library=args.library, source_cell=args.source_cell,
                work_cell=args.work_cell, result_cell=args.result_cell, testbench_cell=args.testbench_cell,
                equivalence_confirmed=True, cdf_evidence=cdf, model_includes=model_includes, bias_mapping=biases,
            )
            workflow.record_optimizer_preparation(outputs.config, outputs.baseline, outputs.evidence)
            return 0
        if args.command == "validate-simulator":
            DesignWorkflow.resume(args.run_dir).record_simulator_validation(args.evidence)
            return 0
        if args.command == "bind-optimizer-run":
            workflow = DesignWorkflow.resume(args.run_dir)
            root = args.optimizer_run_dir
            if workflow.state.current == "simulator_validated":
                workflow.record_optimizer_completion(root / "workflow_state.json", root / "result_manifest.json")
            if workflow.state.current == "optimization_complete":
                workflow.record_pvt_completion(root / "pvt_results.json", expected_points=args.expected_pvt_points)
            if workflow.state.current == "pvt_passed":
                workflow.record_publication(root / "workflow_state.json", root / "publication.confirmed.json")
            if workflow.state.current == "published":
                workflow.record_final_validation(
                    root / "final_validation" / "final_validation.confirmed.json",
                    root / "maestro_validation" / "maestro_validation.confirmed.json",
                    expected_points=args.expected_pvt_points,
                )
            print(workflow.state.current)
            return 0
        if args.command == "resume":
            print(DesignWorkflow.resume(args.run_dir).state.current)
            return 0
        if args.command == "report":
            write_report(args.run_dir)
            return 0
        if args.command == "audit-run":
            print(write_audit_addendum(args.run_dir))
            return 0
        return 2
    except (SpecError, WorkflowError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


