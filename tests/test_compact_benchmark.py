"""The comparison must also work after committing or rebasing the feature."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "compact_benchmark", Path(__file__).resolve().parents[1] / "tools/benchmark_compact_results.py",
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=root, stderr=subprocess.PIPE, text=True, timeout=20,
    ).strip()


@pytest.fixture
def checkouts(tmp_path):
    base, feature = tmp_path / "base", tmp_path / "feature"
    base.mkdir()
    git(base, "init")
    (base / "source.txt").write_text("base\n", encoding="utf-8")
    git(base, "add", "source.txt")
    git(base, "commit", "-m", "base")
    git(tmp_path, "clone", "--no-hardlinks", str(base), str(feature))
    (feature / "source.txt").write_text("feature\n", encoding="utf-8")
    git(feature, "commit", "-am", "feature")
    return base, feature


def test_committed_feature_accepts_clean_ancestor(checkouts):
    base, feature = checkouts
    before, after = benchmark._baseline_revisions(feature, base)
    assert before == git(base, "rev-parse", "HEAD")
    assert after == git(feature, "rev-parse", "HEAD")
    assert before != after


def test_dirty_baseline_is_rejected(checkouts):
    base, feature = checkouts
    (base / "source.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        benchmark._baseline_revisions(feature, base)


def test_newer_baseline_is_not_an_ancestor(checkouts):
    base, feature = checkouts
    with pytest.raises(ValueError, match="ancestor"):
        benchmark._baseline_revisions(base, feature)
