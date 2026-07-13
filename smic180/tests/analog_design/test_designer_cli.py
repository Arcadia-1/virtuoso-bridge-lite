import json

from analog_design.cli import main
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
