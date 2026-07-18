<p align="center">
  <img src="assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://oosmetrics.com/repo/Arcadia-1/virtuoso-bridge-lite"><img src="https://api.oosmetrics.com/api/v1/badge/achievement/8d369c0f-7036-4e79-9ed3-a71689ba4660.svg" alt="oosmetrics — Top 5 in Fullstack by acceleration (2026-05-09)"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/stargazers"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542&logo=github&v=20260523" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/network/members"><img src="https://img.shields.io/github/forks/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542" alt="GitHub forks"/></a>
  <a href="stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fclones-badge.json&style=flat-square&v=2" alt="Clones"/></a>
  <a href="stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fviews-badge.json&style=flat-square&v=2" alt="Views"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues"><img src="https://img.shields.io/github/issues/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Open Issues"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/commits/main"><img src="https://img.shields.io/github/last-commit/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Last Commit"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="Website"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"/></a>
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

## Choose your setup

| You want to... | Use this path | Needs |
|---|---|---|
| Drive Virtuoso on a remote EDA server | Remote mode | SSH access, running Virtuoso, `load(...)` in CIW |
| Drive Virtuoso on the same machine | Local mode | Running Virtuoso, `VB_REMOTE_HOST=localhost` |
| Run Spectre from netlists | Spectre simulator | `spectre` on PATH, or `VB_CADENCE_CSHRC` |
| Run reproducible IC optimization workflows | Optimizer skill + optional external workflow CLI | Spectre/OCEAN setup, requirement files |
| Let a coding agent operate Cadence | Agent skills | Link `skills/` into your agent's skill directory |

Virtuoso SKILL execution and Spectre simulation are independent. You can run
Spectre without the SKILL bridge, and you can use the SKILL bridge without
Spectre.

### Python environment selection

Python entry points discover the nearest parent `.env` containing
`VB_REMOTE_HOST` or `VB_LOCAL_PORT`, then load it with `override=True`; this can
change a long-lived process from local to remote mode. Pin the intended file
before constructing a client when embedding the bridge:

```python
from virtuoso_bridge.env import set_runtime_env_file

set_runtime_env_file("/path/to/virtuoso-bridge.env")
```

## Quick Start

```bash
# 0. Get the source
git clone https://github.com/Arcadia-1/virtuoso-bridge-lite.git
cd virtuoso-bridge-lite

# 1. Install in a virtual environment
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. Create ~/.virtuoso-bridge/.env
virtuoso-bridge init user@host [-J user@jump-host]
# Or: virtuoso-bridge init      # empty template; edit VB_REMOTE_HOST yourself

# 3. Start and verify
virtuoso-bridge start          # starts tunnel and prints the CIW load(...) line
virtuoso-bridge status         # tunnel + Virtuoso daemon + Spectre availability
```

On Windows PowerShell, replace the activation line with
`.\.venv\Scripts\Activate.ps1`.

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

Useful first commands after the bridge is up:

```bash
virtuoso-bridge windows       # list all open Virtuoso windows
virtuoso-bridge screenshot    # screenshot CIW to the user artifact directory
virtuoso-bridge export-visio MyLib MyCell -o MyCell.vsdx  # Windows + Visio
```

…or skip Python entirely — run SKILL straight from the shell:

```bash
# One-liner — full VirtuosoResult JSON on stdout
virtuoso-bridge eval 'getCurrentTime()'

# Multi-line SKILL via heredoc (auto-wrapped in progn; returns the last form)
virtuoso-bridge eval --stdin <<'EOF'
let((libs)
  libs = mapcar(lambda((l) l~>name) ddGetLibList())
  printf("found %d libraries\n" length(libs))
  libs)
EOF

# Whole .il file — uploaded automatically in SSH mode
virtuoso-bridge load my_script.il
```

For detailed setup (jump hosts, multi-profile, local mode), see [`AGENTS.md`](AGENTS.md).

## CLI reference

All commands take `-p PROFILE` / `--env PATH` to pick a non-default config; run `virtuoso-bridge <cmd> --help` for full flags.

| Command | What it does |
|---|---|
| **Tunnel / lifecycle** | |
| `init [user@host] [-J jump]` | Write a starter `.env` (no args = empty template) |
| `start [--bind-venv]` | Start SSH tunnel + deploy daemon; `--bind-venv` (with `-p X`) also binds the active virtualenv to profile `X` |
| `stop` | Stop the SSH tunnel |
| `restart` | Restart tunnel + daemon |
| `status` | Tunnel + daemon health + Spectre availability |
| `license` | Check Spectre license availability |
| **Profile binding** | |
| `profile show` | Print the resolved profile, its source, and the active venv binding path |
| `profile bind PROFILE --venv` | Pin the active virtualenv to `PROFILE` (naked `from_env()` calls in that venv resolve to it) |
| `profile clear --venv` | Remove the venv binding |
| **SKILL execution** | |
| `load FILE.il` | Run a `.il` file in Virtuoso (uploads it in SSH mode). VS Code task–friendly; outputs `VirtuosoResult` JSON |
| `eval 'EXPR'` / `eval --stdin` | Run an inline SKILL expression; supports multi-statement via auto-wrapped `progn(...)` |
| **Interaction / diagnostics** | |
| `windows` | List all open Virtuoso windows (number + name) |
| `screenshot [ciw\|current\|N] [-o DIR\|FILE]` | Capture a window; defaults to the user artifact screenshots directory |
| `dismiss-dialog` | X11 path: find and dismiss blocking GUI dialogs (saves you when SKILL channel deadlocks on a modal) |
| `list-windows [--json]` | X11 path: enumerate Virtuoso-related windows, including frame/child IDs and suggested modal actions |
| `dismiss-window WINDOW_ID [--action enter\|escape\|alt-y\|alt-n]` | X11 path: send an explicit action to one window ID returned by `list-windows` |
| `snapshot [-o DIR] [--history H]` | Dump the focused Virtuoso window (maestro/schematic/...) — brief by default, full disk dump with `-o` |
| **Export** | |
| `export-visio LIB CELL -o OUT.vsdx` | Render a Virtuoso schematic to Microsoft Visio (Windows + pywin32) |
| **SKILL Finder** | |
| `skill-find <query>` | Search SKILL functions by name (fuzzy/prefix/suffix/exact/regex) |
| `skill-info <fn>` | Get detailed More Info docs for a SKILL function |

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

> Want to understand the raw mechanism? Start with [`src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il`](src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il) and [`src/virtuoso_bridge/virtuoso/basic/bridge.py`](src/virtuoso_bridge/virtuoso/basic/bridge.py).

> Want to use Virtuoso locally without SSH? See [Local mode](AGENTS.md#local-mode) in AGENTS.md.

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
<summary><b>virtuoso-bridge-lite</b> — <code>src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il</code></summary>

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

## Star History

<a href="https://star-history.com/#Arcadia-1/virtuoso-bridge-lite&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date&theme=dark"/>
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
  </picture>
</a>
