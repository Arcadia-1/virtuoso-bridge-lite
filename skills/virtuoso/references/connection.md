# Connecting for live EDA work

Read this for first connection, a changed target or a connection failure.
Use the existing environment when it is already working. Documentation edits
and offline tests do not require this procedure.

## One-host Lite setup

Use `uv` and a project virtual environment; from a repository checkout:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

On Windows use the venv's `Scripts/python.exe`. Run commands with the venv's
interpreter/console scripts, or activate it. Avoid global Python installation.

For a requested environment rebuild, inventory this repository's actual venvs
and editable package targets first. Preserve the original Python version,
back up each selected venv, and retain any `.virtuoso-bridge-profile` binding.
Recreate from `pyproject.toml` with `uv sync` (`--extra dev` for development/tests)
rather than copying old site-packages. Verify the imported package path/version,
CLI entrypoint and dependency consistency afterward. A documentation update or
venv rebuild does not reload a daemon already running inside Virtuoso.

Reuse existing configuration. The CLI's `--env FILE` takes priority; otherwise
it searches parent directories for a bridge `.env`, then the user-level
`~/.virtuoso-bridge/.env`. Initialize only when configuration is absent, using
the user's actual SSH target and optional jump:

```bash
virtuoso-bridge init user@compute-host -J user@jump-host
```

For ordinary remote use, `VB_REMOTE_HOST` is where the bridge daemon runs,
not merely the bastion; `VB_JUMP_HOST` is the SSH route. SSH must work without
an interactive password prompt. Read the lab's csh/tcsh guidance before writing
site-specific remote shell commands.

For SKILL execution, confirm the intended Virtuoso process is running, then
start only the requested bridge:

```bash
virtuoso-bridge start
virtuoso-bridge status
```

If the daemon is not loaded, have the user paste the exact generated
`load("...")` line into that CIW. Optional X11 bootstrap requires an explicitly
selected CIW and user authorization; check command availability before using it.
Do not reload/stop a busy shared daemon just to switch profiles.

Commands no longer echo each SSH/scp call; add `--verbose` (or set
`VB_VERBOSE=1`) to see them while diagnosing a connection. They are always
recorded in the command log.

Verify SKILL with a harmless expression on the confirmed endpoint:

```bash
virtuoso-bridge eval '1+2'
```

Each client authenticates to the daemon with the token stored on the daemon
host. To spare an SSH round trip per command, the client keeps a copy and the
last verified daemon identity in `~/.virtuoso-bridge/remote_daemons/`
(owner-only files). A cached token the daemon rejects is fetched again over
SSH, and a restarted daemon is verified again; deleting that directory only
costs the next command those round trips.

Local use sets `VB_REMOTE_HOST=localhost`; `start` skips SSH and prints the
local setup path. Spectre-only work needs the simulator and its environment,
not a Virtuoso process or a loaded SKILL daemon. Validate only the service
the task needs; an unrelated degraded service is not a blocker.

## Cadence environment on the remote host

Spectre runs, `license`, `skill-find` and `doc-search` start tools through a
plain SSH shell, which skips the lab's interactive startup. `virtuoso-bridge
status` shows whether Spectre is found. Pick what matches how the site starts
Cadence; existing settings keep working:

- `spectre` already on the login `PATH`: nothing to set.
- A setup script the lab sources: `VB_CADENCE_CSHRC=/path/to/cadence.cshrc`
  (csh, the default).
- A POSIX sh script, for example one that runs `module load <cadence>`:
  `VB_CADENCE_CSHRC=/path/to/cadence.sh` and `VB_CADENCE_ENV_SHELL=sh`.
- Tools started only through a site wrapper command (`<ic-wrapper> spectre`):
  `VB_SPECTRE_BIN=<ic-wrapper> spectre` serves Spectre only. To also serve
  `license` and the documentation commands, take the wrapper's environment
  in an sh script and select it as above:

  ```sh
  # cadence-env.sh (VB_CADENCE_CSHRC=/path/to/cadence-env.sh, VB_CADENCE_ENV_SHELL=sh)
  eval "$(<ic-wrapper> sh -c 'export -p')"
  ```

Use the setup or wrapper of the IC version the CIW runs, and update it when
that version changes. Hosts or versions with different setups use profile
suffixes such as `VB_CADENCE_CSHRC_worker`.

## Read-only setup check

When a setup misbehaves in a way `status` does not explain, or before handing
a host to automation, run the doctor. It checks one profile per run; select it
with `-p`/`--env` as for `status`:

```bash
virtuoso-bridge doctor
virtuoso-bridge doctor --json   # [{"name", "status", "detail", "fix"}, ...]
```

Each check prints `OK`, `WARN`, `FAIL` or `SKIP` with a one-line reason and,
for WARN/FAIL, a fix; the exit status is 1 only when a check FAILs. It checks
the loaded `.env`, profile and target; the tunnel, signed daemon handshake,
daemon owner and IPC lane; a daemon listening beyond 127.0.0.1
(`VB_DAEMON_LISTEN_ALL`); that the remote deploy directories are yours, mode
0700 and free of symlinks; open dialogs (X11, out of band) and PDK first-use
forms; `<X>_UseNextLicense` values other than `"always"`, whose "Next License"
prompt blocks the CIW on the first schematic, layout or ADE edit (the names
come from the running installation's `tools/dfII/etc/tools/license/.cdsenv`,
VSEL, VLSL, VLSXL, VLSAdvOpt, VIVA, ADEL, ADEXL, ADE and Explorer in IC 6.1.8;
all default to `"prompt"`); Spectre; and the documentation that
`skill-find`/`doc-search` would use.

The doctor changes nothing: it never starts, stops, restarts or deploys, writes
no file on the remote host, sends only read-only SKILL (no `envSetVal`) and
dismisses no dialog; apply its fixes yourself. It sends no SKILL while the
daemon's lane is busy or a dialog blocks the CIW. Without a running tunnel or
daemon, the checks that need one report SKIP, while the scratch directory,
Spectre and documentation are still checked over SSH. A PDK's first-use form
otherwise blocks the first bridge request that touches its technology; for a
headless Virtuoso, initialise the technology in `.cdsinit` or the `-restore`
script before the bridge is used, e.g. `techGetTechFile(ddGetObj("<pdkLib>"))`.

## Existing configuration under an isolated agent home

If an agent redirects `HOME`, select the existing file by an accessible absolute
path; keep the sandbox's home unchanged. No config migration or `init` is needed:

```bash
virtuoso-bridge status --env /allowed/bridge.env
```

Pass `--env` on each CLI invocation. For Python, select it before creating clients:

```python
from virtuoso_bridge.env import set_runtime_env_file
from virtuoso_bridge import VirtuosoClient

