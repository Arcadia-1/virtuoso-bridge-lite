# SOS Cellview API

Virtuoso Bridge can operate on one explicit Cliosoft SOS cellview through the
same profile and GUI-host connection used by the CIW. SOS is optional: normal
Bridge use has no SOS dependency, and SOS calls return an actionable blocked
result when the executable or Cadence integration is unavailable.

## Configuration

The SOS executable is resolved on the GUI host in this order:

1. the `soscmd=` argument;
2. `VB_SOS_COMMAND_<profile>`;
3. `VB_SOS_COMMAND`;
4. `soscmd` on the remote `PATH`;
5. `$CLIOSOFT_DIR/bin/soscmd`.

An explicit value may be an absolute executable or a command name/site wrapper.
It is never silently replaced with another installation after a failure. Plain
TCP clients and native Windows local clients do not prove access to the CIW
filesystem. A same-host POSIX client must explicitly set
`VB_SOS_LOCAL_FILESYSTEM=1`; do not use that setting for an SSH-forwarded
loopback connection.

## Python API

```python
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env(profile="lab")

status = client.sos.status_cellview("LIB", "CELL", "schematic")
checkout = client.sos.checkout_cellview(
    "LIB", "CELL", "schematic", dry_run=True,
)
cancel = client.sos.cancel_checkout_cellview(
    "LIB", "CELL", "schematic", dry_run=True,
)
checkin = client.sos.checkin_cellview(
    "LIB", "CELL", "schematic", message="Fix gain", dry_run=True,
)
initial = client.sos.register_cellview(
    "LIB", "NEW_CELL", "schematic", message="Initial version", dry_run=True,
)
session = client.sos.diagnose_session_cellview("LIB", "CELL", "schematic")
lock = client.sos.lock_info_cellview("LIB", "CELL", "schematic")
restart = client.sos.restart_session_cellview(
    "LIB", "CELL", "schematic", dry_run=True,
)
```

`status_cellview` is read-only for any resolvable view. Writes are limited to
saved OpenAccess `schematic`, `schematicSymbol`, and `maskLayout` views. Maestro,
config, Verilog, and other view types remain read-only. Checkin and registration
reject any target whose logical name or resolved path contains `calibre`.

The API resolves the real view directory and master file in the connected CIW,
verifies the SOS workarea and exact status row, rejects unsaved buffers, and
rechecks target identity immediately before a write. Checkout uses `ddCheckout`.
Some SOS sites deny the formatted `status` operation even though the client is
connected to the project server. In that case Bridge falls back to an exact,
server-queried `nobjstatus` request. It deliberately does not pass `-ucl`, which
would read cached client information. The fallback requires one matching object,
consistent revision fields, and valid modification/lock flags; malformed or
ambiguous output blocks writes.
Checkin and initial registration prefer the installed SOS Design Manager flow
when its runtime capabilities are present. If that native flow is unavailable
while a Maestro session is open, the operation is blocked instead of falling
back to `ddCheckin`, because direct GDM callbacks can change an unrelated ADE
session mode on some IC/SOS releases.

Cancel checkout is deliberately narrower than the SOS GUI's force-discard
option. It accepts only a saved, clean checkout in the current workarea and
invokes `soscmd discardco` without `-F`; modified content is never discarded.
Bridge re-resolves the CIW target and refreshes SOS state immediately before
the one-shot command. Success additionally requires the same revision to be
checked in and clean afterward.

Results contain `target`, `before`, `after`, `outcome`, and `diagnostics`.
`success`, `noop`, and `dry_run` set `ok=True`. `blocked` means the request was
refused before any change, for a reason in `diagnostics` (for example a library
SOS does not manage, unsaved changes, or a lock in another workarea); for
`status` it means no SOS status was read. A timeout or lost response after
dispatch is `unknown`: Bridge queries fresh SOS state but never resends the
mutation and never claims that a matching state proves which request completed.
Use `reconcile_cellview(..., receipt=previous_result)` for a read-only follow-up.

### Session health and lock ownership

`diagnose_session_cellview` begins with `soscmd query is_running`, which does not
start a stopped workarea session. For an existing session it reports server
connectivity, GUI mode, project/server/RSO identity, an exact server-queried
object record, and whether native `status` still works. This distinguishes a
healthy client from a stale client whose server queries work but native status
updates are denied.

`lock_info_cellview` requests one exact object from the server without `-ucl`
and reports whether its checkout belongs to the current workarea, another
workarea, no server lock, or no checkout. It includes the SOS-reported owner,
checkout time, path, and workarea fields without scanning the project or writing
a history report.

SOS returns only available attributes for `nobjstatus -ga...`. `Modified` and
`OutOfDate` remain mandatory binary flags; `CiModified` is treated as false
when omitted by older SOS package records, and is still rejected if present
with a value other than `0` or `1`. Lock metadata that is unavailable for an
unlocked object is reported as empty. A malformed lock record makes the CLI
return a structured `blocked` result rather than a traceback.

`restart_session_cellview` is recovery for an unhealthy, already-running SOS
session. A healthy session returns `noop`; a stopped or unidentified session is
not started implicitly. Recovery is blocked for unsaved buffers and whenever a
Maestro session is active in the connected CIW. It first tries normal
`exitsos`. The force form is available only when
`force_cadence_disconnect=True`, after a confirmed normal-exit refusal. A lost
reply is `unknown` and is never retried. Successful recovery starts SOS through
one read-only exact-object query and requires both server-object and native
status verification.

## CLI

```bash
virtuoso-bridge sos status LIB CELL schematic -p lab --json
virtuoso-bridge sos co LIB CELL schematic --dry-run -p lab --json
virtuoso-bridge sos cancel-co LIB CELL schematic --dry-run -p lab --json
virtuoso-bridge sos ci LIB CELL schematic -m "Fix gain" --dry-run -p lab --json
virtuoso-bridge sos register LIB NEW_CELL schematic -m "Initial version" --dry-run -p lab --json
virtuoso-bridge sos doctor LIB CELL schematic -p lab --json
virtuoso-bridge sos session-doctor LIB CELL schematic -p lab --json
virtuoso-bridge sos lock-info LIB CELL schematic -p lab --json
virtuoso-bridge sos session-restart LIB CELL schematic --dry-run -p lab --json
virtuoso-bridge sos session-restart LIB CELL schematic --yes \
  --force-cadence-disconnect -p lab --json
virtuoso-bridge sos reconcile LIB CELL schematic --receipt unknown.json -p lab --json
```

CLI exit codes are `0` for success/noop/dry-run, `1` for blocked or failed, and
`3` for unknown. `doctor` is read-only and does not acquire a license, save,
checkout, checkin, run history, or create diff reports.
`session-restart` requires `--dry-run` or `--yes`; forced Cadence disconnect is
never implied by `--yes` and must be named separately. Use it only on a
dedicated idle CIW, because `exitsos -F` disconnects Cadence from that workarea's
SOS session.

## Ordinary SOS Files

`client.sos.attach(workarea)` exposes status, object status, history, diff,
checkout, and checkin for explicitly named ordinary files. OA packages,
`.oa`/`master.tag` paths, symlinks, directories, and Calibre-related targets are
rejected by the write methods; use the cellview API for OA data.
