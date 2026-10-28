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

Daemons use OS-assigned ports. `virtuoso-bridge start` removes stale records,
then discovers instances belonging to the SSH user. Optionally filter by
Virtuoso's absolute working directory:

```dotenv
VB_REMOTE_WORK_DIR=/projects/project-a
```

One match connects. Multiple matches list port/PID/directory and require:

```bash
virtuoso-bridge start --port 65123
```

Use `sessions --json` to list records without selecting or provisioning, and
`status --port PORT` to check an exact instance. `start --port` overrides
`VB_REMOTE_PORT`. CLI `--port` is exact. The environment port prefers a matching
record but permits a unique new registered session. A successful discovery with no project candidates
can fall back to that configured old endpoint when no saved registered identity
or exact CLI selector exists. Authentication remains required. Discovery errors,
ambiguity and authentication failures never cause fallback. Selection never writes
or clears allocation requests.
Profiles remain optional; one host and one `.env` suffice.

For a deliberate persistent allocation request, run the installed helper on the
daemon host (replace paths and project directory):

```bash
/usr/bin/python3 /opt/virtuoso-bridge/v2/ramic_registry.py --scratch-root /home/user/.virtuoso-bridge --configure --work-dir /projects/project-a --port 65123
# Clear that directory's request by omitting --port:
/usr/bin/python3 /opt/virtuoso-bridge/v2/ramic_registry.py --scratch-root /home/user/.virtuoso-bridge --configure --work-dir /projects/project-a
# Clear the separate account-wide request by omitting --work-dir too:
/usr/bin/python3 /opt/virtuoso-bridge/v2/ramic_registry.py --scratch-root /home/user/.virtuoso-bridge --configure
```

Directory requests take precedence over account requests. Clear both applicable
scopes to restore automatic allocation. A requested port must bind or startup
fails; existing daemons are unaffected. Earlier clients may have left these
requests behind. Ordinary `start`, `status` and `sessions` never repair them.

Without a usable registered or configured legacy endpoint, start prints a
`load("...")` instruction and exits nonzero without a usable tunnel. In the intended idle CIW, stop its
old child with `RBStop()`, wait for exit, then load the new setup and rerun
`start`. A working old endpoint needs no upgrade simply because it has no record. Missing
build metadata means unknown compatibility; a different build is informational
unless a required protocol capability is absent. Never restart another CIW.
`RBSetPort(N)` switches an idle instance; `RBSetPort(0)` consults saved requests,
then defaults to automatic allocation. Files uploaded to disk do not replace
functions already loaded in Virtuoso.

`restart` keeps the saved CIW choice, schedules one bounded stop/wait/start,
then authenticates its replacement record and reconnects to the new endpoint.
For an old fixed-port daemon, restart verifies the same authenticated CIW birth
and a changed daemon identity, using the old loader functions with a bounded wait.
It restarts the loaded configuration; it does not load newly deployed files.
A busy lane or mismatched exact selector prevents restart/adoption. After a
timeout, inspect the original outcome and sessions before any explicit retry;
an uncertain restart is never replayed.

`RBSetPort(N)` keeps a fixed-port instance discovery-managed with mandatory
per-UID authentication. It does not enable a custom `RB_TOKEN_PATH`. Explicit
legacy generated setup sets `RB_AUTO_PORT=0` for legacy token behavior.

`RBStopAll()` now stops only this CIW's tracked child, like `RBStop()`, rather
than every daemon under the account. For an untracked child, inspect out of band
and verify its owner, command, birth identity and exact parent/CIW association
before an operator terminates it. A saved PID or port alone is insufficient.
Preserve other CIWs and leave unknown-origin dialogs alone.

### Two projects under the same account

Keep separate `.env` files with the same host/user and each project's absolute
`VB_REMOTE_WORK_DIR`. Named profiles (`-p NAME`, using case-sensitive
`VB_*_NAME` suffixes) remain optional. Replace sample ports with listed records:

```bash
virtuoso-bridge sessions --json --env /local/project-a.env
virtuoso-bridge start --port 65123 --env /local/project-a.env
virtuoso-bridge status --env /local/project-a.env
virtuoso-bridge eval 'getWorkingDir()' --env /local/project-a.env
virtuoso-bridge start --port 65124 --env /local/project-b.env
virtuoso-bridge status --env /local/project-b.env
virtuoso-bridge eval 'getWorkingDir()' --env /local/project-b.env
```

