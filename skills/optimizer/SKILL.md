---
name: optimizer
description: "Black-box optimization of design parameters using TuRBO or scipy. TRIGGER when the user wants to optimize, tune, size, sweep, or explore a design space to meet specs. This includes circuit sizing (W/L, bias, passives), finding optimal operating points, minimizing power-delay or noise-power tradeoffs, or any task where multiple parameters need to be searched to hit a target. Also trigger when the user says things like 'find the best sizing', 'help me tune this', 'run an optimization', 'what values give me the best FOM', or 'sweep these parameters to meet spec'. Do NOT trigger for single-variable parametric sweeps or analytical calculations."
---

# Optimizer

## What this is

A black-box optimization framework. You give it:
- A set of **parameters** with bounds (e.g. transistor widths, bias currents, resistor values)
- An **evaluation function** that takes parameters and returns performance metrics
- An **objective** that combines metrics into a single scalar to minimize

The optimizer iteratively picks parameter values, evaluates them, and converges toward the optimum. It treats the evaluation as a black box — it doesn't need to know whether you're running Spectre, Maestro, a Python model, or anything else.

## When to use

- **Circuit sizing** — find W/L, bias currents, passive values that meet gain/BW/noise/power specs
- **Design space exploration** — sweep a high-dimensional parameter space that's too large for manual tuning or parametric sweeps
- **Multi-objective tradeoffs** — minimize power-delay product, noise-power FOM, etc.
- **Any expensive black-box function** — the evaluation can be slow (seconds to minutes per point); TuRBO is sample-efficient

## When NOT to use

- **Single-variable sweep** — just use a parametric sweep in Maestro or a for-loop
- **Analytical solution exists** — if you can derive the optimum, don't search for it
- **< 5 evaluations budget** — TuRBO needs at least `2 * n_params` initial samples

## Algorithm choice

| Situation | Algorithm | Why |
|-----------|-----------|-----|
| ≤ 3 params, smooth | `scipy.optimize.minimize` | Fast, no GP overhead |
| 3–20 params, noisy/expensive | TuRBO (`turbo.Turbo1`) | Sample-efficient Bayesian optimization with trust regions |
| > 20 params | Consider random search + refinement | GP doesn't scale well beyond ~20D |

## Prerequisites

- For TuRBO: `pip install torch gpytorch` and local TuRBO install (`pip install -e TuRBO/`)
  - TuRBO (Trust Region Bayesian Optimization) comes from [uber-research/TuRBO](https://github.com/uber-research/TuRBO)
- For scipy: included in standard Python scientific stack

## Core pattern

```python
import numpy as np
from turbo import Turbo1

# 1. Define parameters and bounds
PARAMS = ["W_tail", "W_inp", "R_load"]
LB = np.array([0.5, 0.5, 100.])
UB = np.array([10., 10., 5000.])

# 2. Objective: params → scalar (minimize)
def objective(x):
    try:
        result = evaluate(x, PARAMS)
    except Exception:
        return 1e6                     # penalty on failure, never nan/inf
    return compute_metric(result)

# 3. Run
turbo = Turbo1(f=objective, lb=LB, ub=UB,
               n_init=2*len(LB), max_evals=100, batch_size=1)
turbo.optimize()
best = turbo.X[turbo.fX.argmin()]
```

## Evaluation backends

The `evaluate()` function is the only part that changes between use cases:

**Spectre netlist** — parameterize a `.scs` template and run remotely:
```python
from virtuoso_bridge.spectre.runner import SpectreSimulator
sim = SpectreSimulator.from_env(work_dir="./opt", output_format="psfascii")

def evaluate(x, params):
    text = Path("tb_template.scs").read_text()
    for name, val in zip(params, x):
        text = text.replace(f"@@{name}@@", f"{val:.6g}")
    Path("opt/tb.scs").write_text(text)
    return sim.run_simulation(Path("opt/tb.scs"), {})
```

**Maestro** — set design variables and run an existing test via SKILL:
```python
from virtuoso_bridge import ramic_send

def evaluate(x, params):
    for name, val in zip(params, x):
        ramic_send(f'maeSetDesignVar("{name}" {val:.6g})')
    ramic_send('maeRunTest("myTest")')
    gain = float(ramic_send('maeGetTestResult("myTest" "gain_db")'))
    bw = float(ramic_send('maeGetTestResult("myTest" "bw_hz")'))
    return {"gain": gain, "bw": bw}
```

**Any Python callable**:
```python
def evaluate(x, params):
    return my_model.predict(dict(zip(params, x)))
```

## Objective design

Return a **scalar float**. Return `1e6` on failure — never `nan` or `inf`, because these break the GP surrogate model and cause the optimizer to diverge.

| Goal | Return |
|------|--------|
| Min power-delay | `power * delay` |
| Max gain-bandwidth | `-(gain_db + 20*log10(bw))` |
| With constraint | `obj + 1e3 * max(0, noise - spec)**2` |

## Related skills

- **spectre** — Spectre simulation runner, netlist syntax, result parsing
- **virtuoso** — Maestro setup, schematic editing, design variable management
