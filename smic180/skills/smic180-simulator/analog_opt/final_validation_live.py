"""Live post-publication verifier built on existing V2 and sim_io adapters."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from analog_opt.final_validation import FinalValidationError, load_published_context, verify_netlist_text, write_confirmation
from analog_opt.evaluator import EvaluationResult
from analog_opt.live import AnalysisRunner, MetricsAdapter, NetlistAdapter, _load_client_class, patch_smic180_corner
from analog_opt.specs import Spec, evaluate_specs
from analog_opt.pvt import PvtConfig, build_pvt_points, pvt_result_from_evaluation, summarize_pvt


class PersistentFinalNetlistAdapter(NetlistAdapter):
    """Reuse the proven temporary-TB replacement path, then retain a final copy."""
    def __init__(self, *args, final_testbench: str, reuse_existing_final: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.final_testbench = final_testbench
        self._final_testbench_copied = bool(reuse_existing_final)

    def _prepare_tb(self):
        temporary = super()._prepare_tb()
        if self._final_testbench_copied:
            return temporary
        skill = ('let((src dst) when(ddGetObj("%s" "%s") error("final testbench already exists")) '
                 'src=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") unless(src error("prepared testbench missing")) '
                 'dst=dbCopyCellView(src "%s" "%s" "schematic") unless(dst error("final testbench copy failed")) '
                 'unless(schCheck(dst) error("final testbench schCheck failed")) unless(dbSave(dst) error("final testbench save failed")) '
                 'when(src dbClose(src)) when(dst dbClose(dst)) "FINAL_TB_COPY_OK")') % (
                     self.library, self.final_testbench, self.library, temporary,
                     self.library, self.final_testbench)
        self._tb_step(skill, "FINAL_TB_COPY_OK")
        self._final_testbench_copied = True
        return temporary


def _published_biases(config, parameters):
    records = config.get("parameters", ()) if isinstance(config, dict) else ()
    result = {}
    for item in records:
        if not isinstance(item, dict) or item.get("target") != "bias":
            continue
        name = item.get("name")
        stimulus = item.get("stimulus") or name
        if name not in parameters or not isinstance(stimulus, str) or not stimulus:
            raise FinalValidationError("published bias parameter mapping is incomplete")
        result[stimulus] = parameters[name]
    return result


def _final_tb_uses_result(client, context, final_testbench):
    skill = ('let((cv inst x ok) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") '
             'unless(cv error("final testbench missing")) foreach(x cv~>instances when(x~>name=="%s" inst=x)) '
             'ok=if(inst&&inst~>master&&inst~>master~>cellName=="%s" t nil) '
             'when(cv dbClose(cv)) if(ok "FINAL_TB_RESULT_OK" "FINAL_TB_RESULT_BAD"))') % (
                 context.library, final_testbench, context.dut_instance, context.result_cell)
    try:
        result = client.execute_skill(skill, timeout=30)
    except Exception:
        return False
    output = (getattr(result, "output", "") or "").strip().strip('"')
    return not getattr(result, "errors", None) and output == "FINAL_TB_RESULT_OK"

def _specs(config):
    return tuple(Spec(metric=item["metric"], op=item["op"], value=item.get("value"),
                      lower=item.get("lower"), upper=item.get("upper"), weight=item.get("weight", 1),
                      hard=item.get("hard", False), tolerance=item.get("tolerance", 0)) for item in config.specs)


def _result_payload(metrics, declarations):
    summary = evaluate_specs(metrics, declarations)
    return {"objective": summary.total, "passed": summary.passed,
            "specs": {item.spec.metric: {"passed": item.passed, "violation": item.violation}
                      for item in summary.results}}


def verify_result(run_dir, *, baseline_testbench=None, final_testbench=None):
    from analog_opt.schema import load_config
    from sim_io.site_config import SiteConfig
    from sim_io.sim.run import export_netlist, run_spectre
    from sim_io.sim.config import resolve_sim_config

    context = load_published_context(run_dir)
    config = load_config(context.config_path)
    baseline = baseline_testbench or context.baseline_testbench
    final_tb = final_testbench or context.final_testbench
    if final_tb in {context.source_cell, context.work_cell, context.result_cell, baseline}:
        raise FinalValidationError("final testbench identifier is not isolated")
    client = _load_client_class().from_env()
    site = SiteConfig.from_env()
    root = context.run_dir / "final_validation"
    reuse_existing_final = False
    if root.exists():
        if (root / "final_validation.confirmed.json").exists():
            raise FinalValidationError("final validation is already confirmed")
        previous_deck = root / "final_deck.scs"
        if not previous_deck.is_file() or not _final_tb_uses_result(client, context, final_tb):
            raise FinalValidationError("existing final validation cannot be safely resumed")
        verify_netlist_text(previous_deck.read_text(encoding="utf-8", errors="replace"), context.result_cell, context.work_cell)
        reuse_existing_final = True
    else:
        root.mkdir(parents=True)
    adapter = PersistentFinalNetlistAdapter(
        client, site, library=context.library, source_tb=baseline, work_cell=context.result_cell,
        dut_instance=context.dut_instance, final_testbench=final_tb, reuse_existing_final=reuse_existing_final, exporter=export_netlist,
        base_deck_factory=lambda **kw: resolve_sim_config(run_dir=context.run_dir, lib=kw["library"], cell=kw["cell"]),
        corner_patcher=lambda deck, corner: patch_smic180_corner(deck, corner, core_model_include=site.pdk_core_spectre_include))
    adapter.analyses = config.analyses
    biases = _published_biases(context.config, context.parameters)
    adapter.configure({}, biases, config.stimuli, {})
    decks = adapter.export_fresh(context.library, context.result_cell, root / "nominal")
    first_deck = next(iter(decks.values()))
    netlist_text = first_deck.read_text(encoding="utf-8", errors="replace")
    verify_netlist_text(netlist_text, context.result_cell, context.work_cell)
    shutil.copy2(first_deck, root / "final_deck.scs")
    runner = AnalysisRunner(lambda path, directory: run_spectre(path, directory, site=site, client=client))
    results = runner.run(decks, root / "nominal", config.analyses)
    metrics = MetricsAdapter(config.analyses)(results)
    declarations = _specs(config)
    nominal = _result_payload(metrics, declarations)
    (root / "nominal_result.json").write_text(json.dumps({"metrics": metrics, **nominal}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not nominal["passed"]:
        raise FinalValidationError("final nominal Spectre validation failed")
    pvt_cfg = dict(config.pvt)
    corners = tuple(pvt_cfg.get("corners", ("TT",)))
    voltages = tuple(pvt_cfg.get("voltages", ()))
    temperatures = tuple(pvt_cfg.get("temperatures_c", pvt_cfg.get("temperatures", (25.0,))))
    if not voltages:
        voltages = (next(float(getattr(value, "value", 0) or getattr(value, "dc", 0)) for value in config.stimuli.values() if getattr(value, "kind", None) == "voltage"),)
    points = build_pvt_points(PvtConfig(corners, voltages, temperatures))
    pvt_rows = []
    voltage_stimulus = pvt_cfg.get("voltage_stimulus")
    for point in points:
        directory = root / "pvt" / point.point_id
        adapter.configure({}, biases, config.stimuli, {"corner": point.corner, "voltage": point.voltage,
                                                    "voltage_stimulus": voltage_stimulus,
                                                    "temperature": point.temperature})
        point_decks = adapter.export_fresh(context.library, context.result_cell, directory)
        point_results = runner.run(point_decks, directory, config.analyses)
        point_metrics = MetricsAdapter(config.analyses)(point_results)
        scored = _result_payload(point_metrics, declarations)
        raw = EvaluationResult(point.point_id, scored["objective"], True, point_metrics, {}, None, scored["specs"])
        pvt_rows.append(pvt_result_from_evaluation(point, raw, {}))
    summary = summarize_pvt(points, pvt_rows, tuple(item.metric for item in declarations))
    pvt_payload = {"overall_passed": summary.overall_passed, "points": list(summary.points),
                   "worst": summary.worst.__dict__, "worst_by_spec": {k: v.__dict__ for k, v in summary.worst_by_spec.items()},
                   "failures": list(summary.failures)}
    (root / "pvt_results.json").write_text(json.dumps(pvt_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not summary.overall_passed:
        raise FinalValidationError("final PVT Spectre validation failed")
    now = time.time()
    checks = {"result_exists": True, "final_tb_exists": True, "dut_uses_result": True,
              "netlist_uses_result": True, "spectre_passed": True, "pvt_passed": True,
              "fresh_results": (root / "nominal_result.json").stat().st_mtime <= now}
    details = {"library": context.library, "source_cell": context.source_cell,
               "result_cell": context.result_cell, "baseline_testbench": baseline,
               "final_testbench": final_tb, "dut_instance": context.dut_instance,
               "candidate_hash": context.candidate_hash}
    return write_confirmation(context.run_dir, checks, details)