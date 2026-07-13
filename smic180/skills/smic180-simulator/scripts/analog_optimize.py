#!/usr/bin/env python3
"""Command line interface for analog optimization V2."""
from __future__ import annotations
import argparse,importlib,json,math,os
from dataclasses import asdict,is_dataclass
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
while str(ROOT) in sys.path: sys.path.remove(str(ROOT))
sys.path.insert(0,str(ROOT))
from analog_opt.schema import ConfigError,load_config
class CliError(ValueError): pass
def _strict_json(path,label):
 def reject(value): raise CliError('%s contains non-finite JSON value: %s'%(label,value))
 try: return json.loads(Path(path).read_text(encoding='utf-8'),parse_constant=reject)
 except OSError as exc: raise CliError('%s path error %s: %s'%(label,path,exc)) from exc
 except json.JSONDecodeError as exc: raise CliError('invalid %s JSON %s: %s'%(label,path,exc)) from exc
def _candidate(path):
 value=_strict_json(path,'candidate')
 if not isinstance(value,dict): raise CliError('candidate JSON must be an object')
 for name,item in value.items():
  if not isinstance(name,str) or not name or isinstance(item,bool) or not isinstance(item,(int,float)) or not math.isfinite(float(item)): raise CliError('candidate values must be finite numbers')
 return value
def _live_factory(config,run_dir):
 module=importlib.import_module(os.getenv('ANALOG_OPT_LIVE_MODULE','analog_opt.live'))
 return module.create_workflow(config,run_dir)
def _parser():
 parser=argparse.ArgumentParser(prog='analog_optimize.py'); sub=parser.add_subparsers(dest='command',required=True)
 for name in ('validate','evaluate','run'):
  cmd=sub.add_parser(name); cmd.add_argument('--config',type=Path,required=True)
  if name=='evaluate': cmd.add_argument('--candidate',type=Path,required=True)
  if name=='run': cmd.add_argument('--replace-work-cell',action='store_true'); cmd.add_argument('--replace-result-cell',action='store_true')
 for name in ('resume','report'):
  cmd=sub.add_parser(name); cmd.add_argument('--run-dir',type=Path,required=True)
 verify=sub.add_parser('verify-result'); verify.add_argument('--run-dir',type=Path,required=True); verify.add_argument('--baseline-testbench'); verify.add_argument('--final-testbench')
 create_maestro=sub.add_parser('create-maestro'); create_maestro.add_argument('--run-dir',type=Path,required=True)
 verify_maestro=sub.add_parser('verify-maestro'); verify_maestro.add_argument('--run-dir',type=Path,required=True); verify_maestro.add_argument('--timeout',type=int,default=1800)
 preflight=sub.add_parser('preflight-maestro'); preflight.add_argument('--run-dir',type=Path,required=True)
 repair_maestro=sub.add_parser('repair-maestro-models'); repair_maestro.add_argument('--run-dir',type=Path,required=True)
 accept=sub.add_parser('accept-maestro-history'); accept.add_argument('--run-dir',type=Path,required=True); accept.add_argument('--history',required=True)

 return parser
def _output(value):
 if is_dataclass(value): value=asdict(value)
 elif not isinstance(value,(Mapping:=dict)): value={k:getattr(value,k) for k in ('candidate_id','objective','success','metrics','metadata','failure','specs')}
 print(json.dumps(value,allow_nan=False,sort_keys=True))
def main(argv=None):
 args=_parser().parse_args(argv)
 try:
  if args.command=='validate': load_config(args.config); print('Valid analog optimization V2 configuration: %s'%args.config); return 0
  if args.command=='verify-result':
   from analog_opt.final_validation_live import verify_result
   print(verify_result(args.run_dir,baseline_testbench=args.baseline_testbench,final_testbench=args.final_testbench)); return 0
  if args.command=='create-maestro':
   from analog_opt.maestro_validation_live import create_maestro
   print(create_maestro(args.run_dir)); return 0
  if args.command=='verify-maestro':
   from analog_opt.maestro_validation_live import verify_maestro
   print(verify_maestro(args.run_dir,timeout=args.timeout)); return 0
  if args.command=='preflight-maestro':
   from analog_opt.maestro_validation_live import preflight_maestro
   print(preflight_maestro(args.run_dir)); return 0
  if args.command=='repair-maestro-models':
   from analog_opt.maestro_validation_live import repair_maestro_models
   print(repair_maestro_models(args.run_dir)); return 0
  if args.command=='accept-maestro-history':
   from analog_opt.maestro_validation_live import accept_maestro_history
   print(accept_maestro_history(args.run_dir,args.history)); return 0
  if args.command=='report':
   if not args.run_dir.is_dir(): raise CliError('run directory path does not exist: %s'%args.run_dir)
   manifest=_strict_json(args.run_dir/'result_manifest.json','result manifest'); from analog_opt.report import write_report; print(write_report(args.run_dir,manifest)); return 0
  if args.command=='resume':
   if not args.run_dir.is_dir(): raise CliError('run directory path does not exist: %s'%args.run_dir)
   manifest=_strict_json(args.run_dir/'run_manifest.json','run manifest'); config_path=manifest.get('config')
   if not isinstance(config_path,str) or not config_path: raise CliError('run manifest config path is missing')
   candidate=(args.run_dir/config_path).resolve(); root=args.run_dir.resolve()
   try: candidate.relative_to(root)
   except ValueError as exc: raise CliError('run manifest config path escapes run directory') from exc
   config=load_config(candidate); _live_factory(config,args.run_dir).resume(); return 0
  config=load_config(args.config)
  if args.command=='evaluate':
   candidate=_candidate(args.candidate); run_dir=Path(config.outputs.get('run_dir','output/analog_optimization/evaluate')); _output(_live_factory(config,run_dir).evaluate(candidate)); return 0
  run_dir=Path(config.outputs.get('run_dir','output/analog_optimization/run')); _live_factory(config,run_dir).run(replace_work_cell=args.replace_work_cell,replace_result_cell=args.replace_result_cell); return 0
 except (CliError,ConfigError,ValueError,OSError) as exc: print('error: %s'%exc,file=sys.stderr); return 2
 except Exception as exc: print('live error: %s'%exc,file=sys.stderr); return 3
if __name__=='__main__': raise SystemExit(main())