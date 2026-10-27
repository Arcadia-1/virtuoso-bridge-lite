# Discover Virtuoso instances through private scratch records

Issue #265: each daemon binds `127.0.0.1:0`, reads `getsockname()`, and keeps
the socket open. The existing listen-all option still applies. The OS allocates
ports; files only advertise live instances. No per-project bootstrap is needed.

## Records and selection

Records contain port, Virtuoso PID, daemon PID, UID, working directory and a
random instance ID. They live in
`<VB_REMOTE_SCRATCH_ROOT>/runs` (root defaults to `~/.virtuoso-bridge`).
Directories are 0700; records are 0600. A record is locked before publication
and held until exit. Startup and discovery remove unlocked stale records using
nonblocking locks and inode checks. SIGKILL releases the kernel lock.

`start` reads records over SSH, filters by user and optional
`VB_REMOTE_WORK_DIR`, then authenticates the exact record through HMAC.
One match connects; multiple matches list port/PID/directory and require
`--port`; no match prints the bootstrap load instruction. Saved selection is
scoped to config, profile, host, user, directory, env port and scratch root.

`status` discovers without changing port requests. A saved connection must
match a live record and pass signed identity checks; otherwise it lists
available instances instead of probing a stale tunnel. Status uses no SKILL.
The default bootstrap is `<scratch>/daemon/virtuoso_setup.il`. Discovery also
reads existing `<scratch>/virtuoso_bridge_<user>/runs` records during migration;
loading the new bootstrap moves a future daemon to `<scratch>/runs`.

## Fixed ports

`start --port PORT` overrides `VB_REMOTE_PORT`. Both select an exact port and
atomically save a persistent fixed-port request for the configured directory,
or the account when no directory is set. Directory requests take precedence;
concurrent writes use the last complete request. A future matching daemon must
bind that port or fail. Existing daemons are unaffected.

Starting without either selector clears that scope. Clear both applicable
scopes to restore automatic allocation. `RBSetPort(N)` switches an idle CIW's
daemon; `RBSetPort(0)` consults the scratch request, then defaults to bind zero.
Low-level explicit-port constructors remain supported.

Configured ports are separate from the reported runtime port: reloading setup
preserves a live instance's identity, and the monitor edits configuration.
`RBStopAll()` stops only the tracked child. It never searches by process name
or port: a read-only Linux probe confirmed that `fuser 0/tcp` matches unrelated
listeners. No destructive probe or X-server crash reproduction was performed.
These cases are covered by `tests/test_auto_port_state.py`.

## Security and deployment

Registry operations use open directory descriptors (`/proc/self/fd`) to resist
path replacement. Symlinks, foreign owners, unsafe permissions, special files
and malformed records are rejected. Locks establish liveness, not authority.

The daemon and SSH client reuse or atomically create the same per-UID secret
in the OS-account home: 32 random bytes, file 0600, directory 0700. Automatic
mode requires authentication and ignores `RB_TOKEN_PATH`. Secrets never enter
SKILL, argv, discovery records or CIW output. One Linux UID is one trust boundary.

`tools/install_shared_daemon.py` installs a new protected versioned bundle;
`VB_DAEMON_SETUP` selects its bootstrap. Without it, the bridge generates a
private bootstrap shared across projects. Daemon and client must agree on the
scratch root: generated setup includes it; shared setup uses `--scratch-root`
or the daemon's `RB_SCRATCH_ROOT` / `VB_REMOTE_SCRATCH_ROOT` environment.
Older daemons need an explicit reload before they can publish records.

## Checks

`tests/test_instance_discovery.py` covers selection and configuration.
`tests/native_instance_registry.py` exercises Linux sockets, locks, races,
stale cleanup, private credentials and signed identity with Python 3 or 2.7
workers. See [test commands](../../tests/README.md#native-linux-instance-discovery-checks).
