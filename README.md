# virtuoso-bridge

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=social)](https://github.com/Arcadia-1/virtuoso-bridge-lite)
[![AI Native](https://img.shields.io/badge/AI%20Native-agent--driven%20development-blueviolet)](https://claude.ai/code)

### You describe intent. The agent writes SKILL, builds layouts, and runs simulations.

**Strongly recommended:** use a coding agent (Claude Code, Cursor, etc.) to read this repo and tailor it to your actual project environment — PDK, libraries, tech node, and directory structure.

Lightweight Python bridge for Cadence Virtuoso — **work from anywhere, on any machine**.

Virtuoso runs on a remote Linux server. This bridge lets you control it from your laptop (macOS, Windows, Linux) over SSH. No VNC, no X11 forwarding, no manual terminal sessions.

### Why use this?

- **Remote-first** — SSH tunnel handles all communication. Your code runs locally; SKILL executes on the server.
- **Persistent SSH** — one long-lived connection with automatic reconnection. No repeated `ssh` logins, no dropped sessions.
- **Jump host support** — works through bastion hosts out of the box (`VB_JUMP_HOST`).
- **AI Native** — designed for AI agents to drive. Describe what you want; the agent generates SKILL, builds layouts, runs simulations.

### Three capabilities:

1. **SKILL execution** — send SKILL commands to a running Virtuoso, get results back
2. **Layout & Schematic editing** — Python API for creating/modifying cellviews
3. **Spectre simulation** — run simulations remotely, parse results automatically

> This is a distilled version of virtuoso-bridge (full). Compared to the full version:
>
> - **Removed** Calibre DRC/LVS/PEX wrappers and all associated CSH scripts
> - **Removed** OCEAN script integration
> - **Removed** example netlists, simulation outputs, and Calibre rule decks
> - **Removed** all hardcoded PDK paths, internal usernames, and site-specific server paths from source and git history
> - **Trimmed** docstrings and redundant code (~21% smaller)
>
> What remains is the core: SSH transport, SKILL execution, layout/schematic editing, and Spectre simulation.

## Quick Start

```bash
pip install -e .
virtuoso-bridge init        # generates .env
```

Edit `.env`, fill in one variable:

```dotenv
VB_REMOTE_HOST=my-server    # SSH host alias from ~/.ssh/config
```

Then:

```bash
virtuoso-bridge start
```

```python
from virtuoso_bridge import BridgeClient

client = BridgeClient()
result = client.execute_skill("1+2")
print(result)  # {'ok': True, 'result': {'output': '3', ...}}
```

Done. SSH keys must already work (`ssh my-server` without password prompt).

## What You Can Do

### 1. Execute any SKILL command

```python
client = BridgeClient()

# Query Virtuoso state
client.execute_skill("hiGetCurrentWindow()")
client.execute_skill("geGetEditCellView()")

# Run SKILL expressions
client.execute_skill('println("hello from Python")')

# Load a SKILL file into Virtuoso
client.load_il("path/to/script.il")
```

### 2. Edit layout

```python
with client.layout.edit("myLib", "myCell", mode="a") as layout:
    # Draw shapes
    layout.add_rect("M1", "drawing", (0.0, 0.0, 1.0, 0.5))
    layout.add_path("M2", "drawing", [(0, 0), (1, 0), (1, 1)], width=0.1)
    layout.add_label("M1", "drawing", (0.5, 0.25), "VDD")

    # Place instances
    layout.add_instance("tsmcN28", "nch_ulvt_mac", (0, 0), "I0",
                        params={"w": "200n", "l": "30n", "nf": 4})

    # Place vias
    layout.add_via("M1_M2", (0.5, 0.25))

    # Read back geometry
    shapes = layout.get_shapes()
```

### 3. Edit schematic

```python
with client.schematic.edit("myLib", "myCell") as sch:
    sch.add_instance("analogLib", "vdc", (0, 0), "V0",
                     params={"vdc": "0.9"})
    sch.add_instance("analogLib", "gnd", (0, -0.5), "GND0")
    sch.add_wire([(0, 0), (0, 0.5)])
    sch.add_pin("VDD", "inputOutput", (0, 1.0))
```

### 4. Run Spectre simulation

```python
from virtuoso_bridge.spectre.runner import SpectreSimulator

sim = SpectreSimulator.from_env(work_dir="./output")
result = sim.run_simulation("tb_inv.scs", {})

print(result.status)            # ExecutionStatus.SUCCESS
print(result.data.keys())       # ['time', 'VOUT', 'VIN', ...]
print(result.data["VOUT"][:5])  # first 5 voltage samples
```

Set `VB_CADENCE_CSHRC` in `.env` if `spectre` is not in the default PATH on the remote machine.

### 5. File transfer & shell commands

```python
# Upload a file to the remote machine
client.upload_file("local/netlist.scs", "/tmp/remote/netlist.scs")

# Download a file
client.download_file("/tmp/remote/results.txt", "local/results.txt")

# Run a shell command on the remote machine
client.run_shell_command("ls /tmp/remote/")
```

## How to Configure PDK Paths

You do **not** need to manually look up PDK paths. Instead:

1. Open any testbench in Virtuoso
2. Export the netlist: **Simulation > Netlist > Create**
3. The `.scs` file contains all the info an AI needs:

```spectre
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT

M0 (VOUT VIN VSS VSS) nch_ulvt_mac l=30n w=1u nf=1
```

From this, an AI assistant knows: PDK model paths, device names (`nch_ulvt_mac`), library names, default parameters, and Spectre syntax. No manual configuration needed.

## AI Agent Skills

The `skills/` directory contains context documents for AI agents:

| Skill | File | Covers |
|---|---|---|
| `virtuoso` | `skills/virtuoso/SKILL.md` | Bridge startup, SKILL execution, layout/schematic editing |
| `spectre` | `skills/spectre/SKILL.md` | Netlist preparation, remote simulation, result parsing |

### How to use with Claude Code

Type the slash command before your task:

```
/virtuoso open the layout for cell INV and add a metal1 rectangle
/spectre run a transient simulation on the inverter netlist
```

### How to use with other AI tools

Point the tool at the skill file:

```
Read skills/virtuoso/SKILL.md, then help me create a layout for a 4x8 NMOS array.
```

The skill file tells the AI which APIs to use, what the workflow looks like, and what pitfalls to avoid. Reference documents under `skills/virtuoso/references/` provide SKILL examples, metal rules, and layout conventions.

### Skill reference files

```
skills/virtuoso/
  SKILL.md                              # main skill document
  references/
    layout.md                           # layout editing patterns and SKILL examples
    schematic.md                        # schematic editing patterns
    t28_metal_rules.md                  # metal width/spacing rules (T28 example)
    bindkey_operation_index.md          # Virtuoso bindkey reference
  assets/
    cfmom_unary_cdac_reference.py       # real-world layout example

skills/spectre/
  SKILL.md                              # Spectre simulation workflow
```

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `VB_REMOTE_HOST` | Yes | SSH host running Virtuoso |
| `VB_REMOTE_USER` | No | SSH user (defaults to local username) |
| `VB_JUMP_HOST` | No | Bastion / jump host |
| `VB_CADENCE_CSHRC` | For Spectre | cshrc that sources Cadence tools on the remote machine |

## CLI

```bash
virtuoso-bridge init      # create .env template
virtuoso-bridge start     # start the bridge service
virtuoso-bridge restart   # force-restart
virtuoso-bridge status    # check connection health
```

## Architecture

```
Local Machine (any OS)            Remote Virtuoso Server
──────────────────────            ──────────────────────
Python script                     Virtuoso (running)
    │                                 │
    ▼                                 ▼
BridgeClient ──TCP──► BridgeService  RAMIC daemon (Python 2.7)
                          │              │
                          └──SSH tunnel──┘
                                 │
                              SKILL execution
```

- `BridgeService` runs as a background process, manages the SSH tunnel
- `RAMIC daemon` is a tiny Python 2.7 TCP server uploaded to the Virtuoso host
- SKILL commands are sent as JSON over the tunnel, executed in Virtuoso, results returned
- `SpectreSimulator` uses SSH to upload netlists and run simulations independently

## Build & Test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Citation

If you use virtuoso-bridge in academic work, please cite:

```bibtex
@article{zhang2025virtuosobridge,
  title   = {Virtuoso-Bridge: An Agent-Native Bridge for Harness Engineering
             in Virtuoso-Centered Workflows},
  author  = {Zhang, Zhishuai and Li, Xintian and Sun, Nan and Jie, Lu},
  year    = {2025}
}
```

## Authors

- **Zhishuai Zhang** — Tsinghua University
- **Xintian Li** — Tsinghua University
- **Nan Sun** — Tsinghua University
- **Lu Jie** — Tsinghua University
