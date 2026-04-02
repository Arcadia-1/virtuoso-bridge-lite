#!/usr/bin/env python3
"""
turbo_optimize_template.py
==========================
Template for TuRBO circuit optimization.

Copy this file to your project's optimization/ folder and adapt:
  1. PARAM_NAMES, LB, UB  — define your design variables and bounds
  2. apply_params(x)       — write x into your simulation module's parameter dict
  3. run_simulation()      — call your simulation and return raw results
  4. objective(x)          — assemble the scalar metric from raw results

Then run:  python turbo_optimize_<your_circuit>.py
"""

import os
import sys
import json
import time
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent  # project root
WORK_DIR    = PROJECT_DIR / "optimization" / "results"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# ── Simulator back-end ─────────────────────────────────────────────────────
# Option A: ngspice via existing skill
# ------------------------------------
# Uncomment and adapt for your target skill (comparator, LDO, bootstrap, etc.)
#
# SKILL_SCRIPTS = PROJECT_DIR / ".claude" / "skills" / "comparator" / "scripts"
# sys.path.insert(0, str(SKILL_SCRIPTS))
# os.environ["ANALOG_WORK_DIR"] = str(WORK_DIR)
#
# import comparator_common as cc
# import simulate_tran_strongarm_noise as sim_noise
# sim_noise.SWEEP_NCYC  = 200          # reduce for speed during optimization
# sim_noise.SWEEP_TSTOP = 200 * cc.TCLK

# Option B: Spectre via subprocess
# ---------------------------------
# import subprocess, re
# NETLIST_TMPL = """
# // Spectre netlist template — fill {PARAM} placeholders
# ...
# """
# def render_netlist(**params):
#     return NETLIST_TMPL.format(**params)

# ── Import TuRBO ───────────────────────────────────────────────────────────
from turbo import Turbo1  # or TurboM for multiple trust regions

# ── Design variables ───────────────────────────────────────────────────────
# Define one entry per continuous parameter.
# All values in consistent units (e.g. all widths in µm, caps in fF).
PARAM_NAMES = [
    "param_0",   # e.g. W_tail (µm)
    "param_1",   # e.g. W_inp  (µm)
    # add more ...
]

LB = np.array([0.5,  0.5])   # lower bounds — same order as PARAM_NAMES
UB = np.array([10.,  10.])   # upper bounds

# ── Results log ────────────────────────────────────────────────────────────
RESULTS_FILE = WORK_DIR / "turbo_results.json"
_results_log = []
_eval_count  = 0


def apply_params(x: np.ndarray):
    """Write x into the simulation module's parameter dict."""
    # Option A (ngspice skill):
    # cc.W["tail"] = float(x[0])
    # cc.W["inp"]  = float(x[1])
    pass   # replace with your implementation


def run_simulation() -> dict:
    """
    Run simulation with current parameters and return raw metrics.

    Returns dict with at least the keys your objective needs.
    Raise an exception on hard failure (caught in objective()).
    """
    # Option A (ngspice comparator skill):
    # return sim_noise.simulate_noise()

    # Option B (Spectre subprocess):
    # netlist = render_netlist(param_0=..., param_1=...)
    # result  = run_spectre(netlist, WORK_DIR)
    # return result

    raise NotImplementedError("Implement run_simulation()")


def extract_metrics(raw: dict) -> tuple[float, ...]:
    """
    Pull scalar metrics from raw simulation output.

    Return a tuple of floats; return (float('nan'), ...) on parse failure.
    """
    # Option A (ngspice comparator):
    # power = raw["power_pt"]["p_avg_uw"]
    # tcmp  = raw["tcmp_pt"]["tcmp_ps"]
    # return power, tcmp

    raise NotImplementedError("Implement extract_metrics()")


