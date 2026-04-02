---
name: turbo
description: "TuRBO (Trust-Region Bayesian Optimization) skill for analog circuit parameter optimization using expensive black-box simulations. Use this skill when the user wants to: (1) optimize transistor widths, biasing, or any continuous circuit parameter to minimize/maximize a performance metric such as power, speed, noise, FOM, or any combination; (2) set up a Bayesian optimization loop around any circuit simulator; (3) understand the TuRBO pattern — design variables, objective function, result extraction, convergence plotting; (4) reproduce or extend the comparator Power×Tcmp optimization example. For simulator-specific wiring, refer to the relevant simulation skill (ngspice, comparator, bootstrap_switch, LDO, etc.) and follow the user's intent. TuRBO is a minimizer — negate objectives for maximization."
---

# TuRBO Circuit Optimization Skill

> **Do not modify skill files during normal use.**
> Write all new optimization scripts and results into the **project working directory** (outside `.claude/`).
> Only edit skill-internal files when the user explicitly asks to improve or extend the skill.

---

## What is TuRBO?

TuRBO (Trust-Region Bayesian Optimization, NeurIPS 2019) is a sample-efficient global optimizer designed for **expensive, noise-free, black-box functions** — exactly what analog circuit simulation is.

It builds a Gaussian Process surrogate of the objective and uses local trust regions to balance exploration and exploitation. Each trust region shrinks on failure and expands on success; when it collapses below a threshold the region restarts from a new random point.

**Two variants:**

| Variant | When to use |
|---------|-------------|
| `Turbo1` | Sequential, single trust region. Best for <200 evaluations or when each simulation is slow (>30s). |
| `TurboM` | Multiple parallel trust regions. Better global coverage for larger budgets; use `n_trust_regions=3–5`. |

**Key rule:** TuRBO **minimizes**. To maximize a metric, return its negative from the objective function.

---

## Prerequisites & Installation

```
TuRBO/          ← must exist in the project root
```

```bash
pip install torch gpytorch          # core dependencies
pip install -e TuRBO/               # install TuRBO from local folder
```

Verify:
```bash
python -c "from turbo import Turbo1, TurboM; print('OK')"
```

---

## General Pattern

Every TuRBO optimization follows the same four-step structure.

### Step 1 — Define design variables and bounds

```python
import numpy as np

PARAM_NAMES = ["W_tail", "W_inp", "W_lat_n", ...]   # human-readable labels
LB = np.array([0.5,  0.5,  0.5, ...])               # lower bounds (same units as params)
UB = np.array([10.,  10.,  6.,  ...])               # upper bounds
```

All parameters must be **continuous** and share **consistent units** (e.g. all widths in µm, all capacitances in fF). TuRBO normalizes internally to [0,1]^d.

### Step 2 — Write the objective function

```python
def objective(x: np.ndarray) -> float:
    """
    x: 1-D numpy array of parameter values (same order as PARAM_NAMES).
    Returns: scalar float to minimize.
    """
    assert x.ndim == 1

    # 1. Apply parameters to your simulation module or script
    apply_params(x)

    # 2. Run simulation — consult the relevant simulation skill for how
    try:
        result = run_simulation()
    except Exception as e:
        print(f"  Simulation error: {e}")
        return 1e6    # penalty — must be >> any valid objective value

    # 3. Extract and return scalar metric
    power = result["power_uw"]
    tcmp  = result["tcmp_ps"]

    if any(v is None or v != v for v in [power, tcmp]):  # NaN/None guard
        return 1e6

    return power * tcmp   # example: minimize power-delay product
```

**Critical rules:**
- Always return a **scalar float**
- Return a **large penalty** (1e6) on simulation failure — never `nan` or `inf`
- Keep `batch_size=1` unless you explicitly manage parallel simulation isolation (see Threading)

### Step 3 — Configure and run TuRBO

```python
from turbo import Turbo1

turbo = Turbo1(
    f                = objective,
    lb               = LB,
    ub               = UB,
    n_init           = 2 * len(LB),   # rule of thumb: 2 × dimensionality
    max_evals        = 100,            # total simulation budget
    batch_size       = 1,
    verbose          = True,
    use_ard          = True,           # ARD kernel: learns per-parameter lengthscales
    n_training_steps = 50,
    device           = "cpu",
    dtype            = "float64",
)

turbo.optimize()
```

### Step 4 — Extract results

