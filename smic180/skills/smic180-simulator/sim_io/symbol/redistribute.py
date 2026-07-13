"""Redistribute symbol pins on 2 sides (left=outer, right=CORE/duplicate).

3-Step Architecture:
  Step 0: TSG (fresh symbol from schematic)
  Step 1: Extract all symbol info via reusable SKILL extractor
  Step 2: Calculate new layout (body + pin positions) in pure Python
  Step 3: Generate + execute design-specific SKILL in one shot

Total network round-trips: 5 (down from 3+N+ceil(N/22)).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_SIM_IO = _PKG_DIR.parent.parent

from virtuoso_bridge import VirtuosoClient

from sim_io.flow import create_run_dir, log_skill_code
from sim_io.symbol.layout_engine import (
    LayoutConfig,
    LayoutEngine,
    Side,
    generate_apply_skill,
    parse_symbol_info,
)

_SKILL_DIR = _SIM_IO / "skill_code"


def run(lib: str, cell: str, *, debug: bool = False):
    client = VirtuosoClient.from_env()
    run_dir = create_run_dir()
    build_dir = run_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    # Log current skill code snapshot
    for il_file in sorted(_SKILL_DIR.glob("*.il")):
        log_skill_code(run_dir, il_file)

    print(f"\n{'='*60}")
    print(f" Symbol Redistribute: {lib}/{cell}")
    print(f" Output: {run_dir}")
    print(f"{'='*60}\n")

    # ── Step 0: Fresh TSG ─────────────────────────────────────
    print("=== Step 0: Regenerate symbol via TSG ===")
    client.execute_skill(f'ddDeleteCellView("{lib}" "{cell}" "symbol")')
    client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
    r = client.execute_skill(
        f'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
    )
    print(f"TSG: {'OK' if not r.errors else r.errors}")

    # ── Step 1: Extract ───────────────────────────────────────
    print("\n=== Step 1: Extract symbol info ===")
    load_r = client.load_il(str(_SKILL_DIR / "extract_symbol_info.il"))
    if not load_r.ok:
        print(f"  ERROR loading extractor: {load_r.errors}")
        return
    r = client.execute_skill(f'extractSymbolInfo("{lib}" "{cell}")', timeout=60)
    if r.errors:
        print(f"  ERROR extracting: {r.errors}")
        return

    info = parse_symbol_info(r.output)
    print(f"  Extracted: {len(info.rects)} rects, {len(info.lines)} lines, "
          f"{len(info.labels)} labels, {len(info.terminals)} terminals")

    # Save raw extraction data
    (build_dir / "extract_raw.txt").write_text(r.output or "", encoding="utf-8")

    # ── Step 2: Calculate layout ──────────────────────────────
    print("\n=== Step 2: Calculate layout ===")
    engine = LayoutEngine(LayoutConfig())
    result = engine.redesign(info)
    body = result.body
    print(f"  Body: ({body.left:.3f}, {body.bottom:.3f}) "
          f"to ({body.right:.3f}, {body.top:.3f})  "
          f"({body.right - body.left:.3f}x"
          f"{body.top - body.bottom:.3f})")
    for side_name in ["left", "right"]:
        count = sum(1 for p in result.pins if p.side.value == side_name)
        print(f"  {side_name}: {count} pins")

    # Save layout result as JSON (debug only)
    if debug:
        layout_data = {
            "lib": lib, "cell": cell,
            "body": {k: v for k, v in asdict(body).items()},
            "pins": [{k: (v.value if isinstance(v, Side) else v)
                      for k, v in asdict(p).items()} for p in result.pins],
        }
        (build_dir / "layout_result.json").write_text(
            json.dumps(layout_data, indent=2), encoding="utf-8"
        )

    # ── Step 3: Apply ─────────────────────────────────────────
    print("\n=== Step 3: Apply layout ===")
    skill_code = generate_apply_skill(lib, cell, result, engine.config)

    # Save generated .il to run_dir
    apply_il = build_dir / "apply_layout.il"
    apply_il.write_text(skill_code, encoding="utf-8")
    print(f"  Saved: {apply_il}")

    load_r = client.load_il(str(apply_il), timeout=120)
    if not load_r.ok:
        print(f"  ERROR: {load_r.errors}")
        return
    print(f"  OK — applied via load_il")

    # ── Verify ────────────────────────────────────────────────
    print("\n=== Verify ===")
    sym = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" nil "r")'
    r = client.execute_skill(f'{sym}~>bBox')
    print(f"  Final bBox: {r.output}")
    r = client.execute_skill(f'length({sym}~>terminals)')
    print(f"  Terminals: {r.output}")
    r = client.execute_skill(f'length({sym}~>shapes)')
    print(f"  Shapes: {r.output}")

    print(f"\n  Output dir: {run_dir}")
    print("\n=== DONE ===")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python symbol_redistribute.py <lib> <cell> [--debug]")
        print("Example: python symbol_redistribute.py LLM_Layout_Design_Lab IO_RING_12x12")
        sys.exit(1)
    debug = "--debug" in sys.argv
    run(sys.argv[1], sys.argv[2], debug=debug)