Check the actual working directory before editing. Two CIWs may share one
directory, so select an exact port when needed. One match connects automatically;
without a project constraint on a shared UID, it could be a colleague's CIW.
Project filters prevent mistakes; they are not a separate access boundary.

### Shared bootstrap

Install once into a new protected directory on the Cadence host:

```bash
uv run python tools/install_shared_daemon.py /opt/virtuoso-bridge/v2 --python /usr/bin/python3
```

Use the printed load line in each project's `.cdsinit`. Set
`VB_DAEMON_SETUP=/opt/virtuoso-bridge/v2/virtuoso_setup.il` on clients to use
that same path in load instructions. Without it, the bridge generates a
private bootstrap at `/tmp/virtuoso_bridge_<user>/daemon/virtuoso_setup.il`,
shared across projects by default.

Generated bundles are reused only when both inventory and bytes match. Automatic
worker launches disable bytecode writes; inspect and explicitly replace old
cache-bearing bundles. Unexpected members are refused before
any existing bundle file is changed. After reviewing the configuration, authorize
replacement with `start --replace-setup` (Python API: `replace_setup=True`). This
changes generated account code only and retains the previous complete bundle at
the printed private backup path. A bounded lock serializes publishers; a staged
directory and recovery name preserve the old bundle across an interrupted
directory exchange. The two renames leave a brief availability gap; the next
publication can recover an interrupted exchange. Post-commit errors report that
the bundle is active. Backups are retained: inspect exact paths and ensure no
CIW/job uses them before operator removal. Running CIWs need an explicit load
to change loaded code.
This option never overwrites a shared installation: install a new versioned path.

`start` prepares code when neither a registered session nor a usable configured
legacy endpoint is available. To stage a generated
remote update while registered CIWs remain running, explicitly call
`SSHClient.from_env(keep_remote_files=True, read_selection=False, replace_setup=True)`
and that client's `ensure_remote_setup()`, then `close()` in `finally`.
Alternatively install a new versioned shared bundle. Neither staging operation
loads or restarts a CIW; coordinate the subsequent stop/wait/load separately.

Order: prepare the Linux host and an absolute Python interpreter; have a trusted
installer create a new protected bundle; add its load line to project `.cdsinit`;
configure the client; list/select an instance; check signed status. Installation
refuses overwrite, symlink ancestors, foreign owners and group/other-writable
ancestors (including `/tmp`). All users loading the bundle trust its installing
UID, which need not be root. Upgrade into a new versioned path.

General scratch retains the stock `/tmp` parent and
`virtuoso_bridge_<user>/<client>/<leaf>` workflow layout. The default private
registry/bundle root is `/tmp/virtuoso_bridge_<user>`; daemon and client must agree
on this root. Explicit private roots from preview installations remain supported.
Explicit `~/` uses the target passwd account home, independent of `$HOME`. Generated setup includes the resolved
root; shared setup takes `--scratch-root`, otherwise daemon `RB_SCRATCH_ROOT`,
then daemon `VB_REMOTE_SCRATCH_ROOT`, then the default. A stale `RB_SCRATCH_ROOT`
inside an existing Virtuoso wins over client settings. Client `.env` edits do
not update a running CIW. The absolute project directory filter is not this root.
Use a private child instead of `/tmp`; do not chmod `/tmp` or the account home.
All relevant hosts must see the chosen files.

Records are private under `<root>/runs`; the authentication secret remains in
the passwd account home. Automatic mode requires per-UID authentication and
ignores `RB_TOKEN_PATH` and unauthenticated overrides. Explicit legacy clients
retain their documented token-path/unauthenticated settings. A shared UID is one
trust boundary: project and port filters prevent mistakes, not access by another
process using that UID. A signed response must match the whole record; record
files alone are not authentic proof of daemon identity.

Existing artifacts in old `/tmp/virtuoso_bridge_<user>/...` or client/profile
directories are not moved or deleted. Durable jobs and other sessions may still
use them. An explicit home-based root consumes home quota; choose a safe private
root according to site needs. Discovery also reads the deployed older
`<root>/virtuoso_bridge_<user>/runs` layout with the same ownership and lock
checks. An already loaded daemon keeps its original record until explicit upgrade;
loading the new bootstrap places its next record in `<root>/runs`.

