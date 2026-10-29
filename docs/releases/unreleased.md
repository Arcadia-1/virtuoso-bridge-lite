# Unreleased discovery and compatibility corrections

Connections now discover registered sessions first, without a mode switch. A
successful discovery with no project candidates permits fallback to an existing `VB_REMOTE_PORT` and
compatible old fixed-port daemon using the existing authentication policy.
Existing users do not need a compulsory daemon/configuration migration.
Registry errors and authentication failures never trigger fallback.

CLI `--port` remains exact. `VB_REMOTE_PORT` prefers a matching record but permits
a unique new registered session when the old port has no record. Ambiguity needs
explicit choice; a saved registered identity cannot silently downgrade to a
legacy endpoint. See the [proposed design](../adr/0004-instance-discovery.md).

## New sessions and scratch paths

New generated setup uses OS-assigned ports and private records. If no usable
endpoint exists, `start` prints a setup instruction. Load it in the intended idle
CIW and run `start` again. Later runs discover that session even with the old
environment port still present. Client settings do not reload a CIW or rewrite
`.cdsinit` automatically.

The stock scratch parent is `/tmp`; existing workflow paths retain
`virtuoso_bridge_<user>/<client>/<leaf>`. Default records/bootstrap live under
private `/tmp/virtuoso_bridge_<user>/runs` and `daemon`. Explicit roots remain
supported. Preview deployments using a home registry should keep that root
explicit on both sides until an intentional move. Old artifacts are not moved
or deleted; never chmod the shared parent.

Bundles are reused only when inventory and bytes match. Differences require
`start --replace-setup`, retaining the old bundle. Shared `VB_DAEMON_SETUP`
installations are never overwritten. Automatic workers disable bytecode writes;
inspect/replace old cache-bearing bundles. Replacement has a recoverable
exchange gap, not uninterrupted availability for concurrent CIW loads.

## Historical tunnel state and optional upgrades

1. With the original `.env`/profile, run `virtuoso-bridge status --inspect-state`.
   Same-owner historical non-secret 0755/0644 metadata is readable; writable,
   foreign and special files remain untrusted.
2. To stop, use **remote_port**. For `port=12345, remote_port=23456`, run
   `virtuoso-bridge stop --port 23456`. Stop uses the saved PID without process
   identity or argv checks. Inspect suspect state with the
   [recovery procedure](../../skills/virtuoso/references/connection.md#legacy-tunnel-recovery).
   Deleting JSON alone never releases a listener.
3. If choosing to upgrade, stage a new generated/versioned shared bootstrap.
   In the intended idle CIW, stop the tracked child, wait for exit, then load
   the intended new setup. Update `.cdsinit` only when deliberately changing
   its path. Working old endpoints need no upgrade merely to continue use.
4. List `sessions --json`, choose `start --port PORT` if ambiguous, then verify
   signed `status`. Allocation requests are separate from selection; see the
   [exact helper commands](../../skills/virtuoso/references/connection.md#selecting-a-running-instance).

## Other corrections and verification

Restart submits one bounded request and authenticates the replacement in the
same CIW using loaded code. An uncertain result retains evidence and must not
be replayed. Polling uses capped backoff. `RBStopAll()` now stops only this CIW's
tracked child. An untracked daemon needs operator owner/birth/parent verification;
the SSH tunnel procedure is not a daemon kill procedure.

SSH stop and workflow staging retain their original behavior. Stop does not
wait for exit and ignores signal errors; workflow scratch paths have no added
ownership gates or umask. Original private deployment/XStream child checks remain.
Token generation uses the existing client/daemon implementation and overrides,
without the separate credential helper. Local screenshots retain absolute path expansion.
Session listings preserve printable Unicode while escaping terminal controls.

Fresh Linux/native/Python 2.7 and isolated Cadence acceptance remains required
for this revision. Historical runs are separate evidence. See
[tests/README.md](../../tests/README.md); offline results do not prove electrical
equivalence.
