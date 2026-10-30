# Discover Virtuoso instances with fixed port compatibility

- Status: Proposed; revised to match the default-off scope in issue #265
- Date: 2026-10-29
- Related: Issue #265

## Compatibility contract

[ADR 0003](0003-lite-default-optional-advanced-config.md) promises no mandatory
migration of existing one-host configuration and Python calls. Automatic sessions
are explicitly enabled with `VB_AUTO_SESSIONS=1`, including profile suffixes.
Unset or zero retains the legacy setup, default-port calculation, state lookup,
and Python connection path without consulting the registry. A work directory
alone does not select the new mode. Future default changes need a separate
upstream design decision; contributor-local authorization does not replace it.

## Records and selection

A **record** is candidate metadata published by a running daemon. A
**selection** is a saved choice of an authenticated record. **Registered sessions**
use the new publication scheme; older fixed-port sessions can serve authenticated
requests without records.

Automatic bootstraps bind `127.0.0.1:0`, read `getsockname()` and hold the socket
before publishing a locked record (created with mode 0600 under a 0700 `runs/`). Metadata
contains port, Virtuoso PID, daemon PID, UID, user, work directory and random
instance ID. The explicit listen-all option remains. Locks establish liveness;
opened descriptors and inodes anchor filesystem checks.
Registry reads and cleanup enforce account ownership, private modes and regular
single-link record files. Record selection checks account metadata; connections
authenticate the complete selected record.

Client discovery reads without creating directories, provisioning credentials,
writing allocation requests or removing stale files. Daemon publication retains
its own stale-record cleanup. CLI `--port` explicitly selects an exact endpoint
and supersedes a saved choice. `VB_REMOTE_PORT` is also an exact constraint,
never a preference that permits migration. A live saved choice is retained;
ambiguity or an expired saved identity requires explicit selection.

Fallback requires successful discovery with no record at the configured port, a configured old port, no saved
registered identity and no exact CLI selector. Registry errors, ambiguity, exact
selector misses and authentication failures never cause fallback. A registered
peer absent from discovery cannot be accepted as a legacy endpoint. A vanished
saved record cannot follow another instance, even within the same project.
Only a bounded explicit restart can accept a replacement after verifying the
same GUI host, kernel boot and CIW process birth identity and any fixed port.
Uncertain application work is never replayed.

## Authentication and state

Record files are candidates, not authentication. Registered connections require
the existing bridge token and a signed handshake matching the full record. One UID
is one trust boundary; filters prevent mistakes rather than isolate hostile
same-UID processes. Token provisioning uses the original home-based path and
token-path overrides. Registered clients still require a signed record match;
an unauthenticated daemon cannot satisfy that check. Legacy connections retain
the existing token and identity checks.

Ordinary execution uses the signed `instance_bound_requests=1` capability to
send `op=execute`, with the authenticated `daemon_instance` included in a
distinct request MAC domain. Both workers check it before any IPC dispatch.
This prevents cached clients and replacements between hello and execution from
running commands on a different same-account daemon after port reuse. Registered
peers without this capability require an updated bundle loaded into the intended
idle CIW; they never fall back to unbound execution. Updated registered daemons
also refuse unbound clients. Older unregistered fixed-port daemons retain their
existing request format and configuration. No uncertain command is replayed.

Current state and selections are published atomically with private permissions.
Historical non-secret connection metadata may be read from same-owner 0755
application directories/0644 regular files, provided neither is writable by
others. New identity/selection fields do not receive this exception. Type,
owner, size, inode and schema checks remain. Windows relies on user ACLs.

