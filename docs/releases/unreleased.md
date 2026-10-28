# Unreleased instance discovery upgrade

Automatic discovery introduces a one-time daemon upgrade for pre-discovery
fixed-port deployments. This is the narrow exception recorded in
[ADR 0004](../adr/0004-instance-discovery.md) to the earlier Lite migration
promise. Profiles, multiple hosts and process metadata remain optional. Existing
explicit legacy Python constructors remain available; the CLI uses discovery.

## Upgrade an existing deployment

1. Use the original `.env` and profile to inspect the old tunnel with
   `virtuoso-bridge status --inspect-state`. Stop it with `stop --port PORT`.
   If its process identity cannot be proven, follow the exact-process
   [legacy recovery procedure](../../skills/virtuoso/references/connection.md#legacy-tunnel-recovery).
   Deleting JSON alone does not stop SSH or release a listening port.
2. Remove an obsolete `VB_REMOTE_PORT` deliberately if automatic selection is
   wanted. A retained value is still an exact selector, not an allocation request.
   Keep valid private scratch roots and intentional selectors. Clear any saved
   allocation requests separately using the registry helper's explicit configure
   operation; ordinary start/list/status commands do not change those requests.
3. Choose the new generated bootstrap or install a new protected shared bundle.
   Generated account files are reused when identical. If they differ, inspect
   the configuration and explicitly authorize replacement with
   `start --replace-setup`; the old complete bundle is retained in a private
   backup. The client never overwrites `VB_DAEMON_SETUP` installations.
4. Update each project's `.cdsinit` only where its setup path changed. In each
   intended **idle** CIW, call `RBStop()`, wait for that tracked child to exit,
   then load the intended new `virtuoso_setup.il`. Updating files or client
   configuration does not upgrade code already loaded by Virtuoso.
5. Run `sessions --json`, select with `start --port PORT` when necessary, then
   verify signed `status` and `eval 'getWorkingDir()'` before editing a design.

The default scratch root is the daemon account's passwd home under
`~/.virtuoso-bridge`. Explicit private roots remain supported; old scratch data,
durable jobs and other sessions are not moved or deleted. Shared setup uses
installer `--scratch-root`, otherwise the daemon's `RB_SCRATCH_ROOT`, then its
`VB_REMOTE_SCRATCH_ROOT`, then the default. It does not read the client's `.env`.
Home quotas may warrant a different private root.

## Command and lifecycle corrections

`sessions --json` produces machine-readable records. On a fresh deployment with
no record, `start` prints the load instruction and exits nonzero without a usable
tunnel: load the setup in the intended CIW and run `start` again.

`restart` retains the selected CIW, schedules one bounded stop/wait/start, then
authenticates the replacement record and reconnects to its endpoint. It restarts
the loaded configuration; it does not deploy or load a new bundle. A busy bridge
lane, changed CIW birth identity or mismatched exact selector prevents adoption.
An uncertain restart is reconciled without resending the request.

`RBStopAll()` is now a compatibility alias for this CIW's tracked-child stop.
It no longer kills all processes under the account. Use the connection guide's
verified orphan recovery procedure when the child is untracked.

A fixed port selected by `RBSetPort(N)` remains discovery-managed and uses the
mandatory per-UID credential. It is distinct from explicit legacy mode, whose
generated setup sets `RB_AUTO_PORT=0` and can use `RB_TOKEN_PATH`. The signed
protocol/authentication/full-record checks determine compatibility; the reported
bundle build is informational. Current bundles advertise `instance-discovery-3`.

Local screenshots now resolve/create an absolute local scratch directory before
calling SKILL and copying the image. Remote home expansion still happens on the
remote host.
