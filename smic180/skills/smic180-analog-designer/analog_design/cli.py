"""Command line interface for the SMIC180 analog designer."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys

from .report import write_report
from .spec import SpecError, load_design_spec
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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


