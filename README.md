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

**1. Three ways to program Virtuoso** — from raw SKILL to Pythonic APIs, your choice.
- **Load entire `.il` files**: hot-load complex SKILL scripts into Virtuoso with one call, then invoke their functions from Python
- **Execute any SKILL expression**: `client.execute_skill('dbOpenCellViewByType(...)')` for full Virtuoso API access
- **Python APIs**: high-level wrappers for layout, schematic, and Spectre simulation when you don't want to write SKILL

**2. AI-native design** — Built for coding agents (Claude Code, Cursor, etc.) to drive.
- CLI-first: agents control the bridge via `virtuoso-bridge start/status/restart`, no GUI needed
- Ships with agent skill files (`skills/`) so the agent knows how to use the bridge immediately
- Persistent SSH tunnel stays alive across calls for high-frequency agent interactions
- All commands logged for full traceability

**3. Batteries included** — 30+ runnable examples, ready to use out of the box.
- Layout: polygon, via, multi-layer routing, bus wiring, read-back geometry
- Schematic: create RC circuits, read connectivity, export CDL netlist
- Spectre: transient, DC+AC frequency response, PSS+Pnoise (StrongArm comparator), veriloga

> **If you are an AI agent**, read [`AGENTS.md`](AGENTS.md) first and follow its setup checklist.

## Comparison with skillbridge

| Feature | virtuoso-bridge-lite | [skillbridge](https://github.com/unihd-cag/skillbridge) |
|---|---|---|
| **Remote execution** | SSH tunnel, jump host, auto-reconnect | Local only |
| **Load .il files** | `client.load_il("script.il")` | Not supported |
| **Raw SKILL execution** | `client.execute_skill(...)` | `ws.db.open_cell_view_by_type(...)` |
| **Layout/schematic API** | `client.layout.edit()` context manager | Raw SKILL calls only |
| **Spectre simulation** | Built-in runner + PSF parser | Not supported |
| **AI agent support** | Skill files, CLI-first, command logging | Not designed for agents |
| **Python ↔ SKILL types** | String-based | Automatic bidirectional mapping |
| **IDE tab completion** | No (agents don't need it) | Yes (Jupyter, PyCharm stubs) |
| **Local mode** | Yes | Yes |

**In short:** skillbridge is a transparent SKILL RPC client optimized for interactive local use. virtuoso-bridge-lite is a full harness for remote automation, AI-driven workflows, and simulation.

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