`status` reports host roles, endpoints and other managed tunnels, uses no SKILL,
and reads existing credentials without creating them. Spectre is checked even
if discovery fails; bridge status remains nonzero until authenticated. Automatic
mode has no legacy CIW banner identity file. A fresh client can reconcile a
vanished instance only within an explicit project constraint and before sending
work; an exact port remains exact. Uncertain application requests are never
replayed. Stop a retained tunnel using its original `--env`/profile and
`stop --port PORT`; this targets saved endpoint state without rediscovery.
Legacy state without process-birth identity needs the
[recovery procedure](#legacy-tunnel-recovery). Windows ACLs, rather than POSIX mode bits, protect
local state. `bootstrap --window` needs an existing generated setup or
`VB_DAEMON_SETUP`; it never proves completion by finding an unrelated instance.

Automatic local discovery and daemon installation require Linux (`flock`, UID
ownership and `/proc/self/fd`). Windows/macOS clients can use SSH to Linux;
independent Spectre workflows and explicit legacy APIs retain their own support.

See the [discovery design](../../../docs/adr/0004-instance-discovery.md) for
locking and security, and [shared-CIW protection](shared-ciw-dialogs.md) before
recovering an uncertain request.

### Legacy tunnel recovery

Use the original `.env`/profile. Inspect exact local state paths, endpoints and
PIDs without SSH or a running daemon, then try explicit stop:

```bash
virtuoso-bridge status --inspect-state --env /local/bridge.env
virtuoso-bridge stop --port 65085 --env /local/bridge.env
```

Use `remote_port`, not local `port`: for `port=12345, remote_port=23456`, stop with
`virtuoso-bridge stop --port 23456`. Same-owner historical non-secret 0644 metadata
in a 0755 application directory remains readable if neither is writable by
others. Unsafe files are reported by path without trusting their contents. On Linux,
explicit stop can establish missing birth identity only for a verified same-owner
standalone forward with `-F /dev/null`, exact endpoints and disabled multiplexing.
The default old launcher omitted `-F /dev/null`, so ordinary historical forwards
usually require the operator branch below. Custom/unknown configuration, extra
forwards, PID reuse or unavailable inspection also require operator recovery.
Current `ssh -G` output cannot prove a running process's historical configuration. No unchecked force switch exists. Current verified
birth identities use the normal stop path.

On Linux, substitute the reported PID and inspect the owner, executable, start
time and full argv. Recheck immediately before terminating that exact process:

```bash
cat -- /absolute/reported/state-user@host_port.json  # inspect retained JSON as untrusted data
ps -p 12345 -o pid=,user=,lstart=,args=
readlink /proc/12345/exe
tr '\0' '\n' < /proc/12345/cmdline
# Only after verifying this standalone user-owned forward serves no other work:
kill -TERM 12345
ps -p 12345 -o pid=,lstart=,args=  # wait for the original process to exit
# Then remove only the exact reported stale state file:
rm -- /absolute/reported/state-user@host_port.json
```

On Windows PowerShell, inspect the command, creation time and owner:

```powershell
Get-Content -LiteralPath 'C:\absolute\reported\state-user@host_port.json'
Get-CimInstance Win32_Process -Filter 'ProcessId = 12345' |
    Select-Object ProcessId,ExecutablePath,CreationDate,CommandLine
Get-CimInstance Win32_Process -Filter 'ProcessId = 12345' |
    Invoke-CimMethod -MethodName GetOwner
# Recheck identity and ensure this process serves no other forwards, then:
Stop-Process -Id 12345
Wait-Process -Id 12345 -Timeout 5 -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'C:\absolute\reported\state-user@host_port.json'
```

Use verified values and literal paths only. Remove a selection pointer only after
confirming it names that stopped target. Retain evidence after an uncertain stop.
Deleting JSON alone does not release a listener. A cleanup failure after verified
termination reports a warning with the exact path; inspect it before restarting.
Discovery-mode shared bootstrap files are never removed by CLI or Python `stop()`.

### Upgrade an existing fixed-port deployment

Upgrading is optional for a working old endpoint. To adopt new loaded code,
follow the [unreleased note](../../../docs/releases/unreleased.md): recover
the old tunnel, choose the intended new
setup, update `.cdsinit` only where its path changed, stop/wait/load in each
intended idle CIW, then run `sessions`, `start`, signed `status` and
`eval 'getWorkingDir()'`. On fresh setup, load after the first no-record start
and run `start` again. Keep valid private roots and exact selectors. Client
configuration edits do not alter running CIWs; preserve old job artifacts.
