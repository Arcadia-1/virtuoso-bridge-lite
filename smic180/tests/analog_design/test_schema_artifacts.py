import json

from analog_design.workflow import DesignWorkflow
from test_ir_builder import load_spec


def test_spec_validation_writes_versioned_schema_artifact(tmp_path):
    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    workflow.validate_spec()
    schema = json.loads((workflow.run_dir / "inputs" / "design_spec.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["version"]["const"] == 1
    assert "metrics" in schema["required"]


def test_sizing_and_ir_stages_write_engineering_report_and_schema(tmp_path):
    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    report = (workflow.run_dir / "sizing" / "calculation_report.md").read_text(encoding="utf-8")
    assert "Formula" in report
    assert "Assumptions" in report
    workflow.build_ir()
    schema = json.loads((workflow.run_dir / "ir" / "circuit_ir.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["version"]["const"] == 1
    assert "instances" in schema["required"]
    assert schema["properties"]["instances"]["items"]["required"] == [
        "id", "role", "device_class", "master_ref", "terminals",
        "logical_parameters", "physical_parameters", "cdf_expectations",
        "optimization_refs", "matching_groups", "rationale",
    ]