set_runtime_env_file("/allowed/bridge.env")
client = VirtuosoClient.from_env()
```

This selects bridge configuration only; it neither grants filesystem permission
nor relocates SSH credentials or runtime files. Grant the agent access only to
the files it needs, rather than exposing or copying the entire real home:

- SSH: `VB_SSH_CONFIG=/allowed/ssh_config` selects the SSH config. Use accessible
  absolute `IdentityFile` and `UserKnownHostsFile` paths (and ensure any jump-host
  credentials or SSH agent socket are accessible). Keep host-key checking enabled.
  `~` expansion depends on the backend and environment; it is not the original home.
- Tunnel state/logs/cache: leave them in the agent's writable runtime directories.
  `VB_STATE_DIR`, `VB_LOG_DIR` and `VB_CACHE_DIR` are optional overrides for those
  paths. Reusing an existing tunnel's state requires the same local process context
  and accessible state directory; the `.env` alone does not attach to it.
- Authentication: the client token/identity cache remains under the Python
  process's `~/.virtuoso-bridge/remote_daemons/`; local-mode tokens use
  `~/.virtuoso-bridge/bridge_token`. The remote daemon's token stays on its host.
  The generic runtime-directory overrides do not move these authentication files.

`VB_CONFIG_DIR` and `VB_HOME` are not selectors for legacy `.env` discovery.
Changing `HOME` globally also affects other tools, SSH paths and token caches;
use explicit paths instead of treating a home override as a bridge-only setting.

## Slow SSH to a local VM (Windows or other clients)

If `start` succeeds but identity checks time out, compare with a plain SSH command
using the same host, account, config and jump route. Use `ssh -v <target> whoami`
locally and inspect where the pause occurs; redact host/user details before sharing
logs. A delay before authentication can be server-side reverse DNS or GSSAPI,
not a busy Virtuoso. The displayed `tunnel.warm` time also includes deployment;
it is not a measurement or proof of DNS latency.

On VMware/VirtualBox host-only networks, an administrator can check guest name
resolution (`getent hosts <client-IP>`), correct DNS or an `/etc/hosts` mapping,
and review sshd's `UseDNS` policy. Do not change or restart system SSH services
without authorization. A daemon identity timeout is not proof of a user mismatch;
keep daemon authentication and identity checks enabled.

For a slow but healthy path, `eval/load --timeout 60` and
`VirtuosoClient.from_env(timeout=60)` / `from_tunnel(..., timeout=60)` now pass that
budget to identity probes as well as SKILL execution, without the old 10-second
cap. These are per-operation timeouts, not an overall wall-clock deadline or a
setting for every SSH setup phase. `status` keeps short diagnostic probes;
repair SSH latency before treating its timeout as a broken Virtuoso session.
Check the installed version before expecting this behavior on older clients.

## Optional advanced installations

Only configure extra roles when the user actually has a split installation.
On versions that support them, GUI/X11 uses `VB_GUI_HOST`, deployment uses
`VB_DEPLOY_HOST`, the daemon tunnel uses `VB_DAEMON_HOST`, and Spectre uses
`VB_SPECTRE_HOST`; unset roles fall back to `VB_REMOTE_HOST`.
If deployed files are not visible to CIW, choose a shared
`VB_REMOTE_SCRATCH_ROOT` instead of assuming the SSH host's `/tmp` is shared.

Named profiles use case-sensitive suffixes and `-p PROFILE`. Keep existing
one-host configuration unchanged unless the task requires these overrides.
Separate `.env` files for different hosts, users or daemon ports keep
separate tunnels and can run at the same time without profiles: each command
acts on the tunnel of the configuration it loads (`--env FILE`), `status`
names the others, and a busy local port moves to a free one written back to
that file.
Configured process/version metadata is a declaration, not verified runtime
CIW identity. Read the checkout's profile documentation for supported fields.

### Selecting a running instance

New bootstraps use OS-assigned ports. `start` discovers live instances for the
SSH user without changing allocation requests. Optionally select a project:

```dotenv
VB_REMOTE_WORK_DIR=/projects/project-a
```

One matching instance connects automatically. Multiple matches require an exact
remote port, which must also match the configured working directory:

```bash
virtuoso-bridge sessions --json
virtuoso-bridge start --port 65123
virtuoso-bridge status --port 65123
```

`VB_REMOTE_PORT` prefers a matching record. If discovery succeeds with no project
candidates, it can use the configured fixed-port endpoint. Discovery errors,
ambiguity, authentication failures and a saved registered selection do not fall
back. Existing fixed-port configuration remains supported.

If no endpoint is available, `start` prints a `load("...")` instruction. Load it
in the intended idle CIW, then rerun `start`. Add the load line to `.cdsinit` for
future sessions. Uploaded code takes effect only after it is loaded in Virtuoso.

For a persistent fixed-port request, run the installed helper on the daemon host:

```bash
/usr/bin/python3 /opt/virtuoso-bridge/v2/ramic_registry.py --scratch-root /home/user/.virtuoso-bridge --configure --work-dir /projects/project-a --port 65123
# Clear the request by omitting --port:
/usr/bin/python3 /opt/virtuoso-bridge/v2/ramic_registry.py --scratch-root /home/user/.virtuoso-bridge --configure --work-dir /projects/project-a
```

Directory requests take precedence over account-wide requests, which omit
`--work-dir`. Requests affect future starts; an occupied requested port fails.
`RBSetPort(N)` switches an idle instance. `RBSetPort(0)` consults saved requests
before using an OS-assigned port.

### Two projects under the same account

Keep separate `.env` files with the same host/user and each project's absolute
`VB_REMOTE_WORK_DIR`. Named profiles remain optional:

```bash
virtuoso-bridge start --env /local/project-a.env
virtuoso-bridge eval 'getWorkingDir()' --env /local/project-a.env
virtuoso-bridge start --env /local/project-b.env
virtuoso-bridge eval 'getWorkingDir()' --env /local/project-b.env
```

Use `--port` when more than one CIW shares the selected directory. Check the
working directory before editing; project filters distinguish sessions within
an account, not access by another process using that account.

### Shared bootstrap

Install a new versioned bundle on the Linux Cadence host:

```bash
uv run python tools/install_shared_daemon.py /opt/virtuoso-bridge/v2 --python /usr/bin/python3
```

Use the printed load line in project `.cdsinit` files and set
`VB_DAEMON_SETUP=/opt/virtuoso-bridge/v2/virtuoso_setup.il` on clients. The installer
requires a new protected destination; install upgrades into a new versioned path.

Without `VB_DAEMON_SETUP`, `start` prepares
`/tmp/virtuoso_bridge_<user>/daemon/virtuoso_setup.il` when no endpoint is available.
Generated bundles are reused when their inventory and content match. To replace
a differing bundle, use `start --replace-setup`; the previous bundle is retained
at the printed backup path. Shared installations are never replaced by clients.
Load the new setup explicitly to update a running CIW.

The registry uses `<root>/runs`, with `/tmp/virtuoso_bridge_<user>` as its default
root. Set `VB_REMOTE_SCRATCH_ROOT` on clients and the matching `RB_SCRATCH_ROOT`
on daemons, or pass `--scratch-root` to the shared installer. Generated setup
embeds this root. Deployment, GUI and daemon hosts must see the same files;
client configuration edits do not update an already loaded CIW.

### Status and restart

`status` authenticates the selected instance without sending SKILL and checks
Spectre independently. `restart` schedules one stop/wait/start in the selected
idle CIW, then authenticates its replacement. It uses the loaded configuration;
it does not load newly deployed files. After an uncertain result, inspect the
original outcome before requesting another restart.

`RBStopAll()` stops only the current CIW's tracked child.
`bootstrap --window` requires an existing generated setup or `VB_DAEMON_SETUP`
and confirms completion against the selected CIW's record and signed reply.

### Legacy tunnel recovery

Inspect retained state and stop its remote endpoint using the original config:

```bash
virtuoso-bridge status --inspect-state --env /local/bridge.env
virtuoso-bridge stop --port 65085 --env /local/bridge.env
```

Use the reported `remote_port`, not the local forwarded `port`. Deleting a state
file does not stop its listener. See [shared-CIW protection](shared-ciw-dialogs.md)
before recovering an uncertain request.

Local discovery requires Linux (`flock` and `/proc/self/fd`); Windows/macOS
clients can use SSH to the Linux daemon host. See the
[discovery design](../../../docs/adr/0004-instance-discovery.md) for record
publication, selection and lifecycle details.
