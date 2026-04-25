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

## Install (both modes)

> **Use `uv` + virtual environment** — never install into the global Python.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e .
```

## Step-by-step setup (remote mode)

**1. Generate config**

```bash
# Preferred — fill host/user/jump in one shot:
virtuoso-bridge init designer1@thu-wei -J designer1@bastion.example.com

# Or — empty template (you edit `.env` manually in step 2):
virtuoso-bridge init
```

Both forms create `~/.virtuoso-bridge/.env`. `-J/--jump` accepts `[user@]host`.
`VB_REMOTE_PORT` / `VB_LOCAL_PORT` are auto-assigned by hashing the **remote**
username (stable per remote user, so two users on the same host don't collide).
Re-running `init` on an existing `.env` is a no-op; pass `--force` to overwrite.

**2. Edit `.env`** (only if step 1 did not already fill it in)

> **Where to put `.env`:** By default the bridge checks `./.env` first, then `~/.virtuoso-bridge/.env`. For CLI commands such as `virtuoso-bridge start`, `--env FILE` has the highest priority.

```dotenv
VB_REMOTE_HOST=my-server              # SSH host alias from ~/.ssh/config
VB_REMOTE_USER=username               # SSH username on the remote
VB_REMOTE_PORT=65081                  # port for the bridge daemon on remote
VB_LOCAL_PORT=65082                   # local port forwarded via SSH tunnel

# Optional — only needed if `spectre` is not already on PATH in the remote shell.
# VB_CADENCE_CSHRC=/path/to/.cshrc   # cshrc that sets up Cadence tools on the remote
```

**3. Start the bridge**

```bash
virtuoso-bridge start
```

**4. Load SKILL in Virtuoso CIW**

```
load("/path/to/virtuoso-bridge-lite/core/ramic_bridge.il")
```

**5. Verify**

```bash
virtuoso-bridge status
```

**6. Connect from Python**

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

> **CIW output vs return value**: `execute_skill()` returns the result to Python but does **not** print in the CIW window. To also display in CIW, use `printf` explicitly:
> `client.execute_skill(r'let((v) v=1+2 printf("1+2 = %d\n" v) v)')`.
> See `examples/01_virtuoso/basic/00_ciw_output_vs_return.py`.

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
   - If not: `pip install -e .` then ask the user for their SSH target. If they give `user@host` (plus optional jump), run `virtuoso-bridge init user@host [-J user@jump]` — it fills everything in one shot. Otherwise run `virtuoso-bridge init` for an empty template and ask them to fill `VB_REMOTE_HOST`.
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
| **Virtuoso daemon** | Execute SKILL expressions in the Virtuoso CIW | A running Virtuoso process + `load("...virtuoso_setup.il")` in CIW (auto-generated by `start`) |
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
- `core/` is the minimal reference implementation (3 source files, ~285 lines). Use the installed package for real work.
- `tools/` contains standalone utilities (e.g. `skill_exec.py` — zero-dependency SKILL execution tool).

## Common gotchas

- **`csh()` returns `t`/`nil`**, not command output. Use `client.download_file()` (SSH/SCP) for remote file operations.
- **`procedurep()` returns `nil` for compiled/built-in functions.** Don't use it to check if `mae*` functions exist.
- **Remote files stay remote.** Functions like `maeCreateNetlistForCorner` write to the remote filesystem. Use `client.download_file()` to retrieve them.

## How to configure PDK paths

Export a netlist from Virtuoso (**Simulation > Netlist > Create**). The `.scs` file contains everything:

```spectre
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT
M0 (VOUT VIN VSS VSS) nch_ulvt_mac l=30n w=1u nf=1
```

## CLI reference

```bash
virtuoso-bridge init [user@host] [-J user@jump] [--force]   # write ~/.virtuoso-bridge/.env
virtuoso-bridge start           # start SSH tunnel + deploy daemon
virtuoso-bridge stop            # stop the SSH tunnel
virtuoso-bridge restart         # force-restart
virtuoso-bridge status          # check tunnel + Virtuoso daemon + Spectre
virtuoso-bridge license         # check Spectre license availability
virtuoso-bridge windows         # list all open Virtuoso windows + focused session
virtuoso-bridge snapshot        # focused maestro: 4 SKILL probe sections to stdout
virtuoso-bridge snapshot -o ROOT  # full disk dump (raw + filtered XMLs + per-point run files)
virtuoso-bridge screenshot      # screenshot CIW (or: current, N)
virtuoso-bridge dismiss-dialog  # dismiss blocking GUI dialogs via X11
```

## Build

> **Recommended: use `uv` to manage the virtual environment.** `uv` refuses to install packages globally (unless `--system` is explicitly passed), preventing accidental pollution of the system Python.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e .
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

## Skills & Reference Map

When working on a task, check this table to find relevant skills and references.

| Domain | Skill | Entry point | Key references |
|---|---|---|---|
| **Virtuoso / SKILL** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/layout-skill-api.md`, `references/schematic-skill-api.md`, `references/maestro-skill-api.md`, `references/troubleshooting.md` |
| **Layout** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/layout-python-api.md`, `references/layout-skill-api.md` |
| **Schematic** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/schematic-python-api.md`, `references/schematic-skill-api.md`, `references/schematic-recreation.md` |
| **Maestro / ADE** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/maestro-python-api.md`, `references/maestro-skill-api.md`, `references/simulation-flow.md` |
| **Spectre simulation** | `spectre` | `skills/spectre/SKILL.md` | `references/netlist_syntax.md`, `references/parallel.md` |
| **Netlist** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/netlist.md`, `references/batch-netlist-si.md` |
| **Testbench duplication** | `virtuoso` | `skills/virtuoso/SKILL.md` | `references/testbench-duplication.md` |
| **Parameter optimization** | `optimizer` | `skills/optimizer/SKILL.md` | — |

All reference paths are relative to the skill directory (e.g. `skills/virtuoso/references/layout-skill-api.md`).
