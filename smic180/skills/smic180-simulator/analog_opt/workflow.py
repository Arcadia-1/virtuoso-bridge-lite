"""Injected live simulation backend and recoverable optimization workflow."""
from __future__ import annotations
import math
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from analog_opt.evaluator import EvaluationFailure, EvaluationResult, atomic_write_json
from analog_opt.parameters import ParameterSpec

_STATES=("validated","work_cell_created","searching","best_replayed","pvt_validated","reported","published")

def _stimulus_value(value: Any) -> tuple:
    if isinstance(value, Mapping): return value.get("value",value.get("dc")), bool(value.get("optimizable",False))
    return getattr(value,"value",None) if getattr(value,"value",None) is not None else getattr(value,"dc",None), bool(getattr(value,"optimizable",False))

def _summary(value: Any) -> Mapping[str,Any]:
    if isinstance(value,Mapping): return value
    if is_dataclass(value): return asdict(value)
    return {"objective":getattr(value,"total",None),"passed":getattr(value,"passed",False),"results":getattr(value,"results",{})}

class AnalogSimulationBackend:
    """Evaluate one already-materialized physical candidate through injected adapters."""
    def __init__(self, parameter_specs: Sequence[ParameterSpec], stimuli: Mapping[str,Any], analyses: Sequence[Mapping[str,Any]], specs: Sequence[Any], *, applier: Any, netlist: Any, runner: Any, metric_extractor: Callable[[Any],Mapping[str,Any]], spec_evaluator: Callable[[Mapping[str,Any]],Any]) -> None:
        self.parameter_specs=tuple(parameter_specs); self.stimuli=dict(stimuli); self.analyses=tuple(analyses); self.specs=tuple(specs); self.applier=applier; self.netlist=netlist; self.runner=runner; self.metric_extractor=metric_extractor; self.spec_evaluator=spec_evaluator
    def __call__(self, physical_candidate: Mapping[str,Any], candidate_dir: Path) -> Mapping[str,Any]:
        if set(physical_candidate)!=set(s.name for s in self.parameter_specs): raise EvaluationFailure("candidate","candidate parameters do not match configured parameter set")
        cdf={}; expected={}
        for spec in self.parameter_specs:
            value=physical_candidate[spec.name]
            try: number=float(value)
            except (TypeError,ValueError,OverflowError) as exc: raise EvaluationFailure("candidate","parameter %s is not finite"%spec.name) from exc
            if not math.isfinite(number): raise EvaluationFailure("candidate","parameter %s is not finite"%spec.name)
            if spec.target=="virtuoso_cdf": cdf[spec.name]=value
            elif spec.target=="spectre_variable": expected[spec.variable or spec.name]=value
            elif spec.target=="bias":
                name=spec.stimulus or spec.name
                if name not in self.stimuli: raise EvaluationFailure("configuration","unknown bias stimulus: %s"%name)
                _,optimizable=_stimulus_value(self.stimuli[name])
                if not optimizable: raise EvaluationFailure("configuration","fixed stimulus cannot be optimized: %s"%name)
                expected[name]=value
            else: raise EvaluationFailure("configuration","unsupported parameter target: %s"%spec.target)
        for name,stimulus in self.stimuli.items():
            value,_=_stimulus_value(stimulus)
            if name not in expected and value is not None: expected[name]=value
        try:
            if cdf: self.applier.apply_cdf(cdf)
            deck=self.netlist.export_fresh(Path(candidate_dir))
            raw=self.runner.run(deck,Path(candidate_dir),self.analyses)
            metrics=dict(self.metric_extractor(raw)); summary=_summary(self.spec_evaluator(metrics))
            if cdf:
                readback=self.applier.read_cdf(cdf)
                for name,value in cdf.items():
                    if name not in readback or not math.isclose(float(readback[name]),float(value),rel_tol=1e-9,abs_tol=0.0): raise EvaluationFailure("confirmation","CDF readback mismatch for %s"%name)
            if self.netlist.confirm_values(deck,expected) is not True: raise EvaluationFailure("confirmation","netlist physical value confirmation failed")
            objective=float(summary.get("objective",summary.get("total",0.0)))
            if not math.isfinite(objective): raise EvaluationFailure("specification","non-finite objective")
            return {"objective":objective,"success":True,"metrics":metrics,"specs":summary.get("results",{}),"metadata":{"netlist":str(deck)}}
        except EvaluationFailure: raise
        except Exception as exc: raise EvaluationFailure("simulation",str(exc)) from exc