```python
X  = turbo.X               # shape (n_evals, n_params) — all evaluated points
fX = turbo.fX.flatten()    # shape (n_evals,)           — all objective values

valid    = fX < 1e5                 # mask out penalty evaluations
best_x   = X[valid][np.argmin(fX[valid])]
best_f   = fX[valid].min()

for name, val in zip(PARAM_NAMES, best_x):
    print(f"  {name} = {val:.3f}")
```

---

## Simulation Back-End

This skill does **not** prescribe how to run the simulator. Consult the relevant skill for that circuit:

- **ngspice examples** → `ngspice` skill
- **StrongArm comparator** → `comparator` skill
- **Bootstrap switch** → `bootstrap_switch` skill
- **LDO regulator** → `LDO` skill
- **Spectre / other simulators** → follow the user's intent and ask if unclear

The agent should read the target simulation skill to understand what parameters to mutate and what metrics it returns, then wire those into `apply_params()` and `run_simulation()` above.

---

## Common Objective Functions

| Goal | Objective expression | Notes |
|---|---|---|
| Power-delay product | `power_uw * tcmp_ps` | Classic figure of merit |
| Energy-noise FOM | `(power_uw / fclk_ghz) * sigma_uv**2` | FOM1 from comparator skill |
| Minimize noise only | `sigma_uv` | Single metric |
| Maximize gain-bandwidth | `-(gain_db + 20*log10(bw_hz))` | Negate to minimize |
| Area proxy | `sum(W * L for W, L in devices)` | Combine with performance as penalty |
| Multi-objective weighted | `w1 * power + w2 * delay + w3 * sigma` | Tune weights to taste |

**Soft constraint penalties** — prefer soft rejection over hard rejection so TuRBO can learn the constraint boundary:

```python
obj = power * tcmp

# Soft penalty: noise must be < 500 µV
if sigma_uv > 500:
    obj += 1e3 * (sigma_uv - 500)**2

return obj
```

---

## Threading and File Safety

When `batch_size=1`, TuRBO calls `objective()` one at a time — no concurrency issues.

When `batch_size > 1`, TuRBO may call the objective in parallel. In that case each worker must have isolated simulation state (separate files, separate processes). The simplest safe choice is always `batch_size=1`.

---

## Convergence Plot

Always plot the convergence curve after optimization to verify TuRBO made progress:

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def plot_convergence(fX, out_path, title="TuRBO Convergence"):
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
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
```

**What to look for:**
- Best-so-far must decrease monotonically (by definition)
- Plateau immediately after n_init → bounds too wide, or GP underfitting — tighten bounds
- All evals near 1e6 → simulation is failing — debug a single eval first before running TuRBO
- Scatter clusters near the best region as TuRBO focuses its trust region — healthy sign

---

## Worked Example: Comparator Power × Tcmp

Reference implementation: `optimization/turbo_optimize_comparator.py`

**Circuit:** StrongArm dynamic comparator (45nm PTM HP, 1GHz clock) via the `comparator` skill.
**Objective:** minimize `P_avg_uW × Tcmp_ps`.
**Variables:** 7 transistor widths, bounds [0.5, 10] µm.
**Budget:** 100 evaluations, ~8 min total.
**Result:** 2.71× improvement over default sizing (6032 → 2230 µW·ps).

Key choices made:
1. Simulation cycle count reduced for speed during optimization; restored for final verification
2. `batch_size=1` — simulation module globals are not safe for concurrent mutation
3. Results logged to JSON after every eval — crash-safe
4. Output directory controlled via env var so it lands in the project, not the skill package

---

## File Conventions

```
optimization/
├── turbo_optimize_{circuit}.py     — main optimization script
├── plot_turbo_convergence.py       — standalone convergence plotter (optional)
└── results/
    ├── turbo_results.json          — per-eval log [{eval, params, metrics, obj}]
    ├── turbo_summary.json          — best result + total time
    └── turbo_convergence.png       — convergence figure
```

- Log results to JSON **after every evaluation** — a crash loses at most one eval
- Never write outputs inside `.claude/` — keep skill packages clean

---

## Quick Reference

```python
from turbo import Turbo1, TurboM
import numpy as np

# TuRBO-1 (single trust region — default choice)
turbo = Turbo1(f=objective, lb=LB, ub=UB,
               n_init=2*len(LB), max_evals=100,
               batch_size=1, use_ard=True, device="cpu", dtype="float64")
turbo.optimize()
best = turbo.X[np.argmin(turbo.fX)]

# TuRBO-M (multiple trust regions — larger budget, better coverage)
turbo = TurboM(f=objective, lb=LB, ub=UB,
               n_init=10, max_evals=300, n_trust_regions=5,
               batch_size=1, use_ard=True, device="cpu", dtype="float64")
```
