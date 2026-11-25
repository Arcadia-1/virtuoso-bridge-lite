"""Compare clean upstream and compact results in separate offline processes.

Install tiktoken only in the benchmark environment, then run:
python tools/benchmark_compact_results.py --baseline-root /path/to/clean/upstream

Counts use a fixed tokenizer, not account usage or a complete agent session.
No Cadence, SSH, PDK, or running daemon is used.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def _worker(root, compact):
    sys.path[:0] = [str(root / "src"), str(root)]
    import mcp_server as mcp
    from virtuoso_bridge.virtuoso.maestro.reader.runs import read_results

    text = json.load(sys.stdin)["csv"]

    class Client:
        calls = 0

        def __init__(self):
            self.responses = iter(['("TRAN")', "t", "t", '"passed"', "nil"])

        def execute_skill(self, command, timeout=None):
            self.calls += 1
            return SimpleNamespace(output=next(self.responses), errors=[])

        def download_file(self, remote, local):
            Path(local).write_text(text, encoding="utf-8")

    client = Client()
    collected = {}

    def read(session, *, history, **kwargs):
        data = read_results(client, session, lib="DEMO", cell="TB", history=history, **kwargs)
        collected.update(data)
        return data

    mcp._client = lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read))
    args = {"session": "s1", "history": "Interactive.7"}
    if compact:
        args["compact"] = True
    response = mcp._impl_maestro_read(args)
    tool_schema = next(tool for tool in mcp._tool_defs() if tool["name"] == "test_results")
    print(json.dumps({"data": collected, "mcp": response, "tool_schema": tool_schema, "skill_calls": client.calls,
                      "source": str(Path(mcp.__file__).resolve())}, ensure_ascii=False))


def _csv(points, outputs):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Point", "Test", "Output", "Nominal", "Spec", "Weight", "Pass/Fail"])
    for point in range(1, points + 1):
        writer.writerow([f"Parameters: iload={point}m, VDD=1.8", "", "", "", "", "", ""])
        for output in range(outputs):
            value = f"{point}.{output:04d}e-6"
            writer.writerow([point, "TRAN", f"metric_{output}", value, "< 1m", "1", "passed"])
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/compact-benchmark"))
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root:
        _worker(args.worker_root.resolve(), args.compact)
        return
    if args.baseline_root is None:
        parser.error("--baseline-root must name a clean checkout of the same upstream commit")
    root = Path(__file__).resolve().parents[1]
    baseline = args.baseline_root.resolve()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=baseline).strip():
        parser.error("baseline checkout must be clean")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).strip()
    if revision != subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=baseline).strip():
        parser.error("baseline and modified checkout must share the same HEAD commit")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(output / "tokenizer-cache"))
    try:
        import tiktoken
    except ImportError:
        parser.error("install optional tiktoken in this benchmark environment")
    encoding = tiktoken.get_encoding("o200k_base")

    def run(checkout, text, compact=False):
        command = [sys.executable, str(Path(__file__).resolve()), "--worker-root", str(checkout)]
        if compact:
            command.append("--compact")
        result = subprocess.run(
            command, input=json.dumps({"csv": text}), cwd=checkout, capture_output=True,
            text=True, encoding="utf-8", timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def tokens(value):
        return len(encoding.encode(json.dumps(value, ensure_ascii=False)))

    report = {"base_commit": revision.decode(), "tokenizer": "o200k_base",
              "measurement": "offline tool-payload token estimates, not billed tokens or total agent usage",
              "cases": []}
    for name, points, outputs in [("single", 1, 4), ("sweep", 10, 8), ("wide_sweep", 40, 8)]:
        text = _csv(points, outputs)
        original = run(baseline, text)
        unchanged = run(root, text)
        compact = run(root, text, True)
        assert original["data"] == unchanged["data"]
        assert original["mcp"] == unchanged["mcp"]
        expected = {k: v for k, v in original["data"].items() if k != "outputs"}
        assert compact["data"] == expected
        reconstructed = [{"point": p["point"], "name": n, "value": v["value"], "spec_status": v["pass_fail"]}
                         for p in compact["data"]["points"] for n, v in p["outputs"].items()]
        assert reconstructed == original["data"]["outputs"]
        assert compact["mcp"]["structuredContent"] == {"session": "s1", **expected}
        assert original["skill_calls"] == compact["skill_calls"] == 5
        metrics = {"name": name, "points": points, "outputs_per_point": outputs,
                   "measurements_equal": True, "default_compatible": True,
                   "skill_calls_per_read": 5, "mcp_tool_calls_per_case": 1}
        for key, old, new in [
            ("python_result", original["data"], compact["data"]),
            ("mcp_structured_only", original["mcp"]["structuredContent"], compact["mcp"]["structuredContent"]),
            ("mcp_text_and_structured", original["mcp"], compact["mcp"]),
            ("mcp_response_plus_tool_definition",
             {"tool": original["tool_schema"], "response": original["mcp"]},
             {"tool": compact["tool_schema"], "response": compact["mcp"]}),
        ]:
            before, after = tokens(old), tokens(new)
            metrics[key] = {"before": before, "after": after,
                            "reduction_percent": round(100 * (1 - after / before), 2)}
        (output / f"{name}-comparison.json").write_text(
            json.dumps({"original": original, "modified_default": unchanged, "modified_compact": compact},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
        report["cases"].append(metrics)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
