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

Check the installed client and running daemon before using newer APIs.
Files deployed on disk do not upgrade a daemon already running inside CIW.
For suspected dialogs or uncertain submissions, use
[shared-ciw-dialogs.md](shared-ciw-dialogs.md) before any restart or retry.
