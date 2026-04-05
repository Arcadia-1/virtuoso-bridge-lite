<p align="center">
  <img src="assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=social" alt="GitHub stars"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="Website"/></a>
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
- Schematic: create circuits, read connectivity, import CDL via spiceIn, export Spectre netlist
- ADE Assembler (Maestro): create tests, AC/tran analysis, parametric sweep, bandwidth spec, display results
- Spectre: transient, DC+AC frequency response, PSS+Pnoise (StrongArm comparator), veriloga

> **If you are an AI agent**, read [`AGENTS.md`](AGENTS.md) first and follow its setup checklist.

## Comparison with skillbridge

| Feature | virtuoso-bridge-lite | [skillbridge](https://github.com/unihd-cag/skillbridge) |
|---|---|---|
| **Core mechanism** | `ipcBeginProcess` + `evalstring` | `ipcBeginProcess` + `evalstring` |
| **Local mode** | Yes | Yes |
| **Remote execution** | SSH tunnel, jump host, auto-reconnect | Not supported |
| **Calling style** | String-based: `execute_skill("dbOpenCellViewByType(...)")` | Pythonic mapping: `ws.db.open_cell_view_by_type(...)` |
| **Load .il files** | `client.load_il()` | Not supported |
| **Layout / schematic API** | `client.layout.edit()` context manager | Raw SKILL only |
| **Spectre simulation** | Built-in runner + PSF parser | Not supported |
| **AI agent support** | Skill files, CLI-first, command logging | Not designed for agents |
| **Python ↔ SKILL types** | String-based | Auto bidirectional mapping |
| **IDE tab completion** | No (not needed by agents) | Yes (Jupyter, PyCharm stubs) |

**In short:** Both projects are built on the same Cadence SKILL IPC facility, using the same core mechanism: `ipcBeginProcess` + `evalstring` + `ipcWriteProcess`. Here are the core lines from each:

<details>
<summary><b>virtuoso-bridge-lite</b> — <code>core/ramic_bridge.il</code></summary>

```skill
RBIpc = ipcBeginProcess(
  sprintf(nil "%s %L %L %L" RBPython RBDPath host RBPort)
  "" 'RBIpcDataHandler 'RBIpcErrHandler 'RBIpcFinishHandler "")

procedure(RBIpcDataHandler(ipcId data)
  if(errset(result = evalstring(data)) then
    ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 2 result 30))
  else
    ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 21 errset.errset 30))
  )
)
```
</details>

<details>
<summary><b>skillbridge</b> — <code>skillbridge/server/python_server.il</code></summary>

```skill
pyStartServer.ipc = ipcBeginProcess(
  executableWithArgs "" '__pyOnData '__pyOnError '__pyOnFinish pyStartServer.logName)

defun(__pyOnData (id data)
  foreach(line parseString(data "\n")
    capturedWarning = __pyCaptureWarnings(errset(result=evalstring(line)))
    ipcWriteProcess(id lsprintf("success %L\n" result))
  )
)
```
</details>

The divergence is in what's built on top: skillbridge stays thin — a Pythonic RPC client for interactive local use. virtuoso-bridge-lite adds SSH remote access, high-level layout/schematic APIs, Spectre simulation, and an AI-agent-ready harness.

## Getting Started

### Prerequisites

1. **SSH**: `ssh my-server` must work in your terminal without a password prompt.
2. **Virtuoso** (for SKILL execution): a Virtuoso process must be running on the remote (or local) machine. After starting the tunnel, load the bridge SKILL script in the CIW.
3. **Spectre** (for simulation only): `spectre` must be on PATH in the remote shell, or set `VB_CADENCE_CSHRC` to a cshrc that adds Cadence tools to PATH.

> Virtuoso and Spectre are **independent** — you can run Spectre simulations without loading the SKILL bridge in Virtuoso, and you can use the SKILL bridge without Spectre installed. `virtuoso-bridge status` reports both.

### Step-by-step setup

**1. Install**

```bash
pip install -e .
```

**2. Generate config**

```bash
virtuoso-bridge init        # creates .env template in current directory
```

**3. Edit `.env`**

Open the generated `.env` and fill in your connection details:

```dotenv
VB_REMOTE_HOST=my-server              # SSH host alias from ~/.ssh/config
VB_REMOTE_USER=username               # SSH username on the remote
VB_REMOTE_PORT=65081                  # port for the bridge daemon on remote
VB_LOCAL_PORT=65082                   # local port forwarded via SSH tunnel

# Optional — only needed if `spectre` is not already on PATH in the remote shell.
# VB_CADENCE_CSHRC=/path/to/.cshrc   # cshrc that sets up Cadence tools on the remote
```

**4. Start the bridge**

```bash
virtuoso-bridge start
```

**5. Load SKILL in Virtuoso CIW**

On the remote machine, in the Virtuoso CIW (Command Interpreter Window), load the bridge SKILL file:

```
load("/path/to/virtuoso-bridge-lite/core/ramic_bridge.il")
```

**6. Verify**

```bash
virtuoso-bridge status      # checks SSH tunnel, remote host, Spectre license
```

**7. Connect from Python**

```python
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()
result = client.execute_skill("1+2")
print(result)  # VirtuosoResult(status=SUCCESS, output='3')
```

Done. For full API reference, see the [documentation site](https://virtuoso-bridge.tokenzhang.com).

### Jump host setup

If you access Virtuoso through a bastion/jump host, set both hosts in `.env`:

```dotenv
VB_REMOTE_HOST=compute-host   # the machine running Virtuoso (NOT the jump host)
VB_JUMP_HOST=jump-host        # the bastion you SSH through
```

Common mistake: setting `VB_REMOTE_HOST` to the jump host. `VB_REMOTE_HOST` must be the machine where Virtuoso is actually running. Verify with `virtuoso-bridge status` — it checks the remote hostname matches.

### Multi-profile setup

To connect to multiple Virtuoso instances simultaneously, use the `-p` flag. Profile names are **case-sensitive** and appended as suffixes to env var names.

Add profile-suffixed variables to your `.env`:

```dotenv
# Default (no profile)
VB_REMOTE_HOST=server-a
VB_REMOTE_USER=user1

# Profile "worker1" — used with `-p worker1`
VB_REMOTE_HOST_worker1=server-b
VB_REMOTE_USER_worker1=user2
VB_CADENCE_CSHRC_worker1=/path/to/.cshrc.worker1

# Profile "worker2" — used with `-p worker2`
VB_REMOTE_HOST_worker2=server-c
VB_REMOTE_USER_worker2=user3
VB_CADENCE_CSHRC_worker2=/path/to/.cshrc.worker2
```

Then start and use each profile independently:

```bash
virtuoso-bridge start -p worker1
virtuoso-bridge start -p worker2
virtuoso-bridge status -p worker1
```

```python
from virtuoso_bridge.spectre import SpectreSimulator

sim = SpectreSimulator.from_env(profile="worker1")
```

> **Note:** Profile suffixes are case-sensitive. `-p worker1` reads `VB_REMOTE_HOST_worker1`, not `VB_REMOTE_HOST_WORKER1`.

## Architecture

<p align="center">
  <img src="assets/arch.png" alt="Architecture" width="100%"/>
</p>

- **VirtuosoClient** — pure TCP SKILL client. Sends SKILL as JSON, gets results. No SSH awareness.
- **SpectreSimulator** — runs Spectre simulations remotely via SSH shell commands, transfers netlists and results via rsync.
- **SSHClient** — maintains a persistent ControlMaster connection that multiplexes three channels: TCP port-forwarding (SKILL execution via the daemon), SSH shell commands (Spectre invocation), and rsync file transfer. Optional — bypassed in local mode.

Fully decoupled: VirtuosoClient works with any TCP endpoint — SSH tunnel, VPN, direct LAN, or local. Multiple connection profiles are supported, each managing an independent tunnel to a separate design server.

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
virtuoso-bridge status    # check tunnel + Virtuoso daemon + Spectre
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
  title   = {Virtuoso-Bridge: An Agent-Native Bridge for Remote Analog and Mixed-Signal Design Automation},
  author  = {Zhang, Zhishuai and Li, Xintian and Sun, Nan and Jie, Lu},
  year    = {2025}
}
```

## Authors

- **Zhishuai Zhang** — Tsinghua University
- **Xintian Li** — Tsinghua University
- **Nan Sun** — Tsinghua University
- **Lu Jie** — Tsinghua University
