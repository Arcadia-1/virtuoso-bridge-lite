# Shared CIW Dialog Protection

Humans and Bridge can operate the same Virtuoso process. A popup in that
process cannot reliably be attributed to either party, even when it appears
immediately after a Bridge request. Never automatically press Enter, Cancel,
or close the current form just to unblock automation.

## Read-only Inspection

```bash
virtuoso-bridge inspect-dialogs --pid 12345 -p worker --json
virtuoso-bridge inspect-dialogs --pid 12345 --display :7 --ciw-window 0x100 --json
```

The PID is on the configured GUI host. This command uses SSH/X11 only, does
not construct a SKILL client, and does not start/restart the Bridge. It may
deploy a small content-addressed inspection helper to the profile scratch
directory. Subsequent inspections reuse that helper within the same runner.

The report contains `status`, `target`, `dialogs`, and `diagnostics`:

- `clear` (exit 0): one verified CIW and no identified candidate blockers.
- `blocked` (exit 2): target-owned modal/candidate windows are present.
- `indeterminate` (exit 1): process, display, window ownership, or X11 access
  cannot be verified. This is not equivalent to an idle CIW.

Ownership uses X11 PID/transient/client-leader metadata, not window titles
or geometry. Titles and geometry can identify a *candidate* blocker but
cannot prove modality or user/Bridge origin. All sources remain `unknown`.
Missing metadata is conservative: an unrelated window with unverifiable
ownership can prevent a clear report. No popup action is suggested.

Inspection filters unmapped windows using read-only Xlib queries, rather than
recursively spawning a command for every toolkit widget. Mapped application
shells and referenced hidden ownership nodes remain subject to verification.
This is dialog-shell detection, not detection of every possible input grab or
reason a CIW can be busy. A `clear` report does not prove the CIW is idle.
Non-drawable InputOnly nodes are not visual dialog candidates. A root-level,
override-redirect, 1x1 window completely outside the root drawable is excluded
only when its queried properties are absent and the query succeeded. Known
application/modal windows and failed property queries are not excluded by this
rule. It does not establish foreign ownership or detect non-rendered modality.

## Opt-in Per-client Guard

For an already configured client:

```python
report = client.dialogs.enable_guard(timeout=15)
print(report.model_dump())

result = client.execute_skill("1+2", timeout=30)
if result.metadata.get("request_sent") is False:
    print(result.metadata["dialog_guard"])

# After the human resolves the popup, inspect before continuing.
report = client.dialogs.inspect()
```

Enabling uses the authenticated daemon capability handshake (no SKILL) to
bind the Virtuoso PID, then inspects its CIW. Optional `pid`, `display`, and
`ciw_window` must match. A blocked/indeterminate initial inspection enables
protection so following calls cannot bypass it. A binding/authentication
failure does not establish a new guard. Different GUI/daemon hosts are not
supported for automatic binding; explicit GUI-PID inspection remains available.
For a genuinely local GUI with no SSH runner, explicitly pass `local_gui=True`.
A loopback TCP endpoint alone is not local-GUI evidence: it can be an SSH tunnel.

Every guarded SKILL request receives a read-only preflight and fresh authenticated
PID handshake, so a tunnel redirected to another CIW is refused. A non-clear
report returns the existing `VirtuosoResult(status=ERROR)` shape with
`metadata.request_sent=False`, `outcome="not_started"`, and `dialog_guard`.
No SKILL is sent. Protection is per client, not a desktop lock or process-wide
reservation. It does not stop a human or another unguarded client from acting.
Shared mode also disables legacy connection retries, even when the caller
passes `retry_connect=True`, because a reset can occur after transmission.

On an in-flight failure, the guard inspects once if time remains and reports
`outcome="unknown"`. When the budget is exhausted it records indeterminate
inspection; call `dialogs.inspect()` separately for diagnosis. It never retries
the operation. A `clear` report after failure does not prove the operation did
not execute. Reconcile the run/save/SOS state before any explicit retry.

Maestro writer errors preserve guard evidence in `DialogBlockedError.result`,
`.inspection`, and `.outcome`. `run_and_wait` no longer closes current forms or
restarts a simulation when no history is acknowledged. Its diagnostic includes
the completion marker so a late completion can still be investigated.
During completion waiting, shared mode inspects out-of-band at most once every
ten seconds (each check capped at five seconds). A blocker stops only the Python
wait, not the simulation. `DialogBlockedError.result.metadata` retains the
acknowledged `history`, `session`, `phase="completion_wait"`, and marker. After
the user resolves the dialog, inspect/read that history rather than invoking
`run_and_wait` again, which would start another run.

`client.dialogs.inspect(pid=12345)` is also available without enabling a guard.
`client.dialogs.disable_guard()` removes protection without closing any window.

## Requests That Open A Popup

Preflight alone cannot protect a request that opens a dialog itself. With an
upgraded daemon, explicitly enable recoverable execution:

```python
client.dialogs.enable_guard(protect_inflight=True, timeout=15)
result = client.execute_skill("1+2", timeout=30)

handle = result.metadata.get("request_handle")
if handle and result.metadata.get("outcome") == "unknown":
    # Preserve this handle. Let the user resolve their dialog; do not resubmit.
    print(result.metadata)
    # Later, query only the original request. This works while CIW is blocked.
    original = client.requests.receipt(handle, timeout=10)
    print(original.model_dump())
```

This mode is opt-in and requires authenticated `recoverable_requests=1`
capabilities. An older daemon is refused, with no legacy SKILL fallback. The
default `protect_inflight=False` preserves preflight-only compatibility.
Changing daemon files on disk does not upgrade a running daemon. Arrange a
reload only in an explicitly selected idle CIW, never automatically in a
shared/active simulation process.

