import json
import sys
import types

from analog_design.cli import main
from analog_design.technology.base import load_technology_profile, write_technology_profile
from test_ir_builder import confirmed_profile, load_spec


def test_cli_validate_plan_build_render_and_report(tmp_path, capsys):
    load_spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    run_dir = tmp_path / "run"
    assert main(["validate-spec", "--spec", str(spec_path)]) == 0
    assert main(["plan", "--spec", str(spec_path), "--run-dir", str(run_dir)]) == 0
    assert main(["build-ir", "--run-dir", str(run_dir)]) == 0
    assert main(["render-netlist", "--run-dir", str(run_dir)]) == 0
    assert main(["report", "--run-dir", str(run_dir)]) == 0
    report = json.loads((run_dir / "reports" / "design_report.json").read_text(encoding="utf-8"))
    assert report["current_state"] == "ir_validated"
    assert report["stages"]["equivalence_passed"] == "unverified"
    assert report["stages"]["published"] == "unverified"
    assert "incomplete" in (run_dir / "reports" / "design_report.md").read_text(encoding="utf-8").lower()


def test_cli_audit_run_writes_additive_snapshot(tmp_path):
    load_spec(tmp_path)
    run_dir = tmp_path / "run"
    assert main(["plan", "--spec", str(tmp_path / "spec.json"), "--run-dir", str(run_dir)]) == 0
    assert main(["build-ir", "--run-dir", str(run_dir)]) == 0
    assert main(["audit-run", "--run-dir", str(run_dir)]) == 0
    assert (run_dir / "audit" / "addendum-v1" / "migration_manifest.json").is_file()

def test_cli_discover_technology_plan_only_writes_queries_without_live_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_CDS_LIB", "/configured/pdk/cds.lib")
    monkeypatch.setenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "/configured/pdk/models/spectre/models.scs")
    output = tmp_path / "discovery_plan.json"
    assert main(["discover-technology", "--output", str(output), "--plan-only"]) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["pdk_roots"] == ["/configured/pdk"]
    assert plan["cds_lib_candidates"] == ["/configured/pdk/cds.lib"]
    assert plan["device_candidates"]["smic180.core_nmos"] == [["smic18ee", "n33e2r", "symbol"]]
    assert plan["device_candidates"]["smic180.miller_capacitor"] == [["smic18ee", "mime2r", "symbol"]]

def test_cli_discover_technology_live_writes_confirmed_profile_from_injected_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_CDS_LIB", "/configured/pdk/cds.lib")
    output = tmp_path / "technology_profile.json"
    roundtrip_path = tmp_path / "roundtrip.json"
    roundtrip_path.write_text("{}", encoding="utf-8")

    class Client:
        def existing_paths(self, paths):
            return list(paths)

        def probe_device(self, master_ref, candidates):
            cells = {
                "smic180.core_nmos": ("mos.nmos", "n33e2r", ["D", "G", "B", "S"], {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"}),
                "smic180.core_pmos": ("mos.pmos", "p33e2r", ["B", "D", "G", "S"], {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"}),
                "smic180.miller_capacitor": ("passive.capacitor", "mime2r", ["PLUS", "MINUS"], {"width": "w", "length": "l", "multiplier": "m", "capacitance": "c"}),
            }
            device_class, cell, terminals, parameter_map = cells[master_ref]
            dimensions = {name: ("integer" if name in {"fingers", "multiplier"} else "capacitance" if name == "capacitance" else "length") for name in parameter_map}
            return {
                "device_class": device_class,
                "library": "smic18ee",
                "cell": cell,
                "view": "symbol",
                "terminals": terminals,
                "parameter_map": parameter_map,
                "parameter_dimensions": dimensions,
                "evidence": {"master": "master.json", "terminals": "terminals.json", "cdf": "roundtrip.json"},
                "netlist_model": cell,
                "netlist_terminals": ["D", "G", "S", "B"] if "mos" in device_class else ["PLUS", "MINUS"],
            }

    module = types.ModuleType("fake_discovery_module")
    module.create_client = lambda evidence_dir, roundtrip: Client()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("ANALOG_DESIGN_DISCOVERY_MODULE", module.__name__)

    assert main([
        "discover-technology",
        "--output", str(output),
        "--evidence-dir", str(tmp_path / "evidence"),
        "--roundtrip-evidence", str(roundtrip_path),
    ]) == 0
    profile = load_technology_profile(output)
    profile.require_live_ready()
    assert profile.resolve("smic180.miller_capacitor").cell == "mime2r"

def test_cli_build_and_render_accept_confirmed_profile_and_shared_site_model(tmp_path, monkeypatch):
    load_spec(tmp_path)
    run_dir = tmp_path / "run"
    profile_path = tmp_path / "technology_profile.json"
    write_technology_profile(profile_path, confirmed_profile())
    assert main(["plan", "--spec", str(tmp_path / "spec.json"), "--run-dir", str(run_dir)]) == 0
    monkeypatch.setenv("AMS_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("SIM_CDS_LIB", "/pdk/cds.lib")
    monkeypatch.setenv("SIM_IC_ROOT", "/opt/eda/cadence/IC618")
    monkeypatch.setenv("SIM_MMSIM_ROOT", "/opt/eda/cadence/SPECTRE181")
    monkeypatch.setenv("SIM_PDK_CORE_SPECTRE_INCLUDE", "/models/e2r018_v1p8_spe.scs")

    assert main(["build-ir", "--run-dir", str(run_dir), "--technology-profile", str(profile_path)]) == 0
    assert main(["render-netlist", "--run-dir", str(run_dir), "--technology-profile", str(profile_path), "--corner", "tt"]) == 0

    text = (run_dir / "windows_sim" / "generated" / "design.scs").read_text(encoding="utf-8")
    assert 'include "/models/e2r018_v1p8_spe.scs" section=tt' in text
    assert 'include "/models/e2r018_v1p8_spe.scs" section=mim_tt' in text
    assert "mime2r" in text
