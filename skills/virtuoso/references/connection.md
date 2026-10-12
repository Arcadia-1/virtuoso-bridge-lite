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

## Optional advanced installations

Only configure extra roles when the user actually has a split installation.
On versions that support them, GUI/X11 uses `VB_GUI_HOST`, deployment uses
`VB_DEPLOY_HOST`, the daemon tunnel uses `VB_DAEMON_HOST`, and Spectre uses
`VB_SPECTRE_HOST`; unset roles fall back to `VB_REMOTE_HOST`.
If deployed files are not visible to CIW, choose a shared
`VB_REMOTE_SCRATCH_ROOT` instead of assuming the SSH host's `/tmp` is shared.

Named profiles use case-sensitive suffixes and `-p PROFILE`. Keep existing
one-host configuration unchanged unless the task requires these overrides.
Configured process/version metadata is a declaration, not verified runtime
CIW identity. Read the checkout's profile documentation for supported fields.

Check the installed client and running daemon before using newer APIs.
Files deployed on disk do not upgrade a daemon already running inside CIW.
For suspected dialogs or uncertain submissions, use
[shared-ciw-dialogs.md](shared-ciw-dialogs.md) before any restart or retry.
