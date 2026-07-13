"""
SMIC180 Multi-Corner Parallel Simulation Engine.

Uses bridge-lite's SpectreSimulator.run_parallel() to run all
SMIC180 process corners (tt/ff/ss/fnsp/snfp) concurrently.

Core principle: no wheel reinvention.  bridge-lite provides:
  - SpectreSimulator.from_env()        -- SSH tunnel + cshrc auto-detect
  - SpectreSimulator.run_parallel()    -- UUID-isolated parallel jobs
  - SpectreSimulator.wait_all()        -- batch result collection

This module only does SMIC180-specific work:
  1. patch_corner()       -- replace ModelInclude.section (tt -> ff/ss/...)
  2. build_corner_deck()  -- write per-corner deck.scs via existing build_sim_deck()
  3. run_corners_parallel() -- orchestrate the parallel batch
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sim_io.sim.config import SimDeckConfig, ModelInclude
from sim_io.sim.deck import build_sim_deck, _SEPARATOR


# SMIC180 e2r018_v1p8 Process Corners
# Verified from PDK: e2r018_v1p8_spe.scs  grep "^section"
#   tt  ff  ss  fnsp  snfp
#   bjt_tt  bjt_ff  bjt_ss  res_tt  res_ff  res_ss  pip_tt  pip_ff  pip_ss  mim_tt  mim_ff  mim_ss
# patch_corner() replaces "tt" -> target, so "bjt_tt" -> "bjt_ff" etc. automatically.

SMIC180_CORNERS: list[str] = ["tt", "ff", "ss", "fnsp", "snfp"]


@dataclass
class CornerResult:
    """Result for a single process corner."""
    corner: str
    ok: bool
    data: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class MultiCornerResult:
    """Aggregated result from all corners."""
    results: dict[str, CornerResult] = field(default_factory=dict)
    passed_corners: list[str] = field(default_factory=list)
    failed_corners: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return len(self.failed_corners) == 0

    def summary(self) -> dict:
        return {
            "corners": list(self.results.keys()),
            "passed": self.passed_corners,
            "failed": self.failed_corners,
            "all_ok": self.all_ok,
            "per_corner": {
                c: {"ok": r.ok, "errors": r.errors[:3]}
                for c, r in self.results.items()
            },
        }

    def save(self, run_dir: Path) -> Path:
        path = run_dir / "corner_results.json"
        path.write_text(
            json.dumps(self.summary(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[corner] Results saved: {path}")
        return path


def patch_corner(deck: SimDeckConfig, corner: str) -> SimDeckConfig:
    """Replace ModelInclude.section values from tt -> target corner.

    Only patches sections containing "tt" (core MOS/BJT/RES/MIM/PIP).
    Empty-section entries (e.g. IO pad models) are left untouched.
    Sub-corners like bjt_tt become bjt_ff when corner="ff".
    """
    patched = copy.deepcopy(deck)
    for mi in patched.model_includes:
        if mi.section and "tt" in mi.section:
            mi.section = mi.section.replace("tt", corner)
    return patched


def build_corner_deck(
    netlist_text: str,
    base_deck: SimDeckConfig,
    corner: str,
    output_dir: Path,
) -> Path:
    """Build a complete Spectre deck for one corner.

    Returns path to output_dir/<corner>/deck.scs.

    If ``netlist_text`` already contains a full deck (separator present),
    strip it back to just the circuit portion before wrapping.  This
    prevents the SFE-59 duplicate-content error when the caller passes
    a deck.scs instead of the raw si-exported netlist.
    """
    # Defensive: strip full deck back to circuit-only netlist
    circuit_text = netlist_text
    sep_pos = netlist_text.find(_SEPARATOR)
    if sep_pos >= 0:
        # Take everything before the separator (the si-generated circuit)
        circuit_text = netlist_text[:sep_pos].rstrip()
        print(f"[corner] Stripped full deck back to circuit-only netlist ({len(circuit_text)} chars)")

    corner_deck = patch_corner(base_deck, corner)
    deck_text = build_sim_deck(circuit_text, corner_deck)
    corner_dir = output_dir / corner
    corner_dir.mkdir(parents=True, exist_ok=True)
    deck_path = corner_dir / "deck.scs"
    deck_path.write_text(deck_text, encoding="utf-8")
    return deck_path


def run_corners_parallel(
    netlist_path: Path,
    base_deck: SimDeckConfig,
    run_dir: Path,
    *,
    corners: list[str] | None = None,
    spectre_mode: str = "lx",
    spectre_timeout: int = 600,
    max_workers: int | None = None,
) -> MultiCornerResult:
    """Run all SMIC180 process corners in parallel via bridge-lite.

    Steps:
      1. Read the si-exported netlist (circuit only).
      2. For each corner, patch ModelInclude.section and write deck.scs.
      3. Call SpectreSimulator.run_parallel() -- bridge-lite handles
         UUID-isolated remote dirs, SSH ControlMaster sharing, etc.
      4. Collect results into MultiCornerResult.

    Parameters
    ----------
    netlist_path : Path
        si-exported netlist file (from run_sim_run Step 3a).
    base_deck : SimDeckConfig
        Resolved simulation deck config (typically tt-based).
    run_dir : Path
        Output directory; corner artifacts go under run_dir/corners/.
    corners : list[str], optional
        Process corners to simulate. Default: SMIC180_CORNERS.
    spectre_mode : str
        Spectre execution mode (spectre/aps/cx/ax/mx/lx/vx).
    spectre_timeout : int
        Per-corner timeout in seconds.
    max_workers : int, optional
        Max parallel workers. Default: len(corners).
    """
    if os.name == "nt":
        os.environ.setdefault("VB_DISABLE_CONTROL_MASTER", "1")

    corners = corners or SMIC180_CORNERS
    corner_dir = run_dir / "corners"
    corner_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Read netlist (shared by all corners)
    netlist_text = netlist_path.read_text(encoding="utf-8")

    # Step 2: Build per-corner deck files
    deck_tasks: list[tuple[Path, dict]] = []
    for corner in corners:
        deck_path = build_corner_deck(netlist_text, base_deck, corner, corner_dir)
        deck_tasks.append((deck_path, {}))
        print(f"[corner] Built deck for {corner}: {deck_path}")

    # Step 3: Parallel Spectre via bridge-lite
    from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

    sim = SpectreSimulator.from_env(
        spectre_args=spectre_mode_args(spectre_mode),
        work_dir=str(corner_dir),
        timeout=spectre_timeout,
    )

    mw = max_workers or len(corners)
    print(f"\n[corner] Running {len(corners)} corners in parallel "
          f"(max_workers={mw}, mode={spectre_mode})...\n")

    results_list = sim.run_parallel(deck_tasks, max_workers=mw)

    # Step 4: Collect results
    multi = MultiCornerResult()
    for corner, sim_result in zip(corners, results_list):
        cr = CornerResult(
            corner=corner,
            ok=sim_result.ok,
            data=dict(sim_result.data) if sim_result.data else {},
            errors=list(sim_result.errors) if sim_result.errors else [],
            metadata=dict(sim_result.metadata) if sim_result.metadata else {},
        )
        multi.results[corner] = cr
        if cr.ok:
            multi.passed_corners.append(corner)
        else:
            multi.failed_corners.append(corner)

        status = "OK" if cr.ok else "FAILED"
        n_signals = len(cr.data) if cr.data else 0
        err_hint = f"  errors={cr.errors[:2]}" if cr.errors else ""
        print(f"  [{corner}] {status}  signals={n_signals}{err_hint}")

    print(f"\n[corner] Done: {len(multi.passed_corners)}/{len(corners)} passed")
    return multi
