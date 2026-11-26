#!/usr/bin/env python3
"""Check curated Spectre netlists for residual generated-artifact issues.

This script is a checker only. It does not rewrite, split, rename, or clean a
netlist. Use it after a semantic cleanup pass to report issues that still need
model/engineer review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_MOS_KEEP = ("l", "w", "nf", "fingers", "m", "multi")

ELEMENT_RE = re.compile(r"^(\s*)(\S+)\s*\(([^)]*)\)\s+(\S+)(.*)$")
SUBCKT_RE = re.compile(r"^\s*(?:inline\s+)?subckt\s+(\S+)\b", re.IGNORECASE)
ENDS_RE = re.compile(r"^\s*ends\b(?:\s+(\S+))?", re.IGNORECASE)
PARAM_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*(\"[^\"]*\"|\S+)")

_BARE_RUN_KEYWORDS = (
    r"ac|dc|tran|noise|pnoise|pss|stb|sp|hb|envlp|montecarlo|"
    r"info|save|probe|assert|checklimit|options|simulatorOptions"
)
_NAMED_RUN_KEYWORDS = (
    r"ac|dc|tran|noise|pnoise|pss|stb|sp|hb|envlp|montecarlo|"
    r"info|options|assert|checklimit"
)
_NON_RUN_LEADING_KEYWORDS = (
    r"include|library|section|endlibrary|parameters|paramset|"
    r"global|simulator|model|subckt|inline|ends|ahdl_include|"
    r"statistics|correlate|process|mismatch"
)
RUN_DIRECTIVE_RE = re.compile(
    rf"^\s*(?:"
    rf"(?:{_BARE_RUN_KEYWORDS})(?=\s+(?!\()|\s*$)"
    rf"|"
    rf"(?!(?:{_NON_RUN_LEADING_KEYWORDS})(?:\s|$))"
    rf"[^\s()=]+\s+(?:{_NAMED_RUN_KEYWORDS})(?=\s|$)"
    rf")",
    re.IGNORECASE,
)
MODEL_DIRECTIVE_RE = re.compile(r"^\s*(?:include|library|section|endlibrary)\b", re.IGNORECASE)
PARAM_DIRECTIVE_RE = re.compile(r"^\s*(?:parameters|paramset)\b", re.IGNORECASE)

RANDOM_NODE_PATTERNS = (
    re.compile(r"^net\d+$", re.IGNORECASE),
    re.compile(r"^_net\d*$", re.IGNORECASE),
    re.compile(r"^[1-9]\d*$"),
    re.compile(r"^N_.+", re.IGNORECASE),
    re.compile(r"^c_\d+_[np]$", re.IGNORECASE),
    re.compile(r"^mesh_\d+$", re.IGNORECASE),
    re.compile(r"^noxref", re.IGNORECASE),
)

GENERIC_INSTANCE_RE = re.compile(r"^[MRCVIX]\d+(?:\\?<\d+\\?>)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    severity: str
    category: str
    path: str
    line: int
    subject: str
    message: str


def logical_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buf = ""
    start_line = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buf and not line.strip():
            lines.append((lineno, ""))
            continue
        if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
            if not buf:
                start_line = lineno
            buf += line.rstrip()[:-1].rstrip() + " "
            continue
        if buf:
            line = buf + line.lstrip()
            lineno = start_line
            buf = ""
            start_line = 0
        lines.append((lineno, line))
    if buf:
        lines.append((start_line, buf.rstrip()))
    return lines


def split_comment(line: str) -> str:
    if "//" not in line:
        return line
    return line.split("//", 1)[0].rstrip()


def is_mos_model(model: str, inst: str) -> bool:
    ml = model.lower()
    il = inst.lower()
    return (
        il.startswith("m")
        or ml.endswith("_mac")
        or "nch" in ml
        or "pch" in ml
        or "nmos" in ml
        or "pmos" in ml
        or "nfet" in ml
        or "pfet" in ml
    )


def line_issue(
    severity: str,
    category: str,
    path: Path,
    line: int,
    subject: str,
    message: str,
) -> Issue:
    return Issue(severity, category, str(path), line, subject, message)


def check_file(path: Path, mode: str, keep: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    subckt_depth = 0
    saw_subckt = False
    saw_run_directive = False
    saw_top_level_element = False

    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, raw_line in logical_lines(text):
        line = split_comment(raw_line).strip()
        if not line or line.startswith(("*", "//")):
            continue

        if SUBCKT_RE.match(line):
            saw_subckt = True
            subckt_depth += 1
            continue
        if ENDS_RE.match(line):
            if subckt_depth == 0:
                issues.append(
                    line_issue(
                        "error",
                        "structure",
                        path,
                        lineno,
                        "ends",
                        "`ends` without matching `subckt`.",
                    )
                )
            else:
                subckt_depth -= 1
            continue

        if RUN_DIRECTIVE_RE.match(line):
            saw_run_directive = True
            if mode == "dut":
                issues.append(
                    line_issue(
                        "warn",
                        "dut-boundary",
                        path,
                        lineno,
                        line.split()[0],
                        "DUT files should not own analyses, saves, probes, or simulator options.",
                    )
                )
            continue

        if mode == "dut" and MODEL_DIRECTIVE_RE.match(line):
            issues.append(
                line_issue(
                    "warn",
                    "dut-boundary",
                    path,
                    lineno,
                    line.split()[0],
                    "Model/library includes usually belong in run decks, not reusable DUT files.",
                )
            )
            continue

        if mode == "dut" and PARAM_DIRECTIVE_RE.match(line) and subckt_depth == 0:
            issues.append(
                line_issue(
                    "warn",
                    "dut-boundary",
                    path,
                    lineno,
                    line.split()[0],
                    "Top-level parameters usually belong in run decks or testbenches.",
                )
            )

        match = ELEMENT_RE.match(line)
        if not match:
            continue

        _indent, inst, pins, model, tail = match.groups()
        if subckt_depth == 0:
            saw_top_level_element = True
            if mode == "dut":
                issues.append(
                    line_issue(
                        "warn",
                        "dut-boundary",
                        path,
                        lineno,
                        inst,
                        "Top-level elements in DUT files usually indicate testbench content.",
                    )
                )

        if GENERIC_INSTANCE_RE.match(inst):
            issues.append(
                line_issue(
                    "info",
                    "generic-instance",
                    path,
                    lineno,
                    inst,
                    "Generic tool instance name; rename when circuit function is known.",
                )
            )

        for pin in pins.split():
            clean = pin.replace("\\<", "<").replace("\\>", ">")
            if any(pattern.match(clean) for pattern in RANDOM_NODE_PATTERNS):
                issues.append(
                    line_issue(
                        "info",
                        "random-node",
                        path,
                        lineno,
                        pin,
                        "Random-looking node name; rename when semantic meaning is known.",
                    )
                )

        if is_mos_model(model, inst):
            extras = sorted({key for key, _value in PARAM_RE.findall(tail) if key.lower() not in keep})
            if extras:
                issues.append(
                    line_issue(
                        "warn",
                        "mos-tail-params",
                        path,
                        lineno,
                        inst,
                        "MOS has parameters outside the clean geometry keep-list: "
                        + ", ".join(extras),
                    )
                )

    if subckt_depth:
        issues.append(
            line_issue(
                "error",
                "structure",
                path,
                0,
                "subckt",
                "File ended before all subckt definitions were closed.",
            )
        )

    if mode == "any" and saw_subckt and (saw_run_directive or saw_top_level_element):
        issues.append(
            line_issue(
                "info",
                "monolithic-deck",
                path,
                0,
                "file",
                "File mixes subckt definitions with run/testbench content; consider splitting.",
            )
        )

    return issues


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="+", type=Path, help="Curated Spectre netlist(s) to check")
    parser.add_argument(
        "--mode",
        choices=("any", "dut", "tb", "run"),
        default="any",
        help="Expected role of the checked file",
    )
    parser.add_argument(
        "--mos-keep",
        default=",".join(DEFAULT_MOS_KEEP),
        help="Comma-separated MOS parameter keep-list",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on-issues", action="store_true")
    return parser.parse_args(argv)


def print_text(issues: list[Issue]) -> None:
    if not issues:
        print("no checker issues found")
        return
    for issue in issues:
        location = f"{issue.path}:{issue.line}" if issue.line else issue.path
        print(f"{issue.severity}: {issue.category}: {location}: {issue.subject}: {issue.message}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    keep = {item.strip().lower() for item in args.mos_keep.split(",") if item.strip()}

    issues: list[Issue] = []
    for path in args.input:
        if not path.exists():
            issues.append(
                line_issue("error", "input", path, 0, str(path), "Input file does not exist.")
            )
            continue
        issues.extend(check_file(path, args.mode, keep))

    if args.format == "json":
        print(json.dumps([asdict(issue) for issue in issues], indent=2))
    else:
        print_text(issues)

    if args.fail_on_issues and issues:
        return 2
    if any(issue.severity == "error" for issue in issues):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
