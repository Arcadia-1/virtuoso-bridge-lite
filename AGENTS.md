# AGENTS.md — AI Agent Guide for virtuoso-bridge-lite

Control Cadence Virtuoso via Python — remotely over SSH or locally on the same machine.

## Two modes

| Mode | When | Setup |
|---|---|---|
| **Remote** | Virtuoso on a server, you work locally | Set `VB_REMOTE_HOST` in `.env`, run `virtuoso-bridge start` |
| **Local** | Virtuoso on your own machine | Load `core/ramic_bridge.il` in CIW, use `VirtuosoClient.local()` |

## Prerequisites

1. **SSH**: `ssh my-server` must work without a password prompt.
2. **Virtuoso** (for SKILL execution): a running Virtuoso process on the remote (or local) machine.
3. **Spectre** (for simulation only): `spectre` on PATH, or set `VB_CADENCE_CSHRC` to a cshrc that adds Cadence tools to PATH.

> Virtuoso and Spectre are **independent** — you can run Spectre without the SKILL bridge, and vice versa.

## Step-by-step setup (remote mode)

**1. Install**

> **Use `uv` + virtual environment** — never install into the global Python.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e .
```

**2. Generate config**

```bash
virtuoso-bridge init        # creates .env template in current directory
```

**3. Edit `.env`**

> **Where to put `.env`:** Can live in the virtuoso-bridge-lite directory or your project root (both searched automatically). Project root is recommended when virtuoso-bridge-lite is a subdirectory.

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

```
load("/path/to/virtuoso-bridge-lite/core/ramic_bridge.il")
```

**6. Verify**

```bash
virtuoso-bridge status
```

**7. Connect from Python**

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

### Jump host setup

If you access Virtuoso through a bastion/jump host, set both hosts in `.env`:

```dotenv
VB_REMOTE_HOST=compute-host   # the machine running Virtuoso (NOT the jump host)
VB_JUMP_HOST=jump-host        # the bastion you SSH through
```

Common mistake: setting `VB_REMOTE_HOST` to the jump host. `VB_REMOTE_HOST` must be the machine where Virtuoso is actually running.

### Multi-profile setup

Connect to multiple Virtuoso instances simultaneously with `-p`. Profile names are **case-sensitive** and appended as suffixes to env var names.

```dotenv
# Default (no profile)
VB_REMOTE_HOST=server-a
VB_REMOTE_USER=user1

# Profile "worker1" — used with `-p worker1`
VB_REMOTE_HOST_worker1=server-b
VB_REMOTE_USER_worker1=user2
VB_CADENCE_CSHRC_worker1=/path/to/.cshrc.worker1
```

```bash
virtuoso-bridge start -p worker1
virtuoso-bridge status -p worker1
```

```python
from virtuoso_bridge.spectre import SpectreSimulator
sim = SpectreSimulator.from_env(profile="worker1")
```

> Profile suffixes are case-sensitive. `-p worker1` reads `VB_REMOTE_HOST_worker1`, not `VB_REMOTE_HOST_WORKER1`.

## First-time setup check

When a user first opens this project, run these checks **before anything else**:

### Remote check

**Three-host model** (common in EDA environments):
```
Your machine  ──SSH──►  Jump host (bastion)  ──SSH──►  Compute host (Virtuoso)
              VB_JUMP_HOST                   VB_REMOTE_HOST
