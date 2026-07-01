# Connecting Claude Code to a Virtuoso session on HPC (from your laptop)

Drive a Virtuoso session running on a remote compute node — through a login /
gateway host — from Claude Code on your laptop. Everything you type runs
locally; the bridge tunnels SKILL to the remote CIW.

```
   Laptop (you + Claude Code + this repo)
        │  virtuoso-bridge start   →  ssh -J <jump> <remote>, forward localPort → 127.0.0.1:daemonPort
        ▼
   Login / gateway host              ← VB_JUMP_HOST   (ProxyJump, built automatically)
        ▼
   Compute node running Virtuoso     ← VB_REMOTE_HOST
        ├─ ramic_bridge daemon  (deployed by `start`)
        └─ Virtuoso CIW  ── paste one load("…virtuoso_setup.il") line once
```

See `AGENTS.md` for the full CLI/API reference; this file is the HPC-specific
quickstart.

## Two rules that trip people up on HPC

1. **Use your SSH *aliases*, not raw IPs/ports.** The bridge does **not** pass
   `-p`; it relies on `~/.ssh/config` for `HostName`, `User`, and `Port`. If your
   hosts use a non-standard SSH port, put the **alias** in the bridge config so
   that port is inherited. A raw IP would default to port 22 and fail.
2. **`VB_REMOTE_PORT` / `VB_LOCAL_PORT` are the *bridge daemon* port — not the SSH
   port.** They're auto-derived by hashing your remote username; leave them
   alone. Don't set them to your SSH port.

## Prerequisites

- **Passwordless SSH all the way to the compute node.** The tool runs `ssh` with
  `BatchMode=yes` and will never prompt. Prove it first:
  ```bash
  ssh -J <jump-alias> <remote-alias> echo ok      # must print ok, no prompt
  ```
- **A running Virtuoso with a CIW** on the compute node (typically via VNC/X),
  launched by you *after* `module load` (see "Lmod modules" below). The bridge
  attaches to an existing session; it does not launch Virtuoso.
- **A `python3` visible in a plain (non-login) SSH shell** on the compute node —
  the bridge probes `ssh <node> python3 --version` to pick the right daemon. If
  Python only appears after a `module load`, add that load to your shell rc, or
  set `RB_PYTHON_PATH` (below).
- **This repo installed locally:**
  ```bash
  uv venv .venv && source .venv/bin/activate
  uv pip install -e .
  ```

## Reuse the SSH-alias pattern you already have

> **Note:** the `server01` / `server02` / `server01_ext` hosts in this machine's
> `~/.ssh/config` are homelab boxes — **they do not run Virtuoso.** They're worth
> looking at only because they already demonstrate the exact pattern you need for
> the real HPC hosts: an **alias** carrying `User`, a **non-standard `Port`**
> (`5148`), and `ForwardAgent yes`:
>
> ```sshconfig
> Host server01_ext
>     HostName 90.213.214.193
>     User ajithkv
>     Port 5148
>     ForwardAgent yes
> ```
>
> Replicate that shape for your actual EDA login + compute hosts, then point the
> bridge at those aliases. Placeholders below: **`hpc-login`** (the gateway you
> can reach from the laptop) and **`hpc-node`** (where Virtuoso runs).

First add the real hosts to `~/.ssh/config` (adjust user/port/proxy to your site):

```sshconfig
Host hpc-login
    HostName login.hpc.example.edu
    User <you>
    # Port <n>            # only if non-standard

Host hpc-node
    HostName <compute-node-or-ip>
    User <you>
    ProxyJump hpc-login   # optional; the bridge can also do the jump itself
```

Confirm it works with no prompt, then pick the scenario matching **where Virtuoso
actually runs.**

### Scenario A — Virtuoso on a compute node, reached via a gateway

```bash
virtuoso-bridge init <you>@hpc-node -J <you>@hpc-login
```
`~/.virtuoso-bridge/.env`:
```dotenv
VB_REMOTE_HOST=hpc-node              # alias → the machine running Virtuoso
VB_REMOTE_USER=<you>
VB_JUMP_HOST=hpc-login               # alias → the gateway you reach from the laptop
# VB_REMOTE_PORT / VB_LOCAL_PORT: leave unset (auto — bridge daemon port, not SSH)
```
The bridge issues `ssh -J <you>@hpc-login <you>@hpc-node`; both aliases are
resolved from `~/.ssh/config`, so any custom port is applied to each hop
automatically.

### Scenario B — Virtuoso on the directly-reachable host (no jump)

If Virtuoso runs on a host you can SSH to directly, skip the jump:

```bash
virtuoso-bridge init <you>@hpc-login
```
```dotenv
VB_REMOTE_HOST=hpc-login             # the machine running Virtuoso
VB_REMOTE_USER=<you>
# no VB_JUMP_HOST
```

## Start, load, verify

```bash
virtuoso-bridge start          # opens the tunnel + deploys the daemon
# → prints:  load("/tmp/virtuoso_bridge_<user>/<client>/virtuoso_bridge/virtuoso_setup.il")
```
Paste that `load("…")` line into the **Virtuoso CIW** once (add it to the remote
`~/.cdsinit` to auto-load on every start). Then:
```bash
virtuoso-bridge status         # [tunnel] running  [daemon] OK  [spectre] OK/NOT FOUND
virtuoso-bridge eval "1+2"     # → 3
```

