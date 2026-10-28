# Discover Virtuoso instances with fixed port compatibility

- Status: Proposed; local implementation authorized, upstream design review pending
- Date: 2026-10-29
- Related: Issue #265

## Compatibility contract

[ADR 0003](0003-lite-default-optional-advanced-config.md) promises no mandatory
migration of existing one-host configuration and Python calls. Connections read
the registry first, with no discovery gate. A successful empty result may fall
back to an existing configured `VB_REMOTE_PORT`, using the existing authentication
policy. Missing records alone do not require upgrading a working old daemon.

The earlier default-off recommendation is superseded locally by the user's
explicit discovery-first compatibility instruction. This authorization does not
establish upstream design acceptance.

## Records and selection

A **record** is private candidate metadata published by a running daemon. A
**selection** is a saved choice of an authenticated record. **Registered sessions**
use the new publication scheme; older fixed-port sessions can serve authenticated
requests without records.

New generated bootstraps bind `127.0.0.1:0`, read `getsockname()` and hold the socket
before publishing a locked 0600 record under a private 0700 `runs/`. Metadata
contains port, Virtuoso PID, daemon PID, UID, user, work directory and random
instance ID. The explicit listen-all option remains. Locks establish liveness;
opened descriptors and inodes anchor filesystem checks.

Client discovery reads without creating directories, provisioning credentials,
writing allocation requests or removing stale files. Daemon publication retains
its own stale-record cleanup. CLI `--port` is exact. `VB_REMOTE_PORT` prefers a
matching record, otherwise permitting a unique registered session under the
configured account/directory. An unchanged old configuration can therefore adopt
a new registered session on later runs. A live saved choice is retained;
ambiguity requires explicit selection.

Fallback requires a successful discovery with no project candidates, a configured old port, no saved
registered identity and no exact CLI selector. Registry errors, ambiguity, exact
selector misses and authentication failures never cause fallback. A registered
peer absent from discovery cannot be accepted as a legacy endpoint. A vanished
saved record can follow a replacement only within an explicit project constraint.
Uncertain application work is never replayed.

## Authentication and state

Record files are candidates, not authentication. Registered connections require
the per-UID credential and a signed handshake matching the full record. One UID
is one trust boundary; filters prevent mistakes rather than isolate hostile
same-UID processes. Registered sessions ignore token-path and unauthenticated
overrides. Legacy connections retain the existing token and identity checks.

Current state and selections are published atomically with private permissions.
Historical non-secret connection metadata may be read from same-owner 0755
application directories/0644 regular files, provided neither is writable by
others. New identity/selection fields do not receive this exception. Type,
owner, size, inode and schema checks remain. Windows relies on user ACLs.

Metadata alone never authorizes a signal. The saved process identity must match.
Legacy automatic adoption is limited to independently proven standalone forwards;
ordinary historical SSH argv may inherit additional work from configuration and
needs [operator recovery](../../skills/virtuoso/references/connection.md#legacy-tunnel-recovery).
Untrusted retained state is reported instead of a successful no-op stop.
`stop --port` names the retained **remote_port**, not the local listener.
`status --inspect-state` requires no daemon.

## Paths and publication

General scratch retains the stock `/tmp` parent and
`virtuoso_bridge_<user>/<client>/<leaf>` layout. Default records and generated
bootstrap use the private `/tmp/virtuoso_bridge_<user>` child, with `runs/` and
`daemon/`. Explicit private discovery roots remain supported; alternate workflow
parents retain username/client nesting. Explicit `~/` expands in the target
account's passwd home. Never chmod `/tmp` or an account home.

Generated setup embeds the daemon registry root. Deployment/GUI hosts must see
the bundle. Shared setup uses installer `--scratch-root`, otherwise daemon
`RB_SCRATCH_ROOT`, then `VB_REMOTE_SCRATCH_ROOT`, then the default private child.
Client settings do not change a loaded CIW. The older nested registry layout
remains readable. No existing job or record is moved automatically.

A working old endpoint is connected without rewriting its setup. New generated
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
