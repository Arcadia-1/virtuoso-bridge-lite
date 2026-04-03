---
name: optimizer
description: "Optimize circuit parameters by looping Spectre simulations. Default algorithm: TuRBO."
---

# Circuit Optimizer Skill

Optimize analog circuit parameters (transistor sizing, biasing, etc.) through automated Spectre simulation loops.

## Pattern

```python
import numpy as np
from virtuoso_bridge.spectre.runner import SpectreSimulator

sim = SpectreSimulator.from_env(work_dir="./opt_output", output_format="psfascii")

# 1. Define parameters and bounds
PARAMS = ["W_tail", "W_inp", "W_lat"]
LB = np.array([0.5, 0.5, 0.5])
UB = np.array([10., 10., 6.])

# 2. Objective function: run Spectre, extract metric, return scalar
def objective(x):
    netlist = generate_netlist(x, PARAMS)  # write params into .scs template
    result = sim.run_simulation(netlist, {})
    if not result.ok:
        return 1e6  # penalty for failed simulations
    power = extract_power(result)
    delay = extract_delay(result)
    return power * delay  # minimize power-delay product

# 3. Run optimizer
from turbo import Turbo1
turbo = Turbo1(f=objective, lb=LB, ub=UB,
               n_init=2*len(LB), max_evals=100, batch_size=1)
turbo.optimize()

# 4. Best result
best_idx = turbo.fX.argmin()
for name, val in zip(PARAMS, turbo.X[best_idx]):
    print(f"  {name} = {val:.3f}")
```

## Netlist parameterization

Use `@@PARAM@@` placeholders in a template netlist:

```python
def generate_netlist(x, param_names):
    template = Path("tb_template.scs").read_text()
    for name, val in zip(param_names, x):
        template = template.replace(f"@@{name}@@", f"{val:.6g}")
    out = Path("opt_output/tb_run.scs")
    out.write_text(template)
    return out
```

## Common objectives

| Goal | Return value |
|---|---|
| Power-delay product | `power * delay` |
| Noise-power FOM | `power * noise**2` |
| Maximize gain-BW | `-(gain_db + 20*log10(bw))` |
| With constraint | `obj + 1e3 * max(0, noise - 500e-6)**2` |

Always return a scalar float. Return `1e6` on failure, never `nan`/`inf`.

## Prerequisites

```bash
pip install torch gpytorch
pip install -e TuRBO/    # local TuRBO repo
```

Or use `scipy.optimize.minimize` for simpler cases — no GP overhead.
