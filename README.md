<p align="center">
  <img src="assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/stargazers"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542&logo=github" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/network/members"><img src="https://img.shields.io/github/forks/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542" alt="GitHub forks"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues"><img src="https://img.shields.io/github/issues/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Open Issues"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/commits/main"><img src="https://img.shields.io/github/last-commit/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Last Commit"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="Website"/></a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"/></a>
  <img src="https://img.shields.io/badge/AI%20Native-agent--driven-blueviolet" alt="AI Native"/>
</p>

A new infrastructure for **Agentic Analog and Mixed-Signal Design**. LLM Agents drive Cadence Virtuoso instances — locally or remotely — turning tedious handcrafting into automated design flows.

### Why is this a "New Infrastructure"?

**1. Deep Virtuoso Integration** — Control across Schematic, Layout, Maestro, and Spectre.
- **Flexible programming**: execute inline SKILL, load `.il` files, or use Python APIs
- **Four design domains**: schematic editing, layout generation, simulation setup (Maestro), and standalone Spectre with PSF parsing

**2. Scalable Architecture** — Multi-server, multi-session, built for distributed design clusters.
- Multi-profile SSH: connect to N design servers, each with independent tunnel
- Run parallel simulations across servers and accounts
- Verified across macOS, Windows, and Linux

**3. AI-Native Design** — Built for coding agents (Claude Code, Cursor, etc.) to drive Virtuoso.
- CLI-first: `virtuoso-bridge start/status/restart`, no GUI needed
- Ships with pre-defined agent skill files (`skills/`) — the agent knows how to use the bridge immediately
- Optimized for high-frequency agent interactions with persistent SSH tunnels

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

## Quick Start

```bash
pip install -e .              # install
virtuoso-bridge init user@host [-J user@jump-host]   # write ~/.virtuoso-bridge/.env in one shot
                                                     # (no args: empty template — edit it yourself)
virtuoso-bridge start         # start SSH tunnel
virtuoso-bridge status        # verify connection
virtuoso-bridge windows       # list all open Virtuoso windows
virtuoso-bridge screenshot    # screenshot CIW (or: current, N)
```

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

For detailed setup (jump hosts, multi-profile, local mode), see [`AGENTS.md`](AGENTS.md).

## Snapshot a maestro run

Pull the currently-focused maestro session's setup + latest-run artifacts to a local folder:

```bash
virtuoso-bridge snapshot -o output                       # auto-picks newest history
virtuoso-bridge snapshot -o output --history Interactive.160   # pin a specific history
```

Output tree (one example):

```
output/20260422_142137__MyLib__myTB/
├── maestro.sdb, active.state                    # raw Cadence files
├── state_from_sdb.xml, state_from_active_state.xml  # filtered, high-signal
├── state_from_skill.txt                         # SKILL-probe setup summary
└── Interactive.N/
    ├── Interactive.N.{log,rdb,msg.db}           # run-level (rdb = SQLite)
    └── <pt>/<tb>/
        ├── netlist/   → netlist, input.scs, qpInformation.ils, paramInfo.ils
        └── psf/       → spectre.out, logFile, dcOp.dc, *.ac, *.tran, ...
```

Per-point `netlist/` keeps only the 4 files that actually describe the design (main SPICE netlist, testbench top level, FOM definitions, corner label). Psf keeps stdout + logs + non-binary analysis results. The full rule set — including what's commented out and why — lives in [`src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml`](src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml); edit the YAML (uncomment / comment lines) to add or drop files, no code change needed. Binary waveforms (`*.raw`, `wavedb/`) are never pulled — read them through `reader.runs.read_results` instead.

## Exposing skills to your coding agent

The `skills/` directory ships [Claude Code](https://claude.com/claude-code) skills
(`virtuoso`, `spectre`, `optimizer`). They are **not** symlinked into the repo's
`.claude/skills/` on purpose — repo-tracked symlinks break on Windows and hardcode
one user's absolute paths. Instead, each user links them into their own
`~/.claude/skills/` once after cloning:

```bash
# macOS / Linux
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/virtuoso"  ~/.claude/skills/virtuoso
ln -s "$(pwd)/skills/spectre"   ~/.claude/skills/spectre
ln -s "$(pwd)/skills/optimizer" ~/.claude/skills/optimizer
```

```powershell
# Windows (PowerShell, Developer Mode or elevated shell)
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\virtuoso"  -Target "$PWD\skills\virtuoso"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\spectre"   -Target "$PWD\skills\spectre"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\optimizer" -Target "$PWD\skills\optimizer"
```

Cursor and other agents that load skills from a user-level directory follow the
same pattern — point their skills path at `skills/` in this repo.

## Architecture

<p align="center">
  <img src="assets/arch.png" alt="Architecture" width="100%"/>
</p>

- **VirtuosoClient** — pure TCP SKILL client. Sends SKILL as JSON, gets results. No SSH awareness.
- **SpectreSimulator** — runs Spectre simulations remotely via SSH shell commands, transfers netlists and results via rsync.
- **SSHClient** — maintains a persistent ControlMaster connection that multiplexes three channels: TCP port-forwarding (SKILL execution via the daemon), SSH shell commands (Spectre invocation), and rsync file transfer. Optional — bypassed in local mode.

Fully decoupled: VirtuosoClient works with any TCP endpoint — SSH tunnel, VPN, direct LAN, or local. Multiple connection profiles are supported, each managing an independent tunnel to a separate design server.

> Want to understand the raw mechanism? See [`core/`](core/) — the entire bridge distilled into 3 files (180 lines).

> Want to use Virtuoso locally without SSH? See [Local mode](AGENTS.md#local-mode) in AGENTS.md.

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
