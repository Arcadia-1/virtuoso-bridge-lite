import json
from pathlib import Path

import pytest

from analog_design.artifacts import ArtifactError, ArtifactStore


def test_create_run_builds_requested_layout_and_updates_latest_atomically(tmp_path):
    store = ArtifactStore(tmp_path)
    run = store.create_run("20260713_120000")
    assert run == tmp_path / "analog_design" / "20260713_120000"
    for relative in ("inputs", "topology", "sizing", "ir", "windows_sim/generated", "windows_sim/iterations", "frozen", "virtuoso", "equivalence", "simulator", "optimizer", "reports"):
        assert (run / relative).is_dir()
    assert (tmp_path / "analog_design" / ".latest_run").read_text(encoding="utf-8").strip() == str(run.resolve())


def test_create_run_refuses_existing_directory(tmp_path):
    store = ArtifactStore(tmp_path)
    store.create_run("same")
    with pytest.raises(ArtifactError, match="already exists"):
        store.create_run("same")


def test_write_json_rejects_nonfinite_values_and_leaves_no_partial_file(tmp_path):
    store = ArtifactStore(tmp_path)
    target = tmp_path / "bad.json"
    with pytest.raises(ArtifactError, match="finite"):
        store.write_json(target, {"value": float("nan")})
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_confirmation_records_hashes_and_detects_tampering(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = tmp_path / "artifact.json"
    store.write_json(artifact, {"value": 1})
    marker = store.confirm(tmp_path / "confirmed.json", "unit_gate", [artifact])
    store.verify_confirmation(marker)
    artifact.write_text(json.dumps({"value": 2}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="hash mismatch"):
        store.verify_confirmation(marker)