```
`VB_REMOTE_HOST` must be the machine running Virtuoso, **not** the jump host. This is the most common misconfiguration.

1. **Check `.env`** — does it exist and have `VB_REMOTE_HOST` set?
   - If not: `pip install -e .` then `virtuoso-bridge init`, ask the user to fill in their SSH host.
   - Verify: `VB_REMOTE_HOST` = compute host (where Virtuoso runs), `VB_JUMP_HOST` = bastion (if any).

2. **Check SSH** — `ssh <VB_REMOTE_HOST> echo ok` (or via jump host if configured)
   - If this fails: tell the user to fix SSH first. The bridge assumes `ssh <host>` already works.

3. **Check Virtuoso** — `ssh <VB_REMOTE_HOST> "pgrep -f virtuoso"`
   - If no process: tell the user to start Virtuoso first.

4. **Start bridge** — `virtuoso-bridge start`
   - If "degraded": tell the user to paste the `load("...")` command in Virtuoso CIW.

5. **Verify** — `virtuoso-bridge status`

6. **Quick test** — `VirtuosoClient.from_env().execute_skill("1+2")`

### Local mode

No tunnel, no `.env`, no SSH. Just load `core/ramic_bridge.il` in Virtuoso CIW and connect directly.

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

## Two independent services

The bridge manages two **independent** capabilities on the remote host:

| Service | What it does | Requires |
|---|---|---|
| **Virtuoso daemon** | Execute SKILL expressions in the Virtuoso CIW | A running Virtuoso process + `load("...virtuoso_setup.il")` in CIW |
| **Spectre** | Run circuit simulations via SSH | `spectre` on PATH (or `VB_CADENCE_CSHRC` set) |

They are fully independent — you can run Spectre without loading the SKILL bridge, and you can use the SKILL bridge without Spectre.

`virtuoso-bridge status` reports both. Example output:
```
[tunnel]  running          ← SSH tunnel is up
[daemon]  OK               ← Virtuoso CIW connected (or NO RESPONSE if not loaded)
[spectre] OK               ← spectre found on remote (or NOT FOUND)
```

### How Spectre is located

Each SSH command runs in a **fresh shell** with no prior state. To find `spectre`, the bridge:

1. Tries `which spectre` directly — works if the user's login shell already has Cadence on PATH.
2. If not found and `VB_CADENCE_CSHRC` is set, sources that cshrc in a csh sub-shell to set up `PATH`, `LM_LICENSE_FILE`, `LD_LIBRARY_PATH`, etc., then retries.

This cshrc is sourced **every time** (status check, license check, every simulation run) because each SSH command is a new process with no memory of previous sessions.

If `spectre` is already on PATH in the remote user's default shell (e.g., via `~/.bashrc` or `~/.cshrc`), `VB_CADENCE_CSHRC` is not needed.

## Key conventions

- All SKILL execution goes through `VirtuosoClient`. Never SSH and run SKILL manually.
- Layout/schematic editing: `client.layout.edit()` / `client.schematic.edit()` context managers.
- Spectre simulation: `SpectreSimulator.from_env()`. See "How Spectre is located" above.
- `core/` is for understanding the mechanism (3 files, 180 lines). Use the installed package for real work.

## How to configure PDK paths

Export a netlist from Virtuoso (**Simulation > Netlist > Create**). The `.scs` file contains everything:

```spectre
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT
M0 (VOUT VIN VSS VSS) nch_ulvt_mac l=30n w=1u nf=1
```

## CLI reference

```bash
virtuoso-bridge init      # create .env template
virtuoso-bridge start     # start SSH tunnel + deploy daemon
virtuoso-bridge restart   # force-restart
virtuoso-bridge status    # check tunnel + Virtuoso daemon + Spectre
```

## Build & test

> **Recommended: use `uv` to manage the virtual environment.** `uv` refuses to install packages globally (unless `--system` is explicitly passed), preventing accidental pollution of the system Python.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e ".[dev]"
pytest
```

## Windows: fix symlinks

Git on Windows clones symlinks as plain text files (`core.symlinks = false`),
which breaks skill loading for any agent that follows `.claude/skills/` (or
similar) links. Run this **once** after cloning:

```bash
bash scripts/fix-symlinks.sh
```

The script replaces broken symlinks with NTFS junctions — no admin rights, no
Developer Mode required.

## Skills

| Skill | File | Covers |
|---|---|---|
| `virtuoso` | `skills/virtuoso/SKILL.md` | SKILL execution, layout/schematic editing |
| `spectre` | `skills/spectre/SKILL.md` | Simulation, result parsing |

```
skills/virtuoso/
  SKILL.md
  references/
    layout.md       # layout API reference
    schematic.md    # schematic API reference

skills/spectre/
  SKILL.md          # simulation workflow + result parsing
```
