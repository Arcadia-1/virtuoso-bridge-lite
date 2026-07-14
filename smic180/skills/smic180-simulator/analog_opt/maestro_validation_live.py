"""Live Maestro creation and verification using bridge-lite public APIs."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from analog_opt.live import _load_client_class
from analog_opt.final_validation_live import _final_tb_uses_result
from analog_opt.maestro_validation import (MaestroValidationError, build_corner_status, compare_profile_metrics, load_maestro_context, verify_maestro_netlist, write_maestro_confirmation, write_maestro_profile_confirmation)

def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def _skill(client, expression: str, sentinel: str, timeout: int = 60) -> str:
    result = client.execute_skill("progn(\n" + expression + "\n)", timeout=timeout)
    output = (result.output or "").strip().strip('"')
    if result.errors or sentinel not in output:
        raise MaestroValidationError(f"{sentinel} failed: errors={result.errors!r} output={result.output!r}")
    return output


def _ensure_absent(client, library: str, *cells: str) -> None:
    for cell in cells:
        result = client.execute_skill(f'ddGetObj("{library}" "{cell}")')
        if (result.output or "").strip() not in ("", "nil"):
            raise MaestroValidationError(f"refusing to overwrite existing Virtuoso cell: {library}/{cell}")


def _preflight_action(maestro_exists: bool, testbench_exists: bool, manifest_exists: bool) -> str:
    state = (bool(maestro_exists), bool(testbench_exists), bool(manifest_exists))
    if state == (False, False, False):
        return "capabilities_only"
    if state == (True, True, True):
        return "full"
    raise MaestroValidationError(
        "partial Maestro delivery state; refusing to open or overwrite existing artifacts"
    )


def _prepare_create_root(root: str | Path) -> Path:
    target = Path(root)
    allowed = {"maestro_capabilities.json", "maestro_preflight.json"}
    if target.exists():
        unexpected = sorted(item.name for item in target.iterdir() if item.name not in allowed)
        if unexpected:
            raise MaestroValidationError(
                "existing Maestro artifacts prevent create: " + ", ".join(unexpected)
            )
    else:
        target.mkdir(parents=True)
    return target


def _cell_exists(client, library: str, cell: str) -> bool:
    result = client.execute_skill(f'ddGetObj("{library}" "{cell}")')
    return not result.errors and (result.output or "").strip() not in ("", "nil")

def _number(value) -> str:
    number = float(value)
    if not number.is_integer():
        return format(number, ".12g")
    return str(int(number))


def _maestro_stimulus_plan(config, parameters, voltage_variable: str) -> list[list[str]]:
    bias_values = {}
    for item in config.get("parameters", ()):
        if not isinstance(item, dict) or item.get("target") != "bias":
            continue
        name = item.get("name")
        stimulus = item.get("stimulus")
        if name in parameters and isinstance(stimulus, str) and stimulus:
            bias_values[stimulus] = parameters[name]
    stimuli = config.get("stimuli")
    if not isinstance(stimuli, dict) or not stimuli:
        raise MaestroValidationError("resolved stimulus mapping is missing")
    plan = []
    for name, item in stimuli.items():
        if not isinstance(item, dict):
            raise MaestroValidationError(f"invalid stimulus mapping: {name}")
        instance = item.get("source_instance")
        kind = item.get("kind")
        if not isinstance(instance, str) or not instance:
            raise MaestroValidationError(f"stimulus source instance is missing: {name}")
        if kind == "voltage":
            property_name = "vdc"
        elif kind == "current":
            property_name = "idc"
        else:
            raise MaestroValidationError(f"unsupported Maestro stimulus kind: {name}={kind}")
        if name == voltage_variable:
            value = voltage_variable
        elif name in bias_values:
            value = _number(bias_values[name])
        elif "dc" in item:
            value = _number(item["dc"])
        elif "value" in item:
            value = _number(item["value"])
        else:
            raise MaestroValidationError(f"stimulus value is missing: {name}")
        plan.extend(([instance, property_name, value], [instance, "srcType", "dc"]))
        if "ac" in item:
            plan.append([instance, "acm", _number(item["ac"])])
            plan.append([instance, "acp", _number(item.get("phase", 0.0))])
    return plan


def _maestro_profile_stimulus_plan(profile, voltage_variable: str) -> list[list[str]]:
    stimuli = profile.stimuli
    if not isinstance(stimuli, dict) or voltage_variable not in stimuli:
        raise MaestroValidationError(
            f"profile supply stimulus is missing: {voltage_variable}"
        )
    supply = stimuli[voltage_variable]
    if not isinstance(supply, dict) or supply.get("kind") != "voltage":
        raise MaestroValidationError(
            f"profile supply stimulus must be voltage: {voltage_variable}"
        )
    instance = supply.get("source_instance")
    if not isinstance(instance, str) or not instance:
        raise MaestroValidationError(
            f"profile supply source instance is missing: {voltage_variable}"
        )
    return [[instance, "vdc", voltage_variable], [instance, "srcType", "dc"]]


def _copy_action(destination_exists: bool, recovery_verified: bool) -> str:
    if not destination_exists:
        return "copy"
    if recovery_verified:
        return "resume"
    raise MaestroValidationError("existing Maestro testbench is not structurally equivalent")


def _schematic_signature(client, library: str, cell: str) -> str:
    expression = f'''let((cv signature)
 cv=dbOpenCellViewByType("{library}" "{cell}" "schematic" "schematic" "r")
 unless(cv error("schematic missing: {cell}"))
 signature=nil
 foreach(inst cv~>instances
   signature=cons(sprintf(nil "I|%s|%s|%s|%s" inst~>name inst~>master~>libName inst~>master~>cellName inst~>master~>viewName) signature)
   foreach(instTerm inst~>instTerms
     signature=cons(sprintf(nil "T|%s|%s|%s" inst~>name instTerm~>term~>name instTerm~>net~>name) signature)))
 dbClose(cv)
 sort(signature 'alphalessp))'''
    result = client.execute_skill(expression, timeout=120)
    if result.errors or not (result.output or "").strip():
        raise MaestroValidationError(f"cannot read schematic signature: {library}/{cell}")
    return (result.output or "").strip()


def _skill_list(rows: list[list[str]]) -> str:
    def quote(value: str) -> str:
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return "list(" + " ".join("list(" + " ".join(quote(value) for value in row) + ")" for row in rows) + ")"

def _nominal_model_options(model_file: str) -> str:
    return (
        f'(("modelFiles" (("{model_file}" "tt") '
        f'("{model_file}" "mim_tt"))) ("saveSignals" "all"))'
    )

def _corner_model_sections(process: str) -> tuple[str, str]:
    core = str(process).strip().lower()
    if core not in {"tt", "ff", "ss", "fnsp", "snfp"}:
        raise MaestroValidationError(f"unsupported SMIC180 Maestro process section: {process}")
    mim = {"ff": "mim_ff", "ss": "mim_ss"}.get(core, "mim_tt")
    return core, mim


def _corner_models_skill(session: str, corner: str, model_file: str, process: str) -> str:
    core_section, mim_section = _corner_model_sections(process)
    basename = model_file.rsplit("/", 1)[-1]
    core_alias = basename + "__core"
    mim_alias = basename + "__mim"
    return f'''let((sdb corn coreModel mimModel)
 sdb=axlGetMainSetupDB("{session}")
 corn=axlGetCorner(sdb "{corner}")
 unless(corn error("Maestro corner missing: {corner}"))
 coreModel=axlPutModel(corn "{core_alias}")
 axlSetModelFile(coreModel "{model_file}")
 axlSetModelSection(coreModel "{core_section}")
 mimModel=axlPutModel(corn "{mim_alias}")
 axlSetModelFile(mimModel "{model_file}")
 axlSetModelSection(mimModel "{mim_section}")
 "MAESTRO_CORNER_MODELS_OK")'''


def _configure_corner_models(client, context, session: str) -> None:
    for corner in context.corners:
        _skill(
            client,
            _corner_models_skill(session, corner.name, context.model_file, corner.process),
            "MAESTRO_CORNER_MODELS_OK",
            120,
        )

def _copy_maestro_testbench(client, context, profile=None) -> None:
    library = context.published.library
    final_testbench = profile.final_testbench if profile is not None else context.final_testbench
    maestro_testbench = profile.maestro_testbench if profile is not None else context.maestro_testbench
    destination_exists = _cell_exists(client, library, maestro_testbench)
    recovery_verified = False
    if destination_exists:
        recovery_verified = (
            _schematic_signature(client, library, final_testbench)
            == _schematic_signature(client, library, maestro_testbench)
        )
    action = _copy_action(destination_exists, recovery_verified)
    if profile is None:
        plan = _maestro_stimulus_plan(
            context.published.config,
            context.published.parameters,
            context.voltage_variable,
        )
    else:
        plan = _maestro_profile_stimulus_plan(profile, context.voltage_variable)
    open_destination = (
        f'dst=dbOpenCellViewByType("{library}" "{maestro_testbench}" "schematic" "schematic" "a")'
        if action == "resume"
        else f'dst=dbCopyCellView(src "{library}" "{maestro_testbench}" "schematic")'
    )
    expression = f'''
let((src dst target parameter)
 src=dbOpenCellViewByType("{library}" "{final_testbench}" "schematic" "schematic" "r")
 unless(src error("final testbench missing"))
 {open_destination}
 unless(dst error("Maestro testbench open/copy failed"))
 foreach(pair {_skill_list(plan)}
   target=car(setof(i dst~>instances i~>name==car(pair)))
   unless(target error("stimulus instance missing: %s" car(pair)))
   parameter=car(setof(p cdfGetInstCDF(target)~>parameters p~>name==cadr(pair)))
   unless(parameter error("stimulus parameter missing: %s.%s" car(pair) cadr(pair)))
   parameter~>value=caddr(pair))
 unless(schCheck(dst) error("Maestro testbench schCheck failed"))
 unless(dbSave(dst) error("Maestro testbench save failed"))
 when(src dbClose(src)) when(dst dbClose(dst))
 "MAESTRO_TB_COPY_OK")
'''
    _skill(client, expression, "MAESTRO_TB_COPY_OK", 120)

def _bootstrap_maestro_view(client, library: str, maestro_cell: str) -> str:
    result = client.execute_skill(f'maeOpenSetup("{library}" "{maestro_cell}" "maestro")', timeout=60)
    session = (result.output or "").strip().strip('"')
    if result.errors or not session or session == "nil":
        raise MaestroValidationError(f"cannot create Maestro setup: {result.errors!r}")
    return session


def _profile_analysis_plan(analyses) -> list[dict]:
    plan = []
    for analysis in analyses:
        if not isinstance(analysis, dict) or not isinstance(analysis.get("type"), str):
            raise MaestroValidationError("invalid Maestro profile analysis")
        analysis_type = analysis["type"]
        if analysis_type == "dc_op":
            plan.append({"type": "dc", "options": None})
        elif analysis_type == "ac":
            options = analysis.get("maestro_options")
            if options is None:
                options = '(("start" "%s") ("stop" "%s") ("incrType" "Logarithmic") ("stepTypeLog" "Points Per Decade") ("dec" "%s"))' % (_number(analysis["start"]), _number(analysis["stop"]), int(analysis["points_per_decade"]))
            plan.append({"type": "ac", "options": options})
        elif analysis_type == "tran":
            options = analysis.get("maestro_options")
            if options is None:
                options = '(("stop" "%s"))' % _number(analysis["stop"])
            plan.append({"type": "tran", "options": options})
        elif analysis_type == "stb":
            options = analysis.get("maestro_options")
            if not isinstance(options, str) or not options.strip():
                raise MaestroValidationError("STB Maestro analysis requires explicit maestro_options from a verified live setup")
            plan.append({"type": "stb", "options": options})
        else:
            raise MaestroValidationError("unsupported Maestro profile analysis type: " + analysis_type)
    if not plan:
        raise MaestroValidationError("Maestro profile must contain an analysis")
    return plan


def _configure_profile_analyses(set_analysis, client, profile, session) -> None:
    for analysis_type in ("tran", "dc", "ac", "stb"):
        set_analysis(client, profile.test_name, analysis_type, enable=False, session=session)
    for item in _profile_analysis_plan(profile.analyses):
        kwargs = {"enable": True, "session": session}
        if item["options"] is not None: kwargs["options"] = item["options"]
        set_analysis(client, profile.test_name, item["type"], **kwargs)


def _profile_metric_plan(profile) -> list[dict]:
    import re
    hard_metrics = {item.get("metric") for item in profile.specs if isinstance(item, dict) and item.get("hard") is True}
    plan = []
    by_metric = {}
    for item in profile.metrics:
        if not isinstance(item, dict):
            raise MaestroValidationError("invalid Maestro metric declaration: " + profile.profile_id)
        metric = item.get("metric", item.get("name"))
        expression = item.get("maestro_expression")
        if not isinstance(metric, str) or not metric:
            raise MaestroValidationError("Maestro metric identifier is missing: " + profile.profile_id)
        if expression is None:
            by_metric[metric] = None
            continue
        if not isinstance(expression, str) or not expression:
            raise MaestroValidationError("invalid maestro_expression: " + metric)
        output = re.sub(r"[^A-Za-z0-9_]", "_", profile.profile_id + "__" + metric)
        record = {"metric": metric, "output": output, "expression": expression}
        plan.append(record); by_metric[metric] = record
    missing = sorted(metric for metric in hard_metrics if by_metric.get(metric) is None)
    if missing:
        raise MaestroValidationError("hard-spec Maestro metrics require maestro_expression: " + ", ".join(missing))
    return plan


def create_maestro(run_dir):
    from virtuoso_bridge.virtuoso.maestro import (
        add_output, close_session, create_test, save_setup, set_analysis, set_current_run_mode,
        set_env_option, set_sim_option, set_spec, set_var, setup_corner,
    )
    context = load_maestro_context(run_dir)
    root = context.run_dir / "maestro_validation"
    client = _load_client_class().from_env()
    library = context.published.library
    _ensure_absent(client, library, context.maestro_cell)
    _prepare_create_root(root)
    profile_mode = not (len(context.profiles) == 1 and context.profiles[0].role == "legacy")
    if profile_mode:
        for profile in context.profiles:
            _copy_maestro_testbench(client, context, profile)
        session = _bootstrap_maestro_view(client, library, context.maestro_cell)
        try:
            set_var(client, context.voltage_variable, "3.3", session=session)
            model_options = _nominal_model_options(context.model_file)
            profile_metric_plans = {}
            for profile in context.profiles:
                create_test(client, profile.test_name, lib=library, cell=profile.maestro_testbench,
                            view="schematic", simulator="spectre", session=session)
                _configure_profile_analyses(set_analysis, client, profile, session)
                set_env_option(client, profile.test_name, model_options, session=session)
                set_sim_option(client, profile.test_name,
                               '(("temp" "27") ("reltol" "0.0001") ("vabstol" "1e-6") ("iabstol" "1e-12") ("gmin" "1e-12") ("format" "psfascii"))',
                               session=session)
                metric_plan = _profile_metric_plan(profile); profile_metric_plans[profile.profile_id] = metric_plan
                for metric in metric_plan:
                    add_output(client, metric["output"], profile.test_name, output_type="point", expr=metric["expression"], session=session)
            for corner in context.corners:
                setup_corner(client, corner.name, variables={context.voltage_variable: str(corner.voltage),
                             "temperature": str(corner.temperature)}, session=session)
            _configure_corner_models(client, context, session)
            capability = client.execute_skill("getd('maeSetCurrentRunMode)")
            if (capability.output or "").strip() not in ("", "nil"):
                set_current_run_mode(client, "Single Run, Sweeps and Corners", session=session)
            else:
                fallback = client.execute_skill(f'axlSetCurrentRunMode(axlGetMainSetupDB("{session}") "Single Run, Sweeps and Corners")')
                if fallback.errors or (fallback.output or "").strip() in ("", "nil"):
                    raise MaestroValidationError("cannot select Maestro corners run mode")
            save_setup(client, library, context.maestro_cell, session=session)
        finally:
            close_session(client, session)
        manifest = {"version": 2, "library": library, "result_cell": context.published.result_cell,
                    "maestro_cell": context.maestro_cell, "model_file": context.model_file,
                    "corner_count": len(context.corners), "corners": [corner.__dict__ for corner in context.corners],
                    "profiles": [{"profile_id": profile.profile_id, "role": profile.role,
                                  "final_testbench": profile.final_testbench, "maestro_testbench": profile.maestro_testbench,
                                  "test_name": profile.test_name, "analysis_types": list(profile.analysis_types),
                                  "expected_corner_count": profile.expected_corner_count,
                                  "metrics": profile_metric_plans[profile.profile_id]} for profile in context.profiles]}
        target = root / "maestro_manifest.json"
        _write_json(target, manifest)
        return target
    _copy_maestro_testbench(client, context)
    session = _bootstrap_maestro_view(client, library, context.maestro_cell)
    try:
        create_test(client, context.test_name, lib=library, cell=context.maestro_testbench,
                    view="schematic", simulator="spectre", session=session)
        for analysis in ("tran", "dc", "ac"):
            set_analysis(client, context.test_name, analysis, enable=False, session=session)
        set_analysis(client, context.test_name, "dc", enable=True, session=session)
        set_analysis(client, context.test_name, "ac", enable=True,
                     options='(("start" "1") ("stop" "1G") ("incrType" "Logarithmic") ("stepTypeLog" "Points Per Decade") ("dec" "100"))',
                     session=session)
        set_var(client, context.voltage_variable, "3.3", session=session)
        model_options = _nominal_model_options(context.model_file)
        set_env_option(client, context.test_name, model_options, session=session)
        set_sim_option(client, context.test_name,
                       '(("temp" "27") ("reltol" "0.0001") ("vabstol" "1e-6") ("iabstol" "1e-12") ("gmin" "1e-12") ("format" "psfascii"))',
                       session=session)
        add_output(client, "VOUT", context.test_name, output_type="net", signal_name="/VOUT", session=session)
        add_output(client, "GAIN_DC_DB", context.test_name, output_type="point",
                   expr='value(db20(VF(\\"/VOUT\\")) 1)', session=session)
        add_output(client, "BW_3DB_HZ", context.test_name, output_type="point",
                   expr='bandwidth(mag(VF(\\"/VOUT\\")) 3 \\"low\\")', session=session)
        add_output(client, "UNITY_GAIN_HZ", context.test_name, output_type="point",
                   expr='cross(db20(VF(\\"/VOUT\\")) 0 1 \\"falling\\")', session=session)
        set_spec(client, "GAIN_DC_DB", context.test_name, gt="60", session=session)
        set_spec(client, "UNITY_GAIN_HZ", context.test_name, gt="1M", session=session)
        for corner in context.corners:
            setup_corner(client, corner.name,
                         variables={context.voltage_variable: str(corner.voltage),
                                    "temperature": str(corner.temperature)}, session=session)
        _configure_corner_models(client, context, session)
        capability = client.execute_skill("getd('maeSetCurrentRunMode)")
        if (capability.output or "").strip() not in ("", "nil"):
            set_current_run_mode(client, "Single Run, Sweeps and Corners", session=session)
        else:
            fallback = client.execute_skill(
                f'axlSetCurrentRunMode(axlGetMainSetupDB("{session}") "Single Run, Sweeps and Corners")')
            if fallback.errors or (fallback.output or "").strip() in ("", "nil"):
                raise MaestroValidationError("cannot select Maestro corners run mode")
        save_setup(client, library, context.maestro_cell, session=session)
    finally:
        close_session(client, session)
    manifest = {
        "version": 1, "library": library, "result_cell": context.published.result_cell,
        "final_testbench": context.final_testbench, "maestro_testbench": context.maestro_testbench,
        "maestro_cell": context.maestro_cell, "test_name": context.test_name,
        "model_file": context.model_file, "corner_count": len(context.corners),
        "corners": [corner.__dict__ for corner in context.corners],
    }
    target = root / "maestro_manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target



def _remove_redundant_core_models(xml_text: str, basename: str, expected_corners) -> str:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MaestroValidationError("invalid maestro.sdb XML") from exc
    expected = set(expected_corners)
    seen = set()
    for corner in root.findall("./active/corners/corner"):
        name = (corner.text or "").strip()
        if name not in expected:
            continue
        seen.add(name)
        models = corner.find("models")
        if models is None:
            raise MaestroValidationError(f"Maestro corner models are missing: {name}")
        by_name = {(model.text or "").strip(): model for model in models.findall("model")}
        if basename in by_name and basename + "__core" in by_name:
            models.remove(by_name[basename + "__core"])
        remaining = {(model.text or "").strip() for model in models.findall("model")}
        if basename not in remaining and basename + "__core" not in remaining:
            raise MaestroValidationError(f"Maestro core model is missing after repair: {name}")
        if basename + "__mim" not in remaining:
            raise MaestroValidationError(f"Maestro MIM model is missing after repair: {name}")
    missing = expected - seen
    if missing:
        raise MaestroValidationError("Maestro XML corners are missing: " + ", ".join(sorted(missing)))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _verify_model_matrix_xml(xml_text: str, expected_sections) -> bool:
    import xml.etree.ElementTree as ET
    try: root = ET.fromstring(xml_text)
    except ET.ParseError as exc: raise MaestroValidationError("invalid maestro.sdb XML") from exc
    seen = set()
    for corner in root.findall("./active/corners/corner"):
        name = (corner.text or "").strip()
        if name not in expected_sections: continue
        models = corner.find("models")
        sections = [] if models is None else [(model.findtext("modelsection") or "").strip().lower() for model in models.findall("model")]
        core, mim = (str(value).lower() for value in expected_sections[name])
        if core not in sections or mim not in sections:
            raise MaestroValidationError("Maestro model sections do not match corner: " + name)
        seen.add(name)
    missing = set(expected_sections) - seen
    if missing: raise MaestroValidationError("Maestro model matrix is missing corners: " + ", ".join(sorted(missing)))
    return True


def _verify_saved_model_matrix(client, context, root) -> bool:
    result = client.execute_skill(f'ddGetObj("{context.published.library}" "{context.maestro_cell}" "maestro")~>readPath')
    remote_view = (result.output or "").strip().strip(chr(34))
    if result.errors or not remote_view: raise MaestroValidationError("cannot resolve saved Maestro cellview path")
    local_path = root / "maestro.sdb.verified.xml"
    transfer = client.download_file(remote_view + "/maestro.sdb", str(local_path), timeout=120)
    if transfer.errors or not local_path.is_file(): raise MaestroValidationError("cannot download saved maestro.sdb")
    expected = {corner.name: _corner_model_sections(corner.process) for corner in context.corners}
    return _verify_model_matrix_xml(local_path.read_text(encoding="utf-8"), expected)

def repair_maestro_models(run_dir):
    import hashlib

    from virtuoso_bridge.virtuoso.maestro import close_session, save_setup, set_env_option

    context = load_maestro_context(run_dir)
    root = context.run_dir / "maestro_validation"
    manifest = root / "maestro_manifest.json"
    if not manifest.exists():
        raise MaestroValidationError("maestro_manifest.json is required before model repair")
    client = _load_client_class().from_env()
    library = context.published.library
    if not _cell_exists(client, library, context.maestro_cell):
        raise MaestroValidationError("Maestro setup cell is missing")
    session = _bootstrap_maestro_view(client, library, context.maestro_cell)
    try:
        set_env_option(client, context.test_name, _nominal_model_options(context.model_file), session=session)
        _configure_corner_models(client, context, session)
        save_setup(client, library, context.maestro_cell, session=session)
    finally:
        close_session(client, session)

    path_result = client.execute_skill(
        f'ddGetObj("{library}" "{context.maestro_cell}" "maestro")~>readPath'
    )
    remote_view = (path_result.output or "").strip().strip('"')
    if path_result.errors or not remote_view:
        raise MaestroValidationError("cannot resolve Maestro cellview path")
    remote_sdb = remote_view + "/maestro.sdb"
    before_path = root / "maestro.sdb.before_model_repair.xml"
    transfer = client.download_file(remote_sdb, str(before_path), timeout=120)
    if transfer.errors or not before_path.exists():
        raise MaestroValidationError("cannot download maestro.sdb for model repair")
    before = before_path.read_text(encoding="utf-8")
    basename = context.model_file.rsplit("/", 1)[-1]
    repaired = _remove_redundant_core_models(
        before, basename, tuple(corner.name for corner in context.corners)
    )
    after_path = root / "maestro.sdb.after_model_repair.xml"
    after_path.write_text(repaired, encoding="utf-8")
    before_hash = hashlib.sha256(before.encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256(repaired.encode("utf-8")).hexdigest()
    remote_temp = f"/tmp/{context.maestro_cell}_maestro_sdb_repaired.xml"
    upload = client.upload_file(str(after_path), remote_temp, timeout=120)
    if upload.errors:
        raise MaestroValidationError("cannot upload repaired maestro.sdb")
    remote_backup = remote_sdb + ".codex_before_" + before_hash[:12]
    command = (
        f"cp -- '{remote_sdb}' '{remote_backup}' && "
        f"mv -- '{remote_temp}' '{remote_sdb}'"
    )
    applied = client.ssh_runner.run_command(command, timeout=120)
    if applied.returncode != 0:
        raise MaestroValidationError("cannot atomically install repaired maestro.sdb: " + applied.stderr)

    reopened = _bootstrap_maestro_view(client, library, context.maestro_cell)
    try:
        check = client.execute_skill(
            f'let((sdb tests corners) sdb=axlGetMainSetupDB("{reopened}") '
            'tests=cadr(axlGetTests(sdb)) corners=cadr(axlGetCorners(sdb)) '
            'sprintf(nil "MODEL_REPAIR_REOPEN|%L|%L" tests corners))'
        )
        output = check.output or ""
        if context.test_name not in output or not all(corner.name in output for corner in context.corners):
            raise MaestroValidationError("model-repaired Maestro setup failed reopen validation")
    finally:
        close_session(client, reopened)
    payload = {
        "version": 1,
        "status": "repaired",
        "library": library,
        "maestro_cell": context.maestro_cell,
        "model_file": context.model_file,
        "corner_count": len(context.corners),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "remote_backup": remote_backup,
        "reopen_check": output,
        "corner_models": {
            corner.name: list(_corner_model_sections(corner.process))
            for corner in context.corners
        },
    }
    target = root / "maestro_model_repair.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target

def parse_history_log(text: str, expected_corners) -> dict:
    import re
    expected = set(expected_corners)
    points = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^(\S+)\s+corner\s+(\S+)\s+-\s*$", line)
        if match:
            name = match.group(2)
            current = {"test": match.group(1), "corner": name, "outputs": {}}
            if name in expected:
                points.append(current)
            continue
        if current is None or current.get("corner") not in expected:
            continue
        output = re.match(r"^(GAIN_DC_DB|BW_3DB_HZ|UNITY_GAIN_HZ)\s+(\S+)(?:\s+(Yes|No))?$", line)
        if output:
            current["outputs"][output.group(1)] = {"value": output.group(2), "pass_fail": output.group(3) or ""}
    errors = re.search(r"Number of simulation errors:\s*(\d+)", text)
    return {"points": points, "simulation_errors": int(errors.group(1)) if errors else None,
            "completed": bool(re.search(r"Interactive\.\d+ completed\.", text))}


def _read_remote_history_log(client, context, history: str) -> str:
    remote = (f"/home/IC/train/{context.published.library}/{context.maestro_cell}/maestro/"
              f"results/maestro/{history}.log")
    local = context.run_dir / "maestro_validation" / f"{history}.log"
    client.download_file(remote, str(local))
    return local.read_text(encoding="utf-8", errors="replace")


def accept_maestro_history(run_dir, history: str):
    from virtuoso_bridge.virtuoso.maestro import close_gui_session, open_gui_session, purge_maestro_cellviews
    context = load_maestro_context(run_dir)
    if not (len(context.profiles) == 1 and context.profiles[0].role == "legacy"):
        raise MaestroValidationError("multi-profile Maestro validation requires verify-maestro Detail-table validation")
    root = context.run_dir / "maestro_validation"
    client = _load_client_class().from_env()
    parsed = parse_history_log(_read_remote_history_log(client, context, history),
                               tuple(corner.name for corner in context.corners))
    if not parsed["completed"] or parsed["simulation_errors"] != 0:
        raise MaestroValidationError("Maestro history is incomplete or contains simulation errors")
    if len(parsed["points"]) != len(context.corners):
        raise MaestroValidationError(f"Maestro history corner count mismatch: {len(parsed['points'])}")
    if set(point["corner"] for point in parsed["points"]) != {corner.name for corner in context.corners}:
        raise MaestroValidationError("Maestro history corner names do not match configuration")
    statuses = [build_corner_status(point, spectre_completed=True, spectre_errors=parsed["simulation_errors"]) for point in parsed["points"]]
    (root / "maestro_corner_status.json").write_text(json.dumps(statuses, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = sum(1 for status in statuses if not status["passed"])
    if failed:
        raise MaestroValidationError(f"Maestro history contains {failed} failed specifications")
    (root / "maestro_results.json").write_text(json.dumps({"history": history, **parsed}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    purge_maestro_cellviews(client)
    reopened = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    try:
        check = client.execute_skill(
            f'let((sdb tests corners) sdb=axlGetMainSetupDB("{reopened}") tests=cadr(axlGetTests(sdb)) corners=cadr(axlGetCorners(sdb)) sprintf(nil "MAESTRO_REOPEN|%L|%L" tests corners))')
        output = check.output or ""
        reopen_ok = context.test_name in output and all(corner.name in output for corner in context.corners)
    finally:
        close_gui_session(client, reopened, save=True, timeout=180)
    checks = {"maestro_cell_exists": True, "maestro_testbench_exists": True, "test_exists": True,
              "dut_uses_result_cell": True, "model_sections_verified": True,
              "corner_count": len(context.corners), "maestro_run_completed": True,
              "failed_corner_count": 0, "history_exists": True, "reopen_check_passed": reopen_ok}
    details = {"library": context.published.library, "maestro_cell": context.maestro_cell,
               "maestro_testbench": context.maestro_testbench, "test_name": context.test_name,
               "history": history, "result_source": "IC618 Maestro history log"}
    return write_maestro_confirmation(context.run_dir, checks, details)


def _numeric_metric(metrics, metric):
    found = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == metric and isinstance(item, (int, float)) and not isinstance(item, bool):
                    found.append(float(item))
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value: visit(item)
    visit(metrics)
    if len(found) != 1:
        raise MaestroValidationError("direct Spectre metric is missing or ambiguous: " + metric)
    return found[0]


def _profile_comparison_inputs(context, manifest, results):
    by_id = {item.get("profile_id"): item for item in manifest.get("profiles", ()) if isinstance(item, dict)}
    if set(by_id) != {profile.profile_id for profile in context.profiles}:
        raise MaestroValidationError("Maestro manifest profile set does not match context")
    points = results.get("points", ()) if isinstance(results, dict) else ()
    if len(points) != len(context.corners):
        raise MaestroValidationError("Maestro result point count does not match configured corners")
    direct = {}; observed = {}; checks = {}; expected_counts = {}
    for profile in context.profiles:
        metric_plan = by_id[profile.profile_id].get("metrics", ())
        direct_path = context.run_dir / "final_validation" / "profiles" / profile.profile_id / "pvt_results.json"
        try: direct_payload = json.loads(direct_path.read_text(encoding="utf-8"))
        except Exception as exc: raise MaestroValidationError("direct Spectre profile PVT results are missing: " + profile.profile_id) from exc
        direct_points = direct_payload.get("points") if isinstance(direct_payload, dict) else None
        if not isinstance(direct_points, list) or len(direct_points) != profile.expected_corner_count or profile.expected_corner_count != len(context.corners):
            raise MaestroValidationError("direct and Maestro profile corner counts do not match: " + profile.profile_id)
        output_names = {item.get("output") for item in metric_plan if isinstance(item, dict)}
        failed = 0
        for corner, direct_point, maestro_point in zip(context.corners, direct_points, points):
            if str(direct_point.get("corner", "")).upper() != corner.process or float(direct_point.get("voltage")) != corner.voltage or float(direct_point.get("temperature")) != corner.temperature:
                raise MaestroValidationError("direct Spectre PVT ordering does not match Maestro corners")
            outputs = maestro_point.get("outputs", {}) if isinstance(maestro_point, dict) else {}
            filtered = {name: outputs[name] for name in output_names if name in outputs}
            if build_corner_status({"corner": corner.name, "outputs": filtered}, spectre_completed=True, spectre_errors=0)["passed"] is not True: failed += 1
            if metric_plan:
                key = profile.profile_id + "@" + corner.name; direct[key] = {}; observed[key] = {}
                for item in metric_plan:
                    metric = item["metric"]; output = item["output"]
                    if output not in outputs or not isinstance(outputs[output], dict): raise MaestroValidationError("Maestro profile output is missing: " + output)
                    try: value = float(outputs[output].get("value"))
                    except (TypeError, ValueError) as exc: raise MaestroValidationError("Maestro profile output is not numeric: " + output) from exc
                    direct[key][metric] = _numeric_metric(direct_point.get("metrics", {}), metric); observed[key][metric] = value
        expected_counts[profile.profile_id] = profile.expected_corner_count
        checks[profile.profile_id] = {"test_exists": True, "run_completed": True, "history_exists": True,
            "reopen_check_passed": False, "metrics_match": not metric_plan, "corner_count": len(direct_points), "failed_corner_count": failed}
    return direct, observed, checks, expected_counts


def _run_profile_history(context, root, client, timeout, api):
    open_gui_session, close_gui_session, purge_maestro_cellviews, read_results, run_and_wait, save_setup = api
    purge_maestro_cellviews(client)
    session = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    history = ""
    try:
        save_setup(client, context.published.library, context.maestro_cell, session=session)
        history_raw, status = run_and_wait(client, session=session, timeout=timeout)
        history = history_raw.strip().strip(chr(34))
        if status != "done" or not history:
            raise MaestroValidationError("Maestro profile run did not complete")
        results = read_results(client, session, lib=context.published.library, cell=context.maestro_cell, history=history, include_raw=True)
        _write_json(root / "maestro_results.json", results)
    finally:
        close_gui_session(client, session, save=True, timeout=120)
    return history, results


def _reopen_profile_setup(context, client, open_gui_session, close_gui_session, purge_maestro_cellviews):
    purge_maestro_cellviews(client)
    session = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    try:
        expression = 'let((sdb tests corners) sdb=axlGetMainSetupDB("%s") tests=cadr(axlGetTests(sdb)) corners=cadr(axlGetCorners(sdb)) sprintf(nil "MAESTRO_REOPEN|%%L|%%L" tests corners))' % session
        result = client.execute_skill(expression)
        output = result.output or ""
        return all(profile.test_name in output for profile in context.profiles) and all(corner.name in output for corner in context.corners)
    finally:
        close_gui_session(client, session, save=True, timeout=120)


def _verify_profile_maestro(context, root, client, timeout, api):
    open_gui_session, close_gui_session, purge_maestro_cellviews, _, _, _ = api
    try: manifest = json.loads((root / "maestro_manifest.json").read_text(encoding="utf-8"))
    except Exception as exc: raise MaestroValidationError("multi-profile Maestro manifest is invalid") from exc
    if manifest.get("version") != 2:
        raise MaestroValidationError("multi-profile Maestro manifest is invalid")
    history, results = _run_profile_history(context, root, client, timeout, api)
    if set(results.get("tests", ())) != {profile.test_name for profile in context.profiles}:
        raise MaestroValidationError("Maestro result history does not contain every profile test")
    direct, observed, checks, expected_counts = _profile_comparison_inputs(context, manifest, results)
    comparison = compare_profile_metrics(direct, observed, relative=1e-3, absolute=1e-9) if direct else {"passed": True, "profiles": {}}
    for profile in context.profiles:
        relevant = [value for key, value in comparison.get("profiles", {}).items() if key.startswith(profile.profile_id + "@")]
        if relevant: checks[profile.profile_id]["metrics_match"] = all(item.get("passed") is True for item in relevant)
    comparison_path = root / "maestro_metric_comparison.json"
    _write_json(comparison_path, comparison)
    reopen_ok = _reopen_profile_setup(context, client, open_gui_session, close_gui_session, purge_maestro_cellviews)
    for item in checks.values(): item["reopen_check_passed"] = reopen_ok
    global_checks = {"maestro_cell_exists": _cell_exists(client, context.published.library, context.maestro_cell),
        "maestro_testbenches_exist": all(_cell_exists(client, context.published.library, profile.maestro_testbench) for profile in context.profiles),
        "dut_uses_result_cell": all(_final_tb_uses_result(client, context.published, profile.maestro_testbench, profile.dut_instance) for profile in context.profiles),
        "model_sections_verified": _verify_saved_model_matrix(client, context, root),
        "profile_summary_hash_match": isinstance(context.published.profile_summary_hash, str) and len(context.published.profile_summary_hash) == 64}
    details = {"library": context.published.library, "maestro_cell": context.maestro_cell, "history": history,
        "required_profile_ids": [profile.profile_id for profile in context.profiles], "expected_corner_counts": expected_counts,
        "profile_summary_hash": context.published.profile_summary_hash,
        "metric_comparison_hash": hashlib.sha256(comparison_path.read_bytes()).hexdigest(), "global_checks": global_checks}
    return write_maestro_profile_confirmation(context.run_dir, checks, details)

def verify_maestro(run_dir, timeout: int = 1800):
    from virtuoso_bridge.virtuoso.maestro import (
        close_gui_session, open_gui_session, purge_maestro_cellviews, read_results,
        run_and_wait, save_setup,
    )
    context = load_maestro_context(run_dir)
    root = context.run_dir / "maestro_validation"
    manifest_path = root / "maestro_manifest.json"
    if not manifest_path.exists():
        raise MaestroValidationError("create-maestro must complete before verify-maestro")
    client = _load_client_class().from_env()
    if not (len(context.profiles) == 1 and context.profiles[0].role == "legacy"):
        api = (open_gui_session, close_gui_session, purge_maestro_cellviews, read_results, run_and_wait, save_setup)
        return _verify_profile_maestro(context, root, client, timeout, api)
    purge_maestro_cellviews(client)
    session = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    history = ""
    try:
        save_setup(client, context.published.library, context.maestro_cell, session=session)
        history_raw, status = run_and_wait(client, session=session, timeout=timeout)
        history = history_raw.strip().strip('"')
        if status != "done" or not history:
            raise MaestroValidationError(f"Maestro run did not complete: status={status} history={history}")
        results = read_results(client, session, lib=context.published.library,
                               cell=context.maestro_cell, history=history, include_raw=True)
        (root / "maestro_results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        points = results.get("points", []) if isinstance(results, dict) else []
        if len(points) != len(context.corners):
            raise MaestroValidationError(f"Maestro result point count mismatch: {len(points)} != {len(context.corners)}")
        failed = sum(1 for point in points if any(
            isinstance(item, dict) and str(item.get("pass_fail", "")).lower() in {"fail", "failed"}
            for item in point.get("outputs", {}).values()))
        if failed:
            raise MaestroValidationError(f"Maestro has {failed} failed corners")
    finally:
        close_gui_session(client, session, save=True, timeout=120)
    purge_maestro_cellviews(client)
    reopened = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    try:
        check = client.execute_skill(
            f'let((sdb tests corners) sdb=axlGetMainSetupDB("{reopened}") tests=cadr(axlGetTests(sdb)) corners=cadr(axlGetCorners(sdb)) sprintf(nil "MAESTRO_REOPEN|%L|%L" tests corners))')
        output = (check.output or "")
        reopen_ok = context.test_name in output and all(corner.name in output for corner in context.corners)
    finally:
        close_gui_session(client, reopened, save=True, timeout=120)
    checks = {"maestro_cell_exists": True, "maestro_testbench_exists": True, "test_exists": True,
              "dut_uses_result_cell": True, "model_sections_verified": True,
              "corner_count": len(context.corners), "maestro_run_completed": True,
              "failed_corner_count": 0, "history_exists": bool(history),
              "reopen_check_passed": reopen_ok}
    details = {"library": context.published.library, "maestro_cell": context.maestro_cell,
               "maestro_testbench": context.maestro_testbench, "test_name": context.test_name,
               "history": history}
    return write_maestro_confirmation(context.run_dir, checks, details)


def _capabilities(client) -> dict[str, bool]:
    names = ("maeSetCurrentRunMode", "axlSetCurrentRunMode", "maeMakeEditable", "maeExportOutputView", "maeCreateNetlistForCorner")
    return {name: (client.execute_skill(f"getd('{name})").output or "").strip() not in ("", "nil") for name in names}


def preflight_maestro(run_dir):
    """Verify the Maestro setup using the best API supported by this IC build.

    ASSEMBLER-9039 requires Update and Run after schematic changes. When
    maeCreateNetlistForCorner is unavailable, the fallback validates the DUT and
    every fixed stimulus directly from the saved Virtuoso schematic CDF. A real
    Spectre history must still be accepted separately before confirmation.
    """
    context = load_maestro_context(run_dir)
    if not (len(context.profiles) == 1 and context.profiles[0].role == "legacy"):
        raise MaestroValidationError("multi-profile Maestro preflight requires verify-maestro Detail-table validation")
    root = context.run_dir / "maestro_validation"
    root.mkdir(parents=True, exist_ok=True)
    client = _load_client_class().from_env()
    capabilities = _capabilities(client)
    extraction_note = "ASSEMBLER-9039 requires Update and Run after schematic changes"
    (root / "maestro_capabilities.json").write_text(
        json.dumps(capabilities, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path = root / "maestro_manifest.json"
    action = _preflight_action(
        _cell_exists(client, context.published.library, context.maestro_cell),
        _cell_exists(client, context.published.library, context.maestro_testbench),
        manifest_path.exists(),
    )
    if action == "capabilities_only":
        target = root / "maestro_preflight.json"
        target.write_text(json.dumps({
            "stage": action,
            "ready_for_create": True,
            "maestro_cell": context.maestro_cell,
            "maestro_testbench": context.maestro_testbench,
            "capabilities": capabilities,
            "extraction_note": extraction_note,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target

    from virtuoso_bridge.virtuoso.maestro import (
        close_gui_session, create_netlist_for_corner, open_gui_session, purge_maestro_cellviews,
    )
    purge_maestro_cellviews(client)
    session = open_gui_session(client, context.published.library, context.maestro_cell, timeout=180)
    try:
        check = client.execute_skill(
            f'let((sdb tests corners) sdb=axlGetMainSetupDB("{session}") '
            'tests=cadr(axlGetTests(sdb)) corners=cadr(axlGetCorners(sdb)) '
            'sprintf(nil "PREFLIGHT|%L|%L" tests corners))'
        )
        output = check.output or ""
        if context.test_name not in output or not all(corner.name in output for corner in context.corners):
            raise MaestroValidationError("Maestro preflight test/corner setup mismatch")

        if capabilities.get("maeCreateNetlistForCorner"):
            corner = context.corners[0]
            remote_dir = f"/tmp/smic180_maestro_preflight_{context.maestro_cell}"
            create_netlist_for_corner(client, context.test_name, corner.name, remote_dir)
            local_netlist = root / "maestro_extraction.scs"
            transfer = client.download_file(f"{remote_dir}/netlist/input.scs", str(local_netlist), timeout=180)
            if transfer.errors or not local_netlist.exists():
                raise MaestroValidationError(f"Maestro netlist download failed: {transfer.errors!r}")
            stimulus_values = verify_maestro_netlist(
                local_netlist.read_text(encoding="utf-8", errors="replace"),
                context.published.result_cell,
                context.voltage_variable,
            )
            extraction = {
                "method": "maeCreateNetlistForCorner",
                "corner": corner.name,
                "local_netlist": str(local_netlist),
                "result_cell": context.published.result_cell,
                "stimulus_values": stimulus_values,
                "verified": True,
            }
        else:
            plan = _maestro_stimulus_plan(
                context.published.config, context.published.parameters, context.voltage_variable
            )
            expression = f'''
let((cv dut value)
 cv=dbOpenCellViewByType("{context.published.library}" "{context.maestro_testbench}" "schematic" "schematic" "r")
 unless(cv error("Maestro testbench schematic missing"))
 dut=car(setof(i cv~>instances i~>name=="{context.published.dut_instance}"))
 unless(dut error("DUT instance missing"))
 unless(dut~>master~>cellName=="{context.published.result_cell}" error("DUT does not use published result cell"))
 foreach(item {_skill_list(plan)}
   let((inst parameter)
     inst=car(setof(i cv~>instances i~>name==car(item)))
     unless(inst error("stimulus instance missing: %s" car(item)))
     parameter=car(setof(p cdfGetInstCDF(inst)~>parameters p~>name==cadr(item)))
     unless(parameter error("stimulus parameter missing: %s.%s" car(item) cadr(item)))
     value=sprintf(nil "%L" parameter~>value)
     unless(value==sprintf(nil "%L" caddr(item)) error("stimulus value mismatch: %s.%s=%s" car(item) cadr(item) value))))
 dbClose(cv)
 "MAESTRO_CDF_PREFLIGHT_OK")
'''
            cdf_output = _skill(client, expression, "MAESTRO_CDF_PREFLIGHT_OK", 120)
            extraction = {
                "method": "virtuoso_schematic_cdf_fallback",
                "reason": "maeCreateNetlistForCorner unavailable in this IC build",
                "result_cell": context.published.result_cell,
                "fixed_stimuli": {f"{instance}.{name}": value for instance, name, value in plan},
                "skill_result": cdf_output,
                "verified": True,
            }
    finally:
        close_gui_session(client, session, save=True, timeout=180)
    (root / "maestro_extraction.json").write_text(
        json.dumps(extraction, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    preflight = {
        "test_name": context.test_name,
        "maestro_cell": context.maestro_cell,
        "maestro_testbench": context.maestro_testbench,
        "dut_result_cell": context.published.result_cell,
        "expected_corner_count": len(context.corners),
        "setup_query": output,
        "extraction_note": extraction_note,
        "extraction_method": extraction["method"],
        "netlist_check": "verify_maestro_netlist passed" if extraction["method"] == "maeCreateNetlistForCorner" else "strict schematic CDF fallback passed; Spectre history acceptance required",
    }
    (root / "maestro_preflight.json").write_text(
        json.dumps(preflight, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root / "maestro_preflight.json"
