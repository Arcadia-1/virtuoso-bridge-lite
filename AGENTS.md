# AGENTS.md — AI Agent Guide for virtuoso-bridge-lite

Control Cadence Virtuoso via Python — remotely over SSH or locally on the same machine.

## Two modes

| Mode | When | Setup |
|---|---|---|
| **Remote** | Virtuoso on a server, you work locally | Set `VB_REMOTE_HOST` in `.env`, run `virtuoso-bridge start` |
| **Local** | Virtuoso on your own machine | Load `core/ramic_bridge.il` in CIW, use `VirtuosoClient.local()` |

## First-time setup check

When a user first opens this project, run these checks **before anything else**:

### Remote mode

1. **Check `.env`** — does it exist and have `VB_REMOTE_HOST` set?
   - If not: `pip install -e .` then `virtuoso-bridge init`, ask the user to fill in their SSH host.

2. **Check SSH** — `ssh <VB_REMOTE_HOST> echo ok`
   - If this fails: tell the user to fix SSH first. The bridge assumes `ssh <host>` already works.

3. **Check Virtuoso** — `ssh <VB_REMOTE_HOST> "pgrep -f virtuoso"`
   - If no process: tell the user to start Virtuoso first.

4. **Start bridge** — `virtuoso-bridge start`
   - If "degraded": tell the user to paste the `load("...")` command in Virtuoso CIW.

5. **Verify** — `virtuoso-bridge status`

6. **Quick test** — `VirtuosoClient.from_env().execute_skill("1+2")`

### Local mode

1. **Check Virtuoso is running locally**

2. **Load daemon** — in Virtuoso CIW: `load("/path/to/core/ramic_bridge.il")`

3. **Connect** —
   ```python
   from virtuoso_bridge import VirtuosoClient
   bridge = VirtuosoClient.local(port=65432)
   bridge.execute_skill("1+2")
   ```

## Architecture

Two decoupled layers:

- **VirtuosoClient** — pure TCP SKILL client. No SSH. Works with any `localhost:port` endpoint.
- **SSHClient** — manages SSH tunnel + remote daemon deployment. Optional.

```python
# Remote: SSHClient creates the TCP path
from virtuoso_bridge import SSHClient, VirtuosoClient
tunnel = SSHClient.from_env()
tunnel.warm()
bridge = VirtuosoClient.from_tunnel(tunnel)

# Local: no tunnel needed
bridge = VirtuosoClient.local(port=65432)

# Either way, same API:
bridge.execute_skill("1+2")
```

## Key conventions

- All SKILL execution goes through `VirtuosoClient`. Never SSH and run SKILL manually.
- Layout/schematic editing: `client.layout.edit()` / `client.schematic.edit()` context managers.
- Spectre simulation: `SpectreSimulator.from_env()`. Requires `VB_CADENCE_CSHRC` in `.env`.
- `core/` is for understanding the mechanism (3 files, 180 lines). Use the installed package for real work.

## How to configure PDK paths

Export a netlist from Virtuoso (**Simulation > Netlist > Create**). The `.scs` file contains everything:

```spectre
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT
M0 (VOUT VIN VSS VSS) nch_ulvt_mac l=30n w=1u nf=1
```

## Skills

| Skill | File | Covers |
|---|---|---|
| `virtuoso` | `skills/virtuoso/SKILL.md` | SKILL execution, layout/schematic editing |
| `spectre` | `skills/spectre/SKILL.md` | Simulation, result parsing |
| `optimizer` | `skills/optimizer/SKILL.md` | Parameter optimization loops |

```
skills/virtuoso/
  SKILL.md
  references/
    layout.md       # layout API reference
    schematic.md    # schematic API reference

skills/spectre/
  SKILL.md          # simulation workflow + result parsing
```