Tunnel stop uses the original saved-PID behavior, without executable, birth-time
or argv verification and without waiting for exit. A reused PID can therefore
target an unrelated process; denied signals can still clear state. Inspect
suspect state using the [recovery procedure](../../skills/virtuoso/references/connection.md#legacy-tunnel-recovery).
`stop --port` names the retained **remote_port**, not the local listener.
`status --inspect-state` requires no daemon.

## Paths and publication

General scratch retains the stock `/tmp` parent and
`virtuoso_bridge_<user>/<client>/<leaf>` layout. Default records and generated
bootstrap use the `/tmp/virtuoso_bridge_<user>` child, with `runs/` and
`daemon/`. Explicit discovery roots remain supported; alternate workflow
parents retain username/client nesting. Workflow path helpers retain their
original literal-root behavior; use absolute paths for remote staging. Registry
helpers can expand `~/` on the daemon host. Never chmod `/tmp` or an account home.
Existing workflow private-child checks from the main branch remain.

Automatic deployment and registry roots must be owned by the current account
with mode 0700. Each ancestor is opened without following symlinks and must be
owned by root or the current account, without group/other writes. Root-owned
sticky parents such as /tmp are allowed above the private account directory.
Publication, record access and cleanup use verified directory descriptors;
remote bundle input is piped directly to the publisher. Existing unsafe paths
are rejected without chmod. Shared program installations retain their separate
trusted-installer policy and contain no account credentials or runtime state.

Generated setup embeds the daemon registry root. Deployment/GUI hosts must see
the bundle. Shared setup uses installer `--scratch-root`, otherwise daemon
`RB_SCRATCH_ROOT`, then `VB_REMOTE_SCRATCH_ROOT`, then the default per-account child.
Client settings do not change a loaded CIW. The older nested registry layout
remains readable. No existing job or record is moved automatically.

A working old endpoint is connected without rewriting its setup. Opt-in generated
setup uses registration: load the printed path in the intended idle CIW and run
`start` again. Existing `.cdsinit` files are not silently changed. Differing
generated bundles need `--replace-setup`; `VB_DAEMON_SETUP` installations require
a new protected version rather than in-place client replacement.

Reuse checks both inventory and bytes. Unexpected files, including old bytecode
caches, require inspection/replacement. Automatic worker launches disable bytecode
writes. Replacement stages a clean bundle and retains its predecessor. The
two-rename exchange has a documented gap; recovery can restore a complete old
bundle on the next publication, but does not provide uninterrupted availability.
Post-commit errors identify committed output and backup paths. Cleanup preserves
primary errors and closes descriptors. Retained backups are not automatically
pruned; inspect exact paths and establish that no CIW/job uses them before removal.
Stop never deletes shared code.

## Lifecycle and diagnostics

Registered restart authenticates the selected CIW, verifies its Linux birth
identity, sends one bounded timer request and authenticates the replacement.
Legacy restart uses old RBStop/RBStart with a bounded exit wait and verifies the
same CIW birth/PID and a changed authenticated daemon identity. Neither reloads
newly staged code or retries an uncertain request. Polling uses bounded backoff;
busy lanes and identity changes refuse restart.

CIW process birth/ownership checks run on the configured GUI host, using that
host's authenticated account UID rather than the daemon host's numeric UID.
Registry access and TCP forwarding stay on the daemon host. Bundle publication
uses Python detected on the deployment host; the generated launcher retains
the daemon host's Python path. GUI and daemon hosts must both see the bundle.

`RBStopAll()` remains tracked-child-only, an explicitly reviewed safety change.
An untracked daemon needs operator identity investigation, not a broad process
sweep. `RBSetPort(N)` in the new loader remains registered even for fixed ports;
explicit legacy setup is a distinct credential mode.

Status sends a signed handshake and reports roles/endpoints; legacy status also
retains banner/load diagnostics. Independent Spectre diagnostics still run after
bridge failure. Local discovery requires Linux; Windows/macOS clients use SSH
to Linux. Build labels are informational; actual protocol/auth/record checks
establish compatibility.

## Verification

The [native harness](../../tests/native_instance_registry.py) exercises sockets,
locks and publication with Python 3 and actual Python 2.7 workers.
[Test instructions](../../tests/README.md#native-linux-instance-discovery-checks)
separate hosted tests from isolated Cadence lifecycle acceptance. Historical site
runs informed prior fixes; fresh evidence for this revision is still required.
Source-shape tests do not establish SKILL behavior, and offline infrastructure
results do not establish GUI-equivalent electrical results.
