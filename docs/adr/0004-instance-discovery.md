# Discover Virtuoso instances through private records

- Status: Accepted
- Date: 2026-10-27
- Related: Issue #265

This is a narrow exception to [ADR 0003](0003-lite-default-optional-advanced-config.md):
automatic discovery requires an upgraded daemon. Profiles, multiple hosts and
process metadata remain optional; explicit legacy constructors remain available.

## Context

Several CIWs may share one Linux account. A preselected fixed port can collide
or address the wrong CIW. Configuration metadata does not establish which
instance a TCP endpoint serves. The accepted discovery default needs signed
identity verification and a deliberate upgrade path for older daemons.

## Decision

### Records and selection

Daemons bind `127.0.0.1:0`, read `getsockname()` and retain the socket. The explicit
listen-all option remains. A locked 0600 record in private 0700 `<root>/runs`
contains port, Virtuoso PID, daemon PID, UID, user, working directory and random
instance ID. Nonblocking locks and inode checks clean stale records and abandoned
temporary files while preserving active writers. Linux directory descriptors and
`/proc/self/fd` resist path replacement.

`start` filters by account, optional absolute `VB_REMOTE_WORK_DIR`, and exact
port. One match connects; ambiguity lists candidates and requires `--port`.
`--port` overrides `VB_REMOTE_PORT`. Neither selector writes or clears allocation
requests. Explicit helper `--configure` operations control durable requests;
directory scope overrides account scope. `RBSetPort(N)` changes an idle CIW's
child; `RBSetPort(0)` consults requests then defaults to bind zero. The
[connection guide](../../skills/virtuoso/references/connection.md#selecting-a-running-instance)
documents clearing both scopes of pins left by earlier clients.

### Trust and connection state

Records are candidates, not proof of identity. Mandatory per-UID HMAC and signed
comparison of the complete record precede application work. Automatic mode ignores
token-path and unauthenticated overrides. One UID is one trust boundary.
Selection and state are validated, atomically published and configuration scoped.
Forward reuse requires matching endpoints; stopping an adopted PID also requires
matching SSH executable and process birth identity. `status --inspect-state`
reports retained local paths/endpoints without discovery or a daemon. State
readers reject unsafe POSIX ownership, modes and special files. Windows ACLs,
not POSIX mode bits, protect local state.

Before application transmission, a fresh client checks its saved record. Only an
explicit project constraint with one replacement permits reconciliation after
that record vanishes. Exact ports remain exact. Ambiguity, identity mismatch and
authentication failure stop the connection; uncertain application work is never
replayed.

`sessions [--json]` lists without selecting, provisioning or changing allocation.
`status [--port PORT]` reads existing credentials, checks signed identity without
SKILL, reports roles/endpoints/retained tunnels, and attempts independent Spectre
diagnostics after bridge failure. An unverified bridge remains nonzero. Automatic
mode does not generate the legacy CIW banner identity file.
`stop --port PORT` targets a retained managed tunnel through its original
configuration, without selecting another CIW or sending SKILL.

`restart` preserves the authenticated selected CIW and its Linux process birth
identity. It schedules one bounded stop/wait/start, discovers only that CIW's
replacement child, authenticates the new full record and republishes state.
It does not load newly deployed code. Exact selectors remain exact; uncertain
requests are reconciled without replay. A listening forward is not proof that
the new daemon is reachable.

### Bootstrap and scratch roots

The default root and explicit `~/` use the daemon account's passwd home. Generated
setup is `<root>/daemon/virtuoso_setup.il`; `VB_DAEMON_SETUP` selects an existing
protected shared installation. Shared setup uses the installer's explicit root,
otherwise daemon `RB_SCRATCH_ROOT`, then daemon `VB_REMOTE_SCRATCH_ROOT`, then the
account default. Client `.env` changes do not alter a loaded CIW. Both sides must
agree on the root. Older artifacts are neither moved nor deleted. Live verification
confirmed a deployed daemon holding its record in the older
`<root>/virtuoso_bridge_<user>/runs` directory. Discovery therefore retains the
anchored, private-owner scan of that layout alongside `<root>/runs` until an
explicit upgrade. Repository commit history alone did not establish deployment.

Generated account bundles are compared before mutation under a bounded lock.
Identical files are reused; differing files require explicit `start --replace-setup`.
The complete new bundle is staged before publication, and the prior directory
is retained privately on replacement. Directory renames have a short exchange
gap; a durable recovery directory permits restoration after interruption.
`stop()` never removes discovery-mode shared code. This policy also applies
to local generated bundles. A no-record `start` prints the load instruction
and exits nonzero; the user loads it in the intended idle CIW and runs `start`
again. It does not silently upgrade another running daemon.

Shared installation is Linux-only, exclusive and versioned. Every ancestor must
be a real directory owned by root or the trusted installing UID, without group
or other write permission. Completed files are published together. Loading
accounts trust the installer; its ownership need not be root ownership.

### Compatibility and lifecycle

Signed capabilities report the build. Missing metadata means unknown; differing
builds produce informational guidance. Actual rejection checks protocol version,
authentication and signed record identity; build equality is not a gate.
Upgrade by stopping the intended idle CIW's tracked child, waiting for exit, then
loading the intended new bootstrap. Never replace a shared daemon automatically.
`RBStopAll` is tracked-child-only. Monitor changes use bounded stop/wait/start;
malformed bind banners leave the runtime port unchanged.

`RBSetPort(N)` keeps the instance discovery-managed even when N is nonzero;
its per-UID token remains mandatory. Explicit legacy setup sets `RB_AUTO_PORT=0`
and retains legacy token-path behavior. Allocation and authentication mode
must not be conflated by clearing the discovery flag on every fixed-port request.

## Alternatives

Fixed-port defaults avoid an upgrade but retain collision and wrong-instance
risks. Mandatory profiles conflict with Lite. Extra UUID/PID selectors add surface
without fixing the immediate defects. Disabling authentication or trusting record
files alone is unsuitable on shared hosts.

## Consequences and verification

Pre-discovery fixed-port daemons require one explicit upgrade for the CLI:
the narrow ADR 0003 exception described in the
[unreleased upgrade note](../releases/unreleased.md). Recover old tunnel state,
remove obsolete exact selectors intentionally, update setup paths where needed,
stop/wait/load in each intended idle CIW, then select and verify again. Existing
valid roots/selectors remain supported. One host and one `.env` remain sufficient.
Home quotas may motivate a private alternate root.
Shared-account users can access each other's records and credentials. Local
discovery requires Linux; Windows/macOS clients use SSH to Linux.

The [native harness](../../tests/native_instance_registry.py) tests real sockets,
locks, private tokens and signed protocol with Python 3 and actual Python 2.7
workers in Linux CI. [Test instructions](../../tests/README.md#native-linux-instance-discovery-checks)
require both Linux matrix jobs and the actual Python 2.7 worker lane before merge,
and separate these tests from Cadence execution. `test_auto_port_state.py` is a
source-shape guard, not SKILL behavior proof. Live restart/reload/monitor checks
require an isolated CIW and must preserve other instances. Infrastructure results
do not establish electrical equivalence.