The daemon acknowledges a submission without waiting for CIW completion. One
worker owns the IPC lane until the original framed response arrives. That
worker has **no process-level SIGINT watchdog**. Other SKILL submissions,
including legacy clients, are refused while the lane is occupied; hello and
receipt queries remain available. Closing a client or ending its wait does
not cancel or interrupt the original operation. There is no automatic queue.

While a request remains pending, the client polls receipts and inspects the
selected X11 process at a low frequency (five seconds between inspections,
each inspection capped at five seconds and the remaining wait budget).
`blocked` or `indeterminate` stops the client wait and retains the handle.
`phase="awaiting_user"` and `waiting_for_user=True` mean an identified candidate
popup needs human attention; indeterminate inspection is not proof of a popup.
Its provenance is still unknown, even immediately after a Bridge submission.
No Enter/Cancel/close action is performed.

The handle contains only request ID, daemon-instance ID and Virtuoso PID,
not the token or SKILL source. It can be serialized and queried by a client
authenticated to the same daemon, including through a replacement SSH tunnel.
Reuse the existing client when possible. Ordinary `from_env`/`from_tunnel`
factories may run legacy SKILL-based identity checks; do not invoke them as
popup recovery while CIW is blocked. If reconstructing a receipt-only client,
use its existing authenticated TCP endpoint/credential without starting a
daemon or sending a SKILL probe. Never log the credential.
`request_state="completed"` preserves the original success/error response;
it is not proof of higher-level design or simulation acceptance. `running` or
`unknown` never grants permission to resubmit. A missing submission
acknowledgement is unknown too, with `request_sent=None`, not not-started.
Only an authenticated submission refusal reports `outcome="not_started"`.

Maestro writer errors retain the handle in `RequestRecoveryError.handle` and
the full `VirtuosoResult` in `.result`. `run_and_wait()` retains its session and
completion marker when startup is uncertain. A recovered run-history response
must be reconciled with that original marker/history, not used to start a
second simulation. Querying a receipt does not resume a multi-step workflow.

Receipts are in-memory and bounded, not a durable job database. A daemon
restart, lost IPC pipe, malformed response, receipt expiry or unavailable
endpoint can make the outcome unverifiable. Unknown results require manual
state reconciliation; do not restart a pending daemon just to unblock an agent.
Native X11 calls and invisible input grabs retain the inspection limits below.

Finished receipts are retained for one hour with a maximum of 4096 entries,
8 MiB per original response and 32 MiB total response data. The daemon reserves
space for a worst-case response before accepting another execution and refuses
at capacity, rather than evicting young receipts. A pending request never
expires or releases the IPC lane merely because its client stops waiting.
Malformed/partial responses, EOF or legacy timeout poison the lane: later
execution is refused because a delayed frame could otherwise be misattributed.

Request IDs contain a millisecond creation timestamp and random suffix. New
submissions must be no more than five minutes old or thirty seconds in the
future relative to the daemon's clock. This daemon advertises signed server
time in its hello reply, which the client prefers when creating the ID; other
implementations lacking that field require synchronized client/server clocks.
The completed-receipt retention exceeds this acceptance window, so an expired
ID cannot be used as a fresh submission. Receipt queries do not require a
fresh timestamp. This is bounded in-memory replay prevention, not durable
exactly-once execution across crashes or arbitrary clock changes.

## Explicit Recovery And Compatibility

Let the user complete their dialog whenever its provenance is unknown.
An explicitly authorized window/action can use `dismiss-window --display DISPLAY`;
the exact `target.display` from inspection is required for this workflow, since
window IDs are not unique across displays. Confirm the window still represents
the intended dialog before invoking it. Sending a key
is not proof that the underlying save or simulation succeeded.

Legacy bulk behavior is opt-in only:

```bash
virtuoso-bridge dismiss-dialog --legacy-bulk
```

Python requires `client.dismiss_dialog(allow_legacy_bulk=True)` or the same
keyword on `x11.dismiss_dialogs`. These operations can affect unrelated/user
windows and are inappropriate in a shared CIW. The client refuses bulk
dismissal while its shared guard is enabled. Existing explicitly selected
`dismiss-window`/bootstrap commands remain opt-in tools, not automatic recovery.

## Limits

- Linux/X11 process metadata, libX11, and the existing X11 utilities are required. Missing
  dependencies, permissions, or a Wayland-only GUI return indeterminate.
- Preflight is not atomic with user interaction. A new popup can appear after
  inspection and block an in-flight request.
- Native Xlib calls are count-bounded and deadline-checked before and after
  each call, but an individual synchronous call can outlive the helper's budget.
  X server I/O loss can terminate the helper. Failed/malformed inspection is
  indeterminate, not clear; invisible input grabs are not detected.
- Legacy execution, including preflight-only mode, still uses the process-level
  SIGINT watchdog. Only explicit recoverable execution avoids it. Human actions,
  another daemon in the same Virtuoso process, or external tools may still
  interrupt the operation; this is not a process-wide safety guarantee.
- Existing client factory identity checks, and other clients using the same
  CIW, are not protected before this guard is enabled.
- No background polling, automatic saving, notification scheduler, or generic
  Computer Use dependency is introduced.

Xlib's distinction between drawable `InputOutput` windows and non-drawable
`InputOnly` windows is documented in the
[Xlib specification](https://www.x.org/releases/X11R7.6/doc/libX11/specs/libX11/libX11.html).
Both types can participate in input grabs; this inspection does not claim to
detect invisible grabs or infer their owning human/agent operation.
