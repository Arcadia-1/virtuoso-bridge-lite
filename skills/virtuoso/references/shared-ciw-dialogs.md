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

## Explicit Recovery And Compatibility

### Reviewed Informational Close (Opt-in)

For an informational dialog whose Close behavior has been independently verified,
the API provides a two-step, single-use close. This is not generic popup approval:
save/discard/overwrite, SOS, simulation start/cancel and unknown workflows remain
outside this feature. Do not decide safety from the title or pixel hash alone.

```python
# Existing authenticated client; avoid factory SKILL probes while blocked.
client.dialogs.enable_guard(pid=verified_pid)
ticket = client.dialogs.prepare_close(
    inspected_window_id, expected_title="Exact reviewed informational title",
)
# Review ticket.preview_png_b64 and the documented Close behavior, or match an
# independently reviewed exact visual whitelist. Do NOT blindly approve every
# hash returned by the API. Preview images can contain confidential text.
result = client.dialogs.close(
    ticket, authorized=True, expected_content_sha256=approved_content_hash,
)
```

`prepare_close` only reads the target and preview. `close` defaults to unauthorized
and consumes a ticket before transport. Tickets are client-local, expire after
120 seconds, cannot be modified/replayed, and are limited to 16 outstanding
previews. Rebinding to a different endpoint or CIW invalidates approval. The
signed hello must match the selected PID; the native helper binds its process
start time to reject PID reuse. Older authenticated daemons need no protocol
upgrade for this API. When advertised, daemon-instance changes also invalidate
approval; an older daemon's restart within the same CIW is not separately detected.

The remote helper rechecks the one target-owned modal, exact window/title,
process start identity, geometry, WM metadata and visible pixels. It requires
WM_DELETE_WINDOW support and sends that message to the selected window once.
There is no Enter/escape/global keyboard input, focus stealing, window destruction,
process kill, or fallback when a window does not support the protocol. A changed,
unmapped, ambiguous or unrecognized target is refused. No SKILL is used.

A short X-server grab protects the final native comparison and message submission;
no subprocess runs while grabbed. A helper-local hard deadline prevents a hung
native call from holding the grab indefinitely. This briefly pauses the display;
it is not a desktop reservation or protection against every client-side race.

Results distinguish `not_started` (`action_sent=false`), `requested`, `closed`,
and `unknown`. A requested message is NOT proof that the window closed. A closed
window is NOT proof that a design/save/run operation succeeded. Query the original
request receipt separately when using the recoverable-request feature. Unknown
transport outcomes never trigger automatic repeat; inspect and reconcile instead.

First release is an explicit reviewed-close API, not a built-in catalog of Cadence
dialogs, a persistent automatic click policy, or a CLI that transports approval
tickets between processes. Rendering differences, obscured windows, animation and
button highlighting can invalidate a visual match; refusal is intentional.

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
- The daemon watchdog still sends process-level SIGINT on timeout. This guard
  only avoids sending requests into *already detected* blockers. Watchdog and
  late-response recovery require a separate protocol change.
- Existing client factory identity checks, and other clients using the same
  CIW, are not protected before this guard is enabled.
- No background polling, automatic saving, notification scheduler, or generic
  Computer Use dependency is introduced.

Xlib's distinction between drawable `InputOutput` windows and non-drawable
`InputOnly` windows is documented in the
[Xlib specification](https://www.x.org/releases/X11R7.6/doc/libX11/specs/libX11/libX11.html).
Both types can participate in input grabs; this inspection does not claim to
detect invisible grabs or infer their owning human/agent operation.
