<p align="center">
  <img src="assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=social" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite"><img src="https://img.shields.io/github/downloads/Arcadia-1/virtuoso-bridge-lite/total?color=brightgreen" alt="Downloads"/></a>
  <a href="https://claude.ai/code"><img src="https://img.shields.io/badge/AI%20Native-agent--driven%20development-blueviolet" alt="AI Native"/></a>
</p> 

Control Cadence Virtuoso from anywhere, locally or remotely. Verified across macOS, Windows, and Linux.

### Why use this?

**1. Dual-level programmability** — Write raw SKILL or use Pythonic wrappers, your choice.
- **Direct SKILL injection**: hot-load `.il` files on the fly, execute any SKILL expression
- **Python APIs**: high-level functions for basic operations in layout, schematic, and simulation

**2. AI-native design** — Built for coding agents (Claude Code, Cursor, etc.) to drive.
- You describe intent; the agent writes SKILL, builds layouts, runs simulations, optimizes parameters
- Ships with agent skill files (`skills/`) that teach the agent how to use the bridge

**3. Batteries included** — 30+ runnable examples, ready to use out of the box.
- Layout: polygon, via, multi-layer routing, bus wiring, read-back geometry
- Schematic: create RC circuits, read connectivity, export CDL netlist
- Spectre: transient, DC+AC frequency response, PSS+Pnoise (StrongArm comparator)

> **If you are an AI agent**, read [`AGENTS.md`](AGENTS.md) first and follow its setup checklist.

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
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()
result = client.execute_skill("1+2")
print(result)  # VirtuosoResult(status=SUCCESS, output='3')
```

Done.

### Prerequisites

1. **SSH**: `ssh my-server` must work in your terminal without a password prompt.
2. **Virtuoso**: a Virtuoso process must be running on the remote (or local) machine.

## Architecture

<p align="center">
  <img src="assets/arch.png" alt="Architecture" width="100%"/>
</p>

- **VirtuosoClient** — pure TCP SKILL client. Sends SKILL, gets results. No SSH awareness.
- **SSHClient** — manages SSH tunnel + daemon deployment. Provides the `localhost:port` that VirtuosoClient connects to. Optional (not needed for local mode).

Fully decoupled: VirtuosoClient works with any TCP endpoint — SSH tunnel, VPN, direct LAN, or local.

> Want to understand the raw mechanism? See [`core/`](core/) — the entire bridge distilled into 3 files (180 lines).

### Local mode (no SSH)

```python
from virtuoso_bridge import VirtuosoClient

bridge = VirtuosoClient.local(port=65432)
bridge.execute_skill("1+2")
```

No tunnel, no `.env`, no SSH. Just load `core/ramic_bridge.il` in Virtuoso CIW and connect.

## CLI

```bash
virtuoso-bridge init      # create .env template
virtuoso-bridge start     # start SSH tunnel + deploy daemon
virtuoso-bridge restart   # force-restart
virtuoso-bridge status    # check connection + Spectre license
```

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
             in Analog and Mixed-Signal Workflows},
  author  = {Zhang, Zhishuai and Li, Xintian and Sun, Nan and Jie, Lu},
  year    = {2025}
}
```

## Authors

- **Zhishuai Zhang** — Tsinghua University
- **Xintian Li** — Tsinghua University
- **Nan Sun** — Tsinghua University
- **Lu Jie** — Tsinghua University
