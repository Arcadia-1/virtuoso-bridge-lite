#!/usr/bin/env python3
"""Command line interface for analog optimization V2."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from analog_opt.schema import ConfigError, load_config

class CliError(ValueError): pass

def _strict_json(path:Path,label:str):
    def reject(value): raise CliError("%s contains non-finite JSON value: %s"%(label,value))
    try: value=json.loads(path.read_text(encoding="utf-8"),parse_constant=reject)
    except OSError as exc: raise CliError("%s path error %s: %s"%(label,path,exc)) from exc
    except json.JSONDecodeError as exc: raise CliError("invalid %s JSON %s: %s"%(label,path,exc)) from exc
    return value

def _candidate(path:Path):
    value=_strict_json(path,"candidate")
    if not isinstance(value,dict): raise CliError("candidate JSON must be an object")
    for name,item in value.items():
        if not isinstance(name,str) or not name or isinstance(item,bool) or not isinstance(item,(int,float)) or not math.isfinite(float(item)): raise CliError("candidate values must be finite numbers")
    return value

def _live_factory(config,run_dir):
    """Import bridge-dependent assembly only for live commands."""
    try:
        from analog_opt.live import create_workflow
    except ImportError as exc: raise CliError("live adapter factory is unavailable: %s"%exc) from exc
    return create_workflow(config,run_dir)

def _parser():
    parser=argparse.ArgumentParser(prog="analog_optimize.py"); sub=parser.add_subparsers(dest="command",required=True)
    for name in ("validate","evaluate","run"):
        cmd=sub.add_parser(name); cmd.add_argument("--config",type=Path,required=True)
        if name=="evaluate": cmd.add_argument("--candidate",type=Path,required=True)
        if name=="run": cmd.add_argument("--replace-work-cell",action="store_true"); cmd.add_argument("--replace-result-cell",action="store_true")
    for name in ("resume","report"):
        cmd=sub.add_parser(name); cmd.add_argument("--run-dir",type=Path,required=True)
    return parser

def main(argv=None):
    args=_parser().parse_args(argv)
    try:
        if args.command=="validate":
            load_config(args.config); print("Valid analog optimization V2 configuration: %s"%args.config); return 0
        if args.command=="evaluate":
            config=load_config(args.config); candidate=_candidate(args.candidate); run_dir=Path(config.outputs.get("run_dir","output/analog_optimization/evaluate")); workflow=_live_factory(config,run_dir); result=workflow.evaluate(candidate); print(json.dumps(result,allow_nan=False,sort_keys=True)); return 0
        if args.command=="run":
            config=load_config(args.config); run_dir=Path(config.outputs.get("run_dir","output/analog_optimization/run")); workflow=_live_factory(config,run_dir); workflow.run(replace_work_cell=args.replace_work_cell,replace_result_cell=args.replace_result_cell); return 0
        if not args.run_dir.is_dir(): raise CliError("run directory path does not exist: %s"%args.run_dir)
        if args.command=="report":
            manifest=_strict_json(args.run_dir/"result_manifest.json","result manifest"); from analog_opt.report import write_report; print(write_report(args.run_dir,manifest)); return 0
        config=load_config(args.run_dir/"analog_opt_config.resolved.json"); workflow=_live_factory(config,args.run_dir); workflow.resume(); return 0
    except (CliError,ConfigError,ValueError,OSError) as exc:
        print("error: %s"%exc,file=sys.stderr); return 2
    except Exception as exc:
        print("live error: %s"%exc,file=sys.stderr); return 3
if __name__=="__main__": raise SystemExit(main())