def objective(x: np.ndarray) -> float:
    """TuRBO calls this with a 1-D parameter vector; must return a scalar float."""
    global _eval_count
    _eval_count += 1
    n = _eval_count

    apply_params(x)
    w_str = "  ".join(f"{nm}={v:.3f}" for nm, v in zip(PARAM_NAMES, x))
    print(f"\n[eval {n:>3d}]  {w_str}", flush=True)

    t0 = time.perf_counter()
    try:
        raw = run_simulation()
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)
        _log_result(n, x, None, None)
        return 1e6   # penalty

    elapsed = time.perf_counter() - t0

    try:
        metrics = extract_metrics(raw)
    except Exception as e:
        print(f"  PARSE ERROR: {e}", flush=True)
        _log_result(n, x, None, None)
        return 1e6

    # Guard against NaN / non-positive
    if any(m != m or m <= 0 for m in metrics):
        print(f"  INVALID metrics: {metrics}", flush=True)
        _log_result(n, x, None, None)
        return 1e6

    # ── Compose objective ────────────────────────────────────────────────
    # Adapt this line to your goal (see SKILL.md § Common Objective Functions)
    # Examples:
    #   power * delay          — minimize power-delay product
    #   sigma_uv               — minimize noise
    #   -(gain * bandwidth)    — maximize GBW (negate for minimization)
    obj = metrics[0] * metrics[1]   # e.g. power * tcmp

    print(f"  => metrics={metrics}  obj={obj:.3g}  ({elapsed:.1f}s)", flush=True)
    _log_result(n, x, metrics, obj)
    return obj


def _log_result(n, x, metrics, obj):
    _results_log.append({
        "eval":    n,
        "params":  dict(zip(PARAM_NAMES, x.tolist())),
        "metrics": list(metrics) if metrics is not None else None,
        "obj":     float(obj) if obj is not None else None,
    })
    with open(RESULTS_FILE, "w") as f:
        json.dump(_results_log, f, indent=2)


def plot_convergence(fX: np.ndarray, out_path: Path, title: str = "TuRBO Convergence"):
    valid = fX < 1e5
    best  = np.full_like(fX, np.nan)
    cur   = np.inf
    for i, v in enumerate(fX):
        if valid[i] and v < cur:
            cur = v
        best[i] = cur

    evals = np.arange(1, len(fX) + 1)
    fig, ax = plt.subplots(figsize=(9, 4))
    masked = fX.copy().astype(float)
    masked[~valid] = np.nan
    ax.scatter(evals[valid], masked[valid], s=20, alpha=0.5, color="steelblue", label="Each eval")
    ax.plot(evals, best, color="crimson", lw=2, label="Best so far")
    ax.set_xlabel("Evaluation")
    ax.set_ylabel("Objective")
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Plot -> {out_path}")


def main():
    N_INIT    = 2 * len(LB)   # rule of thumb
    MAX_EVALS = 100

    print("=" * 60)
    print("  TuRBO-1 Optimization")
    print("=" * 60)
    print(f"  Params   : {PARAM_NAMES}")
    print(f"  LB       : {LB.tolist()}")
    print(f"  UB       : {UB.tolist()}")
    print(f"  n_init   : {N_INIT}   max_evals : {MAX_EVALS}")
    print(f"  Output   : {WORK_DIR}")
    print()

    turbo = Turbo1(
        f                = objective,
        lb               = LB,
        ub               = UB,
        n_init           = N_INIT,
        max_evals        = MAX_EVALS,
        batch_size       = 1,       # keep 1 — simulation modules are not thread-safe
        verbose          = True,
        use_ard          = True,    # ARD learns per-parameter lengthscales
        n_training_steps = 50,
        device           = "cpu",
        dtype            = "float64",
    )

    t_start = time.perf_counter()
    turbo.optimize()
    total_time = time.perf_counter() - t_start

    X  = turbo.X
    fX = turbo.fX.flatten()

    valid = fX < 1e5
    best_x = X[valid][np.argmin(fX[valid])] if valid.any() else X[np.argmin(fX)]
    best_f = fX[valid].min()               if valid.any() else fX.min()

    print()
    print("=" * 60)
    print("  Optimization Complete")
    print("=" * 60)
    print(f"  Total time : {total_time/60:.1f} min")
    print(f"  Best obj   : {best_f:.4g}")
    for name, val in zip(PARAM_NAMES, best_x):
        print(f"    {name} = {val:.4f}")

    plot_convergence(fX, WORK_DIR / "turbo_convergence.png")

    summary = {
        "best_obj":        float(best_f),
        "best_params":     {k: float(v) for k, v in zip(PARAM_NAMES, best_x)},
        "total_evals":     int(len(fX)),
        "total_time_min":  total_time / 60,
    }
    with open(WORK_DIR / "turbo_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary -> {WORK_DIR / 'turbo_summary.json'}")


if __name__ == "__main__":
    main()
