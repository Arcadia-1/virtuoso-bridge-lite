#!/usr/bin/env python3
"""Export a Virtuoso layout to GDS with XStream Out."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from virtuoso_bridge import VirtuosoClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--stream-map", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--view", default="layout")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--skill-timeout", type=float, default=30.0)
    parser.add_argument("--finalization-reserve", type=float, default=30.0)
    parser.add_argument(
        "--cleanup-policy",
        choices=("success", "always", "never"),
        default="success",
    )
    return parser


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def main() -> int:
    args = _parser().parse_args()

    try:
        client = VirtuosoClient.from_env()
        probe = client.execute_skill(
            "1+1",
            timeout=min(10.0, args.skill_timeout),
        )
    except Exception as exc:
        _print_json(
            {
                "status": "error",
                "reason": "bridge_probe_failed",
                "errors": [str(exc) or type(exc).__name__],
            }
        )
        return 2

    if not probe.ok:
        _print_json(
            {
                "status": "error",
                "reason": "bridge_probe_failed",
                "errors": list(probe.errors)
                or ["bridge probe returned a non-success status"],
            }
        )
        return 2

    try:
        result = client.layout.export_gds(
            args.library,
            args.cell,
            args.output,
            stream_map=args.stream_map,
            view=args.view,
            log_path=args.log,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            skill_timeout=args.skill_timeout,
            finalization_reserve=args.finalization_reserve,
            cleanup_policy=args.cleanup_policy,
        )
    except (ValueError, FileNotFoundError) as exc:
        _print_json(
            {
                "status": "error",
                "reason": "invalid_arguments",
                "errors": [str(exc)],
            }
        )
        return 2

    log_result = result.log_result
    _print_json(
        {
            "status": result.status.value,
            "reason": result.reason.value,
            "timed_out": result.timed_out,
            "execution_time": result.execution_time,
            "local_gds_path": (
                str(result.local_gds_path)
                if result.local_gds_path is not None
                else None
            ),
            "local_log_path": (
                str(result.local_log_path)
                if result.local_log_path is not None
                else None
            ),
            "error_count": (
                log_result.error_count if log_result is not None else None
            ),
            "warning_count": (
                log_result.warning_count if log_result is not None else None
            ),
            "warnings": list(result.warnings),
            "errors": list(result.errors),
            "translated_structures": (
                [asdict(item) for item in log_result.translated_structures]
                if log_result is not None
                else []
            ),
            "local_run_dir": (
                str(result.local_run_dir)
                if result.local_run_dir is not None
                else None
            ),
            "remote_run_dir": result.remote_run_dir,
            "remote_files_retained": result.remote_files_retained,
        }
    )
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
