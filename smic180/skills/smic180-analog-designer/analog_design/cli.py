"""Command line interface for the SMIC180 analog designer."""

from __future__ import annotations

import argparse
import json
import importlib
import os
from pathlib import Path
import sys

from .jsonio import load_strict_json
from .report import write_report
from .spec import SpecError, load_design_spec
from .technology.base import write_technology_profile
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
    for name in ("build-ir", "render-netlist", "resume", "report"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
    simulate = commands.add_parser("simulate")
    simulate.add_argument("--run-dir", type=Path, required=True)
    simulate.add_argument("--iteration", type=int, required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--run-dir", type=Path, required=True)
    freeze.add_argument("--allow-near-feasible", action="store_true")
    freeze.add_argument("--reason", default="")
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
            DesignWorkflow.resume(args.run_dir).build_ir()
            return 0
        if args.command == "render-netlist":
            DesignWorkflow.resume(args.run_dir).render_netlist()
            return 0
        if args.command == "simulate":
            module = importlib.import_module(os.getenv("ANALOG_DESIGN_LIVE_MODULE", "analog_design.live"))
            workflow = DesignWorkflow.resume(args.run_dir)
            workflow.simulate(module.create_backend(args.run_dir), iteration=args.iteration)
            return 0
        if args.command == "freeze":
            DesignWorkflow.resume(args.run_dir).freeze(allow_near_feasible=args.allow_near_feasible, reason=args.reason)
            return 0
        if args.command == "resume":
            print(DesignWorkflow.resume(args.run_dir).state.current)
            return 0
        if args.command == "report":
            write_report(args.run_dir)
            return 0
        return 2
    except (SpecError, WorkflowError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


