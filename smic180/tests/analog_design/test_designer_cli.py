import json
import sys
import types

from analog_design.cli import main
from analog_design.technology.base import load_technology_profile
from test_ir_builder import load_spec


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


def test_cli_discover_technology_plan_only_writes_queries_without_live_connection(tmp_path):
    output = tmp_path / "discovery_plan.json"
    assert main(["discover-technology", "--output", str(output), "--plan-only"]) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["pdk_roots"] == [
        "/home/IC/Tech/smic18ee_2",
        "/home/IC/Tech/smic18ee_2P6M_20100810",
    ]
    assert plan["device_candidates"]["smic180.core_nmos"] == [["smic18ee", "n33e2r", "symbol"]]
    assert plan["device_candidates"]["smic180.miller_capacitor"] == [["smic18ee", "mime2r", "symbol"]]

def test_cli_discover_technology_live_writes_confirmed_profile_from_injected_client(tmp_path, monkeypatch):
    output = tmp_path / "technology_profile.json"
    roundtrip_path = tmp_path / "roundtrip.json"
    roundtrip_path.write_text("{}", encoding="utf-8")

    class Client:
        def existing_paths(self, paths):
            return [path for path in paths if "2P6M_20100810" in path]

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