class OptimizationWorkflow:
    """Persist and resume the ordered optimization lifecycle."""
    def __init__(self, run_dir: Any, *, applier: Any, search: Callable[[bool],Any], replay: Callable[[Mapping[str,Any],Path],EvaluationResult], validate_pvt: Callable[[Mapping[str,Any]],Any], reporter: Callable[[Mapping[str,Any]],Any]) -> None:
        self.run_dir=Path(run_dir); self.run_dir.mkdir(parents=True,exist_ok=True); self.applier=applier; self.search=search; self.replay=replay; self.validate_pvt=validate_pvt; self.reporter=reporter; self.state_path=self.run_dir/"workflow_state.json"
    def _load(self) -> dict:
        if not self.state_path.exists(): return {"state":"validated"}
        import json
        data=json.loads(self.state_path.read_text(encoding="utf-8")); state=data.get("state")
        if state not in _STATES: raise ValueError("invalid workflow state: %s"%state)
        return data
    def _save(self,state:str,**data:Any)->None: atomic_write_json(self.state_path,dict(data,state=state))
    @staticmethod
    def _parameters(best:Any)->Mapping[str,Any]:
        metadata=getattr(best,"metadata",{})
        if isinstance(metadata,Mapping) and isinstance(metadata.get("physical_candidate"),Mapping): return metadata["physical_candidate"]
        if hasattr(best,"parameters") and isinstance(best.parameters,Mapping): return best.parameters
        if isinstance(best,Mapping): return best.get("physical_candidate",best.get("parameters",{}))
        return {}
    def run(self,replace_work_cell:bool=False,replace_result_cell:bool=False)->Mapping[str,Any]:
        return self._execute(False,replace_work_cell,replace_result_cell)
    def resume(self,replace_result_cell:bool=False)->Mapping[str,Any]: return self._execute(True,False,replace_result_cell)
    def _execute(self,resume:bool,replace_work:bool,replace_result:bool)->Mapping[str,Any]:
        data=self._load(); state=data["state"]; index=_STATES.index(state)
        if index==0:
            self.applier.create_work_cell(replace=replace_work); self._save("work_cell_created"); state="work_cell_created"; index=1
        if index<=2:
            self._save("searching"); result=self.search(resume or index==2); best=result.best
            if best is None: raise EvaluationFailure("search","search produced no successful candidate")
            parameters=self._parameters(best); replayed=self.replay(parameters,self.run_dir/"best_replay")
            atomic_write_json(self.run_dir/"best_replay.json",{"parameters":parameters,"objective":replayed.objective,"success":replayed.success})
            self._save("best_replayed",parameters=parameters); data=self._load(); state="best_replayed"; index=3
        else: parameters=data.get("parameters",{})
        if index<=3:
            pvt=self.validate_pvt(parameters); pvt_data=_summary(pvt); atomic_write_json(self.run_dir/"pvt_results.json",pvt_data); self._save("pvt_validated",parameters=parameters,pvt=pvt_data); data=self._load(); index=4
        else: pvt_data=data.get("pvt",{})
        if index<=4:
            report_data={"parameters":parameters,"pvt":pvt_data}; self.reporter(report_data); self._save("reported",parameters=parameters,pvt=pvt_data); index=5
        if index<=5 and pvt_data.get("overall_passed") is True:
            self.applier.publish_result_cell(replace=replace_result); self._save("published",parameters=parameters,pvt=pvt_data)
        return self._load()