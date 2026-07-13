#!/usr/bin/env python3
"""Generate optimizer config for a new circuit.

Usage:
    python generate_config.py --lib <lib> --cell <cell> --run-dir <run_dir>
"""
import argparse
import json
from pathlib import Path
from virtuoso_bridge import VirtuosoClient
from optimizer import analyze_topology, generate_default_config

def main():
    parser = argparse.ArgumentParser(description="Generate optimizer config for new circuit")
    parser.add_argument("--lib", required=True, help="Library name")
    parser.add_argument("--cell", required=True, help="Cell name")
    parser.add_argument("--run-dir", required=True, help="Run directory")
    parser.add_argument("--output", default=None, help="Output config file")
    args = parser.parse_args()

    client = VirtuosoClient.from_env()
    run_dir = Path(args.run_dir)

    print(f"Analyzing topology for {args.lib}/{args.cell}...")
    topo = analyze_topology(client, args.lib, args.cell)

    if not topo:
        print("WARNING: No topology detected, using default config")
        config = generate_default_config(run_dir)
    else:
        print(f"Found {len(topo)} instances:")
        for inst, info in sorted(topo.items()):
            print(f"  {inst}: {info['role']}")

        # Generate config
        config = generate_default_config(run_dir)
        config["topology_summary"] = {inst: {"role": info["role"], "block": info["block"]}
                                       for inst, info in topo.items()}

    # Save
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = run_dir / "optimization" / "optimizer_config_topology.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"\nConfig saved to: {out_path}")
    print(f"Params: {len(config['params'])}")
    print(f"\nNext steps:")
    print(f"  1. Review and adjust {out_path}")
    print(f"  2. Run: python optimizer.py --run-dir {run_dir} --config {out_path}")

    return 0

if __name__ == "__main__":
    main()
