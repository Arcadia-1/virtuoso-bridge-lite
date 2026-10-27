# Discover Virtuoso instances through private records

Status: accepted through audit response-plan approval on 2026-10-27. Issue #265.
This is a narrow exception to [ADR 0003](0003-lite-default-optional-advanced-config.md):
automatic discovery requires an upgraded daemon. Profiles, multiple hosts and
process metadata remain optional; explicit legacy constructors remain available.

## Context

Several CIWs may share one Linux account. A preselected fixed port can collide
or address the wrong CIW. Configuration metadata does not establish which
instance a TCP endpoint serves. The accepted discovery default needs signed
identity verification and a deliberate upgrade path for older daemons.

## Decision

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

Records are candidates, not proof of identity. Mandatory per-UID HMAC and signed
comparison of the complete record precede application work. Automatic mode ignores
token-path and unauthenticated overrides. One UID is one trust boundary.
Selection and state are validated, atomically published and configuration scoped.
Forward reuse requires matching endpoints; stopping an adopted PID also requires
matching SSH executable and process birth identity. Unknown legacy state remains
available for inspection. Windows ACLs, not POSIX mode bits, protect local state.

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

Shared installation is Linux-only, exclusive and versioned. Every ancestor must
be a real directory owned by root or the trusted installing UID, without group
or other write permission. Completed files are published together. Loading
accounts trust the installer; its ownership need not be root ownership.

Signed capabilities report the build. Missing metadata means unknown; differing
builds produce guidance, with rejection only for missing required capabilities.
Upgrade by stopping the intended idle CIW's tracked child, waiting for exit, then
loading the intended new bootstrap. Never replace a shared daemon automatically.
`RBStopAll` is tracked-child-only. Monitor changes use bounded stop/wait/start;
malformed bind banners leave the runtime port unchanged.

## Alternatives

Fixed-port defaults avoid an upgrade but retain collision and wrong-instance
risks. Mandatory profiles conflict with Lite. Extra UUID/PID selectors add surface
without fixing the immediate defects. Disabling authentication or trusting record
files alone is unsuitable on shared hosts.

## Consequences and verification

Old automatic-mode daemons require one explicit upgrade: the approved ADR 0003
exception. One host and one `.env` remain sufficient; stale exact-port settings
must be removed intentionally. Home quotas may motivate a private alternate root.
Shared-account users can access each other's records and credentials. Local
discovery requires Linux; Windows/macOS clients use SSH to Linux.

The [native harness](../../tests/native_instance_registry.py) tests real sockets,
locks, private tokens and signed protocol with Python 3 and actual Python 2.7
workers in Linux CI. [Test instructions](../../tests/README.md#native-linux-instance-discovery-checks)
separate these tests from Cadence execution. `test_auto_port_state.py` is a
source-shape guard, not SKILL behavior proof. Live restart/reload/monitor checks
require an isolated CIW and must preserve other instances. Infrastructure results
do not establish electrical equivalence.