## Scheduler-allocated compute nodes (SLURM / LSF)

If the compute node is handed out by a scheduler and its hostname changes per
job, either:

- **Update the target each allocation:** start Virtuoso on the allocated node,
  note `$HOSTNAME`, then `virtuoso-bridge init --force ajithkv@<node> -J ajithkv@<login>`
  and `virtuoso-bridge restart`; or
- **Keep a stable alias** in `~/.ssh/config` (e.g. `Host vnode` with
  `ProxyJump server01_ext`) and only edit its `HostName` when the node changes —
  then `VB_REMOTE_HOST=vnode` never moves.

(If your site allows Virtuoso on the login node, Scenario B is simplest — but many
sites forbid EDA on login nodes.)

## Lmod modules (fresh-shell environment)

Every command the bridge runs over SSH is a **fresh, non-interactive shell** — no
`module load` is active in it. But the two services need Cadence env in different
ways, so this splits cleanly:

**SKILL / Virtuoso — handled by you at launch, not by the bridge.** The daemon is
spawned by `ipcBeginProcess` *inside the running CIW* (`ramic_bridge.il`), so it
inherits whatever environment Virtuoso has. Load your modules **before** starting
Virtuoso in the interactive job and everything downstream inherits it:

```bash
# in your interactive/VNC job on the allocated node:
module load cadence/<ver>          # your site's Virtuoso module
virtuoso &                          # CIW now has the full Cadence env
```

The daemon runs `python` (stripping `LD_LIBRARY_PATH`/`LD_PRELOAD` so Cadence libs
don't shadow it). If plain `python` isn't on PATH in Virtuoso's env, set
`RB_PYTHON_PATH` before launching Virtuoso, e.g. `setenv RB_PYTHON_PATH python3`.

**Spectre — point the bridge at a csh init file.** Spectre is invoked over SSH via
`csh -c 'source $VB_CADENCE_CSHRC; spectre …'`, so `VB_CADENCE_CSHRC` must be a
**csh-syntax** file that initializes Lmod and loads the tools. Create one on the
remote host:

```csh
# ~/cadence-env.csh   (csh syntax — sourced in a csh subshell)
source /usr/share/lmod/lmod/init/csh    # defines `module` for csh; adjust path
module load cadence/<ver> spectre/<ver>
```

Then point the bridge at it (per-profile suffixes work too, e.g.
`VB_CADENCE_CSHRC_worker1`):

```dotenv
VB_CADENCE_CSHRC=/home/<you>/cadence-env.csh
```

The `source .../init/csh` line matters: in a bare `csh -c`, Lmod's `module`
command isn't defined unless the init is sourced (some sites do this in
`/etc/csh.cshrc`, in which case you can drop the line — but keeping it is safe).
Verify with `virtuoso-bridge license` / `virtuoso-bridge status` → `[spectre] OK`.
If your tools use a real `.cshrc` instead of Lmod, just point `VB_CADENCE_CSHRC`
straight at that.

## Using it from Claude Code

Run Claude Code on your laptop **inside this repo**. Once `status` is green, the
bridge is Claude Code's hands into the remote Virtuoso — it drives it through the
same API/CLI, plus the bundled `skills/virtuoso`, `skills/spectre`,
`skills/optimizer`:

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()     # reads .env, uses the tunnel
client.execute_skill("1+2")            # runs on the HPC CIW
with client.schematic.edit() as s:
    ...                                # schematic / layout / maestro helpers
```

SKILL always goes **through** the bridge (`client.execute_skill` /
`virtuoso-bridge eval`) — never SSH in and run SKILL by hand.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[daemon] NO RESPONSE` | `load("…")` not pasted into the CIW, or Virtuoso not running on that node. Re-run `status` to reprint the line. |
| Tunnel won't start / auth fails | `ssh -J <jump> <remote> echo ok` must work with **no prompt** — `BatchMode=yes` gives no password fallback. Fix keys/agent first. |
| Connects to wrong host | `VB_REMOTE_HOST` = the node running Virtuoso; `VB_JUMP_HOST` = the gateway. Don't set remote host to the gateway. |
| "Connection refused" on the SSH hop | You used a raw IP instead of the alias, so it tried port 22 — use the `~/.ssh/config` alias so your custom `Port` is applied. |
| 15–30 s stalls on connect | Usually GSSAPI/Kerberos or a slow gateway; the bridge already disables GSSAPI and allows longer jump-host settle time, so first connects are just slow, not broken. |
| Spectre `NOT FOUND` | Independent from the SKILL bridge. Point `VB_CADENCE_CSHRC` at a csh init file that `module load`s the tools (see "Lmod modules" above). |
| Daemon won't start / wrong Python | The daemon inherits Virtuoso's env — `module load` **before** launching Virtuoso, and set `RB_PYTHON_PATH` if plain `python` isn't on its PATH. Daemon *selection* also needs a `python3` in a bare SSH shell. |
